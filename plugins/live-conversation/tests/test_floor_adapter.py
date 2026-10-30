"""The orchestrator with `orchestrator.floor: true` (live/floor_adapter.py), on clock-driven fakes.
Run: .venv/bin/python -m unittest discover -s tests

The old orchestrator's tests (test_orchestrator.py) test its own internals (fillers, holds,
_handle_turn) and stay on the old path until step 6 deletes it; these are their behaviour-level
counterparts under the flag.
"""
import asyncio
import json
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from adapter_fakes import Script, drive, events, make, speech  # noqa: E402

from live.events import Clock, EventLog  # noqa: E402
from live.floor_adapter import FloorAdapter  # noqa: E402
from live.orchestrator import Orchestrator  # noqa: E402

Q1 = "What is in the logs?"


def run(script, cfg=None):
    o, log = make(script, cfg)
    asyncio.run(drive(o))
    return o, log


def heard(o) -> np.ndarray:
    return np.concatenate(o.audio.heard)


class TestSwitch(unittest.TestCase):
    def test_flag_selects_the_adapter(self):
        log = EventLog(Clock())
        o, _ = make(Script())
        self.assertIsInstance(o, FloorAdapter)
        from adapter_fakes import FakeAudio
        old = Orchestrator(FakeAudio(log, Script()), log, o.c, {"orchestrator": {"floor": False}})
        self.assertIsInstance(old, Orchestrator)
        self.assertTrue(o.sink.log_first_audio is False)
        env = os.environ.get("LIVE_FLOOR")
        try:
            os.environ["LIVE_FLOOR"] = "1"                 # the default where the config does not say
            self.assertIsInstance(Orchestrator(FakeAudio(log, Script()), log, o.c, {}), FloorAdapter)
            os.environ.pop("LIVE_FLOOR")
            self.assertIsInstance(Orchestrator(FakeAudio(log, Script()), log, o.c, {}), Orchestrator)
        finally:
            if env is not None:
                os.environ["LIVE_FLOOR"] = env


class TestAnswer(unittest.TestCase):
    def test_question_is_answered_and_logged(self):
        s = Script()
        s.segment(100, 1100, Q1)
        o, log = run(s)
        self.assertEqual([c[0] for c in o.c.brain.calls], [Q1])
        self.assertEqual(len(events(log, "turn_end_detected")), 1)
        self.assertEqual(len(events(log, "first_reply_audio")), 1)
        self.assertEqual([e.extra["text"] for e in events(log, "final_transcript")], [Q1])
        self.assertEqual(len(events(log, "x_turn_end")), 1)
        fa = events(log, "first_reply_audio")[0]
        self.assertEqual(fa.turn, 1)
        self.assertFalse(fa.extra["filler"])
        # both pieces played in full, in order
        h = heard(o)
        p0, p1 = speech("The logs show no errors."), speech("Everything passed.")
        played = h[h != 0]
        self.assertTrue(np.array_equal(played, np.concatenate([p0, p1])))

    def test_transcript_has_the_old_shape(self):
        s = Script()
        s.segment(100, 1100, Q1)
        o, _ = run(s)
        t = o.transcript({1: Q1})
        self.assertEqual(t["turns"][0], {"role": "user", "turn": 1, "script": Q1, "asr_text": Q1})
        a = t["turns"][1]
        self.assertEqual(set(a), {"role", "reply_to", "text_generated", "text_spoken", "interrupted", "fillers"})
        self.assertFalse(a["interrupted"])
        json.dumps(t)


