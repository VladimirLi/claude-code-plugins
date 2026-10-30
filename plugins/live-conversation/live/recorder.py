"""recorder: keeps every live session on disk, so real use can be reviewed and fixed.

One folder per session, under LIVE_SESSIONS_DIR (default ~/.claude/live-conversation/sessions):

    session.json     start time (wall clock), working folder, plugin git revision, config
    events.jsonl     every event as it happens, with t_wall (seconds since the epoch) added
    audio.wav        stereo 24 kHz int16: left = microphone, right = what the speaker played,
                     sample-aligned with the event clock (t_ms = sample / 24)
    feedback.jsonl   notes from `/live feedback <text>` (the mod appends them)

Interface:

    rec = Recorder.open(cwd, cfg)        None when LIVE_RECORD=0
    rec.on_event(event)                  EventLog listener
    rec.audio(mic_frame, out_frame)      one 20 ms frame of each, in step with the clock
    rec.close()

Standard library and numpy only. Writes are flushed often: a killed daemon keeps what it heard.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import wave
from typing import Optional

import numpy as np

from .events import SR, Event

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sessions_dir() -> str:
    return os.environ.get("LIVE_SESSIONS_DIR") or os.path.expanduser("~/.claude/live-conversation/sessions")


def _git_rev() -> str:
    try:
        out = subprocess.run(["git", "-C", PLUGIN, "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, timeout=2)
        dirty = subprocess.run(["git", "-C", PLUGIN, "status", "--porcelain", "--", "."], capture_output=True,
                               text=True, timeout=2).stdout.strip()
        return out.stdout.strip() + ("+dirty" if dirty else "")
    except Exception:
        return ""


class Recorder:
    def __init__(self, path: str, cwd: str, cfg: dict) -> None:
        self.path = path
        os.makedirs(path, exist_ok=True)
        self.t0 = time.time()
        with open(os.path.join(path, "session.json"), "w") as f:
            json.dump({"start_wall": self.t0, "cwd": cwd, "plugin_rev": _git_rev(), "pid": os.getpid(),
                       "config": cfg}, f, indent=1, default=str)
        self._events = open(os.path.join(path, "events.jsonl"), "a", buffering=1)
        self._wav = wave.open(os.path.join(path, "audio.wav"), "wb")
        self._wav.setnchannels(2)
        self._wav.setsampwidth(2)
        self._wav.setframerate(SR)
        self._frames = 0

    @classmethod
    def open(cls, cwd: str, cfg: dict) -> Optional["Recorder"]:
        if os.environ.get("LIVE_RECORD", "1") == "0":
            return None
        name = time.strftime("%Y%m%d-%H%M%S")
        return cls(os.path.join(sessions_dir(), name), cwd, cfg)

    def on_event(self, e: Event) -> None:
        if self._events.closed:
            return
        d = e.to_json()
        d["t_wall"] = round(time.time(), 3)
        self._events.write(json.dumps(d, default=str) + "\n")

    def audio(self, mic: np.ndarray, out: np.ndarray) -> None:
        if self._wav is None:
            return
        n = min(len(mic), len(out))
        st = np.empty(2 * n, np.int16)
        st[0::2] = mic[:n]
        st[1::2] = out[:n]
        self._wav.writeframesraw(st.tobytes())
        self._frames += 1
        if self._frames % 50 == 0:          # once a second: header sizes stay right if we are killed
            self._fix_header()

    def _fix_header(self) -> None:
        try:
            w = self._wav
            pos = w._file.tell()
            w._patchheader()
            w._file.seek(pos)
            w._file.flush()
        except Exception:
            pass

    def close(self) -> None:
        if self._wav is not None:
            try:
                self._wav.close()
            except Exception:
                pass
            self._wav = None
        if not self._events.closed:
            self._events.close()
