"""stt piece: speech to text for one user utterance at a time.

Interface:

    stt = make_stt(cfg)               "azure" (default) or "whisper"
    await stt.start()                 warm up connections / models
    stt.feed(frame)                   every mic frame, always (keeps a rolling buffer)
    stt.begin()                       an utterance starts now (includes `preroll_ms` before)
    stt.on_interim = callable(text)   optional; called with partial text while the user talks
    stt.on_event = callable(name, fields)  optional; STT trouble and how each final was made
                                      (x_stt_final, x_stt_reconnect, x_stt_error, x_stt_dropped)
    text = await stt.finalize()       final text of the audio since begin()
    await stt.close()

Each begin() is matched by one finalize(), in order. The orchestrator calls finalize() from
a task it starts at the turn end, so the next begin() can run first when the mic loop drains
a backlog of frames without yielding (the user talks again right after the turn end). The
utterance that was still open is then frozen in begin() and the next finalize() returns its
text, not the text of the new one (`late: true` in x_stt_final).

Azure ("speculative") design, so the final text is ready when the turn ends,
not one request later:

- A small VAD inside the STT watches the utterance. As soon as the user has
  been below the speech level for `spec_silence_ms` (60 ms) and below the
  softer "sound" level for `spec_quiet_ms` (40 ms), it sends the audio so far
  to Azure gpt-4o-mini-transcribe (batch REST, keep-alive HTTPS, a pool of
  warm connections). It sends it `fanout` (2) times and takes the first answer
  (p90 for a 2 s clip: 478 ms with one request, 328 ms with two). The text is
  back about 400 ms after the end of speech, usually before the endpointer
  decides the turn is over.
- `finalize()` takes the newest finished (or else the earliest running)
  request that covers all sound of the utterance (nothing above the sound
  level after its cut point). Only when there is none (speech went on until
  the turn end) does it send a new request. A request slower than `hedge_ms`
  gets a duplicate; a failed one is sent again.
- While the user talks, a request every `interim_ms` gives interim text
  (`on_interim`). Interim and speculative results go to `on_interim` in order.
- Long utterances are cut at pauses into frozen chunks once they pass
  `chunk_min_s`, so the request at the end only carries the tail (request
  time grows with audio length: ~350 ms for 2 s, ~900 ms for 27 s).

Nothing here can stall the session: `finalize()` has a hard deadline
(`final_timeout_ms`) and then returns the best text it has (the newest
finished request on the utterance), it never raises; warm-up in `start()` is
bounded (`warm_timeout_s`); every HTTP request has a socket timeout
(`http_timeout_s`); `close()` shuts the open sockets so no worker thread is
left blocked in a read.

`kind: "whisper"` keeps the v0 local openai-whisper CLI (batch, slow).
"""
from __future__ import annotations

import asyncio
import concurrent.futures as cf
import http.client
import io
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from .events import FRAME_MS, SR, dbfs

API_VERSION = "2025-03-01-preview"
_AUTO = object()                      # _submit: prompt from the open utterance's chunks


def wav_bytes(x: np.ndarray, sr: int = SR) -> bytes:
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(x.astype("<i2").tobytes())
    return b.getvalue()


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def echoes_prompt(text: str, prompt: Optional[str]) -> bool:
    """True when a transcript only repeats its prompt. gpt-4o-mini-transcribe answers a near-silent
    clip (the pause after a chunk was committed) with the prompt word for word, and the user's last
    sentence then appeared twice in the final (session 20261030-110521, 250 s)."""
    w, p = _words(text), _words(prompt)
    if not w or not p or len(w) > len(p):
        return False
    return p[len(p) - len(w):] == w and (len(w) >= 3 or len(w) == len(p))


# What Whisper-family models write for noise, breath or silence. A final that is only one of
# these is not words (FLOOR-SPEC.md, "Words"). Compared after turntaking.normalize.
STT_HALLUCINATIONS = {
    "you", "thank you", "thanks", "thank you very much", "thanks for watching", "thank you for watching",
    "thank you so much for watching", "we'll see you next time", "see you next time", "bye", "bye bye",
    "please subscribe", "subscribe", "like and subscribe", "so", "the", "i", "okay thank you",
}


def join_text(parts: list[str]) -> str:
    return " ".join(p.strip() for p in parts if p and p.strip())


