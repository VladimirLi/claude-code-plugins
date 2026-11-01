"""Offline tests for live/stt.py (speculative, chunked STT). Run: .venv/bin/python -m unittest discover -s tests"""
import asyncio
import os
import sys
import threading
import time
import unittest

import numpy as np

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PLUGIN)

from live.events import FRAME, SR  # noqa: E402
from live.stt import BaseSTT, SpeculativeSTT, join_text  # noqa: E402

LOUD = (np.sin(np.arange(FRAME) / 5) * 8000).astype(np.int16)
SOFT = (np.sin(np.arange(FRAME) / 5) * 150).astype(np.int16)    # about -50 dBFS: below speech, above sound
QUIET = np.zeros(FRAME, np.int16)


class FakeSTT(SpeculativeSTT):
    """Text = number of loud frames in the request; optional delay and failures."""

    def __init__(self, cfg=None, delay=0.05, fail_first=0, slow_first=None):
        super().__init__({"interim_ms": 0, "fanout": 1, **(cfg or {})})
        self.delay, self.fail_left, self.slow_first = delay, fail_first, slow_first
        self.calls = []
        self.lock = threading.Lock()

    def _transcribe(self, audio, prompt=None):
        with self.lock:
            n = len(self.calls)
            self.calls.append((len(audio), prompt))
            fail = self.fail_left > 0
            self.fail_left -= fail
        time.sleep(self.slow_first if (n == 0 and self.slow_first) else self.delay)
        if fail:
            raise OSError("boom")
        loud = sum(1 for i in range(0, len(audio) - FRAME + 1, FRAME) if np.abs(audio[i:i + FRAME]).max() > 4000)
        return f"w{loud}"


async def feed(stt, frames, realtime=False):
    for f in frames:
        stt.feed(f)
        await asyncio.sleep(FRAME / SR if realtime else 0)


def run(coro):
    return asyncio.run(coro)


