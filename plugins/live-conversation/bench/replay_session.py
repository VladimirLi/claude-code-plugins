"""Replay a recorded live session through the current pipeline, in real time.

    python bench/replay_session.py --session <recorded dir> --out <new dir> [--cwd DIR] [--config FILE]
                                   [--tail-s 3] [--fast]

The left channel (mic) of <session>/audio.wav is fed as the microphone, 20 ms frames, through the
same frame path as LiveAudio (sink tick, recorder, clock, then the orchestrator's STT, barge-in
and endpointer feeds). The components come from build(cfg) with the recorded session.json config:
the real brain (claude), STT and TTS. Nothing plays on a device: the sink output is recorded as
the right channel. <out> gets events.jsonl, audio.wav and session.json written by live/recorder.py,
so `bin/live-review <out>` works unchanged.

Task handoff points at a temp run dir <out>/run (never a real one): a TaskBridge runs on it, so
the brain's handoffs are logged (`task_handoff`) and left there; nothing executes them. Every
`task_result` of the recording is delivered again at the same t_ms through the same TaskBridge
path: a `<n>.handed.json` is written at start and `<n>.result.json` at that time.

Limits: the mic still carries the recording's speaker echo (the old replies), not the new ones.
The brain works in the recorded cwd (or --cwd) with its normal tools.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import wave
from typing import AsyncIterator, Callable, Optional

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, PLUGIN)

from live import tasks  # noqa: E402
from live.audio_io import Sink  # noqa: E402
from live.events import FRAME, SR, Clock, EventLog  # noqa: E402

HANDOFF_HEAD = "\n\nHanding work to Claude Code:"


# -- pure parts (unit tested) ----------------------------------------------------------------
def read_left(path: str) -> np.ndarray:
    """Left channel (mic) of a recorder audio.wav as int16 at 24 kHz. Mono files pass through."""
    with wave.open(path) as w:
        assert w.getsampwidth() == 2 and w.getframerate() == SR, (path, w.getframerate())
        ch = w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    return x.reshape(-1, ch)[:, 0].copy()


def mic_frame(mic: np.ndarray, pos: int) -> np.ndarray:
    """The 20 ms frame at sample pos; zeros past the end (trailing silence)."""
    out = np.zeros(FRAME, np.int16)
    seg = mic[pos:pos + FRAME]
    out[:len(seg)] = seg
    return out


def total_frames(n_samples: int, tail_s: float) -> int:
    return -(-n_samples // FRAME) + int(round(tail_s * SR / FRAME))


def frame_due(t0: float, pos: int) -> float:
    """Wall time (monotonic) at which the frame starting at sample pos has fully arrived."""
    return t0 + (pos + FRAME) / SR


class Schedule:
    """Callbacks at mic-clock times. fire(t_ms) runs each one whose time has come, once, in order."""

    def __init__(self) -> None:
        self.items: list[tuple[float, int, Callable[[], None]]] = []

    def at(self, t_ms: float, fn: Callable[[], None]) -> None:
        self.items.append((float(t_ms), len(self.items), fn))
        self.items.sort(key=lambda x: (x[0], x[1]))

    def fire(self, t_ms: float) -> int:
        n = 0
        while self.items and self.items[0][0] <= t_ms:
            _, _, fn = self.items.pop(0)
            fn()
            n += 1
        return n


def recorded_tasks(events: list[dict]) -> list[dict]:
    """task_result events of a recording, with the task text when the recording shows it."""
    texts: dict[int, str] = {}
    for e in events:
        if e.get("event") == "task_handoff" and e.get("task") is not None:
            texts[int(e["task"])] = e.get("text", "")
    for e in events:          # the announce prompt quotes the task (cut to 200 chars by the log)
        if e.get("event") == "x_announce":
            m = re.match(r'\[Task (\d+) finished[^"]*"(.*)', e.get("text", ""), re.S)
            if m and int(m.group(1)) not in texts:
                texts[int(m.group(1))] = m.group(2).split('".')[0].rstrip('"')
    out = []
    for e in events:
        if e.get("event") == "task_result" and e.get("task") is not None:
            n = int(e["task"])
            out.append({"t_ms": float(e.get("t_ms") or 0.0), "task": n, "reason": e.get("reason", "answer"),
                        "text": texts.get(n, "")})
    return out


def report_text(text: str) -> str:
    """A short, plausible Claude Code report for an injected result."""
    return ("Done. I looked into it and made no file changes. I found two likely causes and wrote "
            "them up with a suggested fix for each; the details are in the session if you want them.")


def replay_config(recorded: dict, overrides: Optional[dict], cwd: str, handoff_cmd: str) -> dict:
    """The recorded config, per-section overrides on top, brain cwd and a fresh handoff prompt."""
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in (recorded or {}).items()}
    for sec, vals in (overrides or {}).items():
        if isinstance(vals, dict) and isinstance(cfg.get(sec), dict):
            cfg[sec] = {**cfg[sec], **vals}
        else:
            cfg[sec] = vals
    b = cfg.setdefault("brain", {})
    b["cwd"] = cwd
    extra = b.get("prompt_extra") or ""
    i = extra.find(HANDOFF_HEAD)
    if i >= 0:
        extra = extra[:i]
    b["prompt_extra"] = extra + tasks.handoff_prompt(handoff_cmd)
    return cfg


# -- the mic -----------------------------------------------------------------------------------
class ReplayAudio:
    """LiveAudio's frame path with the recorded mic as input and no speaker device."""

    def __init__(self, log: EventLog, mic: np.ndarray, recorder=None, tail_s: float = 3.0,
                 realtime: bool = True, schedule: Optional[Schedule] = None) -> None:
        self.log = log
        self.clock = log.clock
        self.sink = Sink(self.clock, log)
        self.recorder = recorder
        self.mic = mic
        self.n_frames = total_frames(len(mic), tail_s)
        self.realtime = realtime
        self.schedule = schedule or Schedule()
        self.done = False

    def turn_hint(self) -> Optional[int]:
        return None

    async def frames(self) -> AsyncIterator[np.ndarray]:
        self.log.emit("session_start", None, t_ms=0.0)
        self.schedule.fire(self.clock.ms)
        t0 = time.monotonic()
        for _ in range(self.n_frames):
            if self.done:
                break
            pos = self.clock.samples
            if self.realtime:
                delay = frame_due(t0, pos) - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
            frame = mic_frame(self.mic, pos)
            out = self.sink.tick(pos, FRAME)
            if self.recorder is not None:
                self.recorder.audio(frame, out)
            self.clock.advance(FRAME)
            self.schedule.fire(self.clock.ms)
            yield frame
        self.done = True


