#!/usr/bin/env python3
"""Piece-level metrics (bench/SPEC.md "Piece-level evaluation"), the same code for both sides.

Usage: python3 bench/piece_metrics.py <piece> <run_dir> [--no-whisper] [--out metrics.json]
Pieces: endpointing, stt, brain, tts, bargein, turntaking, e2e.

Reads only the SPEC run folder format (events.jsonl, transcript.json, WAVs) and
bench/scenarios, so it works on the anonymized copies blind.py makes.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import wave

import numpy as np

BENCH = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BENCH)
import score  # noqa: E402

SCEN = os.path.join(BENCH, "scenarios")
SR = 24000
ONSET_DBFS = -45.0
FILLERS = {"um", "uh", "mm-hmm", "mhm", "hmm", "huh", "mm", "hmm-mm"}
PIECE_SCENARIOS = {
    "endpointing": None, "stt": None, "e2e": None, "brain": None,
    "bargein": ["s08_bargein_early", "s09_bargein_late", "s13_backchannel"],
    "turntaking": ["s07_followup_chain", "s08_bargein_early", "s09_bargein_late", "s10_correction",
                   "s11_silence_yeah", "s13_backchannel"],
    "tts": None,
}
PIECE_MODE = {"endpointing": "e2e", "stt": "e2e", "bargein": "e2e", "turntaking": "e2e", "e2e": "e2e",
              "brain": "brain", "tts": "tts"}

# -- text ---------------------------------------------------------------------------
_ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def num_words(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else " " + _ONES[n % 10])
    if n < 1000:
        return _ONES[n // 100] + " hundred" + ("" if n % 100 == 0 else " " + num_words(n % 100))
    if n < 1_000_000:
        return num_words(n // 1000) + " thousand" + ("" if n % 1000 == 0 else " " + num_words(n % 1000))
    return str(n)


def normalize(text: str) -> list[str]:
    t = text.lower().replace("-", " ").replace("’", "'")
    t = re.sub(r"\d+", lambda m: " " + num_words(int(m.group())) + " ", t)
    t = re.sub(r"[^a-z0-9' ]+", " ", t)
    return [w.strip("'") for w in t.split() if w.strip("'") and w not in FILLERS and w.replace("'", "") not in {"mmhmm", "mhm"}]


def edit_distance(a: list[str], b: list[str]) -> int:
    d = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(b) + 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j - 1] + 1, prev + (a[i - 1] != b[j - 1]))
            prev = cur
    return d[len(b)]


def wer(ref: str, hyp: str) -> tuple[float, int, int]:
    r, h = normalize(ref), normalize(hyp)
    e = edit_distance(r, h)
    return (e / len(r) if r else float(bool(h))), e, len(r)


# -- io -------------------------------------------------------------------------------
def events(d: str) -> list[dict]:
    p = os.path.join(d, "events.jsonl")
    return [json.loads(l) for l in open(p) if l.strip()] if os.path.exists(p) else []


def transcript(d: str) -> dict:
    p = os.path.join(d, "transcript.json")
    return json.load(open(p)) if os.path.exists(p) else {"turns": []}


def read_wav(p: str) -> np.ndarray:
    with wave.open(p) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64) / 32768.0


def ev_times(ev, name, turn=None):
    return [e["t_ms"] for e in ev if e.get("event") == name and (turn is None or e.get("turn") == turn)]


def med(xs):
    xs = [x for x in xs if x is not None]
    return float(np.median(xs)) if xs else None


# -- acoustic -------------------------------------------------------------------------
def frame_db(x, ms=10):
    n = SR * ms // 1000
    m = len(x) // n
    f = x[: m * n].reshape(m, n)
    return 20 * np.log10(np.sqrt((f ** 2).mean(axis=1)) + 1e-12)


def f0_track(x: np.ndarray) -> np.ndarray:
    win, hop = int(0.04 * SR), int(0.01 * SR)
    lo, hi = SR // 400, SR // 70
    out = []
    for s in range(0, len(x) - win, hop):
        f = x[s:s + win]
        if 20 * np.log10(np.sqrt((f ** 2).mean()) + 1e-12) < -40:
            continue
        f = f - f.mean()
        ac = np.fft.irfft(np.abs(np.fft.rfft(f, 2 * win)) ** 2)[:win]
        if ac[0] <= 0:
            continue
        ac = ac / ac[0]
        lag = lo + int(np.argmax(ac[lo:hi]))
        if ac[lag] > 0.5:
            out.append(SR / lag)
    return np.array(out)


def whisper_text(path: str) -> str | None:
    try:
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["whisper", path, "--model", "base.en", "--language", "en", "--output_format", "txt",
                            "--output_dir", d, "--fp16", "False"], capture_output=True, check=True, timeout=300)
            base = os.path.splitext(os.path.basename(path))[0]
            return open(os.path.join(d, base + ".txt")).read().strip()
    except Exception:
        return None


# -- per piece ------------------------------------------------------------------------
def scen(sid):
    p = os.path.join(SCEN, sid, "scenario.json")
    return json.load(open(p)) if os.path.exists(p) else None


def m_endpointing(d, sc):
    ev = events(d)
    ted = sorted(ev_times(ev, "turn_end_detected"))
    out = []
    for t in sc["turns"]:
        n = t["turn"]
        ss, se = ev_times(ev, "user_speech_start", n), ev_times(ev, "user_speech_end", n)
        if not ss or not se:
            continue
        s, e = ss[0], se[0]
        after = [x for x in ted if e <= x <= e + 3000]
        out.append({"turn": n, "false_cutoffs": sum(1 for x in ted if s <= x < e),
                    "endpoint_delay_ms": round(after[0] - e, 1) if after else None,
                    "missed": not after})
    return {"turns": out}


def m_stt(d, sc):
    ev = events(d)
    out = []
    for t in sc["turns"]:
        n = t["turn"]
        ft = [x for x in ev if x.get("event") == "final_transcript" and x.get("turn") == n]
        se = ev_times(ev, "user_speech_end", n)
        hyp = ft[-1].get("text", "") if ft else ""
        w, errs, nref = wer(t["text"], hyp)
        out.append({"turn": n, "ref": t["text"], "hyp": hyp, "wer": round(w, 4), "errors": errs, "ref_words": nref,
                    "stt_delay_ms": round(ft[-1]["t_ms"] - se[0], 1) if ft and se else None})
    return {"turns": out}


def _replies(d):
    return [t for t in transcript(d).get("turns", []) if t.get("role") == "assistant"]


def m_e2e_like(d, sc):
    s = score.score_scenario(d, sc)
    ev = events(d)
    for t in s.get("turns", []):
        n = t["turn"]
        fa = [x for x in ev if x.get("event") == "first_reply_audio" and x.get("turn") == n and not x.get("filler")]
        t["v2v_content_ms"] = round(fa[0]["t_ms"] - t["user_speech_end_ms"], 1) if fa else None
    try:
        fc = _filler_content(d, ev)
    except Exception:
        fc = {}
    for t in s.get("turns", []):
        # the acoustic estimate is a fallback: a `first_reply_audio` event with filler false is exact
        if t["turn"] in fc and fc[t["turn"]][1] is not None and t.get("v2v_content_ms") is None:
            t["v2v_content_ms"] = round(fc[t["turn"]][1] - t["user_speech_end_ms"], 1)
    fillers = [f for r in _replies(d) for f in (r.get("fillers") or [])]
    s["fillers"] = fillers
    s["repeated_fillers"] = sorted({f for f in fillers if fillers.count(f) > 1})
    s["replies"] = [{"reply_to": r.get("reply_to"), "text_spoken": r.get("text_spoken"),
                     "interrupted": r.get("interrupted")} for r in _replies(d)]
    if sc["id"].startswith("s11") and len(sc["turns"]) > 1:
        # nagging = audible assistant audio inside the scripted silence before the user's "Yeah."
        # The harness starts turn 2 `offset_ms` after reply 1 stopped being audible, so the
        # silence window is [turn-2 start - offset_ms, turn-2 start) for both sides.
        try:
            a = read_wav(os.path.join(d, "assistant.wav"))
            db = frame_db(a)
            t2 = sc["turns"][1]
            ss2 = ev_times(ev, "user_speech_start", t2["turn"])[0]
            turn_start = ss2 - t2["audio"]["24k"]["speech_start_sample"] * 1000.0 / SR
            w0 = turn_start - t2["start"].get("offset_ms", 0)
            seg = db[int(max(0, w0) // 10): int(turn_start // 10)]
            s["nagging_ms"] = int((seg > ONSET_DBFS).sum() * 10)
            s["silence_window_ms"] = [round(w0, 1), round(turn_start, 1)]
        except Exception:
            pass
    # Gaps between consecutive assistant audio segments after a filler: filler end -> content start.
    try:
        s["filler_to_content_gap_ms"] = _filler_gaps(d, ev)
    except Exception:
        pass
    return s


def _filler_content(d, ev):
    """Turns whose first audio was a filler: {turn: (filler_end_ms, content_onset_ms)}.

    Content onset is acoustic. If the sink drained after the filler (`reply_audio_end` within
    3 s of the filler start), the content starts at the first audible sample after that. If the
    content was already queued, it starts after the filler's last pause: a filler of k phrases
    ("Ah, got it, so," = 3) has k pauses >= 120 ms, the last one being its trailing pause."""
    a = read_wav(os.path.join(d, "assistant.wav"))
    db = frame_db(a)
    loud = db > ONSET_DBFS
    fillers = {r.get("reply_to"): (r.get("fillers") or [None])[0] for r in _replies(d)}
    out = {}
    for e in ev:
        if e.get("event") != "first_reply_audio" or not e.get("filler"):
            continue
        n, t0 = e.get("turn"), e["t_ms"]
        i0 = int(t0 // 10)
        rae = [x["t_ms"] for x in ev if x.get("event") == "reply_audio_end" and t0 < x["t_ms"] <= t0 + 3000]
        if rae:
            j = int(rae[0] // 10)
            nxt = np.nonzero(loud[j:])[0]
            out[n] = (rae[0], (j + int(nxt[0])) * 10.0 if len(nxt) else None)
            continue
        text = fillers.get(n) or ""
        k = max(1, len(re.findall(r"[,.!?]+", text)))
        quiet, seen, i = 0, 0, i0 + (int(np.argmax(loud[i0:])) if loud[i0:].any() else 0)
        fend = None
        while i < len(loud):
            if loud[i]:
                if quiet >= 12:
                    seen += 1
                    if seen == k:
                        out[n] = (fend, i * 10.0)
                        break
                quiet = 0
            else:
                if quiet == 0:
                    fend = i * 10.0
                quiet += 1
            i += 1
    return out


def _filler_gaps(d, ev):
    """Silence between the end of the filler and the first audible content sample, per turn (ms)."""
    return {str(n): (round(c - f) if c is not None and f is not None else None)
            for n, (f, c) in _filler_content(d, ev).items()}


def _ngrams(words, n=4):
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def _after_interrupt(spoken: str, prev_generated: str, nxt: str) -> dict:
    """How the reply after an interruption relates to what the user already heard."""
    sp, nx, pg = normalize(spoken or ""), normalize(nxt or ""), normalize(prev_generated or "")
    head = nx[:40]
    rep = _ngrams(sp) & _ngrams(head)
    covered = set()
    for g in rep:
        for i in range(len(head) - 3):
            if tuple(head[i:i + 4]) == g:
                covered.update(range(i, i + 4))
    return {
        "next_reply_first_words": " ".join((nxt or "").split()[:14]),
        # share of the next reply's first 40 words that repeat 4-word runs the user already heard
        "next_reply_repeat_ratio": round(len(covered) / len(head), 3) if head else None,
        # the next reply starts like the interrupted one (restarting from the top)
        "next_reply_restarts": bool(pg[:3]) and nx[:3] == pg[:3],
        "next_reply_words": len(nx),
    }


def m_bargein(d, sc):
    s = m_e2e_like(d, sc)
    ev = events(d)
    reps = _replies(d)
    for t in s.get("turns", []):
        if t.get("barge_in") or t.get("backchannel"):
            ss = t["user_speech_start_ms"]
            ps = [x["t_ms"] for x in ev if x.get("event") == "playback_stopped" and x["t_ms"] >= ss - 20]
            bd = [x["t_ms"] for x in ev if x.get("event") == "barge_in_detected" and x["t_ms"] >= ss - 20]
            # event-based: time the system flushed the speaker (the acoustic stop_delay_ms can be
            # earlier when the interrupted audio happens to pause between phrases)
            t["flush_delay_ms"] = round(ps[0] - ss, 1) if ps else None
            t["detect_delay_ms"] = round(bd[0] - ss, 1) if bd else None
            # what the user lives through: audible assistant audio (> -45 dBFS, 10 ms frames) from their
            # speech onset to the flush (or, with no flush, to the acoustic stop). Unlike the delays it
            # does not count time the assistant was already silent.
            end = ps[0] if ps else t.get("assistant_stop_ms")
            if end is not None and t.get("barge_in"):
                db = frame_db(read_wav(os.path.join(d, "assistant.wav")))
                t["talk_over_ms"] = int((db[int(ss // 10): int(end // 10)] > ONSET_DBFS).sum() * 10)
            prev = [r for r in reps if r.get("reply_to") == t["turn"] - 1]
            nxt = [r for r in reps if r.get("reply_to") == t["turn"]]
            if prev and nxt:
                t.update(_after_interrupt(prev[-1].get("text_spoken") or "", prev[-1].get("text_generated") or "",
                                          nxt[0].get("text_generated") or ""))
                t["spoken_before_stop_words"] = len(normalize(prev[-1].get("text_spoken") or ""))
        if not t.get("backchannel"):
            continue
        ss = t["user_speech_start_ms"]
        stopped = [x for x in ev if x.get("event") == "playback_stopped" and ss <= x["t_ms"] <= ss + 1500]
        sd = t.get("stop_delay_ms")
        t["false_barge_in"] = bool(stopped) or (sd is not None and sd <= 1500 and _new_reply_after(ev, t["turn"]))
        if sd is not None:
            a = read_wav(os.path.join(d, "assistant.wav"))
            db = frame_db(a)
            stop = t["assistant_stop_ms"]
            nxt = np.nonzero(db[int(stop // 10):] > ONSET_DBFS)[0]
            t["pause_ms"] = int(nxt[0] * 10) if len(nxt) else None
    return s


def _new_reply_after(ev, turn):
    return any(x.get("event") == "first_reply_audio" and x.get("turn") == turn for x in ev)


def m_brain(d, sc):
    ev = events(d)
    reps = {r.get("reply_to"): r for r in _replies(d)}
    out = []
    for t in sc["turns"]:
        n = t["turn"]
        r = reps.get(n, {})
        text = r.get("text_generated", "") or ""
        sub, fs = ev_times(ev, "x_turn_submitted", n), ev_times(ev, "x_first_sentence", n)
        marks = len(re.findall(r"[*#`]", text)) + len(re.findall(r"(?m)^\s*(\d+\.|-)\s", text)) + \
            len(re.findall(r"https?://|(?:\w+/)+\w+\.\w+", text))
        out.append({"turn": n, "user": t["text"], "reply": text,
                    "time_to_first_sentence_ms": round(fs[0] - sub[0], 1) if sub and fs else None,
                    "words": len(text.split()), "sentences": len(re.findall(r"[.!?]+(\s|$)", text)),
                    "markdown_or_list_marks": marks, "asks_follow_up": text.strip().endswith("?"),
                    "uses_context": None})
    return {"turns": out}


def m_tts(d, text, use_whisper):
    p = os.path.join(d, "assistant.wav")
    if not os.path.exists(p):
        return {"problems": ["missing assistant.wav"]}
    x = read_wav(p)
    ev = events(d)
    t0 = (ev_times(ev, "x_text_available") or [0.0])[0]
    db = frame_db(x)
    aud = np.nonzero(db > ONSET_DBFS)[0]
    r = {"text": text}
    if len(aud) == 0:
        r["problems"] = ["no audible audio"]
        return r
    first = score.first_audible_after(db, t0, x)
    r["first_audio_ms"] = round(first - t0, 1) if first is not None else None
    gaps = np.diff(aud) * 10
    r["internal_gaps_ms"] = [int(g) for g in gaps if g > 250]
    f0 = f0_track(x)
    r["f0_median_hz"] = round(float(np.median(f0)), 1) if len(f0) else None
    r["f0_std_semitones"] = round(float(np.std(12 * np.log2(f0 / np.median(f0)))), 2) if len(f0) > 5 else None
    r["energy_std_db"] = round(float(np.std(db[aud])), 2)
    voiced_s = len(aud) * 0.01
    r["speaking_rate_wpm"] = round(len(text.split()) / voiced_s * 60, 1) if voiced_s else None
    r["clipped_fraction"] = float(np.mean(np.abs(x) >= 32767 / 32768))
    r["clicks"] = int(np.sum(np.abs(np.diff(x)) > 0.5))
    r["dc_offset"] = round(float(np.mean(x)), 6)
    if use_whisper:
        hyp = whisper_text(p)
        r["whisper_text"] = hyp
        r["whisper_wer"] = round(wer(text, hyp)[0], 4) if hyp is not None else None
    return r


def compute(piece: str, run_dir: str, use_whisper: bool = True, only: list | None = None) -> dict:
    out = {"piece": piece, "scenarios": {}}
    if piece == "tts":
        texts = {t["id"]: t["text"] for t in json.load(open(os.path.join(BENCH, "tts_texts.json")))["texts"]}
        for name in sorted(os.listdir(run_dir)):
            base = name[:-7] if name.endswith("_stream") else name
            if base in texts and os.path.isdir(os.path.join(run_dir, name)) and (not only or name in only):
                out["scenarios"][name] = m_tts(os.path.join(run_dir, name), texts[base], use_whisper)
        rows = list(out["scenarios"].values())
        out["summary"] = {
            "first_audio_ms_median": med([r.get("first_audio_ms") for r in rows]),
            "whisper_wer_mean": (float(np.mean([r["whisper_wer"] for r in rows if r.get("whisper_wer") is not None]))
                                 if any(r.get("whisper_wer") is not None for r in rows) else None),
            "f0_std_semitones_median": med([r.get("f0_std_semitones") for r in rows]),
            "energy_std_db_median": med([r.get("energy_std_db") for r in rows]),
            "gaps_over_250ms_total": sum(len(r.get("internal_gaps_ms", [])) for r in rows),
            "clicks_total": sum(r.get("clicks", 0) for r in rows),
        }
        return out
    fn = {"endpointing": m_endpointing, "stt": m_stt, "brain": m_brain, "bargein": m_bargein,
          "turntaking": m_e2e_like, "e2e": m_e2e_like}[piece]
    for sid in sorted(os.listdir(run_dir)):
        d = os.path.join(run_dir, sid)
        sc = scen(sid)
        if not os.path.isdir(d) or sc is None or (only and sid not in only):
            continue
        out["scenarios"][sid] = fn(d, sc)
    turns = [t for s in out["scenarios"].values() for t in s.get("turns", [])]
    summ: dict = {"scenarios": len(out["scenarios"]), "turns": len(turns)}
    if piece == "endpointing":
        summ.update(false_cutoffs_total=sum(t["false_cutoffs"] for t in turns),
                    endpoint_delay_ms_median=med([t["endpoint_delay_ms"] for t in turns]),
                    missed_total=sum(t["missed"] for t in turns))
    elif piece == "stt":
        e, n = sum(t["errors"] for t in turns), sum(t["ref_words"] for t in turns)
        summ.update(wer_pooled=round(e / n, 4) if n else None,
                    stt_delay_ms_median=med([t["stt_delay_ms"] for t in turns]))
    elif piece == "brain":
        summ.update(time_to_first_sentence_ms_median=med([t["time_to_first_sentence_ms"] for t in turns]),
                    words_median=med([t["words"] for t in turns]),
                    markdown_or_list_marks_total=sum(t["markdown_or_list_marks"] for t in turns))
    else:
        lat = [t.get("v2v_latency_ms") for t in turns if not t.get("barge_in") and not t.get("backchannel")]
        pos = [v for v in lat if v is not None and v >= 0]
        summ.update(v2v_p50_ms=med(pos), v2v_p90_ms=float(np.percentile(pos, 90)) if pos else None,
                    v2v_content_p50_ms=med([t.get("v2v_content_ms") for t in turns if t.get("v2v_content_ms", -1) is not None and t.get("v2v_content_ms", -1) >= 0]),
                    replies_before_user_finished=sum(1 for v in lat if v is not None and v < 0),
                    no_reply_turns=sum(1 for v in lat if v is None),
                    stop_delay_ms_median=med([t.get("stop_delay_ms") for t in turns if t.get("barge_in")]),
                    flush_delay_ms_median=med([t.get("flush_delay_ms") for t in turns if t.get("barge_in")]),
                    talk_over_ms_median=med([t.get("talk_over_ms") for t in turns if t.get("barge_in")]),
                    false_barge_ins=sum(1 for t in turns if t.get("false_barge_in")),
                    dead_air_total=sum(len(s.get("dead_air_ms", [])) for s in out["scenarios"].values()),
                    overlap_ms_total=sum(t.get("overlap_ms", 0) for t in turns if not t.get("barge_in") and not t.get("backchannel")),
                    fillers_total=sum(len(s.get("fillers", [])) for s in out["scenarios"].values()))
        ep = {sid: m_endpointing(os.path.join(run_dir, sid), scen(sid)) for sid in out["scenarios"]}
        et = [t for s in ep.values() for t in s["turns"]]
        summ.update(false_cutoffs_total=sum(t["false_cutoffs"] for t in et),
                    endpoint_delay_ms_median=med([t["endpoint_delay_ms"] for t in et]))
    out["summary"] = summ
    return out


if __name__ == "__main__":
    piece, run = sys.argv[1], sys.argv[2]
    res = compute(piece, run, use_whisper="--no-whisper" not in sys.argv)
    dst = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else None
    s = json.dumps(res, indent=2, default=float)
    if dst:
        open(dst, "w").write(s)
    print(json.dumps(res["summary"], default=float))
