"""Orchestrator wiring tests with fake components (no network, no models)."""
import asyncio
import os
import sys
import unittest

import numpy as np

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PLUGIN)

from live.audio_io import Sink  # noqa: E402
from live.events import FRAME, SR, Clock, EventLog  # noqa: E402
from live.orchestrator import KEEPALIVE, Components, Orchestrator  # noqa: E402
from live.turntaking import TurnTaking  # noqa: E402

TONE = (np.sin(np.arange(SR // 10) / 5) * 8000).astype(np.int16)


class FakeAudio:
    def __init__(self, log):
        self.clock = log.clock
        self.sink = Sink(self.clock, log)
        self.done = False

    def turn_hint(self):
        return None


class FakeSTT:
    def __init__(self, text):
        self.text = text
        self.on_interim = None

    def feed(self, frame):
        pass

    def begin(self):
        pass

    async def finalize(self):
        return self.text

    async def start(self):
        pass

    async def close(self):
        pass


class FakeBrain:
    def __init__(self, reply="Canberra.", delay=0.2):
        self.reply, self.delay, self.cancelled, self.calls = reply, delay, 0, []

    async def start(self):
        pass

    async def respond(self, text, interrupted=None):
        self.calls.append((text, interrupted))
        await asyncio.sleep(self.delay)
        yield self.reply

    async def cancel(self):
        self.cancelled += 1

    async def close(self):
        pass


class FakeTTS:
    def __init__(self, delay=0.05):
        self.delay, self.calls = delay, []

    async def start(self):
        pass

    async def close(self):
        pass

    async def synthesize(self, text):
        self.calls.append(text)
        await asyncio.sleep(self.delay)
        yield TONE


class Nop:
    def feed(self, *a):
        return [] if len(a) == 2 else None

    def on_transcript(self, *a):
        pass


def make(text="Why is the sky blue?", brain_delay=0.2, tts_delay=0.05):
    log = EventLog(Clock())
    audio = FakeAudio(log)
    c = Components(endpointer=Nop(), stt=FakeSTT(text), brain=FakeBrain(delay=brain_delay),
                   tts=FakeTTS(tts_delay), bargein=Nop(), turntaking=TurnTaking({"fillers": True}))
    return Orchestrator(audio, log, c, {}), log


class TestFillerPrediction(unittest.TestCase):
    def test_predicted_filler_plays_without_synthesis_wait(self):
        async def go():
            o, log = make()
            o._on_speech_start()
            o.c.stt.on_interim("Why is the")
            self.assertIsNotNone(o.prediction)
            await asyncio.sleep(0.1)                       # prefetch done while the user talks
            o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            first = [e for e in log.events if e.event == "first_reply_audio"]
            self.assertEqual(len(first), 1)
            self.assertTrue(first[0].extra["filler"])
            self.assertEqual(o.stats[-1]["filler_source"], "predicted")
            self.assertIn(o.stats[-1]["filler"], o.c.turntaking.used)
            await o.reply_task
        asyncio.run(go())

    def test_unpredicted_filler_is_split(self):
        async def go():
            o, log = make()
            o._on_speech_start()
            o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            await o.reply_task
            f = o.replies[-1].fillers[0]
            head = f.split(",")[0] + ","
            self.assertEqual(o.c.tts.calls[0], head)
        asyncio.run(go())


class FakeEndpointer(Nop):
    def __init__(self, tail):
        self.trace = [{"pause_start_ms": 0.0, "text": tail, "class": "yield"}]


class SlowSTT(FakeSTT):
    async def finalize(self):
        await asyncio.sleep(0.3)
        return self.text


class TestEarlyFiller(unittest.TestCase):
    def test_filler_plays_at_turn_end_before_final_text(self):
        async def go():
            o, log = make()
            o.c.stt = SlowSTT("Why is the sky blue?")
            o.c.endpointer = FakeEndpointer("Why is the sky blue?")
            o._on_speech_start()
            o._on_interim("Why is the")
            await asyncio.sleep(0.1)
            o.in_user_turn = False
            o._early_filler(o.label)
            first = [e for e in log.events if e.event == "first_reply_audio"]
            self.assertEqual(len(first), 1)                 # before the final transcript
            self.assertTrue(o._reply_in_flight())
            await o._handle_turn(o.label, o.gen)
            r = o.replies[-1]
            self.assertEqual(len(r.fillers), 1)
            self.assertEqual(o.stats[-1]["filler_source"], "early")
            await o.reply_task
            self.assertTrue(o.c.brain.calls[0][0].startswith("[Already spoken aloud"))
            self.assertEqual(o.c.tts.calls.count(r.fillers[0]), 1)   # synthesized once, ahead
        asyncio.run(go())

    def test_unpredicted_turn_starts_split_filler_before_final_text(self):
        async def go():
            o, log = make(brain_delay=0.05)
            o.c.stt = SlowSTT("Huh, and why is that?")
            o.c.endpointer = FakeEndpointer("Huh, and why is that?")
            o._on_speech_start()
            o.in_user_turn = False
            o._early_filler(o.label)
            self.assertIsNotNone(o.early)
            self.assertEqual(o.early["source"], "early_split")
            await asyncio.sleep(0.07)                        # head synthesized, final text not back yet
            self.assertEqual(len([e for e in log.events if e.event == "first_reply_audio"]), 1)
            await o._handle_turn(o.label, o.gen)
            await o.reply_task
            r = o.replies[-1]
            idx = [w[0] for w in r.writes]
            self.assertEqual(idx[:2], [0, 0])                # both filler parts before the content
            self.assertTrue(all(i >= 1 for i in idx[2:]))
            starts = [w[1] for w in r.writes]
            self.assertEqual(starts, sorted(starts))
        asyncio.run(go())

    def test_barge_in_on_early_filler_keeps_the_turn(self):
        async def go():
            o, log = make()
            o.c.stt = SlowSTT("Why is the sky blue?")
            o.c.endpointer = FakeEndpointer("Why is the sky blue?")
            o._on_speech_start()
            o._on_interim("Why is the")
            await asyncio.sleep(0.1)
            o._early_filler(o.label)
            o._barge_in()
            self.assertFalse(o.sink.is_playing())
            await o._handle_turn(o.label, o.gen)
            self.assertEqual(o.carry, "Why is the sky blue?")
            self.assertIsNone(o.reply)
        asyncio.run(go())


class WarmTTS(FakeTTS):
    def __init__(self, delay=0.05):
        super().__init__(delay)
        self.warmed = 0

    async def warm(self):
        self.warmed += 1


class TurnEndEndpointer(Nop):
    """speech_start on the first frame, turn_end on the second."""
    def __init__(self):
        self.n = 0
        self.trace = []

    def feed(self, frame, t_ms):
        from live.endpointing import Decision
        self.n += 1
        return [Decision("speech_start", t_ms)] if self.n == 1 else \
            [Decision("turn_end", t_ms, "semantic_yield")] if self.n == 2 else []


class FrameAudio(FakeAudio):
    async def frames(self):
        for _ in range(3):
            self.clock.advance(FRAME)
            yield np.zeros(FRAME, np.int16)
            await asyncio.sleep(0)
        self.done = True


class TestWarmTTS(unittest.TestCase):
    def _run(self, fillers):
        async def go():
            log = EventLog(Clock())
            audio = FrameAudio(log)
            c = Components(endpointer=TurnEndEndpointer(), stt=FakeSTT("What's going on?"), brain=FakeBrain(delay=0.05),
                           tts=WarmTTS(0.0), bargein=Nop(), turntaking=TurnTaking({"fillers": fillers}))
            c.endpointer.trace = [{"pause_start_ms": 0.0, "text": "What's going on?", "class": "yield"}]
            o = Orchestrator(audio, log, c, {})
            await o.run()
            await asyncio.sleep(0.2)
            return c.tts.warmed
        return asyncio.run(go())

    def test_turn_end_warms_the_model(self):
        # 2026-10-30: after 10-20 s idle Kokoro took ~505 ms for the first piece, ~325 ms warmed
        self.assertEqual(self._run(fillers=False), 1)

    def test_no_warm_up_in_front_of_a_filler(self):
        self.assertEqual(self._run(fillers=True), 0)


class TestKeepalive(unittest.TestCase):
    def test_long_silence_in_reply_gets_a_holding_line(self):
        async def go():
            log = EventLog(Clock())
            audio = FakeAudio(log)
            c = Components(endpointer=Nop(), stt=FakeSTT("Run the tests."), brain=FakeBrain("All passed.", delay=1.0),
                           tts=FakeTTS(0.01), bargein=Nop(), turntaking=TurnTaking({"fillers": False}))
            o = Orchestrator(audio, log, c, {"orchestrator": {"keepalive_after_ms": 300, "keepalive_every_ms": 10000}})

            async def tick():
                while True:
                    await asyncio.sleep(0.02)
                    audio.clock.advance(FRAME)
            t = asyncio.ensure_future(tick())
            o._on_speech_start()
            await o._handle_turn(o.label, o.gen)
            await o.reply_task
            t.cancel()
            said = [e.extra["text"] for e in log.events if e.event == "x_keepalive"]
            self.assertEqual(len(said), 1)
            self.assertIn(said[0], o.replies[-1].sentences)
        asyncio.run(go())


class TestBargeInDuringFiller(unittest.TestCase):
    def test_filler_only_reply_merges_with_resumed_speech(self):
        async def go():
            o, log = make(text="Tell me about Rome.", brain_delay=5.0)
            o._on_speech_start()
            o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            await asyncio.sleep(0.2)
            r = o.reply
            self.assertTrue(r.audio_started)
            self.assertFalse(r.content_started)
            o._barge_in()
            self.assertEqual(o.carry, "Tell me about Rome.")
            self.assertIsNone(o.pending_interrupt)
            self.assertFalse(o.sink.is_playing())
        asyncio.run(go())

    def test_content_reply_barge_in_is_an_interruption(self):
        async def go():
            o, log = make(text="Tell me about Rome.", brain_delay=0.01)
            o._on_speech_start()
            o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            await asyncio.sleep(0.3)
            self.assertTrue(o.reply.content_started)
            o._barge_in()
            self.assertEqual(o.carry, "")
            self.assertIsNotNone(o.pending_interrupt)
        asyncio.run(go())


class SeqSTT(FakeSTT):
    """Finals in order: the user's turn, then each resumed segment."""

    def __init__(self, texts):
        super().__init__("")
        self.texts = list(texts)

    async def finalize(self):
        return self.texts.pop(0)


def make_resume(texts, brain_delay=0.3, hold_max_ms=1500.0):
    log = EventLog(Clock())
    audio = FakeAudio(log)
    c = Components(endpointer=Nop(), stt=SeqSTT(texts), brain=FakeBrain("The yes is for the fix.", delay=brain_delay),
                   tts=FakeTTS(0.01), bargein=Nop(), turntaking=TurnTaking({"fillers": False}))
    return Orchestrator(audio, log, c, {"orchestrator": {"keepalive_after_ms": 0, "hold_max_ms": hold_max_ms}}), log


class TestResumedNoise(unittest.TestCase):
    """Bar session 20261030-110521, turn 3: "What do you need my yes for?" was answered, then quiet
    noise (no words, final "") started a segment before any reply audio. The reply was cancelled
    and sent again with the same text, three times (13.4 s to first sound)."""

    def test_wordless_sound_does_not_restart_the_reply(self):
        async def go():
            o, log = make_resume(["What do you need my yes for?", "", ""])
            o._on_speech_start()
            o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            r = o.reply
            for _ in range(2):                       # two noise segments before any reply audio
                o._on_speech_start()
                self.assertIs(o.reply, r)            # held, not cancelled
                await asyncio.sleep(0.1)
                self.assertFalse(r.audio_started)    # nothing plays while the sound may be words
                o.in_user_turn = False
                await o._handle_turn(o.label, o.gen)
            await o.reply_task
            self.assertEqual(len(o.c.brain.calls), 1)
            self.assertEqual(o.c.brain.cancelled, 0)
            self.assertTrue(r.content_started)
            self.assertFalse([e for e in log.events if e.event == "x_reply_text" and e.extra.get("interrupted")])
            self.assertEqual(o.user_turns[r.turn], "What do you need my yes for?")
        asyncio.run(go())

    def test_resumed_words_merge_into_one_turn(self):
        async def go():
            o, log = make_resume(["What are you doing?", "Hello?"])
            o._on_speech_start()
            o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            first = o.reply
            await asyncio.sleep(0.05)                # the brain is working on the first part
            o._on_speech_start()
            o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            self.assertIsNot(o.reply, first)
            self.assertNotIn(first, o.replies)
            self.assertEqual(o.reply.user_text, "What are you doing? Hello?")
            await o.reply_task
            self.assertEqual([t for t, _ in o.c.brain.calls], ["What are you doing?", "What are you doing? Hello?"])
            self.assertTrue(o.reply.content_started)
        asyncio.run(go())

    def test_sound_during_final_transcript_holds_the_reply(self):
        async def go():
            o, log = make_resume(["What are you doing?", ""])
            o._on_speech_start()
            o.in_user_turn = False
            label, gen = o.label, o.gen
            o._on_speech_start()                     # noise starts before the final is back
            await o._handle_turn(label, gen)
            r = o.reply
            self.assertIsNotNone(r)
            self.assertIsNotNone(r.held)
            o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)     # the noise had no words
            await o.reply_task
            self.assertTrue(r.content_started)
            self.assertEqual(len(o.c.brain.calls), 1)
        asyncio.run(go())


class TestHoldCap(unittest.TestCase):
    """Replay of session 20261030-110521: wordless noise after "What are you doing?" lasted 3.3 s,
    and the held reply waited for its end and final transcript: 5.4 s to first sound."""

    def test_wordless_hold_plays_after_hold_max_ms(self):
        async def go():
            o, log = make_resume(["What are you doing?", ""], brain_delay=0.05, hold_max_ms=200.0)
            o._on_speech_start()
            o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            r = o.reply
            o._on_speech_start()                     # noise goes on, its final is far away
            o._on_interim("")
            o.clock.advance(SR)                      # 1 s of mic audio (the session clock)
            await asyncio.sleep(0.6)
            self.assertTrue(r.audio_started)         # played without waiting for the final
            self.assertTrue([e for e in log.events if e.event == "x_resume" and e.extra.get("decision") == "released"])
            o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)     # the noise had no words
            await o.reply_task
            self.assertIs(o.reply, r)
            self.assertEqual(len(o.c.brain.calls), 1)
            self.assertEqual(o.user_turns[r.turn], "What are you doing?")
        asyncio.run(go())

    def test_words_in_interim_keep_the_hold(self):
        async def go():
            o, log = make_resume(["What are you doing?", "Hello?"], brain_delay=0.05, hold_max_ms=200.0)
            o._on_speech_start()
            o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            first = o.reply
            o._on_speech_start()
            o._on_interim("Hello")
            o.clock.advance(SR)                      # 1 s of mic audio (the session clock)
            await asyncio.sleep(0.6)
            self.assertFalse(first.audio_started)    # words: still held past hold_max_ms
            o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            self.assertEqual(o.reply.user_text, "What are you doing? Hello?")
            await o.reply_task
        asyncio.run(go())

    def test_words_after_release_stop_the_reply_and_merge(self):
        async def go():
            o, log = make_resume(["What are you doing?", "Hello?"], brain_delay=0.05, hold_max_ms=200.0)
            o._on_speech_start()
            o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            first = o.reply
            o._on_speech_start()
            o.clock.advance(SR)                      # 1 s of mic audio (the session clock)
            await asyncio.sleep(0.6)
            self.assertTrue(first.audio_started)
            o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)     # the sound was words after all
            self.assertTrue(first.interrupted)
            self.assertEqual(o.reply.user_text, "What are you doing? Hello?")
            await o.reply_task
        asyncio.run(go())



