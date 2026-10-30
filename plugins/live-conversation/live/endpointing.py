"""endpointing piece: decides when the user started speaking and when their turn is over.

Interface:

    ep = Endpointer(cfg: dict | None)
    ep.feed(frame: np.ndarray[int16, 480], t_ms: float) -> list[Decision]
    ep.on_transcript(text: str, final: bool)   hint from stt (final=False: text of the turn so far)
    ep.reset()

Decision.kind is "speech_start" or "turn_end"; Decision.source names the rule.
The orchestrator logs every turn_end as a SPEC `turn_end_detected` event. At
most one turn_end per speech_start.

How a turn end is decided
-------------------------
1. Energy VAD with a learned noise floor finds speech and pauses.
2. When a pause starts, two probes run in worker threads (never in the mic loop):
   - text probe: the last few seconds of the turn are transcribed (Azure
     gpt-4o-mini-transcribe, about 0.35 s, with a duplicate request if slow;
     or, with text_probe="hint", interim text the stt module passes in with
     on_transcript(text, final=False)). The words say how the turn ends:
       question, exclamation, "yeah" / "okay" / "thanks" ...   -> yield
       other text ending with a period                         -> statement
       one or two words ("Got it.", "Mm-hmm." heard as Cyrillic) -> short
       "...", a trailing comma, a filler (um, uh) or a
       dangling word (if, the, to, and ...)                    -> hold
   - Smart Turn v3.2 (live/endpointing_model.py), an audio model of whether the
     speaker sounds finished, at 200 and 450 ms of pause.
3. The turn ends when the pause is at least as long as the wait for its class:
       yield      yield_ms (250)       in practice: when the text is back, ~0.4 s
       short      yield_ms if Smart Turn says complete, else statement_ms
       statement  statement_ms (850) if Smart Turn says complete, else
                  statement_unsure_ms (1000). Text cannot tell "...for a couple
                  of months now." mid-story from a finished statement, so a
                  statement waits out normal between-sentence pauses.
       hold       hold_ms (2500): never cut in at "I was wondering if ..."
       no text    no_text_ms (1600): text probe late or failed, audio only
   Speech of resume_ms or more ends the pause and drops its probes.

The decision time is the time of the frame on which the rule fires.
"""
from __future__ import annotations

import concurrent.futures as cf
import http.client
import io
import json
import os
import re
import threading
import time
import uuid
import wave
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .events import FRAME_MS, SR, dbfs


@dataclass
class Decision:
    kind: str           # "speech_start" | "turn_end"
    t_ms: float
    source: str = "vad"


DEFAULTS = {
    # VAD
    "speech_dbfs": -50.0,          # absolute floor for "speech"
    "floor_margin_db": 15.0,       # speech = above learned noise floor + margin ...
    "max_speech_dbfs": -38.0,      # ... but never needs more than this
    "start_ms": 60.0,              # loud this long -> speech_start
    "resume_ms": 60.0,             # loud this long inside a pause -> the pause is over
    "preroll_ms": 300.0,           # audio kept before speech_start for the probes
    # probes
    "text_probe": "azure",         # "azure" | "local" (mlx-whisper on device) | "hint" (only stt hints) | "off"
    "text_probe_at_ms": 60.0,      # pause length that fires the text probe
    "text_hedge_ms": 300.0,        # a text probe slower than this gets a duplicate request
    "text_probe_s": 3.5,           # seconds of the turn sent (enough for the last words; shorter is faster)
    "text_deployment": "gpt-4o-mini-transcribe",
    "smart_turn": True,
    "smart_turn_at_ms": [200.0, 450.0],   # pause lengths that run Smart Turn
    "smart_turn_threshold": 0.5,
    # waits (pause length before turn_end, per class)
    "yield_ms": 250.0,
    "statement_ms": 850.0,         # statement + Smart Turn says complete
    "statement_unsure_ms": 1000.0,
    "hold_ms": 2500.0,
    "no_text_ms": 1600.0,
    "warm": True,                   # warm the model and the HTTPS connection at construction
}

