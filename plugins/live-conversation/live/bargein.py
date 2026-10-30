"""bargein piece: decides whether user sound during assistant speech is an interruption.

Interface:

    bi = BargeIn(cfg)
    bi.feed(frame, t_ms, assistant_speaking: bool) -> str | None
        "barge_in"  stop the assistant now (flush audio, cancel the brain)
        "duck"      the user started to make a sound: pause or lower the reply
                    now, the decision follows ("barge_in" or "resume")
        "resume"    the sound was a backchannel ("mm-hmm") or noise: carry on
        None        nothing to do
    bi.on_transcript(text)   final STT text: settles an open episode (backchannel words
                             resume, anything else barges in) on the next feed()
    bi.reset()
    bi.last                  info on the last decision: kind, reason, onset_ms, t_ms

How it decides (v1). An "episode" starts at the first speech frame. A frame
is speech when it is loud enough (above `speech_dbfs` and `snr_db` over a
tracked noise floor) and is either voiced (periodic in
70-400 Hz) or a fricative (strong energy above 1 kHz). Inside an episode:

- After `duck_ms` of speech: "duck" (once).
- "barge_in" as soon as the sound cannot be a backchannel:
    open     `open_frames` voiced frames in a row with > `open_hi` of their energy above
             1 kHz. Open and front vowels ("actually", "hey", "I mean") have it; a
             closed-mouth hum ("mm-hmm", "hmm", "mhm") does not.
    fric     `fric_frames` fricative frames (s, sh, ch, f, t releases): words,
             not a hum.
    burst    a second burst of speech after a gap of `burst_gap_ms` or more
             (or within `rejoin_ms` of a "resume"): several words ("wait, wait").
    long     `long_ms` of voiced speech in total: too long for a backchannel.
  The fric and burst rules also need some voicing, so a hiss, a fan switching
  on or a click never stops the reply.
- "resume" after `end_ms` of no speech without a barge-in.

User speech is followed even while the assistant is silent. If a reply starts
while the user is still talking (gap <= `carry_gap_ms`), the evidence carries over,
so a reply that starts over the user stops at once. If the user had stopped,
their speech was their own turn and is forgotten.

Without "duck" support the stop happens at the "barge_in" decision, so the
rules are ordered to decide as early as the sound allows. With "duck" support
the user hears the reply pause about `duck_ms` after they start, whatever the rule.
Contract for an orchestrator that acts on "duck": keep passing
`assistant_speaking=True` while the reply is paused, and when it barges in,
take the spoken text at the pause time (`bi.last["onset_ms"]` + `duck_ms`).

In file mode the mic has no echo of the assistant; live mode needs headphones
(an echo of the reply is speech to this detector).
"""
from __future__ import annotations

import re
from collections import deque
from typing import Optional

import numpy as np

from .events import FRAME, FRAME_MS, SR

DEFAULTS = {
    "speech_dbfs": -50.0,      # absolute level a speech frame must exceed
    "snr_db": 16.0,            # and this far above the tracked noise floor
    "voicing": 0.45,           # normalized autocorrelation peak, 70-400 Hz
    "duck_ms": 60.0,           # speech before "duck"
    "long_ms": 600.0,          # voiced speech too long for a backchannel ("min_ms" is the old name)
    "min_voiced_ms": 20.0,     # the fric and burst rules also need this much voicing
    "open_frames": 2,          # consecutive voiced frames with high-frequency energy -> words
    "open_hi": 0.25,           # share of energy above 1 kHz for an "open" voiced frame
    "fric_frames": 2,          # fricative frames -> words
    "fric_hi": 0.6,            # share of energy above 1 kHz for a fricative frame
    "fric_cen": 2500.0,        # spectral centroid (Hz) for a fricative frame
    "burst_gap_ms": 140.0,     # a gap this long splits bursts
    "first_burst_min_ms": 100.0,  # the first burst must be this long for the burst rule
    "burst_min_ms": 40.0,      # the second burst must be this long
    "end_ms": 350.0,           # silence that closes an episode -> "resume"
    "carry_gap_ms": 60.0,      # user speech ongoing (gap <= this) when a reply starts -> judge it at once
    "rejoin_ms": 800.0,        # speech this soon after a "resume" continues the same turn (burst rule)
    "duck": True,              # emit "duck" / "resume" (orchestrators may ignore them)
    "floor_window_ms": 1500.0,  # noise floor = quietest frame in this window (+ floor_offset_db)
    "floor_offset_db": 3.0,
    "initial_floor_db": -70.0,  # floor assumed before the mic has been heard for floor_window_ms
}