# -- main ---------------------------------------------------------------------------------------
def _write_json(path: str, obj: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


async def replay(a) -> None:
    from live.orchestrator import Orchestrator, build
    from live.recorder import Recorder

    sess = os.path.abspath(a.session)
    out = os.path.abspath(a.out)
    if os.path.exists(os.path.join(out, "events.jsonl")):
        raise SystemExit(f"{out} already holds a session")
    meta = json.load(open(os.path.join(sess, "session.json")))
    events = [json.loads(line) for line in open(os.path.join(sess, "events.jsonl")) if line.strip()]
    mic = read_left(os.path.join(sess, "audio.wav"))
    overrides = json.load(open(a.config)) if a.config else None

    run_dir = os.path.join(out, "run")
    os.makedirs(run_dir, exist_ok=True)
    cmd = tasks.command(sys.executable, os.path.join(PLUGIN, "bin", "live-task"), run_dir)
    cwd = os.path.abspath(a.cwd or meta.get("cwd") or os.getcwd())
    cfg = replay_config(meta.get("config") or {}, overrides, cwd, cmd)

    rec = Recorder(out, cwd, cfg)
    with open(os.path.join(out, "session.json")) as f:
        sj = json.load(f)
    sj["replay_of"] = sess
    _write_json(os.path.join(out, "session.json"), sj)

    log = EventLog(Clock(), echo=a.echo, listener=rec.on_event)
    log.emit("x_recording", None, t_ms=0.0, path=out)

    # recorded task results: handed now (so live-task numbers new tasks after them), result at t_ms
    tdir = os.path.join(run_dir, "tasks")
    os.makedirs(tdir, exist_ok=True)
    sched = Schedule()
    for t in recorded_tasks(events):
        n = t["task"]
        _write_json(os.path.join(tdir, f"{n}.handed.json"), {"task": n, "text": t["text"], "t": time.time()})
        res = {"task": n, "reason": t["reason"], "answer": report_text(t["text"]), "durationMs": 60000}
        sched.at(t["t_ms"], lambda n=n, res=res: _write_json(os.path.join(tdir, f"{n}.result.json"), res))

    audio = ReplayAudio(log, mic, recorder=rec, tail_s=a.tail_s, realtime=not a.fast, schedule=sched)
    orch = Orchestrator(audio, log, build(cfg), cfg)
    bridge = None
    try:
        await orch.start()
        sched.fire(0.0)          # results waiting before the session started, as in the recording
        bridge = asyncio.create_task(
            tasks.TaskBridge(run_dir, log).run(lambda res: orch.announce(tasks.result_prompt(res))))
        await orch.run()
    finally:
        if bridge is not None:
            bridge.cancel()
        await asyncio.shield(orch.close())
        log.emit("session_end", None)
        rec.close()
    print(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--session", required=True, help="recorded session folder (audio.wav, events.jsonl, session.json)")
    ap.add_argument("--out", required=True, help="new folder for the replayed session")
    ap.add_argument("--cwd", help="folder the brain works in (default: the recorded one)")
    ap.add_argument("--config", help="JSON with per-module sections, merged over the recorded config")
    ap.add_argument("--tail-s", type=float, default=3.0, help="silence fed after the recording ends")
    ap.add_argument("--fast", action="store_true", help="feed frames as fast as possible (debug only)")
    ap.add_argument("--echo", action="store_true", help="print events on stderr")
    a = ap.parse_args()
    asyncio.run(replay(a))


if __name__ == "__main__":
    main()