# Words that rarely end a finished English turn (as the last word, even with a period after it).
# A wrong "hold" only costs delay; words that often end sentences ("it", "you", "do", "in") are out.
DANGLING = set("""
a an the and but or nor because cause if whether than although though unless until while
of to for with from into onto at by about my your our their his its i we they he she i'm we're
you're they're very really just also let's gonna wanna gotta kinda sorta um uh er erm uhm ah like
who whose whom which
""".split())
# Short replies that are a whole turn by themselves.
ACKS = set("""
yeah yes yep yup no nope nah mhm mm-hmm mmhmm uh-huh uh huh sure okay ok right exactly correct
great perfect thanks thank you cool nice fine alright gotcha good
""".split())
FILLER_ONLY = {"um", "uh", "er", "erm", "uhm", "hmm", "ah", "so", "and", "but", "like"}


def classify_text(text: str) -> str:
    """How a (partial) transcript ends: "yield", "statement", "hold" or "none" (no words)."""
    t = text.strip()
    if not t:
        return "none"
    if re.search(r"(\.\.\.|…|[-–—,;:])\s*$", t):
        return "hold"
    words = re.findall(r"[\w']+(?:-[\w']+)*", t.lower())
    if not words:
        return "none"
    last = words[-1]
    if t.endswith("?") or t.endswith("!"):
        return "yield"
    if all(w in ACKS for w in words) and len(words) <= 3:
        return "yield"
    if last in DANGLING or all(w in FILLER_ONLY for w in words):
        return "hold"
    if len(words) <= 2:
        return "short"             # "Mm-hmm." (sometimes heard in another script), "Got it."
    if t[-1] in ".\"')":
        return "statement"
    return "statement_unsure"      # no final punctuation: the ASR was not sure either


def _wav_bytes(x: np.ndarray) -> bytes:
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(x.astype("<i2").tobytes())
    return b.getvalue()


class AzureTextProbe:
    """Batch transcription of the turn so far, one keep-alive HTTPS connection per worker thread."""

    API_VERSION = "2025-03-01-preview"

    def __init__(self, deployment: str) -> None:
        self.host = f"{os.environ['AZURE_OPENAI_RESOURCE_NAME']}.openai.azure.com"
        self.key = os.environ["AZURE_OPENAI_API_KEY"]
        self.deployment = deployment
        self._tls = threading.local()

    def _conn(self, fresh: bool = False) -> http.client.HTTPSConnection:
        c = getattr(self._tls, "conn", None)
        if fresh or c is None or time.monotonic() - getattr(self._tls, "used", 0.0) > 40.0:
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass
            c = http.client.HTTPSConnection(self.host, timeout=10)
            c.connect()
            self._tls.conn = c
        self._tls.used = time.monotonic()
        return c

    def refresh_if_idle(self) -> None:
        if time.monotonic() - getattr(self._tls, "used", 0.0) > 40.0:
            self._conn(fresh=True)

    def transcribe(self, pcm: np.ndarray) -> str:
        bd = uuid.uuid4().hex
        body = io.BytesIO()

        def part(name, value, filename=None, ctype=None):
            body.write(f"--{bd}\r\n".encode())
            disp = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else "")
            body.write(f"Content-Disposition: {disp}\r\n".encode())
            if ctype:
                body.write(f"Content-Type: {ctype}\r\n".encode())
            body.write(b"\r\n")
            body.write(value if isinstance(value, bytes) else value.encode())
            body.write(b"\r\n")
        part("file", _wav_bytes(pcm), "audio.wav", "audio/wav")
        part("model", self.deployment)
        part("response_format", "json")
        part("language", "en")
        body.write(f"--{bd}--\r\n".encode())
        path = f"/openai/deployments/{self.deployment}/audio/transcriptions?api-version={self.API_VERSION}"
        headers = {"api-key": self.key, "Content-Type": f"multipart/form-data; boundary={bd}"}
        data = body.getvalue()
        for attempt in range(2):
            try:
                c = self._conn(fresh=attempt > 0)
                c.request("POST", path, body=data, headers=headers)
                r = c.getresponse()
                raw = r.read()
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}: {raw[:200]!r}")
                return json.loads(raw).get("text", "")
            except (http.client.HTTPException, OSError):
                if attempt:
                    raise
        return ""