class SlowFirstSTT(FakeSTT):
    """The first segment's final is slow; a later short segment's final ('') comes back first."""

    def __init__(self, first, later=("",), delay=0.2):
        super().__init__("")
        self.first, self.later, self.delay, self.n = first, list(later), delay, 0

    async def finalize(self):
        self.n += 1
        if self.n == 1:
            await asyncio.sleep(self.delay)
            return self.first
        return self.later.pop(0)


def _voiced(i, on, db=-35.0, rng=np.random.default_rng(0)):
    """One frame of a 150 Hz harmonic sound (voiced noise or quiet speech) over a -65 dB floor."""
    t = (np.arange(FRAME) + i * FRAME) / SR
    n = rng.normal(0, 1, FRAME) * 10 ** (-65 / 20)
    if on:
        x = sum(np.sin(2 * np.pi * 150 * k * t) / k for k in range(1, 20))
        n = n + x / np.max(np.abs(x)) * 10 ** (db / 20)
    return (n * 32767).astype(np.int16)


class TestHoldEdges(unittest.TestCase):
    """Holds and releases of a reply over a sound that came after the user's turn (bar issue 1)."""

    def test_no_hold_on_a_segment_whose_final_already_came_back(self):
        # G1's final is slow; noise segment G2 starts and ends and its '' comes back first. The reply
        # to G1 was held on G2, whose final never comes again: silent until hold_max_ms (or forever at 0).
        async def go():
            o, log = make_resume([], hold_max_ms=0.0)
            o.c.stt = SlowFirstSTT("What are you doing?")
            o._on_speech_start()
            o.in_user_turn = False
            t1 = asyncio.create_task(o._handle_turn(o.label, o.gen))
            await asyncio.sleep(0.02)
            o._on_speech_start()
            o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)            # G2's final '' first
            await t1                                        # then G1's
            self.assertIsNotNone(o.reply)
            self.assertIsNone(o.reply.held)
            await asyncio.wait_for(o.reply_task, 2)
            self.assertTrue(o.reply.content_started)
            self.assertEqual([t for t, _ in o.c.brain.calls], ["What are you doing?"])
        asyncio.run(go())

    def test_words_of_a_segment_that_came_back_first_join_the_turn(self):
        async def go():
            o, log = make_resume([], hold_max_ms=0.0)
            o.c.stt = SlowFirstSTT("What are you doing?", later=["Right now?"])
            o._on_speech_start()
            o.in_user_turn = False
            t1 = asyncio.create_task(o._handle_turn(o.label, o.gen))
            await asyncio.sleep(0.02)
            o._on_speech_start()
            o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            await t1
            await asyncio.wait_for(o.reply_task, 2)
            self.assertIsNone(o.reply.held)
            self.assertEqual(o.c.brain.calls[-1][0], "What are you doing? Right now?")
            self.assertEqual(len([r for r in o.replies if r.content_started]), 1)
        asyncio.run(go())

    def _released(self, finals, cap=200.0):
        """The reply to the first final is held on a sound, then let go by hold_max_ms and plays."""
        async def setup():
            o, log = make_resume(finals, brain_delay=0.05, hold_max_ms=cap)
            o._on_speech_start()
            o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            r = o.reply
            o._on_speech_start()
            o._on_interim("")
            self.assertIsNotNone(r.held)
            o.clock.advance(SR)                              # the hold runs out over the sound
            while not r.content_started:
                await asyncio.sleep(0.01)
            return o, log, r
        return setup

    def test_released_reply_cut_by_its_own_sound_is_answered_after_an_empty_final(self):
        # The barge-in rules stopped the released reply on the same sound; its final was '' and the
        # question was never answered.
        async def go():
            o, log, r = await self._released(["What are you doing?", ""])()
            o._barge_in()
            self.assertIsNone(o.reply)
            o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            self.assertIsNotNone(o.reply)
            await asyncio.wait_for(o.reply_task, 2)
            calls = o.c.brain.calls
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1][0], "What are you doing?")
            self.assertIsNotNone(calls[1][1])                # told it was interrupted
            self.assertTrue(o.reply.content_started)
        asyncio.run(go())

    def test_released_reply_cut_by_words_merges_them_with_the_question(self):
        async def go():
            o, log, r = await self._released(["What are you doing?", "And why?"])()
            o._barge_in()
            o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            await asyncio.wait_for(o.reply_task, 2)
            self.assertEqual(o.c.brain.calls[1][0], "What are you doing? And why?")
        asyncio.run(go())

    def test_released_reply_is_not_barged_in_on_at_once_by_the_sound_it_plays_over(self):
        # Real BargeIn: the episode of the held sound carried over to the reply's first frame and the
        # "long" rule fired 20 ms after the reply audio began.
        from live.bargein import BargeIn

        async def go():
            o, log = make_resume(["What are you doing?", ""], brain_delay=0.05, hold_max_ms=1500.0)
            o.c.bargein = BargeIn(None)
            o.c.brain.reply = "First sentence here. Second sentence follows now. Third one."
            o._on_speech_start()
            o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            r = o.reply
            o._on_speech_start()
            o._on_interim("")
            t, fired, first_audio = 0.0, None, None
            for i in range(1, 200):                          # up to 4 s: 300 ms on, 100 ms off
                on = (t % 400) < 300
                t += FRAME * 1000.0 / SR
                o.clock.advance(FRAME)
                holds = o._assistant_holds_floor()
                if holds and first_audio is None:
                    first_audio = t
                if o.c.bargein.feed(_voiced(i, on), t, holds) == "barge_in":
                    fired = t
                    o._barge_in()
                    break
                await asyncio.sleep(0.005)
            self.assertIsNotNone(first_audio)
            if fired is not None:
                self.assertGreaterEqual(fired - first_audio, 500.0)
            o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)             # the sound's final: ''
            if o.reply_task is not None:
                await asyncio.wait_for(o.reply_task, 3)
            answered = (fired is None and r.content_started and not r.interrupted) or len(o.c.brain.calls) == 2
            self.assertTrue(answered, "the question was left unanswered")
        asyncio.run(go())

    def test_backchannel_after_a_released_hold_keeps_the_reply(self):
        # "Okay." in the same segment stopped the reply, marked all of it interrupted and asked again.
        async def go():
            o, log, r = await self._released(["What is the capital of Australia?", "Okay."])()
            await o.reply_task
            o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            return o, r
        o, r = asyncio.run(go())
        self.assertEqual(len(o.c.brain.calls), 1)
        self.assertFalse(r.interrupted)

    def test_backchannel_while_the_released_reply_plays_keeps_it(self):
        async def go():
            o, log, r = await self._released(["What is the capital of Australia?", "Mm-hmm."])()
            o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            self.assertIs(o.reply, r)
            await o.reply_task
            return o, r
        o, r = asyncio.run(go())
        self.assertEqual(len(o.c.brain.calls), 1)
        self.assertFalse(r.interrupted)

    def test_question_after_the_released_reply_played_is_a_new_turn(self):
        async def go():
            o, log, r = await self._released(["What is the capital of Australia?", "And of Canada?"])()
            await o.reply_task
            o.clock.advance(5 * SR)                          # played to the end
            o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            await o.reply_task
            return o, r
        o, r = asyncio.run(go())
        self.assertEqual([t for t, _ in o.c.brain.calls], ["What is the capital of Australia?", "And of Canada?"])
        self.assertIsNone(o.c.brain.calls[1][1])
        self.assertFalse(r.interrupted)
        self.assertEqual(o.user_turns[r.turn], "What is the capital of Australia?")


