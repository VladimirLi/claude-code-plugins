"""audio_io piece: the mic, the speaker sink and the session clock.

Interface (what the orchestrator uses):

    audio.clock                      Clock, advanced only by mic frames
    async for frame in audio.frames():   20 ms int16 frames at 24 kHz, real time
    audio.sink.write(pcm, turn=, filler=) -> float   start time (ms) the chunk is scheduled at
    audio.sink.flush(spoken_text=None)  drop queued audio (barge-in)
    audio.sink.is_playing() -> bool     queued audio not yet played
    audio.sink.queued_end_ms -> float
    audio.turn_hint() -> int | None     the bench turn being fed (file mode), else None
    audio.done                          True when the session should end

Two implementations:

- FileAudio: plays one bench scenario as the mic in real time, following the
  turn start rules of bench/SPEC.md, and records mic/assistant/timeline WAVs.
  The harness events (session_start, user_speech_start/end, x_no_reply, ...)
  are written here because only this module knows the ground truth.
- LiveAudio: the real mic and speaker through sox `rec` and `play`.

The sink follows the SPEC: a chunk is written at max(clock when it arrived,
end of audio already queued). The sink emits first_reply_audio,
reply_audio_end and playback_stopped.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import wave
from typing import AsyncIterator, Optional

import numpy as np

from .events import FRAME, SR, Clock, EventLog

ONSET_DBFS = -45.0
QUIET_DBFS = -50.0
SUB = SR // 100  # 10 ms analysis frame for audibility


def read_wav(path: str) -> np.ndarray:
    with wave.open(path) as w:
        assert w.getsampwidth() == 2 and w.getnchannels() == 1, path
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").copy()
        if w.getframerate() != SR:
            x = resample(x, w.getframerate(), SR)
    return x


def write_wav(path: str, x: np.ndarray, sr: int = SR) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(np.asarray(x, dtype="<i2").tobytes())


def resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    """Linear-interpolation resampler (good enough for 16k <-> 24k speech)."""
    if sr_in == sr_out or len(x) == 0:
        return x.astype(np.int16)
    n = int(round(len(x) * sr_out / sr_in))
    t = np.arange(n) * (sr_in / sr_out)
    y = np.interp(t, np.arange(len(x)), x.astype(np.float64))
    return np.clip(np.round(y), -32768, 32767).astype(np.int16)


def _db(x: np.ndarray) -> float:
    f = x.astype(np.float64) / 32768.0
    return float(20 * np.log10(np.sqrt(np.mean(f * f)) + 1e-12)) if len(f) else -120.0


class Sink:
    """Playback buffer on the mic clock. Position p is heard when clock passes p."""

    def __init__(self, clock: Clock, log: EventLog) -> None:
        self.clock = clock
        self.log = log
        self.buf = np.zeros(SR * 60, dtype=np.int16)
        self.queued_end = 0
        self.reply_turn: Optional[int] = None
        self._first_logged: set = set()
        self._was_playing = False

    def _ensure(self, n: int) -> None:
        if n > len(self.buf):
            nb = np.zeros(max(n, len(self.buf) * 2), dtype=np.int16)
            nb[: len(self.buf)] = self.buf
            self.buf = nb

    @property
    def queued_end_ms(self) -> float:
        return self.queued_end * 1000.0 / SR

    def write(self, pcm: np.ndarray, turn: Optional[int] = None, filler: bool = False) -> float:
        start = max(self.clock.samples, self.queued_end)
        n = len(pcm)
        self._ensure(start + n)
        self.buf[start:start + n] = pcm
        self.queued_end = start + n
        self.reply_turn = turn
        if turn not in self._first_logged and n:
            self._first_logged.add(turn)
            self.log.emit("first_reply_audio", turn, t_ms=start * 1000.0 / SR, filler=filler)
        self._was_playing = True
        return start * 1000.0 / SR

    def flush(self, spoken_text: Optional[str] = None) -> None:
        now = self.clock.samples
        if self.queued_end > now:
            self.buf[now:self.queued_end] = 0
        self.queued_end = min(self.queued_end, now)
        self._was_playing = False
        extra = {} if spoken_text is None else {"spoken_text": spoken_text}
        self.log.emit("playback_stopped", self.reply_turn, **extra)

    def is_playing(self) -> bool:
        return self.queued_end > self.clock.samples

    def tick(self, start: int, n: int) -> np.ndarray:
        """Samples heard in [start, start+n). Called by the mic loop once per frame."""
        self._ensure(start + n)
        out = self.buf[start:start + n].copy()
        if self._was_playing and self.queued_end <= start + n:
            self._was_playing = False
            self.log.emit("reply_audio_end", self.reply_turn, t_ms=self.queued_end * 1000.0 / SR)
        return out

    def heard(self, n_total: int) -> np.ndarray:
        self._ensure(n_total)
        return self.buf[:n_total]


class _ReplyTracker:
    """Acoustic onset/offset of the reply after a user turn (SPEC turn start rules)."""

    def __init__(self, from_sample: int) -> None:
        self.from_sample = from_sample
        self.start: Optional[int] = None
        self.last_audible_end: Optional[int] = None
        self.end: Optional[int] = None
        self.quiet = 0

    def feed(self, block: np.ndarray, pos: int) -> None:
        for i in range(0, len(block) - SUB + 1, SUB):
            p = pos + i
            if p < self.from_sample:
                continue
            seg = block[i:i + SUB]
            d = _db(seg)
            if d > ONSET_DBFS:
                if self.start is None:
                    hit = np.nonzero(np.abs(seg.astype(np.int32)) > 32768 * 10 ** (ONSET_DBFS / 20))[0]
                    self.start = p + (int(hit[0]) if len(hit) else 0)
                self.last_audible_end = p + SUB
                self.end = None
                self.quiet = 0
            elif d < QUIET_DBFS:
                if self.start is not None and self.end is None:
                    self.quiet += SUB
                    if self.quiet >= int(0.3 * SR):
                        self.end = self.last_audible_end
            else:
                self.quiet = 0


class FileAudio:
    """Scenario as the mic, real time, outputs captured to buffers."""

    NO_REPLY_S = 15.0
    MAX_AFTER_LAST_S = 60.0
    END_AFTER_REPLY_S = 3.0

    def __init__(self, scenario_dir: str, log: EventLog, realtime: bool = True,
                 noise_path: Optional[str] = None) -> None:
        self.dir = scenario_dir
        self.sc = json.load(open(os.path.join(scenario_dir, "scenario.json")))
        self.turns = self.sc["turns"]
        self.wavs = [read_wav(os.path.join(scenario_dir, t["audio"]["24k"]["file"])) for t in self.turns]
        noise_path = noise_path or os.path.join(os.path.dirname(scenario_dir), "noise_floor_24k.wav")
        self.noise = read_wav(noise_path)
        self.log = log
        self.clock = log.clock
        self.sink = Sink(self.clock, log)
        self.realtime = realtime
        self.mic_chunks: list[np.ndarray] = []
        self.done = False
        self._next = 0                     # index of next turn to start
        self._start_at: Optional[int] = None
        self._cur: Optional[int] = None    # index of turn being fed
        self._cur_pos = 0
        self._turn_start: dict[int, int] = {}
        self._speech_end: dict[int, int] = {}
        self._trackers: dict[int, _ReplyTracker] = {}
        self._hint: Optional[int] = None

    def turn_hint(self) -> Optional[int]:
        return self._hint

    def _reply_active(self) -> bool:
        """The system says a reply is still being generated (set `reply_active` to a callable)."""
        f = getattr(self, "reply_active", None)
        try:
            return bool(f()) if f is not None else False
        except Exception:
            return False

    # -- turn start rules --------------------------------------------------
    def _resolve_start(self, now: int) -> Optional[int]:
        if self._next >= len(self.turns):
            return None
        t = self.turns[self._next]
        st = t["start"]
        off = int(st.get("offset_ms", 0) * SR / 1000)
        if st["anchor"] == "session_start":
            return off
        k = st.get("turn", self.turns[self._next - 1]["turn"])
        tr = self._trackers.get(k)
        if tr is None:
            return None
        se = self._speech_end[k]
        if st["anchor"] == "assistant_audio_start":
            if tr.start is not None:
                return tr.start + off
        elif st["anchor"] == "assistant_audio_end":
            # As for the bar (bench/reference_runner.py): the reply is over only when nothing is
            # queued or still being generated, and then 300 ms below -50 dBFS.
            if tr.end is not None and not self.sink.is_playing() and not self._reply_active():
                return tr.end + off
        if tr.start is None and now - se > self.NO_REPLY_S * SR:
            self.log.emit("x_no_reply", k)
            return now
        return None

    def _begin_turn(self, idx: int, at: int) -> None:
        t = self.turns[idx]
        n = t["turn"]
        a = t["audio"]["24k"]
        self._cur, self._cur_pos = idx, 0
        self._turn_start[n] = at
        self._speech_end[n] = at + a["speech_end_sample"]
        self._hint = n
        self.log.emit("user_speech_start", n, t_ms=(at + a["speech_start_sample"]) * 1000.0 / SR)
        self.log.emit("user_speech_end", n, t_ms=(at + a["speech_end_sample"]) * 1000.0 / SR)
        # Reply tracking for the previous turn stops at this turn's start, except
        # the barge-in check below which uses its own timing.
        self._trackers[n] = _ReplyTracker(at + a["speech_end_sample"])
        st = t["start"]
        if st["anchor"] == "assistant_audio_start":
            k = st.get("turn")
            tr = self._trackers.get(k)
            if tr is not None and tr.end is not None and tr.end <= at:
                self.log.emit("x_reply_shorter_than_offset", n)
        self._next = idx + 1
        self._start_at = None

    def _make_frame(self, pos: int) -> np.ndarray:
        out = np.empty(FRAME, dtype=np.int16)
        i = 0
        while i < FRAME:
            p = pos + i
            if self._cur is not None:
                w = self.wavs[self._cur]
                take = min(FRAME - i, len(w) - self._cur_pos)
                out[i:i + take] = w[self._cur_pos:self._cur_pos + take]
                self._cur_pos += take
                i += take
                if self._cur_pos >= len(w):
                    self._cur = None
                continue
            if self._start_at is None:
                self._start_at = self._resolve_start(p)
            if self._start_at is not None and self._start_at <= p:
                self._begin_turn(self._next, p)
                continue
            # noise floor until the next turn (or end of frame)
            lim = FRAME - i
            if self._start_at is not None:
                lim = min(lim, self._start_at - p)
            lim = max(1, lim)
            idx = (np.arange(p, p + lim)) % len(self.noise)
            out[i:i + lim] = self.noise[idx]
            i += lim
        return out

    def _check_end(self, pos: int) -> None:
        if self._next < len(self.turns) or self._cur is not None:
            return
        last = self.turns[-1]["turn"]
        tr = self._trackers[last]
        se = self._speech_end[last]
        if tr.end is not None and not self.sink.is_playing() and not self._reply_active() \
                and pos >= tr.end + self.END_AFTER_REPLY_S * SR:
            self.done = True
        elif tr.start is None and pos - se > self.NO_REPLY_S * SR:
            self.log.emit("x_no_reply", last)
            self.done = True
        elif pos - se > self.MAX_AFTER_LAST_S * SR:
            self.done = True

    async def frames(self) -> AsyncIterator[np.ndarray]:
        self.log.emit("session_start", None, t_ms=0.0)
        t0 = time.monotonic()
        while not self.done:
            pos = self.clock.samples
            frame = self._make_frame(pos)
            if self.realtime:
                due = t0 + (pos + FRAME) / SR
                delay = due - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
            heard = self.sink.tick(pos, FRAME)
            for tr in self._trackers.values():
                tr.feed(heard, pos)
            self.mic_chunks.append(frame)
            self.clock.advance(FRAME)
            yield frame
            self._check_end(self.clock.samples)

    def save(self, out_dir: str) -> None:
        mic = np.concatenate(self.mic_chunks) if self.mic_chunks else np.zeros(0, np.int16)
        a = self.sink.heard(len(mic)).copy()
        tl = ((mic.astype(np.int32) + a.astype(np.int32)) // 2).astype(np.int16)
        write_wav(os.path.join(out_dir, "mic.wav"), mic)
        write_wav(os.path.join(out_dir, "assistant.wav"), a)
        write_wav(os.path.join(out_dir, "timeline.wav"), tl)


class LiveAudio:
    """Real mic and speaker through sox. Use headphones: v0 has no echo cancellation."""

    def __init__(self, log: EventLog, recorder=None) -> None:
        self.log = log
        self.clock = log.clock
        self.sink = Sink(self.clock, log)
        self.recorder = recorder        # live/recorder.py: keeps mic + speaker audio of the session
        self.done = False
        self._rec = None
        self._play = None

    def turn_hint(self) -> Optional[int]:
        return None

    async def frames(self) -> AsyncIterator[np.ndarray]:
        fmt = ["-q", "-t", "raw", "-r", str(SR), "-e", "signed", "-b", "16", "-c", "1"]
        self._rec = await asyncio.create_subprocess_exec("rec", *fmt, "-", stdout=asyncio.subprocess.PIPE)
        self._play = await asyncio.create_subprocess_exec("play", *fmt, "-", stdin=asyncio.subprocess.PIPE)
        self.log.emit("session_start", None, t_ms=0.0)
        # small lead so the speaker never starves
        self._play.stdin.write(np.zeros(FRAME * 2, np.int16).tobytes())
        try:
            while not self.done:
                data = await self._rec.stdout.readexactly(FRAME * 2)
                frame = np.frombuffer(data, dtype="<i2").copy()
                pos = self.clock.samples
                out = self.sink.tick(pos, FRAME)
                self._play.stdin.write(out.tobytes())
                if self.recorder is not None:
                    self.recorder.audio(frame, out)
                self.clock.advance(FRAME)
                yield frame
        finally:
            for p in (self._rec, self._play):
                if p and p.returncode is None:
                    p.terminate()
