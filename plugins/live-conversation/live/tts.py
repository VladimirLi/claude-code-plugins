"""tts piece: text to 24 kHz speech, plus the chunker that decides what to speak when.

Interface:

    chunker = Chunker()
    chunker.push(delta: str) -> list[str]   pieces ready to speak now
    chunker.flush() -> list[str]            whatever is left at the end of the reply

    tts = make_tts(cfg)
    await tts.start()
    async for pcm in tts.synthesize(text):  int16 chunks at 24 kHz, in order
        ...
    await tts.close()

The orchestrator may call synthesize() for the next piece while the previous
one is still playing; implementations must allow concurrent calls.

Default engine: Kokoro-82M (local neural TTS, StyleTTS2 + iSTFTNet) on the
Apple GPU (MPS), kept warm in this process. A piece is synthesized whole (the
prosody model sees the full sentence), then its leading silence is cut so the
first audible sample comes out at once, and its tail is cut to a pause that
fits the punctuation it ends on. Synthesis runs on one worker thread, one piece
at a time, in call order (asyncio.Lock is FIFO); a cancelled waiter (barge-in)
drops out of the queue without costing compute.

Fallback engine (`{"kind": "say"}` or when Kokoro cannot load): macOS `say`.

Config (`cfg["tts"]`), all optional:
    kind        "kokoro" | "say"                     default "kokoro"
    voice       Kokoro voice id, or "a,b" to blend   default "af_aoede" (best of 7 on WER, F0 range, clicks)
    speed       Kokoro speed factor                  default 1.0
    device      decoder device "cpu" | "mps"         default cpu (MPS stalled 10-27 s under load)
    front_device text/prosody model device           default "cpu" (its LSTM is slow to compile on MPS)
    bucket_frames  pad decoder input to a multiple   default 8 on MPS, 0 on CPU
    warm_buckets / warm_max_frames  compile bucket shapes at start (MPS)   default true / 320
    lead_frames cap on the leading pad token frames  default none (capping hurts the first word)
    threads     torch CPU threads                    default 8
    prewarm     synthesize OPENERS at start (cache)  default true
    cache_size  short pieces kept in the audio cache default 256
    cache_max_words  longest piece that is cached    default 3
    level_dbfs  active speech RMS target             default -18
    peak_dbfs   peak ceiling                         default -1
    lead_ms     audio kept before the first onset    default 12
    tail_ms     decay kept after the last offset     default 60
    pause_ms    {punct: ms} silence after a piece    see PAUSES
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import re
import sys
import tempfile
import time
import wave
from collections import OrderedDict
from typing import AsyncIterator, Optional

import numpy as np

from .events import SR

# -- chunker -------------------------------------------------------------------------
_SENT = re.compile(r"(.+?[.!?…]+[\"')\]]*)(\s+|$)", re.S)
# a clause boundary: , ; : or a dash, followed by whitespace (so "1,000" never splits)
_CLAUSE = re.compile(r"(.+?(?:[,;:]|\s[-–—]))(\s+)", re.S)


class Chunker:
    """Splits streamed reply text into pieces to synthesize.

    - Sentences are the normal unit (the prosody model needs the whole sentence).
    - The first piece of a reply may be a leading clause ("Honestly,", "Oh nice,",
      "Two hundred and forty tests passed,") so the voice starts sooner. Only a
      short clause (<= first_clause_max_words) is split off, since that is where a
      speaker pauses anyway.
    """

    def __init__(self, min_chars: int = 2, first_clause_max_words: int = 6) -> None:
        self.buf = ""
        self.min_chars = min_chars
        self.first_clause_max_words = first_clause_max_words
        self.emitted = 0

    def _emit(self, piece: str, out: list) -> None:
        piece = piece.strip()
        if len(piece) >= self.min_chars:
            out.append(piece)
            self.emitted += 1

    def push(self, delta: str) -> list[str]:
        self.buf += delta
        out: list[str] = []
        while True:
            m = _SENT.match(self.buf)
            sent_ok = bool(m) and m.end(1) < len(self.buf)        # need the char after the stop to be sure
            if self.emitted == 0 and not out:
                c = _CLAUSE.match(self.buf)
                if c and (not sent_ok or c.end(1) < m.end(1)) and \
                        1 <= len(c.group(1).split()) <= self.first_clause_max_words:
                    self.buf = self.buf[c.end():]
                    self._emit(c.group(1), out)
                    continue
            if sent_ok:
                self.buf = self.buf[m.end():]
                self._emit(m.group(1), out)
                continue
            break
        return out

    def flush(self) -> list[str]:
        rest = self.buf.strip()
        self.buf = ""
        self.emitted = 0
        return [rest] if rest else []


# -- text clean-up ---------------------------------------------------------------------
_MD = re.compile(r"[*_#`>|]+")


def clean_text(text: str) -> str:
    """What the voice should read: no markdown symbols, single spaces."""
    t = _MD.sub(" ", text)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    return re.sub(r"\s+", " ", t).strip()


# Pronunciation fixes for Kokoro's G2P, checked with whisper base.en: the default
# phonemes of these words are heard as another word ("git" -> "get", "tilde" ->
# "till the", "ugh" -> "ah, hey", "Canberra" -> "Canbera").
LEXICON = {"git": "ɡˈɪʔt", "tilde": "tˈɪldi", "ugh": "ˈʌɡ", "canberra": "kˈænbəɹə"}
_LEX = re.compile(r"(?<![\w\[])(" + "|".join(LEXICON) + r")(?![\w\]])", re.I)


def apply_lexicon(text: str) -> str:
    """Mark words with a fixed pronunciation in misaki's [word](/phonemes/) syntax."""
    return _LEX.sub(lambda m: f"[{m.group(1)}](/{LEXICON[m.group(1).lower()]}/)", text)


