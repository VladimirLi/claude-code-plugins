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


class TestTurnEnd(unittest.TestCase):
    def test_turn_end_warms_the_tts_and_logs_the_endpoint(self):
        s = Script()
        s.segment(100, 1100, Q1)
        o, log = make(s)
        o.c.tts.warmed = 0

        async def warm():
            o.c.tts.warmed += 1
        o.c.tts.warm = warm
        o.c.endpointer.trace = [{"pause_start_ms": 1100.0, "class": "yield"}]
        asyncio.run(drive(o))
        self.assertEqual(o.c.tts.warmed, 1)
        self.assertEqual(len(events(log, "x_endpoint")), 1)


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


class TestAdapterReview(unittest.TestCase):
    """The adapter critic's findings (scratchpad exp/e1-e5)."""

    def test_reply_drained_then_paused_never_leaves_the_sink_paused(self):
        # e1: the queue empties between sentences, a blip pauses, the brain then finishes
        s = Script(end_ms=8000)
        s.segment(100, 1100, Q1)
        s.detector(4000)
        o, log = make(s)

        async def respond(text, interrupted=None):
            o.c.brain.calls.append((text, interrupted))
            from adapter_fakes import wait_ms
            await wait_ms(log.clock, 300)
            yield "The logs show no errors. "
            await wait_ms(log.clock, 1800)
            yield " "
        o.c.brain.respond = respond

        async def go():
            await o.start()
            await asyncio.wait_for(o.run(), 20)
            await o.close()
        asyncio.run(go())
        self.assertFalse(o.sink.paused)
        self.assertEqual(len(events(log, "playback_paused")), len(events(log, "playback_resumed")))
        self.assertEqual(len(events(log, "x_reply_text")), 1)
        self.assertLess(log.clock.ms, 9000)

    def test_tts_failure_ends_the_reply(self):
        # e2
        s = Script()
        s.tts_errors = {"Everything"}
        s.segment(100, 1100, Q1)
        o, log = run(s)
        self.assertEqual(len(events(log, "x_tts_error")), 1)
        self.assertIsNone(o.floor.reply)
        self.assertEqual(events(log, "x_task_error"), [])
        self.assertLess(log.clock.ms, 21000)

    def test_first_reply_audio_is_the_first_heard_sample(self):
        # e3: stamped at the write start, not the end of the frame that reported it
        s = Script(tts_ms=37)                      # a write that starts mid-frame
        s.segment(100, 1100, Q1)
        o, log = run(s)
        h = heard(o)
        first = int(np.nonzero(h)[0][0]) * 1000.0 / 24000
        self.assertAlmostEqual(events(log, "first_reply_audio")[0].t_ms, first, places=3)

    def test_live_review_waits_from_the_speech_stop(self):
        # e4: x_endpoint has no label on the floor path; live-review pairs it by time
        import subprocess
        import tempfile
        s = Script()
        s.segment(100, 1100, Q1)
        o, log = run(s)
        d = tempfile.mkdtemp()
        log.write(os.path.join(d, "events.jsonl"))
        plugin = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.run([sys.executable, os.path.join(plugin, "bin", "live-review"), d],
                             capture_output=True, text=True).stdout
        row = [line for line in out.splitlines() if line.startswith("| 1 |")][0]
        first = events(log, "first_reply_audio")[0].t_ms
        self.assertIn(f"{round(first - 1100)}", row.replace(" ms", ""))   # from the stop at 1100 ms

    def test_live_review_reads_a_long_turns_late_final(self):
        # replay of 20261030-110521, 217.8 s: a 44 s turn's final came 43 s after the pause
        import subprocess
        import tempfile
        d = tempfile.mkdtemp()
        rows = [{"t_ms": 0.0, "event": "session_start", "turn": None},
                {"t_ms": 1000.0, "event": "x_bargein", "turn": None, "decision": "barge_in", "reason": "burst",
                 "onset_ms": 900.0},
                {"t_ms": 1000.0, "event": "barge_in_detected", "turn": 1},
                {"t_ms": 44000.0, "event": "final_transcript", "turn": 2,
                 "text": "but i don't think you should look through the plugin code"}]
        with open(os.path.join(d, "events.jsonl"), "w") as f:
            f.write("\n".join(json.dumps(r) for r in rows) + "\n")
        plugin = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.run([sys.executable, os.path.join(plugin, "bin", "live-review"), d],
                             capture_output=True, text=True).stdout
        self.assertIn("probably false: 0", out)
        self.assertIn("plugin code", out)

    def test_close_closes_every_component_even_if_one_raises(self):
        for flag in (True, False):
            o, log = make(Script(end_ms=200), {"orchestrator": {"floor": flag}})
            closed = []

            async def boom():
                raise RuntimeError("brain would not stop")

            async def mark(name):
                closed.append(name)
            o.c.brain.close = boom
            o.c.stt.close = lambda: mark("stt")
            o.c.tts.close = lambda: mark("tts")
            asyncio.run(o.close())
            self.assertEqual(closed, ["stt", "tts"], f"floor={flag}")
            self.assertEqual([e.extra["where"] for e in events(log, "x_task_error")], ["brain close"])

    def test_old_path_logs_task_exceptions(self):
        o, log = make(Script(), {"orchestrator": {"floor": False}})

        async def go():
            async def fails():
                raise ValueError("planted")
            t = o._spawn(fails())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return t
        asyncio.run(go())
        self.assertIn("ValueError: planted", [e.extra["error"] for e in events(log, "x_task_error")])

    def test_session_end_mid_reply_ends_it(self):
        s = Script(brain_ms=300, end_ms=2600)
        s.replies = {"": "One two three four five six seven eight nine ten."}
        s.segment(100, 1100, Q1)
        o, log = make(s)
        o.audio._busy = lambda: False              # the wall cap: frames stop mid-reply
        asyncio.run(drive(o))
        ends = [e.extra.get("reason") for e in events(log, "x_reply_text")]
        self.assertEqual(ends, ["end"])
        self.assertTrue(o.transcript()["turns"][1]["interrupted"])

    def test_reply_text_reason_and_no_audio_end_after_a_stop(self):
        s = Script()
        s.segment(100, 1100, Q1)
        s.segment(2600, 3200, "stop show the errors", interims=[(3000, "stop show")])
        o, log = run(s)
        self.assertEqual([e.extra["reason"] for e in events(log, "x_reply_text")], ["interrupt", "drained"])
        self.assertEqual(len(events(log, "reply_audio_end")), 1)      # the second reply's only

    def test_transcript_orders_turns_numerically(self):
        o, _ = make(Script())
        o.user_turns = {n: f"q{n}" for n in range(1, 12)}
        turns = [t["turn"] for t in o.transcript()["turns"]]
        self.assertEqual(turns, list(range(1, 12)))

    def test_controller_exception_is_logged(self):
        s = Script()
        s.segment(100, 1100, Q1, interims=[(800, "What is")])
        o, log = make(s)
        on = o.floor.on

        def broken(ev):
            if type(ev).__name__ == "Interim":
                raise ValueError("planted")
            return on(ev)
        o.floor.on = broken
        asyncio.run(drive(o))
        self.assertEqual(len(events(log, "x_task_error")), 1)

    def test_keepalive_queued_behind_content_is_dropped(self):
        s = Script(brain_ms=400, tts_ms=300)       # content synthesis 400-700 holds the TTS lock
        s.segment(100, 1100, Q1)
        o, log = run(s, {"orchestrator": {"keepalive_after_ms": 500}})
        self.assertEqual(events(log, "x_keepalive"), [])
        self.assertFalse([t for a, b, t in o.sink.tags if t[1].startswith("k")])