class BaseSTT:
    """Rolling mic buffer with absolute sample positions (`self.total` samples fed so far)."""

    KEEP_S = 120

    def __init__(self, cfg: Optional[dict] = None) -> None:
        self.cfg = cfg or {}
        self.preroll = int(self.cfg.get("preroll_ms", 300) * SR / 1000)
        self.chunks: list[np.ndarray] = []
        self.base = 0          # absolute sample index of chunks[0][0]
        self.total = 0         # absolute samples fed
        self.mark = 0          # absolute start of the current utterance
        self.on_interim: Optional[Callable[[str], None]] = None
        # the floor controller's interims: (utterance id from begin(), text)
        self.on_interim_utt: Optional[Callable[[int, str], None]] = None
        self.utt = 0
        self._marks: dict[int, int] = {}       # utterance id -> its mark (final_for)

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    def feed(self, frame: np.ndarray) -> None:
        self.chunks.append(frame)
        self.total += len(frame)
        while len(self.chunks) > 1 and self.total - self.base - len(self.chunks[0]) > SR * self.KEEP_S:
            self.base += len(self.chunks.pop(0))
        self.mark = max(self.mark, self.base)

    def begin(self) -> int:
        """A new utterance starts now (minus the preroll). Returns its id, for final_for()."""
        self.mark = max(self.base, self.total - self.preroll)
        self.utt += 1
        self._marks[self.utt] = self.mark
        for u in [u for u in self._marks if u < self.utt - 50]:
            del self._marks[u]
        return self.utt

    def audio(self, start: int, end: int) -> np.ndarray:
        """Absolute sample range [start, end) of what is still buffered."""
        if not self.chunks:
            return np.zeros(0, np.int16)
        x = np.concatenate(self.chunks)
        s, e = max(0, start - self.base), max(0, end - self.base)
        return x[s:e]

    def utterance(self) -> np.ndarray:
        return self.audio(self.mark, self.total)

    async def finalize(self) -> str:
        audio = self.utterance()
        if len(audio) < SR // 10:
            return ""
        return (await asyncio.to_thread(self._transcribe, audio)).strip()

    async def final_for(self, utt: int) -> tuple[str, str]:
        """(text, how) of utterance `utt`: from its mark to the next utterance's mark, or to now.
        Finals bind by id, never by call order, and nothing is dropped for being late."""
        if utt not in self._marks:
            return "", "gone"
        end = self._marks.get(utt + 1, self.total)
        audio = self.audio(self._marks[utt], end)
        if len(audio) < SR // 10:
            return "", "short"
        return (await asyncio.to_thread(self._transcribe, audio)).strip(), "base"

    def _transcribe(self, audio: np.ndarray, prompt: Optional[str] = None) -> str:
        raise NotImplementedError


@dataclass
class Job:
    """One transcription request over the absolute sample range [start, end)."""
    start: int
    end: int
    kind: str                       # "spec" | "interim" | "final" | "hedge"
    utt: int
    fut: Optional[asyncio.Future] = None
    t0: float = 0.0
    t_done: Optional[float] = None    # time.monotonic() when the text was back
    text: Optional[str] = None
    hedges: list = field(default_factory=list)
    prompt: Optional[str] = None