class TestPause(unittest.TestCase):
    def test_blip_pauses_then_plays_on_from_the_pause_point(self):
        s = Script()
        s.segment(100, 1100, Q1)                   # reply starts 2000, first audio about 2350
        s.detector(2700)                           # a click: no segment
        o, log = run(s)
        self.assertEqual(len(events(log, "playback_paused")), 1)
        self.assertEqual(len(events(log, "playback_resumed")), 1)
        self.assertEqual(events(log, "playback_stopped"), [])
        h = heard(o)
        played = h[h != 0]
        whole = np.concatenate([speech("The logs show no errors."), speech("Everything passed.")])
        self.assertTrue(np.array_equal(played, whole))      # 4a: no sample lost or played twice
        self.assertGreaterEqual(o.c.bargein.resets, 1)       # play resets the detector

    def test_words_over_content_interrupt_with_context(self):
        s = Script()
        s.segment(100, 1100, Q1)
        s.segment(2600, 3200, "stop show the errors", interims=[(3000, "stop show")])
        o, log = run(s)
        qs = [c[0] for c in o.c.brain.calls]
        self.assertEqual(qs, [Q1, "stop show the errors"])
        ctx = o.c.brain.calls[1][1]
        self.assertEqual(ctx.question, Q1)
        self.assertTrue(ctx.spoken_text)
        self.assertEqual(len(events(log, "playback_stopped")), 1)
        t = o.transcript()
        first = [x for x in t["turns"] if x.get("reply_to") == 1][0]
        self.assertTrue(first["interrupted"])
        self.assertEqual(first["text_spoken"], ctx.spoken_text)

    def test_noise_before_audio_holds_and_never_restarts(self):
        s = Script()
        s.segment(100, 1100, Q1)
        s.segment(2100, 2300, "", final_ms=200)    # a cough before the first audio
        o, log = run(s)
        self.assertEqual(len(o.c.brain.calls), 1)
        self.assertEqual(events(log, "playback_paused"), [])  # a hold is no barge-in


class TestRewindAndDuck(unittest.TestCase):
    def test_long_pause_rewinds_to_a_word_start(self):
        s = Script()
        s.replies = {"": "One two three four five six seven eight."}
        s.segment(100, 1100, Q1)                   # the piece plays from about 2350 (8 words, 2.4 s)
        s.detector(3500, "barge_in", "open")       # pause about 1.15 s in
        s.segment(3540, 3700, "", final_ms=1200)   # absorbs the blip; '' at about 5500: 2 s pause
        o, log = run(s)
        play = [e for e in events(log, "x_floor") if e.extra.get("act") == "play"]
        self.assertTrue(play)
        h = heard(o)
        piece = speech("One two three four five six seven eight.")
        played = h[h != 0].astype(np.int64)
        d = np.diff(played)
        jumps = np.nonzero((d != 1) & (d != -19999))[0]       # the ramp wraps every 20000 samples
        self.assertEqual(len(jumps), 1)                        # played once more from one point
        pause_at = int(jumps[0]) + 1                           # samples of the piece before the pause
        back = int((played[jumps[0]] - played[jumps[0] + 1] + 1) % 20000)
        restart = pause_at - back
        word = len(piece) // 8
        self.assertEqual(restart % word, 0)                    # a word start
        self.assertGreater(back, 0)
        self.assertLessEqual(back, 500 * 24 + word)            # 500 ms, moved back to the word start
        self.assertEqual(len(played), len(piece) + back)       # nothing else lost or repeated

    def test_duck_sets_the_sink_gain_while_ducked(self):
        s = Script(brain_ms=300)
        s.replies = {"": "One two three four five six seven eight."}
        s.segment(100, 1100, Q1)
        s.detector(2700, "duck", "onset")
        s.detector(3100, "resume", "ended")
        o, log = make(s)
        gains = []

        async def go():
            await o.start()

            async def watch():
                while True:
                    gains.append((o.clock.ms, o.sink.gain_db))
                    await asyncio.sleep(0)
            w = asyncio.ensure_future(watch())
            await o.run()
            w.cancel()
            await o.close()
        asyncio.run(go())
        self.assertIn(-12.0, [g for t, g in gains if 2700 <= t < 3100])
        self.assertEqual([g for t, g in gains if t > 3200][-1], 0.0)


