#!/usr/bin/env python3
"""
lfo_chat - talk to a strange local AI in your terminal; every answer you give
reshapes an LFO that streams MIDI CC into Ableton (or anything else).

Windows + Ollama + loopMIDI.

  CC 20 (default)  main LFO, shaped by the conversation
  CC 21 (default)  rhythm lane: a step sequence replaying your keystroke timing
"""
import argparse
import datetime
import json
import math
import os
import random
import statistics
import sys
import threading
import time
from dataclasses import dataclass, field, asdict

import mido
import requests

try:
    import msvcrt  # Windows: char-by-char input so we can time keystrokes
except ImportError:
    msvcrt = None

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

OLLAMA_URL = "http://localhost:11434/api/chat"
N_POINTS = 16
HISTORY_KEEP = 24  # messages kept besides the system prompt

SYSTEM_PROMPT = """You are the voice inside a synthesizer. You talk with the musician playing it through a terminal, and every answer they give reshapes a low-frequency oscillator that modulates their sound.

Conversation style: curious, odd, gently unsettling, never cruel. Ask personal questions and open strange or philosophical topics: memory, dreams, objects, time, what they'd trade, what they're avoiding, what a color sounds like. Follow threads that seem charged, drop ones that go flat, and occasionally swerve somewhere absurd. Keep replies to 1-3 sentences, usually ending with a question. Never mention LFOs, parameters, numbers, or JSON in the reply. If the person seems genuinely distressed or asks to change the subject, be kind and steer somewhere lighter.

Each user message ends with [typing: {...}], measured from their keystrokes: hesitation before they started typing, typing speed, backspaces, how uneven their rhythm was, and the longest mid-answer pause. Treat these as body language.

Every turn, output JSON with:
- reply: what you say next
- mood: one word for the emotional texture of their last answer
- rate_hz (0.02-8): calm or reflective -> slow; excited, anxious, rapid-fire -> fast
- depth (0-1): how much they revealed, or how intense the answer was
- center (0-1): overall brightness or openness of the answer
- jitter (0-1): evasiveness, contradiction, hesitation, heavy backspacing
- shape: exactly 16 numbers between 0 and 1 drawing one cycle of the waveform. Draw the feeling: a slow rise and sudden drop for tension released, spikes for agitation, flat plateaus for guardedness, lurching asymmetry for contradiction. Do not default to a sine. Each answer should visibly change the shape."""

SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "mood": {"type": "string"},
        "rate_hz": {"type": "number"},
        "depth": {"type": "number"},
        "center": {"type": "number"},
        "jitter": {"type": "number"},
        "shape": {"type": "array", "items": {"type": "number"}},
    },
    "required": ["reply", "mood", "rate_hz", "depth", "center", "jitter", "shape"],
}

# tempo-sync divisions: (label, beats per cycle)
DIVISIONS = [("4 bars", 16), ("2 bars", 8), ("1 bar", 4), ("1/2", 2),
             ("1/4", 1), ("1/8", 0.5), ("1/16", 0.25)]


# ---------------------------------------------------------------- helpers

def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def lerp(a, b, t):
    return a + (b - a) * t


def resample(shape, n=N_POINTS):
    if len(shape) == n:
        return list(shape)
    out = []
    for i in range(n):
        x = i * len(shape) / n
        i0 = int(x) % len(shape)
        i1 = (i0 + 1) % len(shape)
        out.append(lerp(shape[i0], shape[i1], x - int(x)))
    return out


def sample_shape(shape, phase):
    """Cyclic cosine interpolation through the drawn points."""
    n = len(shape)
    x = phase * n
    i0 = int(x) % n
    i1 = (i0 + 1) % n
    f = x - math.floor(x)
    f = (1 - math.cos(f * math.pi)) / 2
    return lerp(shape[i0], shape[i1], f)


def default_shape():
    return [0.5 + 0.5 * math.sin(2 * math.pi * i / N_POINTS) for i in range(N_POINTS)]


BARS = "▁▂▃▄▅▆▇█"


def spark(shape):
    return "".join(BARS[min(7, int(v * 8))] for v in shape)


# ---------------------------------------------------------------- patch

@dataclass
class Patch:
    rate_hz: float = 0.25
    shape: list = field(default_factory=default_shape)
    depth: float = 0.5
    center: float = 0.5
    jitter: float = 0.0
    mood: str = "idle"
    sync: str = ""