class SpeculativeSTT(BaseSTT):
    """Speculative, chunked batch transcription (see module docstring). Subclasses give `_transcribe`."""

    DEFAULTS = {
        "speech_dbfs": -48.0,        # absolute floor for "speech" (the bench noise floor is -65 dBFS)
        "floor_margin_db": 14.0,     # speech = above (noise floor + margin), learned between utterances
        "max_speech_dbfs": -38.0,    # never require more than this to count as speech
        "spec_silence_ms": 60.0,     # below the speech level this long after speech -> speculative request
        "sound_margin_db": 8.0,      # "sound" = above (speech level - this); used to check a request covers all audio
        "spec_quiet_ms": 40.0,       # ... and below the sound level this long
        "interim_ms": 1000.0,        # interim request every this much audio while talking (0 = off)
        "chunk_min_s": 6.0,          # freeze a chunk at a pause once the open segment is this long
        "fanout": 2,                 # speculative/final requests go out this many times; first answer wins
                                     # (Azure p90 for a 2 s clip: 478 ms single, 328 ms with 2)
        "hedge_ms": 550.0,           # a final-critical request slower than this gets a duplicate
        "workers": 8,
        "use_prompt": True,          # previous chunk text as `prompt` for the next chunk
        "final_timeout_ms": 4000.0,  # finalize() never waits longer; then the best text so far
        "warm_timeout_s": 8.0,       # start() never waits longer for the warm-up requests
        "late_final_s": 2.0,         # a frozen utterance (begin() before its finalize()) is kept this long
        "echo_speech_ms": 300.0,     # a clip with less speech than this whose text repeats the prompt is empty
    }

    def __init__(self, cfg: Optional[dict] = None) -> None:
        super().__init__(cfg)
        self.p = {**self.DEFAULTS, **self.cfg}
        self.pool = cf.ThreadPoolExecutor(max_workers=int(self.p["workers"]), thread_name_prefix="stt")
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.utt = 0
        self.active = False
        self._floor_hist: list[float] = []
        self.floor = -65.0
        self._reset_utt()
        self.stats: list[dict] = []           # per finalize: how the final was obtained (bench/debug)
        self.on_event: Optional[Callable[[str, dict], None]] = None
        self._ended: list[dict] = []          # utterances frozen by begin() before their finalize()

    def _reset_utt(self) -> None:
        self.seg_start = self.mark            # start of the open (not frozen) segment
        self.chunks_done: list[Job] = []      # frozen chunks, in order
        self.jobs: list[Job] = []             # requests on the open segment
        self.last_speech = None               # absolute end of the last speech frame
        self.last_sound = None                # absolute end of the last frame above the (lower) sound level
        self.silence_run = 0.0                # ms below the speech level
        self.quiet_run = 0.0                  # ms below the sound level
        self.sound_since_job = False
        self.last_interim_end = self.mark
        self.delivered_end = -1
        self.pending_commit: Optional[Job] = None

    # -- vad -------------------------------------------------------------------
    def threshold(self) -> float:
        return min(self.p["max_speech_dbfs"], max(self.p["speech_dbfs"], self.floor + self.p["floor_margin_db"]))

    def feed(self, frame: np.ndarray) -> None:
        super().feed(frame)
        db = dbfs(frame)
        if not self.active:
            self._floor_hist.append(db)
            if len(self._floor_hist) > 100:
                self._floor_hist = self._floor_hist[-100:]
            if len(self._floor_hist) >= 10:
                self.floor = float(np.percentile(self._floor_hist, 30))
            return
        if self.loop is None:
            return
        if db > self.threshold() - self.p["sound_margin_db"]:
            # any sound at all (also soft word tails): a request must reach past it to count as final
            self.last_sound = self.total
            self.sound_since_job = True
            self.quiet_run = 0.0
        else:
            self.quiet_run += FRAME_MS
        if db > self.threshold():
            self.last_speech = self.total
            self.silence_run = 0.0
            if self.pending_commit is not None:
                self._commit(self.pending_commit)
            if self.p["interim_ms"] and (self.total - self.last_interim_end) >= self.p["interim_ms"] * SR / 1000 \
                    and not any(j.kind == "interim" and not j.fut.done() for j in self.jobs):
                self.last_interim_end = self.total
                self._submit(self.seg_start, self.total, "interim")
            return
        self.silence_run += FRAME_MS
        if self.sound_since_job and self.silence_run >= self.p["spec_silence_ms"] \
                and self.quiet_run >= self.p["spec_quiet_ms"]:
            self.sound_since_job = False
            j = self._submit(self.seg_start, self.total, "spec")
            self.last_interim_end = self.total
            if (self.total - self.seg_start) >= self.p["chunk_min_s"] * SR:
                self.pending_commit = j     # frozen if speech resumes after this pause

    # -- requests ----------------------------------------------------------------
    def _event(self, name: str, **fields) -> None:
        """Report to on_event on the loop thread (safe to call from the pool threads)."""
        cb = self.on_event
        if cb is None:
            return
        try:
            try:
                here = asyncio.get_running_loop()
            except RuntimeError:            # a pool thread
                here = None
            loop = self.loop
            if here is None and loop is not None and loop.is_running():
                loop.call_soon_threadsafe(cb, name, fields)
            else:
                cb(name, fields)
        except Exception:
            pass

    def _prompt(self, chunks: Optional[list] = None) -> Optional[str]:
        chunks = self.chunks_done if chunks is None else chunks
        if not self.p["use_prompt"] or not chunks:
            return None
        last = chunks[-1]
        if last.fut is not None and last.fut.done() and not last.fut.cancelled() and not last.fut.exception():
            return last.fut.result()[-400:] or None
        return None

    def _submit(self, start: int, end: int, kind: str, prompt: object = _AUTO, utt: Optional[int] = None,
                track: bool = True) -> Job:
        audio = self.audio(start, end)
        if prompt is _AUTO:
            prompt = self._prompt()
        j = Job(start, end, kind, self.utt if utt is None else utt, t0=time.monotonic(), prompt=prompt)
        n = max(1, int(self.p["fanout"])) if kind in ("spec", "final") else 1
        try:
            subs = [asyncio.wrap_future(self.pool.submit(self._safe_transcribe, audio, prompt), loop=self.loop)
                    for _ in range(n)]
        except RuntimeError as e:          # pool already shut down (closing): a failed job, never a crash
            f = self.loop.create_future()
            f.set_exception(e)
            subs = [f]
            n = 1
        j.fut = subs[0] if n == 1 else self._first_ok(subs)
        j.fut.add_done_callback(lambda f, j=j: self._on_done(j))
        if track:
            self.jobs.append(j)
        return j

    def _first_ok(self, subs: list) -> asyncio.Future:
        """A future with the first successful result of `subs` (the error of the last one if all fail)."""
        out = self.loop.create_future()
        left = [len(subs)]

        def done(f: asyncio.Future) -> None:
            left[0] -= 1
            if out.done():
                return
            if not f.cancelled() and f.exception() is None:
                out.set_result(f.result())
            elif left[0] == 0:
                out.set_exception(f.exception() if not f.cancelled() else RuntimeError("stt: cancelled"))
        for f in subs:
            f.add_done_callback(done)
        return out

    def _safe_transcribe(self, audio: np.ndarray, prompt: Optional[str]) -> str:
        if len(audio) < SR // 10:
            return ""
        text = self._transcribe(audio, prompt).strip()
        if prompt and echoes_prompt(text, prompt) and self._speech_ms(audio) < self.p["echo_speech_ms"]:
            return ""                      # a near-silent clip answered with its prompt
        return text

    def _speech_ms(self, audio: np.ndarray) -> float:
        """How much of a clip is above the speech level."""
        th = self.threshold()
        n = int(SR * FRAME_MS / 1000)
        return FRAME_MS * sum(1 for i in range(0, len(audio) - n + 1, n) if dbfs(audio[i:i + n]) > th)

    def _on_done(self, j: Job) -> None:
        if j.fut.cancelled() or j.fut.exception() is not None:
            return
        j.t_done = time.monotonic()
        j.text = j.fut.result()
        if j.utt != self.utt or not self.active or j.kind == "hedge" or j.end <= self.delivered_end:
            return
        if j.start != self.seg_start:
            return
        self.delivered_end = j.end
        prefix = [c.text or "" for c in self.chunks_done]
        for cb, args in ((self.on_interim, ()), (self.on_interim_utt, (j.utt,))):
            if cb is not None:
                try:
                    cb(*args, join_text(prefix + [j.text]))
                except Exception:
                    pass

    def _commit(self, j: Job) -> None:
        self.pending_commit = None
        self.chunks_done.append(j)
        self.seg_start = j.end
        self.jobs = []
        self.last_interim_end = self.total

    # -- interface ---------------------------------------------------------------
    def _freeze(self, end: int) -> dict:
        """What finalize() needs of the open utterance, cut at absolute sample `end`."""
        last = max(x for x in (self.last_sound, self.last_speech, self.seg_start) if x is not None)
        return {"utt": self.utt, "mark": self.mark, "end": end, "seg_start": self.seg_start,
                "chunks": list(self.chunks_done),
                # not a request that reaches past the cut (an interim sent on the next utterance's
                # first loud frames, before begin() froze this one): its text holds the next words
                "jobs": [j for j in self.jobs if j.start == self.seg_start and j.end <= end],
                "last": min(last, end), "late": False}

    def begin(self) -> int:
        if self.active:
            # The previous utterance was not finalized yet: its finalize() is on its way (a task
            # started at the turn end that has not run). Keep it as it was. Its speech ended at
            # least a pause before the new speech, so it ends where the new utterance starts.
            new_mark = max(self.base, self.total - self.preroll)
            u = self._freeze(max(self.mark, new_mark))
            u["late"] = True
            u["frozen_at"] = time.monotonic()
            self._ended.append(u)
            self.pending_commit = None
        super().begin()                    # (it advances self.utt)
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = None
        self.active = True
        self._reset_utt()
        self._on_begin()
        return self.utt

    def _on_begin(self) -> None:
        pass

    async def _result(self, j: Job, critical: bool = True) -> str:
        """Text of a job; hedge it with a duplicate when slow, retry it once when it fails."""
        hedge_s = self.p["hedge_ms"] / 1000.0
        futs = [j.fut]
        waited = time.monotonic() - j.t0
        if critical and not j.fut.done() and hedge_s > 0:
            try:
                return await asyncio.wait_for(asyncio.shield(j.fut), max(0.0, hedge_s - waited))
            except asyncio.TimeoutError:
                h = self._submit_hedge(j)
                futs.append(h.fut)
            except Exception:
                pass
        errors = 0
        pending = set(futs)
        while True:
            for f in list(pending):
                if f.done():
                    if not f.cancelled() and f.exception() is None:
                        return f.result()
                    pending.discard(f)
                    errors += 1
            if not pending:
                if errors > 2:
                    raise RuntimeError("stt: transcription failed")
                pending = {self._submit_hedge(j).fut}
            await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)

    def _submit_hedge(self, j: Job) -> Job:
        h = self._submit(j.start, j.end, "hedge", prompt=j.prompt, utt=-1, track=False)
        j.hedges.append(h)
        return h

    def _take(self) -> dict:
        """The utterance this finalize() is for: the oldest frozen one, else the open one."""
        now = time.monotonic()
        while self._ended and now - self._ended[0]["frozen_at"] > self.p["late_final_s"]:
            u = self._ended.pop(0)         # never finalized: must not be taken for a later turn
            self._event("x_stt_dropped", audio_s=round((u["end"] - u["mark"]) / SR, 2))
        if self._ended:
            return self._ended.pop(0)
        self.active = False
        self.pending_commit = None
        return self._freeze(self.total)

    async def final_for(self, utt: int) -> tuple[str, str]:
        """(text, how) of utterance `utt` (the id begin() returned), in any order: a frozen one is
        never dropped for being late, and is never handed to another utterance. ("", "gone") if
        it was finalized already."""
        u = next((x for x in self._ended if x["utt"] == utt), None)
        if u is not None:
            self._ended.remove(u)
        elif self.active and self.utt == utt:
            self.active = False
            self.pending_commit = None
            u = self._freeze(self.total)
        else:
            return "", "gone"
        n = len(self.stats)
        text = await self._final_of(u)
        return text, (self.stats[-1].get("how") if len(self.stats) > n else None) or "spec"

    async def finalize(self) -> str:
        """Final text since begin(). Never raises and never waits past `final_timeout_ms`."""
        return await self._final_of(self._take())

    async def _final_of(self, u: dict) -> str:
        t_call = time.monotonic()
        if self.loop is None:
            self.loop = asyncio.get_running_loop()
        snap: dict = {"chunks": u["chunks"], "jobs": list(u["jobs"])}
        try:
            text = await asyncio.wait_for(self._finalize(u, t_call, snap), self.p["final_timeout_ms"] / 1000.0)
        except asyncio.CancelledError:
            raise
        except Exception as e:             # timeout, or every retry failed
            text = self._best_effort(snap)
            self.stats.append({"how": "timeout" if isinstance(e, TimeoutError) else "failed",
                               "error": type(e).__name__, "chunks": len(snap.get("chunks", [])),
                               "wait_ms": round((time.monotonic() - t_call) * 1000, 1), "tail_s": None,
                               "ready_mono": time.monotonic(), "late": u["late"]})
        st = self.stats[-1] if self.stats else {}
        self._event("x_stt_final", how=st.get("how"), late=u["late"], wait_ms=st.get("wait_ms"),
                    chunks=st.get("chunks"), audio_s=round(max(0, u["end"] - u["mark"]) / SR, 2), empty=not text)
        return text

    @staticmethod
    def _ok_text(j: Job) -> Optional[str]:
        f = j.fut
        if f is not None and f.done() and not f.cancelled() and f.exception() is None:
            return f.result()
        return None

    def _best_effort(self, snap: dict) -> str:
        """Best text without waiting: finished chunk texts plus the newest finished request on the tail."""
        parts = []
        for c in snap.get("chunks", []):
            t = self._ok_text(c)
            if t is None:
                t = next((self._ok_text(h) for h in reversed(c.hedges) if self._ok_text(h) is not None), "")
            parts.append(t)
        jobs = snap.get("jobs", [])
        tail = [j for j in jobs + [h for x in jobs for h in x.hedges] if self._ok_text(j) is not None]
        if tail:
            parts.append(self._ok_text(max(tail, key=lambda j: (j.end, j.t_done or 0))))
        return join_text(parts)

    async def _finalize(self, u: dict, t_call: float, snap: dict) -> str:
        end = u["end"]
        if end - u["mark"] < SR // 10:
            self.stats.append({"how": "short", "chunks": 0, "wait_ms": 0.0, "tail_s": 0.0,
                               "ready_mono": time.monotonic(), "late": u["late"]})
            return ""
        chunks = u["chunks"]
        cover = [j for j in u["jobs"] if j.end >= u["last"] and j.kind != "hedge"
                 and not (j.fut.done() and (j.fut.cancelled() or j.fut.exception() is not None))]
        how = "spec"
        if cover:
            # newest covering job that has finished, else the earliest sent (it finishes first)
            done = [j for j in cover if j.fut.done()]
            tail = done[-1] if done else cover[0]
            if not done:
                how = "spec_wait"
        else:
            how = "new"
            tail = self._submit(u["seg_start"], end, "final", prompt=self._prompt(chunks), utt=u["utt"], track=False)
            snap["jobs"].append(tail)
        parts = [await self._result(c) for c in chunks]
        parts.append(await self._result(tail))
        text = join_text(parts)
        self.stats.append({"how": how, "chunks": len(chunks), "wait_ms": round((time.monotonic() - t_call) * 1000, 1),
                           "tail_s": round((tail.end - tail.start) / SR, 2), "late": u["late"],
                           "ready_mono": max([x.t_done or time.monotonic() for x in chunks + [tail]])})
        return text

    async def close(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)