class TestStaleCallbacks(unittest.TestCase):
    def test_a_stopped_reply_never_reaches_the_sink(self):
        s = Script(brain_gap_ms=1500)              # the old reply keeps producing after its stop
        s.replies = {Q1: "One. Two. Three. Four.", "": "Okay sure."}
        s.segment(100, 1100, Q1)
        s.segment(2600, 3200, "stop show the errors", interims=[(3000, "stop show")])
        o, log = run(s)
        stopped = [x for x in events(log, "x_floor") if x.extra.get("act") == "end"
                   and x.extra.get("reason") == "interrupt"]
        t_stop = stopped[0].t_ms
        late = [tag for a, b, tag in o.sink.tags if a * 1000 / 24000 > t_stop and tag[0] == "r1"]
        self.assertEqual(late, [])


class TestKeepaliveAndAnnounce(unittest.TestCase):
    def test_slow_brain_gets_a_keepalive_line(self):
        s = Script(brain_ms=7000)
        s.segment(100, 1100, Q1)
        o, log = run(s)
        self.assertEqual(len(events(log, "x_keepalive")), 1)
        self.assertEqual(len(events(log, "first_reply_audio")), 1)   # a keepalive is no first audio

    def test_announcement_is_told_when_the_floor_is_free(self):
        s = Script(end_ms=6000)
        o, log = make(s)

        async def go():
            await o.start()

            async def later():
                while o.clock.ms < 1000:
                    await asyncio.sleep(0)
                o.announce("Task 1 finished: tests pass.")
            asyncio.ensure_future(later())
            await o.run()
            await o.close()
        asyncio.run(go())
        self.assertEqual([c[0] for c in o.c.brain.calls], ["Task 1 finished: tests pass."])
        self.assertEqual(len(events(log, "x_announce")), 1)

    def _announce_at(self, s, at, prompt):
        o, log = make(s)

        async def go():
            await o.start()

            async def later():
                while o.clock.ms < at:
                    await asyncio.sleep(0)
                o.announce(prompt)
            asyncio.ensure_future(later())
            await o.run()
            await o.close()
        asyncio.run(go())
        return o, log

    def test_announcement_cut_by_words_rides_on_the_user_reply_once(self):
        from live.floor_adapter import ANNOUNCE_CARRY_NOTE
        s = Script(brain_ms=1500)
        s.segment(2000, 2600, "hi there", interims=[(2500, "hi")])   # before the announcement's audio
        o, log = self._announce_at(s, 100, "[Task 5 finished.]")
        prompts = [c[0] for c in o.c.brain.calls]
        self.assertEqual(len(prompts), 2)
        self.assertTrue(prompts[1].startswith(ANNOUNCE_CARRY_NOTE))
        self.assertIn("[Task 5 finished.]", prompts[1])
        self.assertTrue(prompts[1].endswith("hi there"))
        heard = [e for e in events(log, "x_floor") if e.extra.get("act") == "announce_heard"]
        self.assertEqual(len(heard), 1)

    def test_announcement_that_never_plays_is_retried_then_dropped(self):
        from live.floor_adapter import ANNOUNCE_RETRY_NOTE
        s = Script(end_ms=8000)
        s.errors = {"Task 5"}
        o, log = self._announce_at(s, 100, "[Task 5 finished.]")
        prompts = [c[0] for c in o.c.brain.calls]
        self.assertEqual(len(prompts), 3)
        self.assertEqual(prompts[0], "[Task 5 finished.]")
        self.assertTrue(all(p.startswith(ANNOUNCE_RETRY_NOTE) for p in prompts[1:]))
        self.assertEqual(len(events(log, "x_announce_dropped")), 1)

    def test_brain_error_ends_the_reply(self):
        s = Script()
        s.errors = {Q1}
        s.segment(100, 1100, Q1)
        o, log = run(s)
        self.assertEqual(len(events(log, "x_brain_error")), 1)
        self.assertIsNone(o.floor.reply)


if __name__ == "__main__":
    unittest.main()