def _local_probe_worker(conn, model: str) -> None:
    """Child process of LocalTextProbe: owns the Whisper model, answers one request at a time."""
    import mlx_whisper
    from scipy.signal import resample_poly
    while True:
        try:
            pcm = conn.recv()
        except EOFError:
            return
        if pcm is None:
            return
        try:
            x = resample_poly(pcm.astype(np.float32) / 32768.0, 2, 3).astype(np.float32)   # 24 -> 16 kHz
            r = mlx_whisper.transcribe(x, path_or_hf_repo=model, language="en", temperature=0.0,
                                       condition_on_previous_text=False, without_timestamps=True)
            conn.send(("ok", (r.get("text") or "").strip()))
        except Exception as e:
            conn.send(("error", f"{type(e).__name__}: {e}"[:300]))


class LocalTextProbe:
    """On-device transcription of the turn so far: Whisper base.en on the Apple GPU (mlx-whisper).

    About 40 ms for 3.5 s of audio on an M2 Pro (Azure: 320-600 ms), with the end punctuation
    the classes need ("...of Australia?", "I was wondering if."). The model runs in its own
    process, shared by every Endpointer of this process: in-process, the TTS and the other
    worker threads held it up to 300-600 ms (GIL and CPU contention)."""

    MODEL = "mlx-community/whisper-base.en-mlx"
    _shared: dict = {}
    _shared_lock = threading.Lock()

    def __init__(self, model: str = MODEL) -> None:
        import importlib.util
        if importlib.util.find_spec("mlx_whisper") is None:
            raise ImportError("mlx_whisper is not installed")
        self.model = model
        self.lock = threading.Lock()

    def _worker(self):
        with LocalTextProbe._shared_lock:
            w = LocalTextProbe._shared.get(self.model)
            if w is None or not w[0].is_alive():
                import multiprocessing as mp
                ctx = mp.get_context("spawn")
                parent, child = ctx.Pipe()
                proc = ctx.Process(target=_local_probe_worker, args=(child, self.model), daemon=True)
                proc.start()
                w = (proc, parent, threading.Lock())
                LocalTextProbe._shared[self.model] = w
            return w

    def refresh_if_idle(self) -> None:
        pass

    def transcribe(self, pcm: np.ndarray) -> str:
        proc, conn, lock = self._worker()
        with lock:
            conn.send(np.asarray(pcm, dtype=np.int16))
            if not conn.poll(15.0):
                raise TimeoutError("local text probe: no answer in 15 s")
            kind, text = conn.recv()
        if kind != "ok":
            raise RuntimeError(text)
        return text


@dataclass
class _Pause:
    gen: int
    start_ms: float                       # t_ms of the first quiet frame
    text_job: Optional[cf.Future] = None
    text_sent_ms: float = 0.0
    text: Optional[str] = None            # transcript of the turn so far, once known
    text_class: Optional[str] = None
    text_ms: Optional[float] = None
    text_pcm: Optional[np.ndarray] = None
    hedge_job: Optional[cf.Future] = None
    st_jobs: list = field(default_factory=list)
    st_p: list = field(default_factory=list)