def blend(a, b, t):
    return Patch(
        rate_hz=math.exp(lerp(math.log(a.rate_hz), math.log(b.rate_hz), t)),
        shape=[lerp(x, y, t) for x, y in zip(a.shape, b.shape)],
        depth=lerp(a.depth, b.depth, t),
        center=lerp(a.center, b.center, t),
        jitter=lerp(a.jitter, b.jitter, t),
        mood=b.mood if t >= 0.5 else a.mood,
        sync=b.sync if t >= 0.5 else a.sync,
    )


def quantize_rate(rate_hz, bpm):
    best = min(DIVISIONS, key=lambda d: abs(math.log(rate_hz) - math.log(bpm / 60 / d[1])))
    return bpm / 60 / best[1], best[0]


def to_patch(d, bpm=None):
    def num(key, lo, hi, default):
        try:
            return clamp(float(d.get(key, default)), lo, hi)
        except (TypeError, ValueError):
            return default

    rate = num("rate_hz", 0.02, 8.0, 0.25)
    shape = d.get("shape")
    try:
        shape = [clamp(float(v)) for v in shape]
        if len(shape) < 2:
            raise ValueError
        shape = resample(shape)
    except (TypeError, ValueError):
        shape = default_shape()
    sync = ""
    if bpm:
        rate, sync = quantize_rate(rate, bpm)
    return Patch(rate_hz=rate, shape=shape,
                 depth=num("depth", 0, 1, 0.5), center=num("center", 0, 1, 0.5),
                 jitter=num("jitter", 0, 1, 0.0),
                 mood=str(d.get("mood", "?"))[:24], sync=sync)


def describe(p):
    rate = f"{p.rate_hz:.2f} Hz" + (f" ({p.sync})" if p.sync else "")
    return (f"~ {p.mood} | {rate} | depth {p.depth:.2f} center {p.center:.2f} "
            f"jitter {p.jitter:.2f}  {spark(p.shape)}")


# ---------------------------------------------------------------- engine

class LFOEngine:
    """Runs on its own thread; morphs between patches and sends MIDI CC."""

    def __init__(self, port, channel=0, cc_main=20, cc_rhythm=21,
                 morph_s=4.0, update_hz=120):
        self.port = port
        self.channel = channel
        self.cc_main = cc_main
        self.cc_rhythm = cc_rhythm
        self.morph_s = morph_s
        self.update_hz = update_hz
        self.lock = threading.Lock()
        self.old = Patch()
        self.new = Patch()
        self.morph_t0 = time.perf_counter() - morph_s
        self.phase = 0.0
        self.noise = 0.0
        self.noise_target = 0.0
        self.next_noise = 0.0
        self.rhythm = []  # [(duration_s, value 0-1)]
        self.r_idx = 0
        self.r_next = 0.0
        self.r_val = 0.0
        self.solo = None  # None | "main" | "rhythm"
        self.last_sent = {}
        self.running = False
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.running = True
        self.thread.start()

    def stop(self):
        self.running = False
        self.thread.join(timeout=1)

    def _current(self, now):
        t = 1.0 if self.morph_s <= 0 else clamp((now - self.morph_t0) / self.morph_s)
        t = t * t * (3 - 2 * t)  # smoothstep
        return blend(self.old, self.new, t)

    def current(self):
        with self.lock:
            return self._current(time.perf_counter())

    def set_patch(self, patch):
        with self.lock:
            now = time.perf_counter()
            self.old = self._current(now)
            self.new = patch
            self.morph_t0 = now

    def set_rhythm(self, steps):
        with self.lock:
            self.rhythm = steps
            self.r_idx = 0
            self.r_next = time.perf_counter()

    def _send(self, cc, value):
        if self.last_sent.get(cc) == value:
            return
        self.last_sent[cc] = value
        self.port.send(mido.Message("control_change", channel=self.channel,
                                    control=cc, value=value))

    def step(self, now, dt):
        """Advance one tick; returns (main_value, rhythm_value_or_None, solo)."""
        with self.lock:
            p = self._current(now)
            self.phase = (self.phase + p.rate_hz * dt) % 1.0

            if now >= self.next_noise:
                self.noise_target = random.uniform(-1, 1)
                self.next_noise = now + clamp(1 / (p.rate_hz * 4), 0.05, 2.0)
            self.noise += (self.noise_target - self.noise) * min(1.0, dt * 10)

            s = sample_shape(p.shape, self.phase)
            v = p.center + (s - 0.5) * p.depth + self.noise * p.jitter * 0.35
            main = int(round(clamp(v) * 127))

            rv = None
            if self.rhythm:
                if now >= self.r_next:
                    dur, self.r_val = self.rhythm[self.r_idx]
                    self.r_idx = (self.r_idx + 1) % len(self.rhythm)
                    self.r_next = now + dur
                rv = int(round(self.r_val * 127))
            return main, rv, self.solo

    def _run(self):
        period = 1 / self.update_hz
        last = time.perf_counter()
        while self.running:
            now = time.perf_counter()
            main, rv, solo = self.step(now, now - last)
            last = now
            try:
                if solo != "rhythm":
                    self._send(self.cc_main, main)
                if rv is not None and solo != "main":
                    self._send(self.cc_rhythm, rv)
            except Exception as e:  # port vanished etc.
                print(f"\n[midi error: {e}]")
                self.running = False
            time.sleep(max(0.0, period - (time.perf_counter() - now)))


