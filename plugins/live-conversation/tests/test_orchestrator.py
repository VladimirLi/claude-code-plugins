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
from live.orchestrator import Components, Orchestrator  # noqa: E402
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
                   tts=FakeTTS(tts_delay), bargein=Nop(), turntaking=TurnTaking())
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


if __name__ == "__main__":
    unittest.main()
