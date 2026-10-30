"""Fakes for the floor adapter (live/floor_adapter.py) on the mic clock: every wait is clock.ms, so
a run with realtime=False frames is exact and fast. Used by test_floor_adapter.py and
test_adapter_fuzz.py.

    script = Script()
    script.segment(start_ms=100, end_ms=1100, utt_text="What is in the logs?", final_ms=300)
    script.detector(2500, "barge_in", "open")
    o, log = make(script)            # an Orchestrator with {"orchestrator": {"floor": True}}
    asyncio.run(drive(o))
"""
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PLUGIN)

from live.audio_io import Sink  # noqa: E402
from live.endpointing import Decision  # noqa: E402
from live.events import FRAME, SR, Clock, EventLog  # noqa: E402
from live.orchestrator import Components, Orchestrator  # noqa: E402
from live.turntaking import TurnTaking  # noqa: E402

MS_PER_WORD = 300.0


async def wait_ms(clock: Clock, ms: float) -> None:
    """Wait `ms` of mic clock (the frames advance it). Before the frames run (start()), at once."""
    if not getattr(clock, "running", False):
        return
    until = clock.ms + ms
    while clock.ms < until:
        await asyncio.sleep(0)


def speech(text: str) -> np.ndarray:
    """A piece's audio: MS_PER_WORD per word, every sample distinct within the piece (a ramp with
    an id), so tests can tell which part of which piece played."""
    n = int(max(1, len(text.split())) * MS_PER_WORD * SR / 1000)
    return ((np.arange(n) % 20000) + (sum(map(ord, text)) % 7) * 1000 + 1).astype(np.int16)


@dataclass
class Script:
    """What the user does and how the components answer, in clock ms."""
    segments: list = field(default_factory=list)     # dicts: start, end, text, final_ms, interims
    detectors: list = field(default_factory=list)    # (t, decision, reason)
    brain_ms: float = 300.0                          # start -> first text
    brain_gap_ms: float = 50.0                       # between sentences
    replies: dict = field(default_factory=dict)      # question substring -> text ("" = the default)
    errors: set = field(default_factory=set)         # question substrings whose brain fails
    tts_ms: float = 50.0
    tts_errors: set = field(default_factory=set)     # sentence substrings the TTS fails on
    cancel_ms: float = 0.0                           # brain.cancel: the next respond waits this long
    end_ms: float = 20000.0

    def segment(self, start_ms, end_ms, text, final_ms=300.0, interims=(), how="spec"):
        """`how`: "spec", or "timeout" (the final comes back empty), as the STT reports it."""
        self.segments.append({"start": start_ms, "end": end_ms, "text": text, "final_ms": final_ms,
                              "interims": list(interims), "how": how})

    def detector(self, t, decision="barge_in", reason="open"):
        self.detectors.append((t, decision, reason))

    def reply_for(self, question: str) -> str:
        for k, v in self.replies.items():
            if k and k in question:
                return v
        return self.replies.get("", "The logs show no errors. Everything passed.")


class FakeEndpointer:
    def __init__(self, script: Script):
        self.s = script
        self.transcripts = []
        self.trace = []                  # what the real endpointer keeps for x_endpoint

    def feed(self, frame, t_ms):
        out = []
        for seg in self.s.segments:
            if seg["start"] + 60 <= t_ms < seg["start"] + 80:
                out.append(Decision("speech_start", t_ms))
            if seg["end"] + 600 <= t_ms < seg["end"] + 620:
                out.append(Decision("turn_end", t_ms, pause_start_ms=seg["end"]))
                self.trace.append({"pause_start_ms": seg["end"], "class": "statement", "text_ms": 50})
        return out

    def on_transcript(self, text, final):
        self.transcripts.append((text, final))


