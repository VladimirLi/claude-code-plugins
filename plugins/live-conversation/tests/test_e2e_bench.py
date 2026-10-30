"""bench/run_ours.py e2e helpers: one TTS per run, next brain started during the tail."""
import asyncio
import os
import sys
import unittest
from collections import OrderedDict

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PLUGIN, "bench"))

import run_ours  # noqa: E402


class FakeTTS:
    def __init__(self):
        self.cache = OrderedDict()
        self.starts = self.closes = 0

    async def start(self):
        self.starts += 1
        if not self.cache:
            self.cache["okay."] = ["load"]

    async def synthesize(self, text):
        self.cache.setdefault(text.lower(), [text])
        yield text

    async def close(self):
        self.closes += 1


class SharedTTSTest(unittest.TestCase):
    def test_every_scenario_starts_from_the_warm_cache(self):
        async def go():
            inner = FakeTTS()
            tts = run_ours.SharedTTS(inner)
            await tts.start()
            async for _ in tts.synthesize("Hmm,"):         # the orchestrator's interjection warm-up
                pass
            tts.mark_warm()
            async for _ in tts.synthesize("Canberra is the capital."):   # scenario 1 content
                pass
            await tts.close()                              # orchestrator close: no-op
            self.assertEqual(inner.closes, 0)
            await tts.start()                              # scenario 2
            self.assertEqual(list(inner.cache), ["okay.", "hmm,"])
            tts.mark_warm()                                # only the first mark counts
            await tts.aclose()
            self.assertEqual(inner.closes, 1)
        asyncio.run(go())


class FakeBrain:
    started = 0

    def __init__(self, cfg=None):
        self.cfg = cfg
        self.closed = False

    async def start(self):
        FakeBrain.started += 1
        await asyncio.sleep(0.01)

    async def close(self):
        self.closed = True


class BrainPrestartTest(unittest.TestCase):
    def setUp(self):
        self.orig = run_ours.Brain
        run_ours.Brain = FakeBrain
        FakeBrain.started = 0

    def tearDown(self):
        run_ours.Brain = self.orig

    def test_prestarted_brain_starts_once(self):
        async def go():
            p = run_ours.BrainPrestart({})
            self.assertIsNone(p.take())
            p.fire()
            p.fire()                                       # fired once per scenario
            b = p.take()
            await b.start()                                # the orchestrator's start only waits
            self.assertEqual(FakeBrain.started, 1)
            self.assertEqual(b.cfg["cwd"], run_ours.REPO)
            self.assertIsNone(p.take())
        asyncio.run(go())

    def test_unused_prestart_is_closed(self):
        async def go():
            p = run_ours.BrainPrestart({})
            p.fire()
            b = p.brain
            await p.discard()
            self.assertTrue(b.closed)
        asyncio.run(go())


class Tracker:
    def __init__(self, start):
        self.start = start


class TailAudio:
    def __init__(self, fed, heard, active):
        self.turns = [{"turn": 1}, {"turn": 2}]
        self._next = 2 if fed else 1
        self._cur = None
        self._trackers = {2: Tracker(1000 if heard else None)}
        self._active = active
        self.done = False

    def _reply_active(self):
        return self._active


class TailTest(unittest.TestCase):
    def test_only_when_last_reply_is_out(self):
        self.assertTrue(run_ours._only_tail_left(TailAudio(fed=True, heard=True, active=False)))
        self.assertFalse(run_ours._only_tail_left(TailAudio(fed=False, heard=True, active=False)))
        self.assertFalse(run_ours._only_tail_left(TailAudio(fed=True, heard=False, active=False)))
        self.assertFalse(run_ours._only_tail_left(TailAudio(fed=True, heard=True, active=True)))


if __name__ == "__main__":
    unittest.main()