# Short openers a reply often starts with. They are synthesized at start() and cached,
# so a reply that opens with one sounds at once, whatever the CPU load is.
OPENERS = ["Sure,", "Sure.", "Sure!", "Okay,", "Okay.", "Okay so,", "Yeah,", "Yeah.", "Yes,", "Yes.", "Yep,", "No,", "Nope,",
           "Right,", "Right.", "Got it.", "Got it,", "Honestly,", "Oh nice,", "Oh nice!", "Nice,", "Nice!", "Hmm,", "Hmm.",
           "Ugh,", "Oh,", "Oh!", "Oh no,", "Ah,", "Well,", "So,", "Alright,", "Alright.", "Good question.", "Great question.",
           "Of course.", "Of course,", "Absolutely.", "Totally.", "Sounds good.", "Let me check.", "One sec.", "Let me look.",
           "Good morning!", "Hey!", "Hi!", "Thanks!", "Fair enough.", "I see.", "Interesting.", "Exactly.", "Wait,",
           "Actually,", "Basically,", "Short version:", "Done.", "Hello!", "Hey there!", "Mm-hmm.", "Uh-huh."]

PAUSES = {".": 360, "!": 360, "?": 420, "…": 450, ",": 170, ";": 230, ":": 230, "-": 200, "–": 200, "—": 200, "": 140}


def end_punct(text: str) -> str:
    t = text.rstrip(" \"')]”’")
    return t[-1] if t and t[-1] in PAUSES else ""