# ---------------------------------------------------------------- typing

def typing_metrics(t_prompt, times, backspaces, text):
    if not times:
        return {"hesitation_s": 0, "typing_s": 0, "chars": len(text),
                "backspaces": backspaces, "chars_per_s": 0,
                "unevenness": 0, "longest_pause_s": 0}
    iv = [b - a for a, b in zip(times, times[1:])]
    total = times[-1] - times[0]
    mean = statistics.mean(iv) if iv else 0
    sd = statistics.pstdev(iv) if len(iv) > 1 else 0
    return {
        "hesitation_s": round(times[0] - t_prompt, 2),
        "typing_s": round(total, 2),
        "chars": len(text),
        "backspaces": backspaces,
        "chars_per_s": round(len(times) / total, 1) if total > 0 else 0,
        "unevenness": round(sd / mean, 2) if mean else 0,
        "longest_pause_s": round(max(iv), 2) if iv else 0,
    }


def rhythm_steps(times, speed=1.0, keep=32):
    """Keystroke intervals -> step sequence. Long pauses = high values."""
    iv = [b - a for a, b in zip(times, times[1:])][-keep:]
    if len(iv) < 2:
        return []
    lo, hi = 0.03, 1.5
    steps = []
    for i in iv:
        i = clamp(i, lo, hi)
        val = (math.log(i) - math.log(lo)) / (math.log(hi) - math.log(lo))
        steps.append((i / speed, val))
    return steps


def read_line_timed(prompt):
    """Returns (text, keystroke_times, backspaces, t_prompt)."""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    t_prompt = time.perf_counter()
    if msvcrt is None:  # non-Windows fallback: no per-key timing
        text = input()
        return text, [time.perf_counter()], 0, t_prompt
    buf, times, backspaces = [], [], 0
    while True:
        ch = msvcrt.getwch()
        now = time.perf_counter()
        if ch in ("\r", "\n"):
            sys.stdout.write("\n")
            break
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch in ("\x00", "\xe0"):  # arrow / function keys: swallow
            msvcrt.getwch()
            continue
        if ch == "\x08":
            if buf:
                buf.pop()
                sys.stdout.write("\b \b")
                backspaces += 1
                times.append(now)
        else:
            buf.append(ch)
            sys.stdout.write(ch)
            times.append(now)
        sys.stdout.flush()
    return "".join(buf), times, backspaces, t_prompt


# ---------------------------------------------------------------- llm

class LLMError(Exception):
    pass


def ask(model, messages, temperature):
    body = {"model": model, "messages": messages, "stream": False,
            "format": SCHEMA, "options": {"temperature": temperature}}
    try:
        r = requests.post(OLLAMA_URL, json=body, timeout=180)
    except requests.ConnectionError:
        raise LLMError("can't reach Ollama. Is it running? (open the Ollama app or run `ollama serve`)")
    if r.status_code == 404:
        raise LLMError(f"model '{model}' not found. Run: ollama pull {model}")
    if r.status_code != 200:
        raise LLMError(f"Ollama error {r.status_code}: {r.text[:200]}")
    content = r.json()["message"]["content"]
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        raise LLMError("model returned malformed JSON; try again or use a bigger model")
    return data, content


# ---------------------------------------------------------------- main