class FakeSTT:
    """Utterance n is the script's n-th segment. Interims at their times; finals after final_ms."""

    def __init__(self, script: Script, clock: Clock):
        self.s, self.clock = script, clock
        self.on_interim_utt = None
        self.on_interim = None
        self.on_event = None
        self.utt = 0
        self.tasks = []
        self.finalized: set = set()

    def feed(self, frame):
        pass

    def begin(self):
        self.utt += 1
        seg = self.s.segments[self.utt - 1]
        for t, text in seg["interims"]:
            self.tasks.append(asyncio.ensure_future(self._interim(self.utt, t, text)))
        return self.utt

    async def _interim(self, utt, t, text):
        await wait_ms(self.clock, t - self.clock.ms)
        if self.on_interim_utt is not None:
            self.on_interim_utt(utt, text)

    async def final_for(self, utt):
        seg = self.s.segments[utt - 1]
        await wait_ms(self.clock, seg["final_ms"])
        if seg.get("how") == "timeout":
            return "", "timeout"
        if utt in self.finalized:
            return "", "gone"
        self.finalized.add(utt)
        return seg["text"], "spec"

    async def start(self):
        pass

    async def close(self):
        for t in self.tasks:
            t.cancel()


class FakeBargeIn:
    def __init__(self, script: Script):
        self.s = script
        self.last = {}
        self.resets = 0
        self.floor = []

    def feed(self, frame, t_ms, assistant):
        self.floor.append((t_ms, assistant))
        for t, d, why in self.s.detectors:
            if t <= t_ms < t + FRAME * 1000 / SR:
                self.last = {"kind": d, "reason": why}
                return d
        return None

    def reset(self):
        self.resets += 1


class FakeBrain:
    def __init__(self, script: Script, clock: Clock):
        self.s, self.clock = script, clock
        self.calls = []
        self.cancelled = 0
        self.busy_until = 0.0

    async def start(self):
        pass

    async def respond(self, text, interrupted=None):
        # like the real brain: a process still stopping the last reply makes this one wait
        await wait_ms(self.clock, self.busy_until - self.clock.ms)
        self.calls.append((text, interrupted))
        await wait_ms(self.clock, self.s.brain_ms)
        if any(e in text for e in self.s.errors):
            raise RuntimeError("brain failed")
        sentences = [x.strip() + "." for x in self.s.reply_for(text).split(".") if x.strip()]
        for i, sent in enumerate(sentences):
            if i:
                await wait_ms(self.clock, self.s.brain_gap_ms)
            yield sent + " "

    async def cancel(self):
        self.cancelled += 1
        self.busy_until = self.clock.ms + self.s.cancel_ms

    async def close(self):
        pass


class FakeTTS:
    def __init__(self, script: Script, clock: Clock):
        self.s, self.clock = script, clock
        self.calls = []
        self.lock = asyncio.Lock()       # like Kokoro: one synthesis at a time, first come first served

    async def start(self):
        pass

    async def close(self):
        pass

    async def synthesize(self, text):
        async with self.lock:
            self.calls.append(text)
            await wait_ms(self.clock, self.s.tts_ms)
            if any(e in text for e in self.s.tts_errors):
                raise RuntimeError("tts failed")
            pcm = speech(text)
        yield pcm


class FakeAudio:
    """FileAudio's frame loop without files: the sink ticks, then the clock advances."""

    def __init__(self, log: EventLog, script: Script):
        self.clock = log.clock
        self.sink = Sink(self.clock, log)
        self.s = script
        self.done = False
        self.heard: list = []

    def turn_hint(self):
        return None

    async def frames(self):
        self.clock.running = True
        while self.clock.ms < self.s.end_ms or self._busy():
            if self.clock.ms > self.s.end_ms + 60000:
                break
            pos = self.clock.samples
            self.heard.append(self.sink.tick(pos, FRAME))
            self.clock.advance(FRAME)
            yield np.zeros(FRAME, np.int16)
            for _ in range(3):
                await asyncio.sleep(0)

    def _busy(self) -> bool:
        f = getattr(self, "reply_active", None)
        return bool(f and f()) or self.sink.is_playing()      # as FileAudio waits (paused counts)


def make(script: Script, cfg: Optional[dict] = None):
    log = EventLog(Clock())
    audio = FakeAudio(log, script)
    c = Components(endpointer=FakeEndpointer(script), stt=FakeSTT(script, log.clock),
                   brain=FakeBrain(script, log.clock), tts=FakeTTS(script, log.clock),
                   bargein=FakeBargeIn(script), turntaking=TurnTaking({"fillers": False}))
    cfg = {**(cfg or {})}
    cfg["orchestrator"] = {"floor": True, **(cfg.get("orchestrator") or {})}
    return Orchestrator(audio, log, c, cfg), log


async def drive(o) -> None:
    await o.start()
    await o.run()
    await o.close()


def events(log: EventLog, name: str) -> list:
    return [e for e in log.events if e.event == name]