def shape(x: np.ndarray, pause_ms: float, lead_ms: float = 12, tail_ms: float = 60,
          level_dbfs: float = -18.0, peak_dbfs: float = -1.0, gate_dbfs: float = -48.0) -> np.ndarray:
    """Float audio in [-1, 1] -> int16: cut leading silence, cut the tail to tail_ms
    plus pause_ms of silence, set the level, short fades so cuts never click."""
    x = np.asarray(x, np.float32)
    n = SR // 200                                          # 5 ms frames
    m = len(x) // n
    if m == 0:
        return np.zeros(int(SR * pause_ms / 1000), np.int16)
    rms = np.sqrt((x[: m * n].reshape(m, n) ** 2).mean(axis=1) + 1e-12)
    db = 20 * np.log10(rms)
    on = np.nonzero(db > max(gate_dbfs, db.max() - 45))[0]
    if len(on) == 0:
        return np.zeros(int(SR * pause_ms / 1000), np.int16)
    a = max(0, on[0] * n - int(SR * lead_ms / 1000))
    b = min(len(x), (on[-1] + 1) * n + int(SR * tail_ms / 1000))
    y = x[a:b].copy()
    act = rms[on]
    level = 20 * np.log10(np.sqrt(np.mean(act ** 2)) + 1e-12)
    g = 10 ** ((level_dbfs - level) / 20)
    pk = np.abs(y).max() * g
    if pk > 10 ** (peak_dbfs / 20):
        g *= 10 ** (peak_dbfs / 20) / pk
    y *= g
    fi, fo = min(len(y), SR * 3 // 1000), min(len(y), SR * 20 // 1000)
    y[:fi] *= np.linspace(0, 1, fi, dtype=np.float32)
    y[len(y) - fo:] *= np.linspace(1, 0, fo, dtype=np.float32)
    y = np.concatenate([y, np.zeros(int(SR * pause_ms / 1000), np.float32)])
    return np.clip(np.round(y * 32767), -32767, 32767).astype(np.int16)


def _chunks(x: np.ndarray, step: int = SR // 10):
    for i in range(0, len(x), step):
        yield x[i:i + step]


# -- Kokoro ------------------------------------------------------------------------------
class KokoroTTS:
    def __init__(self, cfg: Optional[dict] = None) -> None:
        self.cfg = cfg or {}
        self.voice = self.cfg.get("voice", "af_aoede")
        self.speed = float(self.cfg.get("speed", 1.0))
        self.pauses = {**PAUSES, **self.cfg.get("pause_ms", {})}
        self.shape_kw = {k: float(self.cfg[k]) for k in ("lead_ms", "tail_ms", "level_dbfs", "peak_dbfs")
                         if k in self.cfg}
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="kokoro")
        self.lock = asyncio.Lock()
        self.start_lock = asyncio.Lock()
        self.model = None
        self.cache: "OrderedDict[str, list[np.ndarray]]" = OrderedDict()   # lower-cased piece -> audio
        self.stats: list[dict] = []          # per synthesize() call: wait_ms, synth_ms, audio_ms

    # everything below with _ runs on the worker thread
    def _load(self) -> None:
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        import warnings
        warnings.filterwarnings("ignore")
        import torch
        from loguru import logger
        logger.remove()
        from kokoro import KModel, KPipeline
        torch.set_num_threads(int(self.cfg.get("threads", 8)))
        self.torch = torch
        self.fdev = self.cfg.get("front_device", "cpu")
        self.ddev = self.cfg.get("device", "cpu")    # MPS: faster when idle, but stalls for seconds under memory pressure
        self.bucket = int(self.cfg.get("bucket_frames", 8 if self.ddev == "mps" else 0))
        m = KModel().eval()
        for part in (m.bert, m.bert_encoder, m.predictor, m.text_encoder):
            part.to(self.fdev)
        m.decoder.to(self.ddev)
        self.model = m
        self.pipe = KPipeline(lang_code=self.voice[0] if self.voice[0] in "ab" else "a", model=False)
        self.pack = self.pipe.load_voice(self.voice)
        for t in ("Okay.", "Kwazorb flimbits Canberra.", "Sure, running them now.", "Most people guess Sydney, so you're in good company if you did.",
                  "I'd pick the part that breaks most often, put a few tests around it, and clean that up first, "
                  "then we can see what is left and decide together."):
            for _ in self._gen(t):
                pass
        if self.cfg.get("prewarm", True):
            for t in OPENERS:
                self._cached(t)
        if self.bucket and self.cfg.get("warm_buckets", True):
            for n in range(self.bucket * 2, int(self.cfg.get("warm_max_frames", 320)) + 1, self.bucket):
                self._decode_frames(n)

    def _decode_frames(self, n: int) -> None:
        """Run the decoder once on n frames of silence-like input (compiles that shape on MPS)."""
        torch, m = self.torch, self.model
        with torch.inference_mode():
            ref = self.pack[20].to(self.ddev)
            asr = torch.zeros(1, 512, n, device=self.ddev)
            f0 = torch.zeros(1, 2 * n, device=self.ddev)
            m.decoder(asr, f0, f0, ref[:, :128])

    def _infer(self, ps: str) -> np.ndarray:
        """KModel.forward, except that with bucket_frames the frame count is padded up to a
        bucket by lengthening the final pad token, so the MPS decoder sees few distinct shapes
        (each new shape costs a slow MPS compile). Optional lead_frames caps the leading pad
        (measured: capping it to 3-6 frames makes whisper lose the first word, so it is off)."""
        torch, m = self.torch, self.model
        fd = self.fdev
        ids = [i for i in map(m.vocab.get, ps) if i is not None]
        x = torch.LongTensor([[0, *ids, 0]]).to(fd)
        L = torch.LongTensor([x.shape[-1]]).to(fd)
        mask = torch.zeros_like(x, dtype=torch.bool)
        ref = self.pack[len(ps) - 1].to(fd)
        s = ref[:, 128:]
        bd = m.bert(x, attention_mask=(~mask).int())
        d_en = m.bert_encoder(bd).transpose(-1, -2)
        d = m.predictor.text_encoder(d_en, s, L, mask)
        h, _ = m.predictor.lstm(d)
        dur = torch.sigmoid(m.predictor.duration_proj(h)).sum(axis=-1) / self.speed
        dur = torch.round(dur).clamp(min=1).long().squeeze(0)
        if self.cfg.get("lead_frames") is not None:              # off by default: a short pad eats the first word
            dur[0] = min(int(dur[0]), int(self.cfg["lead_frames"]))
        real = int(dur.sum())
        if self.bucket:
            dur[-1] += (-real) % self.bucket
        idx = torch.repeat_interleave(torch.arange(x.shape[1], device=fd), dur)
        aln = torch.zeros((x.shape[1], idx.shape[0]), device=fd)
        aln[idx, torch.arange(idx.shape[0], device=fd)] = 1
        aln = aln.unsqueeze(0)
        en = d.transpose(-1, -2) @ aln
        F0, N = m.predictor.F0Ntrain(en, s)
        asr = m.text_encoder(x, L, mask) @ aln
        dd = self.ddev
        audio = m.decoder(asr.to(dd), F0.to(dd), N.to(dd), ref[:, :128].to(dd)).squeeze().cpu().numpy()
        n = len(audio) * real // int(dur.sum())
        return audio[:n]

    def _gen(self, text: str):
        """Yields float32 audio per model segment (a piece longer than ~510 phonemes has several)."""
        torch = self.torch
        _, tokens = self.pipe.g2p(apply_lexicon(text))
        for _gs, ps, _tks in self.pipe.en_tokenize(tokens):
            if not ps:
                continue
            with torch.inference_mode():
                yield self._infer(ps[:510])

    def _synth(self, text: str) -> list[np.ndarray]:
        segs = [a for a in self._gen(text)]
        if not segs:
            return []
        p = self.pauses.get(end_punct(text), self.pauses[""])
        out = []
        for i, a in enumerate(segs):
            last = i == len(segs) - 1
            out.append(shape(a, p if last else self.pauses[","], **self.shape_kw))
        return out

    def _cached(self, text: str) -> list[np.ndarray]:
        key = text.lower()
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        segs = self._synth(text)
        if len(text.split()) > int(self.cfg.get("cache_max_words", 3)):
            return segs                     # only short stock phrases are worth keeping
        self.cache[key] = segs
        while len(self.cache) > int(self.cfg.get("cache_size", 256)):
            self.cache.popitem(last=False)
        return segs

    async def _run(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self.pool, fn, *args)

    async def start(self) -> None:
        async with self.start_lock:                 # synthesize() before start() must not load twice
            if self.model is None:
                await self._run(self._load)

    async def close(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)

    async def synthesize(self, text: str) -> AsyncIterator[np.ndarray]:
        text = clean_text(text)
        if not re.search(r"\w", text):
            return
        if self.model is None:
            await self.start()
        t0 = time.perf_counter()
        async with self.lock:
            t1 = time.perf_counter()
            hit = text.lower() in self.cache
            segs = self._cached(text) if hit else await self._run(self._cached, text)
        self.stats.append({"text": text, "cache": hit, "wait_ms": round((t1 - t0) * 1000, 1),
                           "synth_ms": round((time.perf_counter() - t1) * 1000, 1),
                           "audio_ms": round(sum(len(a) for a in segs) * 1000 / SR, 1)})
        if os.environ.get("LIVE_TTS_DEBUG"):
            print("tts", self.stats[-1], file=sys.stderr, flush=True)
        for a in segs:
            for c in _chunks(a):
                yield c


# -- macOS say (fallback) ---------------------------------------------------------------
class SayTTS:
    def __init__(self, cfg: Optional[dict] = None) -> None:
        self.cfg = cfg or {}
        self.voice = self.cfg.get("say_voice", self.cfg.get("voice", "Flo (English (US))"))
        self.rate = self.cfg.get("rate_wpm", 190)

    async def start(self) -> None:
        async for _ in self.synthesize("ok"):
            pass

    async def close(self) -> None:
        pass

    async def synthesize(self, text: str) -> AsyncIterator[np.ndarray]:
        text = clean_text(text)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "o.wav")
            proc = await asyncio.create_subprocess_exec(
                "say", "-v", self.voice, "-r", str(self.rate), f"--data-format=LEI16@{SR}", "-o", p, text,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc.wait()
            if not os.path.exists(p):
                return
            with wave.open(p) as w:
                x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").copy()
        for c in _chunks(x):
            yield c


def make_tts(cfg: Optional[dict] = None):
    cfg = cfg or {}
    if cfg.get("kind", "kokoro") == "say":
        return SayTTS(cfg)
    import importlib.util
    if not all(importlib.util.find_spec(m) for m in ("kokoro", "torch", "misaki")):   # no import here: torch is slow to import
        return SayTTS({k: v for k, v in cfg.items() if k != "voice"})
    return KokoroTTS(cfg)