class _Conn(http.client.HTTPSConnection):
    """HTTPS keep-alive connection that keeps a duplicate of its TCP socket, so another thread can
    shut the connection down even during the TLS handshake (when `sock` is detached)."""

    killer: Optional["object"] = None

    def connect(self) -> None:
        http.client.HTTPConnection.connect(self)
        self.killer = self.sock.dup()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)

    def kill(self) -> None:
        for s in (self.killer, self.sock):
            try:
                if s is not None:
                    s.shutdown(2)          # wakes a thread blocked in recv on this connection
            except Exception:
                pass
        for s in (self.killer,):
            try:
                if s is not None:
                    s.close()
            except Exception:
                pass
        try:
            self.close()
        except Exception:
            pass


class AzureSTT(SpeculativeSTT):
    def __init__(self, cfg: Optional[dict] = None) -> None:
        super().__init__(cfg)
        self.host = f"{os.environ['AZURE_OPENAI_RESOURCE_NAME']}.openai.azure.com"
        self.key = os.environ["AZURE_OPENAI_API_KEY"]
        self.deployment = self.cfg.get("deployment", "gpt-4o-mini-transcribe")
        self.idle_s = float(self.cfg.get("refresh_idle_s", 45))
        self.http_timeout = float(self.cfg.get("http_timeout_s", 8))
        self._open: set = set()               # every live connection, so close() can unblock reads
        self._open_lock = threading.Lock()
        self.closed = False
        self.base_prompt = self.cfg.get("prompt")
        self._tls = threading.local()
        self.calls: list[tuple] = []          # (status or error, ms, audio seconds) per HTTP request

    # one keep-alive connection per pool thread
    def _conn(self, fresh: bool = False) -> _Conn:
        c = getattr(self._tls, "conn", None)
        if fresh or c is None or time.monotonic() - getattr(self._tls, "used", 0) > self.idle_s:
            if c is not None:
                self._drop(c)
            if self.closed:
                raise OSError("stt closed")
            c = _Conn(self.host, timeout=self.http_timeout)
            with self._open_lock:
                self._open.add(c)
            self._tls.conn = None
            try:
                c.connect()
            except Exception:
                self._drop(c)
                raise
            self._tls.conn = c
        self._tls.used = time.monotonic()
        return c

    def _drop(self, c: _Conn) -> None:
        with self._open_lock:
            self._open.discard(c)
        c.kill()

    async def close(self) -> None:
        self.closed = True
        await super().close()
        with self._open_lock:
            conns = list(self._open)
        for c in conns:
            self._drop(c)

    def _refresh(self) -> None:
        if self.closed:
            return
        if time.monotonic() - getattr(self._tls, "used", 0) > self.idle_s:
            self._conn(fresh=True)
        time.sleep(0.05)   # keep this thread busy so the other refresh tasks land on other threads

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        # Warm every pool thread: TLS + one tiny request each, in parallel.
        n = int(self.p["workers"])
        silence = np.zeros(SR // 2, np.int16)
        warm = asyncio.gather(*[asyncio.wrap_future(self.pool.submit(self._transcribe, silence)) for _ in range(n)],
                              return_exceptions=True)
        try:
            await asyncio.wait_for(warm, float(self.p["warm_timeout_s"]))
        except TimeoutError:
            pass                           # cold connections are made on first use

    def _on_begin(self) -> None:
        # Connections idle for a long time may be dropped by the server: refresh them while the user talks.
        for _ in range(int(self.p["workers"])):
            self.pool.submit(self._refresh)

    def _transcribe(self, audio: np.ndarray, prompt: Optional[str] = None) -> str:
        boundary = uuid.uuid4().hex
        body = io.BytesIO()

        def part(name, value, filename=None, ctype=None):
            body.write(f"--{boundary}\r\n".encode())
            disp = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else "")
            body.write(f"Content-Disposition: {disp}\r\n".encode())
            if ctype:
                body.write(f"Content-Type: {ctype}\r\n".encode())
            body.write(b"\r\n")
            body.write(value if isinstance(value, bytes) else value.encode())
            body.write(b"\r\n")
        part("file", wav_bytes(audio), "audio.wav", "audio/wav")
        part("model", self.deployment)
        part("response_format", "json")
        part("language", "en")
        pr = join_text([self.base_prompt or "", prompt or ""])
        if pr:
            part("prompt", pr)
        body.write(f"--{boundary}--\r\n".encode())
        path = f"/openai/deployments/{self.deployment}/audio/transcriptions?api-version={API_VERSION}"
        headers = {"api-key": self.key, "Content-Type": f"multipart/form-data; boundary={boundary}"}
        data = body.getvalue()
        for attempt in range(2):
            t0 = time.monotonic()
            try:
                c = self._conn(fresh=attempt > 0)
                c.request("POST", path, body=data, headers=headers)
                r = c.getresponse()
                out = r.read()
                self._tls.used = time.monotonic()
                self.calls.append((r.status, round((self._tls.used - t0) * 1000), round(len(audio) / SR, 2)))
                if r.status != 200:
                    retry = r.status in (429, 500, 502, 503, 504) and not attempt
                    self._event("x_stt_error", error=f"http {r.status}", retry=retry,
                                ms=self.calls[-1][1], audio_s=self.calls[-1][2])
                    if retry:
                        continue
                    raise RuntimeError(f"stt http {r.status}: {out[:200]!r}")
                return json.loads(out).get("text", "")
            except (http.client.HTTPException, OSError) as e:
                ms = round((time.monotonic() - t0) * 1000)
                self.calls.append((type(e).__name__, ms, round(len(audio) / SR, 2)))
                c = getattr(self._tls, "conn", None)
                if c is not None:
                    self._drop(c)
                self._tls.conn = None
                if attempt or self.closed:
                    if not self.closed:
                        self._event("x_stt_error", error=type(e).__name__, retry=False, ms=ms,
                                    audio_s=round(len(audio) / SR, 2))
                    raise
                self._event("x_stt_reconnect", error=type(e).__name__, ms=ms)
        return ""


class WhisperSTT(BaseSTT):
    def _transcribe(self, audio: np.ndarray, prompt: Optional[str] = None) -> str:
        model = self.cfg.get("model", "base.en")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "u.wav")
            open(p, "wb").write(wav_bytes(audio))
            subprocess.run(["whisper", p, "--model", model, "--language", "en", "--output_format", "txt",
                            "--output_dir", d, "--fp16", "False"], capture_output=True, check=True)
            return open(os.path.join(d, "u.txt")).read()


def make_stt(cfg: Optional[dict] = None) -> BaseSTT:
    cfg = cfg or {}
    azure = os.environ.get("AZURE_OPENAI_API_KEY") and os.environ.get("AZURE_OPENAI_RESOURCE_NAME")
    kind = cfg.get("kind", "azure" if azure else "whisper")
    return AzureSTT(cfg) if kind == "azure" else WhisperSTT(cfg)
