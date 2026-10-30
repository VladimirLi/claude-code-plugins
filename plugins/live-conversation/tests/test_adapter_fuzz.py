"""Adapter fuzzer (FLOOR-SPEC.md "Fuzzer", second layer): the orchestrator under
`orchestrator.floor: true` (live/floor_adapter.py) on random scripts of clock-driven fakes
(tests/adapter_fakes.py), with realtime-free frames. Run: .venv/bin/python -m unittest tests.test_adapter_fuzz

    ADAPTER_FUZZ_SEEDS=2000   how many seeds (default 100)

It checks what only the adapter can break:
- no stale callback reaches the sink: every write is the current reply's, never a stopped one's,
  never while its gate is paused (late text or audio of a stopped reply, synthesis across a pause,
  a keepalive in flight);
- the floor Checker's invariants and rule oracles (tests/floor_driver.py) on the adapter's real
  events and actions, with the sink's real positions as `played`.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from typing import Optional

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from adapter_fakes import FakeAudio, FakeBargeIn, FakeBrain, FakeEndpointer, FakeSTT, FakeTTS, Script  # noqa: E402
from floor_driver import Checker  # noqa: E402

from live import floor as F  # noqa: E402
from live.events import Clock, EventLog  # noqa: E402
from live.floor_adapter import FloorAdapter  # noqa: E402
from live.orchestrator import Components  # noqa: E402
from live.turntaking import TurnTaking, is_hesitation, normalize, fold_hums  # noqa: E402

SEEDS = int(os.environ.get("ADAPTER_FUZZ_SEEDS", "100"))
WORDS = ["show me the staging logs", "check database errors", "rerun the lint", "open the config file",
         "compare test failures", "summarize the queue depth", "explain the login flow"]
REPLIES = ["The build is green.", "Deploy finished in four minutes.", "The cache was cold.",
           "Branch merged cleanly.", "Nothing else changed.", "Should I keep going?"]


def said(text: str) -> str:
    toks = fold_hums(normalize(text)).split()
    return " ".join(w for w in toks if w not in F.HUMS and not is_hesitation(w))


class Truth:
    """utt -> words; a lone backchannel is judged by the checker from what played."""

    def __init__(self, shim) -> None:
        self.shim = shim
        self.words: dict = {}
        self.backchannel: set = set()

    def _over(self, utt):
        c = self.shim.checker
        return c.seg_over_heard(utt) if utt in c.segs else None

    def get(self, utt, default=None):
        if utt in self.backchannel:
            return "" if self._over(utt) else self.words[utt]
        return self.words.get(utt, default)

    def __getitem__(self, utt):
        return self.get(utt)

    def items(self):
        return [(u, self.get(u)) for u in self.words if not self.optional(u)]

    def optional(self, utt) -> bool:
        return utt in self.backchannel and self._over(utt) is None

    def interim_has_words(self, utt, text) -> bool:
        if utt in self.backchannel:
            return False
        return bool(set(said(text).split()) & set(said(self.words.get(utt, "")).split()))


class _SinkView:
    """The sink as the Checker sees a Sim's: the current reply's gate and what is left of it."""

    def __init__(self, o) -> None:
        self.o = o

    @property
    def rid(self):
        return self.o.reply.rid if self.o.reply is not None else self.o.last_rid

    @property
    def paused(self) -> bool:
        return self.o.reply is not None and self.o.reply.paused

    @property
    def q(self) -> list:
        r = self.o.reply
        if r is None:
            return []
        if r.paused:
            return r.pieces[r.resume[0]:]
        return [1] if self.o.sink.queued_end > self.o.clock.samples or r.written < len(r.pieces) else []

    def audible(self) -> bool:
        return not self.paused and self.o.sink.queued_end > self.o.clock.samples and self.o.reply is not None


class _Shim:
    """What floor_driver.Checker reads of a Sim, from the adapter."""

    def __init__(self, o, truth_cls=Truth) -> None:
        self.o = o
        self.f = o.floor
        self.truth = truth_cls(self)
        self.events: list = []
        self.q: list = []
        self.sink = _SinkView(o)
        self.checker: Optional[Checker] = None

    @property
    def t(self) -> float:
        return self.o.clock.ms

    @property
    def timers(self) -> dict:
        return self.o.timers


class CheckedAdapter(FloorAdapter):
    def _spawn(self, coro) -> asyncio.Task:
        t = super()._spawn(coro)

        def done(t):
            if not t.cancelled() and t.exception() is not None:
                self.failures.append(f"task failed: {type(t.exception()).__name__}: {t.exception()}")
        t.add_done_callback(done)
        return t

    """FloorAdapter with the Checker on every event and action, and a guard on every sink write."""

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.last_rid = None
        self.stopped: set = set()
        self.dropped: set = set()                  # (rid, piece) the controller dropped
        self.failures: list = []                   # found inside tasks, where a raise is lost
        self.shim = _Shim(self)
        self.checker = self.shim.checker = Checker(self.shim)
        write = self.sink.write

        def guarded(pcm, turn=None, filler=False, tag=None):
            r = self.reply
            if tag is None or r is None or tag[0] != r.rid or tag[0] in self.stopped or r.paused:
                self.failures.append(f"stale write {tag} at {self.clock.ms:.0f} (current "
                                     f"{r.rid if r else None}, paused {r.paused if r else None})")
            elif (tag[0], tag[1]) in self.dropped:
                self.failures.append(f"dropped piece {tag} written at {self.clock.ms:.0f}")
            return write(pcm, turn=turn, filler=filler, tag=tag)
        self.sink.write = guarded

    def _on(self, ev) -> None:
        rid = getattr(ev, "rid", None)
        if rid is not None and (self.reply is None or self.reply.rid != rid):
            return
        ev.t_ms = self.clock.ms
        self.shim.events.append((ev.t_ms, ev))
        self.checker.before(ev)
        acts = self.floor.on(ev)
        for a in acts:
            self.checker.on_action(a)
            if isinstance(a, F.StopReply):
                self.stopped.add(a.rid)
            if isinstance(a, F.StartReply):
                self.last_rid = a.rid
            if isinstance(a, F.Drop):
                self.dropped.add((a.rid, a.piece))
            self._do(a)
        self.checker.after(ev, acts)


def scenario(seed: int):
    rng = np.random.default_rng(seed)
    s = Script(brain_ms=float(rng.choice([300, 800, 3000, 7000])), brain_gap_ms=float(rng.uniform(50, 2000)),
               tts_ms=float(rng.uniform(20, 600)), end_ms=25000.0)
    s.replies = {"": " ".join(str(x) for x in rng.choice(REPLIES, int(rng.integers(1, 4)), replace=False))}
    kinds: dict = {}
    t = 100.0
    used: set = set()
    while t < 22000:
        kind = rng.choice(["words", "words", "noise", "hallucination", "backchannel"])
        dur = float(rng.uniform(150, 2000)) if kind == "words" else float(rng.uniform(100, 700))
        if kind == "words":
            text = str(rng.choice(WORDS))
            if text in used:
                text += f" ticket {len(used)}"
            used.add(text)
            ints = [(t + 60 + k * 1000 + rng.uniform(350, 500), " ".join(text.split()[:2]))
                    for k in range(1, int(dur // 1000) + 2)]
        elif kind == "backchannel":
            text, ints = str(rng.choice(["yeah", "okay", "right"])), []
        elif kind == "hallucination":
            text, ints = "Thank you.", []
        else:
            text, ints = "", []
        s.segment(t, t + dur, text, final_ms=float(rng.choice([100, 300, 800, 2500])), interims=ints)
        kinds[str(len(s.segments))] = kind
        if rng.random() < 0.3:
            s.detector(t + 40, "barge_in", "open")
        t += dur + 620 + float(rng.uniform(0, 6000))
    for _ in range(int(rng.integers(0, 4))):
        s.detector(float(rng.uniform(1000, 22000)), "barge_in", "open")      # clicks
    if rng.random() < 0.2:
        s.errors = {str(rng.choice(WORDS))}
    tasks = [float(rng.uniform(0, 20000)) for _ in range(int(rng.choice([0, 0, 1, 2])))]
    speakers = bool(rng.random() < 0.2)
    return s, kinds, tasks, speakers


def run_seed(seed: int) -> None:
    s, kinds, tasks, speakers = scenario(seed)
    log = EventLog(Clock())
    audio = FakeAudio(log, s)
    c = Components(endpointer=FakeEndpointer(s), stt=FakeSTT(s, log.clock), brain=FakeBrain(s, log.clock),
                   tts=FakeTTS(s, log.clock), bargein=FakeBargeIn(s), turntaking=TurnTaking({"fillers": False}))
    o = CheckedAdapter(audio, log, c, {"orchestrator": {"floor": True}, "floor": {"speakers": speakers}})
    truth = o.shim.truth
    for i, seg in enumerate(s.segments, 1):
        utt = str(i)
        truth.words[utt] = seg["text"] if kinds[utt] in ("words", "backchannel") else ""
        if kinds[utt] == "backchannel":
            truth.backchannel.add(utt)
    busy = audio._busy
    audio._busy = lambda: busy() or bool(o.timers) or any(not t.done() for t in c.stt.tasks) or bool(o.tasks)

    async def go():
        await o.start()

        async def announce(at, n):
            while o.clock.ms < at:
                await asyncio.sleep(0)
            o.announce(f"Task {n} finished.")
        for n, at in enumerate(tasks, 1):
            asyncio.ensure_future(announce(at, n))
        await o.run()
        o.checker.quiescent()
        await o.close()
    asyncio.run(go())
    if o.failures:
        raise AssertionError("; ".join(o.failures[:3]))


class TestAdapterFuzz(unittest.TestCase):
    def test_seeds(self):
        found = []
        for seed in range(SEEDS):
            try:
                run_seed(seed)
            except Exception as e:                   # noqa: BLE001 - any crash is a finding
                found.append(f"seed {seed}: {type(e).__name__}: {e}")
                if len(found) >= 5:
                    break
        self.assertEqual(found, [], "\n".join(found))


if __name__ == "__main__":
    unittest.main()