def open_port(name_part):
    names = mido.get_output_names()
    for n in names:
        if name_part.lower() in n.lower():
            return mido.open_output(n), n
    print(f"No MIDI output matching '{name_part}'. Available outputs:")
    for n in names:
        print(f"  - {n}")
    print("Create a port in loopMIDI, or pass --port with part of a name above.")
    sys.exit(1)


HELP = """commands:
  /solo main     only send the main LFO (use while MIDI-mapping in Ableton)
  /solo rhythm   only send the keystroke-rhythm lane
  /solo off      send both
  /patch         show the current patch
  /quit          exit"""


def main():
    ap = argparse.ArgumentParser(description="Converse with an AI that rewrites your LFO.")
    ap.add_argument("--model", default="llama3.2", help="Ollama model name")
    ap.add_argument("--port", default="loopMIDI", help="part of the MIDI output name")
    ap.add_argument("--channel", type=int, default=1, help="MIDI channel 1-16")
    ap.add_argument("--cc", type=int, default=20, help="CC number for the main LFO")
    ap.add_argument("--rhythm-cc", type=int, default=21, help="CC number for the typing-rhythm lane")
    ap.add_argument("--morph", type=float, default=4.0, help="seconds to morph between patches")
    ap.add_argument("--bpm", type=float, default=None, help="quantize LFO rate to divisions of this tempo")
    ap.add_argument("--rhythm-speed", type=float, default=1.0, help="playback speed of the typing rhythm")
    ap.add_argument("--temperature", type=float, default=0.95)
    args = ap.parse_args()

    port, port_name = open_port(args.port)
    engine = LFOEngine(port, channel=int(clamp(args.channel, 1, 16)) - 1,
                       cc_main=args.cc, cc_rhythm=args.rhythm_cc, morph_s=args.morph)
    engine.start()

    os.makedirs("sessions", exist_ok=True)
    log_path = os.path.join("sessions", datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + ".jsonl")
    log = open(log_path, "a", encoding="utf-8")

    print(f"lfo_chat | {args.model} -> {port_name} ch{args.channel} "
          f"CC{args.cc} (lfo) CC{args.rhythm_cc} (rhythm) | /help for commands")
    print(f"logging to {log_path}\n")

    history = [{"role": "system", "content": SYSTEM_PROMPT}]

    def turn(user_content):
        history.append({"role": "user", "content": user_content})
        del history[1:-HISTORY_KEEP]
        print("   ...", end="\r", flush=True)
        try:
            data, raw = ask(args.model, history, args.temperature)
        except LLMError as e:
            history.pop()
            print(f"[{e}]")
            return None
        history.append({"role": "assistant", "content": raw})
        patch = to_patch(data, args.bpm)
        engine.set_patch(patch)
        print(f"ai> {data.get('reply', '').strip()}")
        print(f"    {describe(patch)}\n")
        return data, patch

    try:
        if turn("[The session begins. Open with one strange question.]") is None:
            return

        while True:
            text, times, backspaces, t_prompt = read_line_timed("you> ")
            cmd = text.strip()
            if not cmd:
                continue
            if cmd.startswith("/"):
                parts = cmd.split()
                if parts[0] in ("/quit", "/exit"):
                    break
                elif parts[0] == "/help":
                    print(HELP)
                elif parts[0] == "/patch":
                    print("    " + describe(engine.current()))
                elif parts[0] == "/solo" and len(parts) > 1 and parts[1] in ("main", "rhythm", "off"):
                    engine.solo = None if parts[1] == "off" else parts[1]
                    print(f"    solo: {parts[1]}")
                else:
                    print(HELP)
                continue

            metrics = typing_metrics(t_prompt, times, backspaces, text)
            steps = rhythm_steps(times, args.rhythm_speed)
            if steps:
                engine.set_rhythm(steps)
            result = turn(f"{text}\n\n[typing: {json.dumps(metrics)}]")
            if result:
                data, patch = result
                log.write(json.dumps({
                    "time": datetime.datetime.now().isoformat(timespec="seconds"),
                    "you": text, "typing": metrics,
                    "ai": data.get("reply"), "patch": asdict(patch),
                }) + "\n")
                log.flush()
    except KeyboardInterrupt:
        print()
    finally:
        engine.stop()
        port.close()
        log.close()
        print("bye.")


if __name__ == "__main__":
    main()
