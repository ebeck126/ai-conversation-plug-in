# lfo_chat

Talk to a strange local AI in your terminal. It asks personal questions and wanders into odd philosophical territory. Every answer you give redraws an LFO that streams MIDI CC into your DAW.

- **CC 20 — main LFO.** The model draws a 16-point waveform and sets rate, depth, center and jitter from what you said. Your typing body language feeds in too: hesitation, backspaces and uneven rhythm. New patches morph in smoothly over a few seconds.
- **CC 21 — rhythm lane.** A step sequence that replays the timing of your last answer's keystrokes. Long pauses produce high steps.

Everything runs locally through Ollama. Sessions are logged to `sessions/*.jsonl`.

## Setup (Windows)

1. **Ollama.** Install it from ollama.com, then pull a model:
   ```
   ollama pull llama3.2
   ```
   Bigger models, around 7–8B, are weirder and better conversationalists but slower. Use `--model` to swap.
2. **loopMIDI.** Install Tobias Erichsen's loopMIDI and create a port. The default name `loopMIDI Port` works as-is.
3. **Python deps**
   ```
   pip install -r requirements.txt
   ```
   If `python-rtmidi` tries to compile and fails, use a Python version that has prebuilt wheels, for example 3.11 or 3.12.

## Ableton setup

1. Go to Preferences → Link, Tempo & MIDI. Under MIDI Ports, find the loopMIDI **input** and turn on **Remote**.
2. Run the script, then type `/solo main` so only CC 20 is moving.
3. Press **Ctrl+M** for MIDI Map Mode. Click the parameter you want (Auto Filter frequency is a good first target) and wait a moment for it to grab CC 20. Press Ctrl+M again to exit.
4. Type `/solo rhythm` and map CC 21 the same way, then `/solo off`.

## Run

```
python lfo_chat.py
python lfo_chat.py --model qwen2.5:7b --bpm 124 --morph 8
```

| flag | default | |
|---|---|---|
| `--model` | llama3.2 | any Ollama model |
| `--port` | loopMIDI | part of the MIDI output name |
| `--channel` | 1 | MIDI channel |
| `--cc` / `--rhythm-cc` | 20 / 21 | CC numbers |
| `--morph` | 4 | seconds to crossfade between patches |
| `--bpm` | off | snap LFO rate to tempo divisions (1/16 … 4 bars) |
| `--rhythm-speed` | 1.0 | playback speed of the typing rhythm |

In-chat commands: `/solo main|rhythm|off`, `/patch`, `/help`, `/quit`.

`--bpm` snaps the *rate* only. Phase is free-running, so it won't lock to Ableton's bar line.

## Ideas for later

- Sync phase to Ableton via MIDI clock from a second loopMIDI port
- Wrap the LFO engine in a real plugin (JUCE / nih-plug) with the chat as an external process over OSC
- A session replay mode that plays back a logged conversation's patches