class DelaySTT(FakeSTT):
    """Finals as (text, delay_s): each finalize() returns its text after its own delay."""

    def __init__(self, items):
        super().__init__("")
        self.items = list(items)

    async def finalize(self):
        text, d = self.items.pop(0)
        await asyncio.sleep(d)
        return text


class TestHoldRaces(unittest.TestCase):
    """Second sweep of the hold code (after session 20261030-110521)."""

    def test_out_of_order_empty_final_does_not_drop_the_question(self):
        # G1 question; G2 words with a slow final; G3 a short noise whose '' comes back first.
        async def go():
            o, log = make_resume([], brain_delay=0.3, hold_max_ms=0.0)
            o.c.stt = DelaySTT([("What is in the logs?", 0), ("and the errors too", 0.2), ("", 0)])
            o._on_speech_start(); o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            r1 = o.reply
            o._on_speech_start(); o.in_user_turn = False
            t2 = asyncio.ensure_future(o._handle_turn(o.label, o.gen))
            await asyncio.sleep(0.02)
            o._on_speech_start(); o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            self.assertIsNotNone(r1.held)            # G2's final is still on its way
            await t2
            await asyncio.wait_for(o.reply_task, 3)
            self.assertEqual(o.user_turns, {r1.turn: "What is in the logs? and the errors too"})
            self.assertEqual(o.c.brain.calls[-1][0], "What is in the logs? and the errors too")
        asyncio.run(go())

    def test_out_of_order_finals_without_words_play_the_held_reply(self):
        async def go():
            o, log = make_resume([], brain_delay=0.05, hold_max_ms=0.0)
            o.c.stt = DelaySTT([("What is in the logs?", 0), ("", 0.2), ("", 0)])
            o._on_speech_start(); o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            r1 = o.reply
            o._on_speech_start(); o.in_user_turn = False
            t2 = asyncio.ensure_future(o._handle_turn(o.label, o.gen))
            await asyncio.sleep(0.02)
            o._on_speech_start(); o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            self.assertIsNotNone(r1.held)
            await t2
            self.assertIsNone(r1.held)               # both segments had no words: it plays
            await asyncio.wait_for(o.reply_task, 3)
            self.assertTrue(r1.content_started)
            self.assertEqual(len(o.c.brain.calls), 1)
        asyncio.run(go())

    def test_hum_final_keeps_the_held_reply(self):
        for hum in ["Hmm.", "Mm.", "Mmm.", "Mhm.", "Ah.", "Oh.", "Um."]:
            with self.subTest(hum=hum):
                async def go():
                    o, log = make_resume(["What are you doing?", hum], brain_delay=0.05, hold_max_ms=200.0)
                    o._on_speech_start(); o.in_user_turn = False
                    await o._handle_turn(o.label, o.gen)
                    r = o.reply
                    o._on_speech_start()
                    o._on_interim(hum.rstrip("."))
                    o.clock.advance(SR)
                    await asyncio.sleep(0.6)
                    self.assertTrue(r.audio_started)  # a hum is no words: released at the cap
                    o.in_user_turn = False
                    await o._handle_turn(o.label, o.gen)
                    await o.reply_task
                    self.assertEqual(len(o.c.brain.calls), 1)
                    self.assertEqual(o.c.brain.cancelled, 0)
                asyncio.run(go())

    def test_announcement_waits_for_a_pending_final(self):
        # A released reply is cut by a barge-in on its own segment; that segment's final ('') is slow.
        # The announcement must not take the conversation (and the interrupt note) in that gap.
        async def go():
            o, log = make_resume([], brain_delay=0.05, hold_max_ms=200.0)
            o.c.stt = DelaySTT([("What are you doing?", 0), ("", 1.2)])
            o._on_speech_start(); o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            r = o.reply
            o._on_speech_start(); o._on_interim("")
            o.clock.advance(SR)
            while not r.content_started:
                await asyncio.sleep(0.01)
            o._barge_in()
            self.assertIs(o.cut_released, r)
            a = o.announce("[Task 5 finished.]")
            o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            await asyncio.sleep(0.1)
            a.cancel()
            calls = o.c.brain.calls
            self.assertEqual(calls[1][0], "What are you doing?")      # re-asked first, with the cut
            self.assertIsNotNone(calls[1][1])
            self.assertIn("cut_reasked", [e.extra.get("decision") for e in log.events if e.event == "x_resume"])
            if o.reply_task:
                o.reply_task.cancel()
        asyncio.run(go())

    def test_keepalive_made_before_a_hold_waits_for_it(self):
        class SignalTTS(FakeTTS):
            def __init__(self, delay):
                super().__init__(delay)
                self.started = asyncio.Event()

            async def synthesize(self, text):
                if text in KEEPALIVE:
                    self.started.set()
                async for p in super().synthesize(text):
                    yield p

        async def go():
            log = EventLog(Clock())
            audio = FakeAudio(log)
            tts = SignalTTS(0.3)
            c = Components(endpointer=Nop(), stt=FakeSTT("Run the tests."), brain=FakeBrain("All passed.", delay=3.0),
                           tts=tts, bargein=Nop(), turntaking=TurnTaking({"fillers": False}))
            o = Orchestrator(audio, log, c, {"orchestrator": {"keepalive_after_ms": 100, "keepalive_every_ms": 10000}})

            async def tick():
                while True:
                    await asyncio.sleep(0.02)
                    audio.clock.advance(FRAME)
            t = asyncio.ensure_future(tick())
            o._on_speech_start(); o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            r = o.reply
            await asyncio.wait_for(tts.started.wait(), 2)
            o._on_speech_start()                     # the user goes on while the line is made
            self.assertIsNotNone(r.held)
            await asyncio.sleep(0.4)
            self.assertFalse(r.audio_started)
            self.assertFalse(o.sink.is_playing())
            self.assertFalse(o._assistant_holds_floor())
            t.cancel()
            o.reply_task.cancel()
        asyncio.run(go())

    def test_own_final_does_not_settle_a_later_click(self):
        # The early filler plays; a 20 ms click opens a barge-in episode; the question's final lands.
        from live.bargein import BargeIn
        rng = np.random.default_rng(0)

        def quiet():
            return (rng.standard_normal(FRAME) * 10).astype(np.int16)

        def click():
            t = np.arange(FRAME) / SR
            return (np.sin(2 * np.pi * 150 * t) * 32768 * 10 ** (-30 / 20) * 1.414).astype(np.int16)

        async def go():
            o, log = make(brain_delay=1.5)
            o.c.bargein = bi = BargeIn()
            o.c.stt = SlowSTT("What is going on with task two?")
            o.c.endpointer = FakeEndpointer("What is going on with task two?")
            for _ in range(100):                     # the noise floor, before the turn ends
                bi.feed(quiet(), o.clock.ms, o._assistant_holds_floor())
                o.clock.advance(FRAME)
            o._on_speech_start(); o._on_interim("What is going on")
            await asyncio.sleep(0.1)
            o.in_user_turn = False
            o._early_filler(o.label)
            asyncio.ensure_future(o._handle_turn(o.label, o.gen))
            await asyncio.sleep(0)
            for i in range(40):
                o.clock.advance(FRAME)
                if bi.feed(click() if i == 3 else quiet(), o.clock.ms, o._assistant_holds_floor()) == "barge_in":
                    o._barge_in()
                await asyncio.sleep(0.02)
            self.assertFalse([e for e in log.events if e.event == "barge_in_detected"], bi.last)
            self.assertIsNotNone(o.reply)
            self.assertEqual(o.c.brain.cancelled, 0)
            o.reply_task.cancel()
        asyncio.run(go())