class TestSpeculative(unittest.TestCase):
    def test_final_reuses_speculative_request(self):
        async def go():
            s = FakeSTT()
            await feed(s, [QUIET] * 20)
            s.begin()
            await feed(s, [LOUD] * 30 + [QUIET] * 10)
            await asyncio.sleep(0.2)              # endpointer still waiting for its silence
            text = await s.finalize()
            return s, text
        s, text = run(go())
        self.assertEqual(text, "w30")
        self.assertEqual(len(s.calls), 1)        # no new request at the turn end
        self.assertEqual(s.stats[-1]["how"], "spec")
        self.assertLess(s.stats[-1]["wait_ms"], 20)

    def test_speech_after_spec_needs_new_request(self):
        async def go():
            s = FakeSTT()
            await feed(s, [QUIET] * 20)
            s.begin()
            await feed(s, [LOUD] * 10 + [QUIET] * 6)
            await asyncio.sleep(0.1)
            await feed(s, [LOUD] * 5)             # user goes on; turn ends right after (no spec yet)
            return s, await s.finalize()
        s, text = run(go())
        self.assertEqual(text, "w15")
        self.assertEqual(s.stats[-1]["how"], "new")

    def test_soft_tail_after_cut_is_not_lost(self):
        async def go():
            s = FakeSTT({"spec_silence_ms": 40, "spec_quiet_ms": 40})
            await feed(s, [QUIET] * 20)
            s.begin()
            # loud, a short dip below speech level but still sound, then the soft tail continues
            await feed(s, [LOUD] * 10 + [SOFT] * 3)
            await asyncio.sleep(0.05)
            await feed(s, [QUIET] * 4)
            await asyncio.sleep(0.1)
            return s, await s.finalize()
        s, text = run(go())
        self.assertEqual(text, "w10")
        # the only request was sent after the soft tail ended, so it covers it
        self.assertEqual(s.stats[-1]["how"], "spec")
        self.assertEqual(len(s.calls), 1)

    def test_long_utterance_is_chunked_in_order(self):
        async def go():
            s = FakeSTT({"chunk_min_s": 0.5})
            await feed(s, [QUIET] * 20)
            s.begin()
            for n in (30, 30, 7):                 # 0.6 s of speech, pause, 0.6 s, pause, 0.14 s
                await feed(s, [LOUD] * n + [QUIET] * 6)
                await asyncio.sleep(0.08)
            await asyncio.sleep(0.1)
            return s, await s.finalize()
        s, text = run(go())
        self.assertEqual(text, "w30 w30 w7")
        self.assertEqual(s.stats[-1]["chunks"], 2)
        self.assertEqual(s.calls[-1][1], "w30")   # previous chunk text as prompt

    def test_begin_during_finalize_keeps_both(self):
        async def go():
            s = FakeSTT({"chunk_min_s": 0.5}, delay=0.15)
            await feed(s, [QUIET] * 20)
            s.begin()
            await feed(s, [LOUD] * 30 + [QUIET] * 6)
            await asyncio.sleep(0.01)
            await feed(s, [LOUD] * 5)
            t = asyncio.create_task(s.finalize())  # needs a new request (~150 ms)
            await asyncio.sleep(0.01)
            s.begin()                               # user starts the next utterance meanwhile
            await feed(s, [LOUD] * 8 + [QUIET] * 6)
            first = await t
            await asyncio.sleep(0.2)
            second = await s.finalize()
            return first, second
        first, second = run(go())
        self.assertEqual(first, "w30 w5")
        self.assertTrue(second.startswith("w"))
        self.assertGreaterEqual(int(second[1:]), 8)

    def test_hedge_when_slow(self):
        async def go():
            s = FakeSTT({"hedge_ms": 100}, slow_first=1.0)
            await feed(s, [QUIET] * 20)
            s.begin()
            await feed(s, [LOUD] * 10)
            t0 = time.monotonic()
            text = await s.finalize()
            return s, text, time.monotonic() - t0
        s, text, dt = run(go())
        self.assertEqual(text, "w10")
        self.assertLess(dt, 0.5)
        self.assertEqual(len(s.calls), 2)

    def test_fanout_first_answer_wins(self):
        async def go():
            s = FakeSTT({"fanout": 2, "hedge_ms": 0}, slow_first=1.0)
            await feed(s, [QUIET] * 20)
            s.begin()
            await feed(s, [LOUD] * 10)
            t0 = time.monotonic()
            text = await s.finalize()
            return s, text, time.monotonic() - t0
        s, text, dt = run(go())
        self.assertEqual(text, "w10")
        self.assertLess(dt, 0.5)
        self.assertEqual(len(s.calls), 2)

    def test_fanout_one_failure_is_fine(self):
        async def go():
            s = FakeSTT({"fanout": 2}, fail_first=1)
            await feed(s, [QUIET] * 20)
            s.begin()
            await feed(s, [LOUD] * 10)
            return await s.finalize()
        self.assertEqual(run(go()), "w10")

    def test_retry_on_failure(self):
        async def go():
            s = FakeSTT(fail_first=1)
            await feed(s, [QUIET] * 20)
            s.begin()
            await feed(s, [LOUD] * 10)
            return await s.finalize()
        self.assertEqual(run(go()), "w10")

    def test_interims_in_order(self):
        async def go():
            s = FakeSTT({"interim_ms": 200}, delay=0.02)
            got = []
            s.on_interim = got.append
            await feed(s, [QUIET] * 20)
            s.begin()
            await feed(s, [LOUD] * 40 + [QUIET] * 6, realtime=True)
            await asyncio.sleep(0.1)
            await s.finalize()
            return got
        got = run(go())
        self.assertGreaterEqual(len(got), 2)
        nums = [int(x[1:]) for x in got]
        self.assertEqual(nums, sorted(nums))
        self.assertEqual(nums[-1], 40)

    def test_finalize_has_a_deadline_and_falls_back(self):
        """A hung service must not stall the turn: finalize returns the best text by the deadline."""
        async def go():
            s = FakeSTT({"final_timeout_ms": 300, "hedge_ms": 0}, delay=0.02)
            await feed(s, [QUIET] * 20)
            s.begin()
            await feed(s, [LOUD] * 30 + [QUIET] * 10)
            await asyncio.sleep(0.1)                # the speculative request ("w30") is back
            s.delay = 5.0                           # now the service hangs
            await feed(s, [LOUD] * 5)               # speech after it: a new request is needed
            t0 = time.monotonic()
            text = await s.finalize()
            return s, text, time.monotonic() - t0
        s, text, dt = run(go())
        self.assertLess(dt, 0.5)
        self.assertEqual(text, "w30")
        self.assertEqual(s.stats[-1]["how"], "timeout")

    def test_finalize_never_raises(self):
        async def go():
            s = FakeSTT({"hedge_ms": 0}, fail_first=99)
            await feed(s, [QUIET] * 20)
            s.begin()
            await feed(s, [LOUD] * 10)
            return s, await s.finalize()
        s, text = run(go())
        self.assertEqual(text, "")
        self.assertEqual(s.stats[-1]["how"], "failed")

    def test_submit_after_close_is_not_a_crash(self):
        async def go():
            s = FakeSTT()
            await feed(s, [QUIET] * 20)
            s.begin()
            await s.close()
            await feed(s, [LOUD] * 30 + [QUIET] * 10)   # would submit a speculative request
            return await s.finalize()
        self.assertEqual(run(go()), "")

    def test_empty_utterance(self):
        async def go():
            s = FakeSTT()
            s.begin()
            return await s.finalize()
        self.assertEqual(run(go()), "")


class TestAzureShutdown(unittest.TestCase):
    def test_close_unblocks_a_hung_request(self):
        """A server that accepts and never answers: close() must free the worker thread at once."""
        import socket
        from live.stt import AzureSTT
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        held = []
        threading.Thread(target=lambda: held.append(srv.accept()), daemon=True).start()
        os.environ.setdefault("AZURE_OPENAI_API_KEY", "x")
        os.environ.setdefault("AZURE_OPENAI_RESOURCE_NAME", "x")
        s = AzureSTT({"http_timeout_s": 30})
        s.host = f"127.0.0.1:{srv.getsockname()[1]}"
        done = threading.Event()

        def call():
            try:
                s._transcribe(np.zeros(SR // 2, np.int16))
            except Exception:
                pass
            done.set()
        threading.Thread(target=call, daemon=True).start()
        time.sleep(0.3)
        self.assertFalse(done.is_set())
        t0 = time.monotonic()
        asyncio.run(s.close())
        self.assertTrue(done.wait(2.0))
        self.assertLess(time.monotonic() - t0, 2.0)
        for conn, _ in held:
            conn.close()
        srv.close()


class TestBuffer(unittest.TestCase):
    def test_rolling_buffer_keeps_absolute_positions(self):
        s = BaseSTT({"preroll_ms": 20})
        s.KEEP_S = 1
        for _ in range(100):                      # 2 s, only ~1 s kept
            s.feed(QUIET)
        s.begin()
        s.feed(LOUD)
        self.assertEqual(len(s.utterance()), 2 * FRAME)
        self.assertGreater(s.base, 0)
        self.assertEqual(len(s.audio(s.total - FRAME, s.total)), FRAME)

    def test_join_text(self):
        self.assertEqual(join_text([" a ", "", None, "b"]), "a b")


if __name__ == "__main__":
    unittest.main()