BACKCHANNEL_RE = re.compile(
    r"^(?:(?:mm+|m+|hmm+|hm+|mhm+|mm-?hm+|uh-?huh|uh+-?hu+h|yeah|yep|yup|yes|ok(?:ay)?|right|sure|"
    r"i see|got it|cool|nice|wow|oh|ah|aha|uh|um)[\s,.!?-]*)+$")

_NFFT = 1024
_FREQS = np.fft.rfftfreq(_NFFT, 1.0 / SR)
_BAND_ALL = _FREQS >= 60.0
_BAND_HI = _FREQS >= 1000.0
_WIN = np.hanning(FRAME)
_LAG_LO, _LAG_HI = int(SR / 400), int(SR / 70)


def is_backchannel(text: str) -> bool:
    """True if a transcript is only a backchannel ("Mm-hmm.", "yeah", "okay, right")."""
    t = text.strip().lower()
    return bool(t) and bool(BACKCHANNEL_RE.match(t))


def frame_features(frame: np.ndarray, prev: Optional[np.ndarray] = None) -> dict:
    """Level, voicing and spectral shape of one 20 ms frame (prev frame helps low pitch)."""
    x = frame.astype(np.float64) / 32768.0
    e = float(np.mean(x * x))
    db = 10.0 * np.log10(e + 1e-12)
    spec = np.abs(np.fft.rfft(x * _WIN, _NFFT)) ** 2
    tot = float(spec[_BAND_ALL].sum()) + 1e-20
    hi = float(spec[_BAND_HI].sum()) / tot
    cen = float((spec[_BAND_ALL] * _FREQS[_BAND_ALL]).sum()) / tot
    # Voicing on a 40 ms window (this frame + the previous one), unbiased autocorrelation.
    w = x if prev is None else np.concatenate([prev.astype(np.float64) / 32768.0, x])
    w = w - w.mean()
    n = len(w)
    hi_lag = min(_LAG_HI, n // 2)
    r = np.fft.irfft(np.abs(np.fft.rfft(w, 2 * n)) ** 2)[: hi_lag + 1]
    if r[0] <= 0:
        per = 0.0
    else:
        lags = np.arange(_LAG_LO, hi_lag + 1)
        rr = r[_LAG_LO: hi_lag + 1] * n / (n - lags) / r[0]
        per = float(np.clip(rr.max(), 0.0, 1.0)) if len(rr) else 0.0
    return {"db": db, "hi": hi, "cen": cen, "per": per}


class BargeIn:
    def __init__(self, cfg: Optional[dict] = None) -> None:
        cfg = dict(cfg or {})
        if "min_ms" in cfg and "long_ms" not in cfg:
            cfg["long_ms"] = cfg.pop("min_ms")
        self.cfg = {**DEFAULTS, **cfg}
        n = max(1, int(self.cfg["floor_window_ms"] / FRAME_MS))
        self._levels: deque = deque([self.cfg["initial_floor_db"] - self.cfg["floor_offset_db"]] * n, maxlen=n)
        self.floor_db = self.cfg["initial_floor_db"]   # noise floor estimate, survives reset()
        self._prev: Optional[np.ndarray] = None
        self.last: dict = {}
        self._resumed: Optional[tuple] = None   # (t_ms, speech_ms) of the last "resume"
        self._was_speaking = False
        self.reset()

    # -- state -------------------------------------------------------------
    def reset(self) -> None:
        self.active = False            # an episode is open
        self.onset_ms: Optional[float] = None
        self.speech_ms = 0.0
        self.voiced_ms = 0.0
        self.gap_ms = 0.0
        self.bursts = 0
        self.burst_ms = 0.0            # length of the current burst
        self.first_burst_ms = 0.0
        self.open_n = 0
        self.fric_n = 0
        self.ducked = False
        self.fired = False
        self._pending: Optional[str] = None

    def on_transcript(self, text: str) -> None:
        if not self.active or self.fired:
            return
        self._pending = "resume" if is_backchannel(text) else "barge_in"

    # -- per frame ---------------------------------------------------------
    def _is_speech(self, f: dict) -> tuple[bool, bool, bool]:
        c = self.cfg
        loud = f["db"] > c["speech_dbfs"] and f["db"] > self.floor_db + c["snr_db"]
        voiced = loud and f["per"] >= c["voicing"]
        fric = loud and f["hi"] >= c["fric_hi"] and f["cen"] >= c["fric_cen"]
        return voiced or fric, voiced, fric

    def _track_floor(self, db: float) -> None:
        # Minimum statistics: speech has gaps, steady noise (fan, hiss) does not, so the
        # quietest recent frame is the floor. Steady noise never counts as speech.
        self._levels.append(db)
        self.floor_db = min(self._levels) + self.cfg["floor_offset_db"]

    def _decide(self, kind: str, reason: str, t_ms: float) -> str:
        self.last = {"kind": kind, "reason": reason, "onset_ms": self.onset_ms, "t_ms": t_ms,
                     "speech_ms": self.speech_ms}
        return kind

    def _accumulate(self, t_ms: float, f: dict, speech: bool, voiced: bool, fric: bool) -> None:
        c = self.cfg
        if not self.active:
            if not speech:
                return
            self.active = True
            self.onset_ms = t_ms - FRAME_MS
            self.bursts = 1
            if self._resumed and t_ms - self._resumed[0] <= c["rejoin_ms"]:
                # more speech right after a short sound: the user is still going ("no, ... I meant")
                self.bursts, self.first_burst_ms = 2, self._resumed[1]
            self._resumed = None
        if speech:
            if self.gap_ms >= c["burst_gap_ms"]:
                if self.bursts == 1:
                    self.first_burst_ms = self.burst_ms
                self.bursts += 1
                self.burst_ms = 0.0
            self.gap_ms = 0.0
            self.speech_ms += FRAME_MS
            self.voiced_ms += FRAME_MS if voiced else 0.0
            self.burst_ms += FRAME_MS
            # consecutive open frames: a vowel, not the breathy "h" inside "mm-hmm"
            self.open_n = self.open_n + 1 if (voiced and f["hi"] >= c["open_hi"]) else 0
            if fric:
                self.fric_n += 1
        else:
            self.gap_ms += FRAME_MS
            self.open_n = 0

    def _reason(self) -> Optional[str]:
        """Why the open episode cannot be a backchannel, or None."""
        c = self.cfg
        if not self.active:
            return None
        has_voice = self.voiced_ms >= c["min_voiced_ms"]   # a hiss or a click alone is not a word
        if self.open_n >= c["open_frames"]:
            return "open"
        if self.fric_n >= c["fric_frames"] and has_voice:
            return "fric"
        if (self.bursts >= 2 and self.first_burst_ms >= c["first_burst_min_ms"]
                and self.burst_ms >= c["burst_min_ms"] and has_voice):
            return "burst"
        if self.voiced_ms >= c["long_ms"]:
            return "long"
        return None

    def feed(self, frame: np.ndarray, t_ms: float, assistant_speaking: bool) -> Optional[str]:
        f = frame_features(frame, self._prev)
        self._prev = frame
        self._track_floor(f["db"])
        speech, voiced, fric = self._is_speech(f)
        c = self.cfg

        if not assistant_speaking:
            # The reply is over (or was flushed). Keep following the user's speech quietly,
            # so a reply that starts while they are still talking stops at once.
            self._resumed = None
            self._was_speaking = False
            if self.ducked or self.fired:
                self.reset()
            self._accumulate(t_ms, f, speech, voiced, fric)
            if self.active and self.gap_ms >= c["end_ms"]:
                self.reset()
            return None
        if not self._was_speaking:
            self._was_speaking = True
            if self.active and self.gap_ms > c["carry_gap_ms"]:
                self.reset()           # the user had finished: that was their turn, not an interruption
        if self.fired:
            return None

        if self._pending and self.active:
            kind, self._pending = self._pending, None
            if kind == "barge_in":
                self.fired = True
                return self._decide("barge_in", "transcript", t_ms)
            out = self._decide("resume", "transcript", t_ms) if self.ducked else None
            self.reset()
            return out

        self._accumulate(t_ms, f, speech, voiced, fric)
        reason = self._reason()
        if reason:
            self.fired = True
            return self._decide("barge_in", reason, t_ms)

        if self.active and self.gap_ms >= c["end_ms"]:
            out = self._decide("resume", "ended", t_ms) if self.ducked else None
            self._resumed = (t_ms, self.speech_ms)
            self.reset()
            return out

        if c["duck"] and not self.ducked and self.speech_ms >= c["duck_ms"] and self.voiced_ms > 0:
            self.ducked = True
            return self._decide("duck", "onset", t_ms)
        return None


__all__ = ["BargeIn", "DEFAULTS", "frame_features", "is_backchannel", "FRAME"]