class TestRestartedReplyFirstAudio(unittest.TestCase):
    """bin/live-review scores each turn_end_detected by the first first_reply_audio with the same
    turn at or after it. A reply restarted after a released hold must log one, or its wait drops
    out of the latency figures (bar issues 1 and 6)."""

    def _run(self, finals, cut):
        async def go():
            o, log = make_resume(finals, brain_delay=0.05, hold_max_ms=200.0)
            o._on_speech_start(); o.in_user_turn = False
            log.emit("turn_end_detected", o.label)
            await o._handle_turn(o.label, o.gen)
            r = o.reply
            o._on_speech_start(); o._on_interim("")
            o.clock.advance(SR)
            while not r.content_started:
                await asyncio.sleep(0.01)
            if cut:
                o.clock.advance(SR // 2)
                o._barge_in()
            o.clock.advance(SR)
            o.in_user_turn = False
            log.emit("turn_end_detected", o.label)
            end = log.events[-1]
            await o._handle_turn(o.label, o.gen)
            await asyncio.wait_for(o.reply_task, 3)
            o.clock.advance(5 * SR)
            # the turn end, or the same turn end logged again under the label the reply got
            ends = [e for e in log.events if e.event == "turn_end_detected" and e.t_ms == end.t_ms]
            firsts = [e for e in log.events if e.event == "first_reply_audio"
                      and any(e.turn == x.turn and e.t_ms >= x.t_ms - 50 for x in ends)]
            self.assertTrue(firsts, "no first_reply_audio for the second turn end's label")
        asyncio.run(go())

    def test_cut_reasked_reply_logs_first_audio(self):
        self._run(["What are you doing?", ""], cut=True)

    def test_merged_reply_logs_first_audio(self):
        self._run(["What are you doing?", "And why is that?"], cut=False)


class TestSweep3(unittest.TestCase):
    """Third sweep of the hold, cut and barge-in code (bar session 20261030-110521, issues 1 and 6)."""

    def test_late_empty_final_of_an_older_sound_keeps_the_newer_reply(self):
        # G1 noise with a slow '' final; G2 the question, answered first. G1's '' cancelled that
        # reply and asked the same question again.
        async def go():
            o, log = make_resume([], brain_delay=0.3, hold_max_ms=0.0)
            o.c.stt = DelaySTT([("", 0.3), ("What is in the logs?", 0)])
            o._on_speech_start(); o.in_user_turn = False
            t1 = asyncio.ensure_future(o._handle_turn(o.label, o.gen))
            await asyncio.sleep(0.02)
            o._on_speech_start(); o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            r2 = o.reply
            await t1
            self.assertIs(o.reply, r2)
            await asyncio.wait_for(o.reply_task, 3)
            self.assertEqual([t for t, _ in o.c.brain.calls], ["What is in the logs?"])
            self.assertEqual(o.c.brain.cancelled, 0)
            self.assertTrue(r2.content_started)
        asyncio.run(go())

    def test_new_segment_restarts_the_hold_cap(self):
        # Held at noise G2; G3 (words) starts 1.4 s later: the cap counted from G2 let the reply
        # play 0.1 s into G3, before its first interim.
        async def go():
            o, log = make_resume([], brain_delay=0.05, hold_max_ms=1500.0)
            o.c.stt = DelaySTT([("What is in the logs?", 0)])
            o._on_speech_start(); o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            r = o.reply
            o._on_speech_start()
            self.assertIsNotNone(r.held)
            o.clock.advance(int(SR * 1.4))
            o.in_user_turn = False
            o._on_speech_start()
            o.clock.advance(int(SR * 0.2))
            await asyncio.sleep(0.5)
            self.assertFalse(r.audio_started)
            o.reply_task.cancel()
        asyncio.run(go())

    def test_words_of_an_earlier_sound_do_not_lift_the_cap_on_a_new_one(self):
        async def go():
            o, log = make_resume([], brain_delay=0.05, hold_max_ms=200.0)
            o.c.stt = DelaySTT([("What is in the logs?", 0)])
            o._on_speech_start(); o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            r = o.reply
            o._on_speech_start(); o._on_interim("Hello")
            self.assertTrue(r.held_words)
            o.in_user_turn = False
            o._on_speech_start(); o._on_interim("")
            self.assertFalse(r.held_words)
            o.clock.advance(SR)
            await asyncio.sleep(0.6)
            self.assertTrue(r.audio_started)
            o.reply_task.cancel()
        asyncio.run(go())

    def test_words_heard_before_a_hold_count_for_it(self):
        # The hold starts in _handle_turn (the sound began while the final was on its way): the
        # sound's interim with words came before the hold.
        async def go():
            o, log = make_resume([], brain_delay=0.05, hold_max_ms=200.0)
            o.c.stt = DelaySTT([("What is in the logs?", 0.1)])
            o._on_speech_start(); o.in_user_turn = False
            t1 = asyncio.ensure_future(o._handle_turn(o.label, o.gen))
            await asyncio.sleep(0.01)
            o._on_speech_start(); o._on_interim("and the errors")
            await t1
            r = o.reply
            self.assertIsNotNone(r.held)
            self.assertTrue(r.held_words)
            o.clock.advance(SR)
            await asyncio.sleep(0.4)
            self.assertFalse(r.audio_started)
            o.reply_task.cancel()
        asyncio.run(go())

    def _two_under_hold(self, g1):
        async def go():
            o, log = make_resume([], brain_delay=0.3, hold_max_ms=0.0)
            o.c.stt = DelaySTT([("What is in the logs?", 0), (g1, 0.2), ("and the errors", 0)])
            o._on_speech_start(); o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            r0 = o.reply
            o._on_speech_start(); o.in_user_turn = False
            t1 = asyncio.ensure_future(o._handle_turn(o.label, o.gen))
            await asyncio.sleep(0.02)
            self.assertIsNotNone(r0.held)
            o._on_speech_start(); o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            await t1
            await asyncio.wait_for(o.reply_task, 3)
            return o
        return asyncio.run(go())

    def test_slow_empty_final_after_merged_words_keeps_the_question(self):
        o = self._two_under_hold("")
        self.assertEqual(o.c.brain.calls[-1][0], "What is in the logs? and the errors")
        self.assertEqual(list(o.user_turns.values()), ["What is in the logs? and the errors"])

    def test_slow_words_final_after_merged_words_keeps_the_question(self):
        o = self._two_under_hold("and the warnings")
        self.assertEqual(o.c.brain.calls[-1][0], "What is in the logs? and the warnings and the errors")
        self.assertEqual(list(o.user_turns.values()), ["What is in the logs? and the warnings and the errors"])

    def _cut_with_early_filler(self, final):
        # A released reply is cut on its own sound; a "Thank you." hallucination in the probe plays
        # an early filler at that sound's end. The question was replaced by the sound's text.
        async def go():
            o, log = make_resume([], brain_delay=0.05, hold_max_ms=200.0)
            o.c.turntaking = TurnTaking({"fillers": True})
            o.c.stt = DelaySTT([("What are you doing?", 0), (final, 0.3)])
            o._on_speech_start(); o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            r = o.reply
            o._on_speech_start(); o._on_interim("")
            o.clock.advance(SR)
            while not r.content_started:
                await asyncio.sleep(0.01)
            o._barge_in()
            self.assertIs(o.cut_released, r)
            o.c.endpointer = FakeEndpointer("Thank you.")
            o.in_user_turn = False
            o._early_filler(o.label)
            self.assertIsNotNone(o.early)
            await o._handle_turn(o.label, o.gen)
            await asyncio.wait_for(o.reply_task, 3)
            self.assertIsNone(o.cut_released)
            return o, log
        return asyncio.run(go())

    def test_cut_reply_is_asked_again_after_an_early_filler(self):
        o, log = self._cut_with_early_filler("")
        self.assertTrue(o.c.brain.calls[-1][0].endswith("What are you doing?"), o.c.brain.calls[-1][0])
        self.assertIsNotNone(o.c.brain.calls[-1][1])
        self.assertIn("cut_reasked", [e.extra.get("decision") for e in log.events if e.event == "x_resume"])

    def test_cut_reply_merges_words_after_an_early_filler(self):
        o, log = self._cut_with_early_filler("Thank you.")
        self.assertTrue(o.c.brain.calls[-1][0].endswith("What are you doing? Thank you."), o.c.brain.calls[-1][0])

    def test_barge_in_after_the_final_came_back_leaves_no_cut(self):
        async def go():
            o, log = make_resume(["What are you doing?", ""], brain_delay=0.05, hold_max_ms=200.0)
            o._on_speech_start(); o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            r = o.reply
            o._on_speech_start(); o._on_interim("")
            o.clock.advance(SR)
            while not r.content_started:
                await asyncio.sleep(0.01)
            o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)        # '' kept: the reply goes on
            o._barge_in()
            self.assertIsNone(o.cut_released)
            self.assertIsNotNone(o.pending_interrupt)
        asyncio.run(go())