class TestEchoScore(unittest.TestCase):
    """A/B 2026-10-30 with echo: the reply's own audio opened segments the STT wrote as misheard
    fragments ("A full reload.", "something"); they paused and interrupted the reply."""

    def _run(self, s, speakers):
        o, log = make(s, {"floor": {"speakers": speakers}}, echo_gain_db=-26.0)
        asyncio.run(drive(o))
        return o, log

    def _script(self):
        s = Script()
        s.replies = {"": "One two three four five six seven eight nine ten eleven twelve."}
        s.segment(100, 1100, Q1)                   # plays about 2350-5950
        return s

    def test_echo_segment_never_interrupts(self):
        for speakers in (False, True):
            s = self._script()
            s.segment(2800, 3800, "something", interims=[(3500, "some")], voice=False)
            o, log = self._run(s, speakers)
            self.assertEqual([c[0] for c in o.c.brain.calls], [Q1], f"speakers={speakers}")
            self.assertEqual(events(log, "playback_paused"), [])
            self.assertGreater(events(log, "x_echo")[-1].extra["score"], 0.5)

    def test_real_words_over_the_echo_still_interrupt(self):
        for speakers in (False, True):
            s = self._script()
            s.segment(2800, 3800, "stop show me the errors", interims=[(3500, "stop show")])
            o, log = self._run(s, speakers)
            self.assertEqual([c[0] for c in o.c.brain.calls], [Q1, "stop show me the errors"],
                             f"speakers={speakers}")
            self.assertLess(events(log, "x_echo")[-1].extra["score"], 0.5)

    def test_tts_pieces_are_timed(self):
        s = self._script()
        o, log = self._run(s, False)
        t = events(log, "x_tts_piece")[0].extra
        self.assertGreaterEqual(t["ready_ms"], t["queued_ms"])


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