class Endpointer:
    def __init__(self, cfg: Optional[dict] = None) -> None:
        cfg = dict(cfg or {})
        if "silence_ms" in cfg:
            # Plain silence endpointer (the v0 contract): every wait is silence_ms, no probes.
            w = float(cfg.pop("silence_ms"))
            cfg = {"text_probe": "off", "smart_turn": False, "warm": False, "yield_ms": w, "statement_ms": w,
                   "statement_unsure_ms": w, "hold_ms": w, "no_text_ms": w, **cfg}
        self.cfg = {**DEFAULTS, **cfg}
        c = self.cfg
        self.pool = cf.ThreadPoolExecutor(max_workers=5, thread_name_prefix="endpoint")
        self.floor = -65.0
        self._floor_hist: list[float] = []
        self.trace: list[dict] = []            # per pause: what the probes said (bench/debug)
        self.smart = None
        self.text = None
        if c["smart_turn"]:
            try:
                from .endpointing_model import SmartTurn
                self.smart = SmartTurn()
            except Exception as e:             # no model (offline): text + timing only
                self.trace.append({"warning": f"smart turn unavailable: {e}"})
        if c["text_probe"] == "local":
            try:
                self.text = LocalTextProbe(c.get("local_model", LocalTextProbe.MODEL))
            except Exception as e:             # no mlx-whisper: the Azure probe instead
                self.trace.append({"warning": f"local text probe unavailable: {e}"[:200]})
        if c["text_probe"] == "azure" or (c["text_probe"] == "local" and self.text is None):
            try:
                self.text = AzureTextProbe(c["text_deployment"])
            except KeyError:
                self.trace.append({"warning": "no Azure credentials: text probe off"})
        if c["warm"]:
            self.pool.submit(self._warm)
        self.gen = 0
        self.reset()

    # -- public ------------------------------------------------------------------
    def reset(self) -> None:
        self.gen += 1
        self.in_turn = False
        self.speech_run = 0.0
        self.quiet_run = 0.0
        self.pause: Optional[_Pause] = None
        self.buf: list[np.ndarray] = []         # turn audio (with preroll)
        self.pre: list[np.ndarray] = []         # recent frames before speech_start

    def on_transcript(self, text: str, final: bool) -> None:
        """With text_probe="hint": interim text of the turn so far (final=False), sent by stt
        for the current pause, is the text probe. Final text (after turn_end) is not used."""
        p = self.pause
        if final or self.cfg["text_probe"] != "hint" or p is None or p.text is not None:
            return
        p.text = text
        p.text_class = classify_text(text)

    def feed(self, frame: np.ndarray, t_ms: float) -> list[Decision]:
        c = self.cfg
        lvl = dbfs(frame)
        loud = lvl > self._threshold()
        if not self.in_turn:
            self._learn_floor(lvl, loud)
        out: list[Decision] = []

        if not self.in_turn:
            self.pre.append(frame)
            keep = int(c["preroll_ms"] / FRAME_MS) + int(c["start_ms"] / FRAME_MS) + 1
            if len(self.pre) > keep:
                del self.pre[: len(self.pre) - keep]
            self.speech_run = self.speech_run + FRAME_MS if loud else 0.0
            if self.speech_run >= c["start_ms"]:
                self.in_turn = True
                self.buf = list(self.pre)
                self.pre = []
                self.quiet_run = 0.0
                self.pause = None
                if self.text is not None:
                    self.pool.submit(self._refresh)
                out.append(Decision("speech_start", t_ms, "vad"))
            return out

        self.buf.append(frame)
        max_frames = int(30_000 / FRAME_MS)
        if len(self.buf) > max_frames:
            del self.buf[: len(self.buf) - max_frames]

        if loud:
            self.speech_run += FRAME_MS
            if self.pause is not None and self.speech_run >= c["resume_ms"]:
                self._end_pause("resumed")
            if self.pause is None:
                self.quiet_run = 0.0
            return out
        self.speech_run = 0.0
        self.quiet_run += FRAME_MS
        if self.pause is None:
            self.pause = _Pause(self.gen, t_ms - self.quiet_run + FRAME_MS)
        p = self.pause
        quiet = self.quiet_run
        self._run_probes(p, quiet, t_ms)
        wait, why = self._wait(p)
        if quiet >= wait:
            self._end_pause("turn_end", t_ms, why)
            self.in_turn = False
            self.speech_run = 0.0
            self.buf = []
            out.append(Decision("turn_end", t_ms, why))
        return out

    # -- probes --------------------------------------------------------------------
    def _run_probes(self, p: _Pause, quiet: float, t_ms: float) -> None:
        c = self.cfg
        if p.text_job is None and p.text is None and self.text is not None and quiet >= c["text_probe_at_ms"]:
            pcm = self._turn_audio(c["text_probe_s"])
            p.text_sent_ms = t_ms
            p.text_job = self.pool.submit(self.text.transcribe, pcm)
            p.text_pcm = pcm
        if p.text is None and p.text_job is not None:
            sent = [j for j in (p.text_job, p.hedge_job) if j is not None]
            done = [j for j in sent if j.done()]
            ok = [j for j in done if j.exception() is None]
            if ok:
                p.text = ok[0].result()
                p.text_class = classify_text(p.text)
                p.text_ms = t_ms - p.text_sent_ms
            elif p.hedge_job is None and t_ms - p.text_sent_ms >= c["text_hedge_ms"]:
                p.hedge_job = self.pool.submit(self.text.transcribe, p.text_pcm)      # slow or failed: retry
            elif p.hedge_job is not None and len(done) == len(sent):
                p.text, p.text_class = "", "error"
                self.trace.append({"warning": f"text probe failed: {done[0].exception()}"[:200]})
        if self.smart is not None:
            ats = c["smart_turn_at_ms"]
            k = len(p.st_jobs)
            if k < len(ats) and quiet >= ats[k]:
                p.st_jobs.append(self.pool.submit(self.smart.predict_24k, self._turn_audio(8.0)))
            while len(p.st_p) < len(p.st_jobs) and p.st_jobs[len(p.st_p)].done():
                try:
                    p.st_p.append(p.st_jobs[len(p.st_p)].result())
                except Exception:
                    p.st_p.append(None)

    def _wait(self, p: _Pause) -> tuple[float, str]:
        """Pause length needed before this pause ends the turn, and the rule name."""
        c = self.cfg
        cls = p.text_class
        ps = [x for x in p.st_p if x is not None]
        st_yes = bool(ps) and max(ps) >= c["smart_turn_threshold"]
        if cls == "yield":
            return c["yield_ms"], "semantic_yield"
        if cls == "statement":
            return (c["statement_ms"] if st_yes else c["statement_unsure_ms"]), "semantic_statement"
        if cls == "short":
            return (c["yield_ms"] if st_yes else c["statement_ms"]), "semantic_short"
        if cls == "statement_unsure":
            return c["statement_unsure_ms"], "semantic_statement"
        if cls == "hold":
            return c["hold_ms"], "semantic_hold_timeout"
        # No text yet, the probe failed, or it heard no words (breath, noise): timing only.
        return c["no_text_ms"], "vad_timeout"

    def _end_pause(self, outcome: str, t_ms: Optional[float] = None, why: str = "") -> None:
        p = self.pause
        if p is None:
            return
        rec = {"pause_start_ms": round(p.start_ms, 1), "outcome": outcome, "text": p.text, "class": p.text_class,
               "smart_turn": [round(x, 3) if x is not None else None for x in p.st_p], "rule": why,
               "text_ms": p.text_ms,
               "sent_ms": round(p.text_sent_ms - p.start_ms, 1) if p.text_job else None}
        if t_ms is not None:
            rec["t_ms"] = t_ms
        self.trace.append(rec)
        for j in [p.text_job, p.hedge_job, *p.st_jobs]:
            if j is not None:
                j.cancel()
        self.pause = None
        self.quiet_run = 0.0

    # -- helpers ---------------------------------------------------------------------
    def _turn_audio(self, seconds: float) -> np.ndarray:
        n = int(seconds * 1000 / FRAME_MS)
        return np.concatenate(self.buf[-n:]) if self.buf else np.zeros(0, np.int16)

    def _threshold(self) -> float:
        c = self.cfg
        return min(max(c["speech_dbfs"], self.floor + c["floor_margin_db"]), c["max_speech_dbfs"])

    def _learn_floor(self, lvl: float, loud: bool) -> None:
        if loud:
            return
        self._floor_hist.append(lvl)
        if len(self._floor_hist) > 250:            # 5 s of quiet frames
            del self._floor_hist[:50]
        if len(self._floor_hist) >= 25:
            self.floor = float(np.percentile(self._floor_hist, 20))

    def _refresh(self) -> None:
        try:
            self.text.refresh_if_idle()
        except Exception:
            pass

    def _warm(self) -> None:
        try:
            if self.smart is not None:
                self.smart.predict_24k(np.zeros(SR, np.int16))
            if self.text is not None:
                self.text.transcribe(np.zeros(SR // 2, np.int16))
        except Exception as e:
            self.trace.append({"warning": f"warm-up failed: {e}"[:200]})