LONG = (np.sin(np.arange(SR) / 5) * 8000).astype(np.int16)


class LongTTS(FakeTTS):
    async def synthesize(self, text):
        self.calls.append(text)
        await asyncio.sleep(self.delay)
        yield LONG


class SeqFrames:
    def __init__(self, base, seq):
        self.base, self.seq = base, seq

    def turn_hint(self):
        return None

    @property
    def sink(self):
        return self.base.sink

    async def frames(self):
        for f in self.seq:
            self.base.clock.advance(FRAME)
            yield f
            await asyncio.sleep(0.005)


class TestShortBargeIn(unittest.TestCase):
    """A 40 ms voiced sound fires BargeIn ('open' rule) over the filler, but the endpointer needs
    60 ms for speech_start: no turn end follows, and the question kept as carry was never answered."""

    def _run(self, slow_final):
        from live.bargein import BargeIn
        from live.endpointing import Endpointer
        rng = np.random.default_rng(0)

        def quiet():
            return (rng.standard_normal(FRAME) * 10).astype(np.int16)
        t = np.arange(FRAME * 2) / SR
        v = sum(np.sin(2 * np.pi * 150 * k * t) / k ** 0.5 for k in range(1, 20))
        v = v / np.sqrt(np.mean(v ** 2)) * 32768 * 10 ** (-35 / 20)
        vowel = [v[:FRAME].astype(np.int16), v[FRAME:].astype(np.int16)]

        async def go():
            o, log = make(brain_delay=0.05)
            o.c.tts = LongTTS(0.0)
            o.c.bargein = bi = BargeIn()
            if slow_final:
                o.c.stt = SlowSTT("Why is the sky blue?")
            o.c.endpointer = FakeEndpointer("Why is the sky blue?")
            for _ in range(100):
                bi.feed(quiet(), o.clock.ms, False)
                o.clock.advance(FRAME)
            o._on_speech_start(); o._on_interim("Why is the")
            await asyncio.sleep(0.1)
            o.in_user_turn = False
            o._early_filler(o.label)
            self.assertTrue(o.sink.is_playing())
            o._spawn(o._handle_turn(o.label, o.gen))
            o.c.endpointer = ep = Endpointer({"silence_ms": 800})
            for _ in range(100):
                ep.feed(quiet(), 0.0)
            o.audio = SeqFrames(o.audio, [quiet() for _ in range(3)] + vowel + [quiet() for _ in range(150)])
            await o.run()
            await asyncio.sleep(0.3)
            self.assertTrue([e for e in log.events if e.event == "barge_in_detected"], "no barge-in fired")
            self.assertTrue(any(r.content_started for r in o.replies), f"never answered; carry={o.carry!r}")
            self.assertIn("short_bargein_reasked", [e.extra.get("decision") for e in log.events if e.event == "x_resume"])
            self.assertEqual(o.carry, "")
        asyncio.run(go())

    def test_short_barge_in_on_early_filler_slow_final(self):
        self._run(True)

    def test_short_barge_in_on_filler_only_reply_probe_path(self):
        self._run(False)


if __name__ == "__main__":
    unittest.main()
