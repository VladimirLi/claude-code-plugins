#!/usr/bin/env python3
"""Validate a run folder (see bench/SPEC.md) and compute the end-to-end metrics.

Usage: python3 bench/score.py <run_dir> [--scenarios-dir bench/scenarios]
Writes <run_dir>/score.json and prints a short table. Exit code 1 if the folder
breaks the format contract.

All times are on the session audio clock (ms since session sample 0).
Latency is measured acoustically from assistant.wav; the events.jsonl value is
only a cross-check (disagreement > 20 ms is reported as a warning).
"""
import json, os, sys, wave
import numpy as np

SR = 24000
FRAME_MS = 10
ONSET_DBFS = -45.0      # assistant audio frame RMS above this = audible
STOP_DBFS = -50.0       # assistant audio below this for >= STOP_HOLD_MS = stopped
STOP_HOLD_MS = 100
DEAD_AIR_MS = 1000
REQUIRED = ["assistant.wav", "mic.wav", "timeline.wav", "events.jsonl", "transcript.json"]
EVENTS = {"session_start", "user_speech_start", "user_speech_end", "turn_end_detected",
          "final_transcript", "first_reply_text", "first_reply_audio", "reply_audio_end",
          "barge_in_detected", "playback_stopped"}


def read_wav(path):
    with wave.open(path) as w:
        if w.getframerate() != SR or w.getsampwidth() != 2:
            raise ValueError(f"{path}: must be {SR} Hz PCM16")
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64) / 32768.0
        if w.getnchannels() != 1:
            raise ValueError(f"{path}: must be mono")
    return x


def frame_db(x):
    n = SR * FRAME_MS // 1000
    m = len(x) // n
    f = x[: m * n].reshape(m, n)
    return 20 * np.log10(np.sqrt((f ** 2).mean(axis=1)) + 1e-12)


def first_audible_after(db, t_ms, x):
    """Onset of the first audible 10 ms frame after t_ms, refined to the first sample above the onset level."""
    i0 = max(0, int(t_ms // FRAME_MS))
    hits = np.nonzero(db[i0:] > ONSET_DBFS)[0]
    if len(hits) == 0:
        return None
    n = SR * FRAME_MS // 1000
    f = int(i0 + hits[0])
    lo = max(f * n, int(t_ms * SR / 1000))
    seg = np.nonzero(np.abs(x[lo:(f + 1) * n]) > 10 ** (ONSET_DBFS / 20))[0]
    s = lo + (int(seg[0]) if len(seg) else 0)
    return round(s * 1000.0 / SR, 2)


def stopped_after(db, t_ms):
    """First time after t_ms where assistant audio stays below STOP_DBFS for STOP_HOLD_MS."""
    hold = STOP_HOLD_MS // FRAME_MS
    quiet = db < STOP_DBFS
    for i in range(max(0, int(t_ms // FRAME_MS)), len(quiet) - hold):
        if quiet[i:i + hold].all():
            return int(i * FRAME_MS)
    return None


def score_scenario(d, sc):
    problems, warnings = [], []
    for f in REQUIRED:
        if not os.path.exists(os.path.join(d, f)):
            problems.append(f"missing {f}")
    if problems:
        return {"problems": problems}
    a = read_wav(os.path.join(d, "assistant.wav"))
    mic = read_wav(os.path.join(d, "mic.wav"))
    tl = read_wav(os.path.join(d, "timeline.wav"))
    if not (len(a) == len(mic) == len(tl)):
        problems.append(f"length mismatch assistant={len(a)} mic={len(mic)} timeline={len(tl)}")
    ev = [json.loads(l) for l in open(os.path.join(d, "events.jsonl")) if l.strip()]
    for e in ev:
        if e.get("event") not in EVENTS and not str(e.get("event", "")).startswith("x_"):
            problems.append(f"unknown event {e.get('event')} (custom events must start with x_)")
        if not isinstance(e.get("t_ms"), (int, float)):
            problems.append(f"event without numeric t_ms: {e}")
    db = frame_db(a)

    def evs(name, turn=None):
        return [e for e in ev if e.get("event") == name and (turn is None or e.get("turn") == turn)]

    turns = []
    for t in sc["turns"]:
        n = t["turn"]
        se = evs("user_speech_end", n)
        ss = evs("user_speech_start", n)
        if not se or not ss:
            problems.append(f"turn {n}: missing user_speech_start/user_speech_end")
            continue
        end_ms, start_ms = se[0]["t_ms"], ss[0]["t_ms"]
        r = {"turn": n, "user_speech_start_ms": start_ms, "user_speech_end_ms": end_ms,
             "barge_in": t["barge_in"], "backchannel": t["backchannel"]}
        # Interrupted / overlapping audio from the previous reply must stop before we look for the new reply.
        search_from = end_ms
        if t["barge_in"] or t["backchannel"]:
            stop = stopped_after(db, start_ms)
            r["assistant_stop_ms"] = stop
            r["stop_delay_ms"] = None if stop is None else stop - start_ms
            if t["barge_in"] and stop is not None:
                search_from = max(end_ms, stop)
        if not t["backchannel"]:
            onset = first_audible_after(db, search_from, a)
            r["first_reply_audio_ms"] = onset
            r["v2v_latency_ms"] = None if onset is None else onset - end_ms
            fra = evs("first_reply_audio", n)
            if fra and onset is not None and abs(fra[0]["t_ms"] - onset) > 20:
                warnings.append(f"turn {n}: first_reply_audio event {fra[0]['t_ms']} vs acoustic {onset}")
            ted = [e["t_ms"] for e in evs("turn_end_detected", n)]
            r["endpoint_delay_ms"] = (min(x for x in ted if x >= start_ms) - end_ms) if any(x >= start_ms for x in ted) else None
            # A turn-end decision inside the user's own speech/pauses is a false cut-off.
            r["false_cutoffs"] = sum(1 for x in ted if start_ms <= x < end_ms)
            ft = evs("final_transcript", n)
            r["stt_delay_ms"] = (ft[-1]["t_ms"] - end_ms) if ft else None
        # Assistant audio while the user is speaking (not counting a reply being interrupted).
        i0, i1 = int(start_ms // FRAME_MS), int(end_ms // FRAME_MS)
        r["overlap_ms"] = int((db[i0:i1] > ONSET_DBFS).sum() * FRAME_MS)
        turns.append(r)

    # Dead air inside assistant replies: silent stretches > 1 s between first reply audio and reply end.
    dead = []
    for r in turns:
        if r.get("first_reply_audio_ms") is None:
            continue
        nxt = [x["user_speech_start_ms"] for x in turns if x["user_speech_start_ms"] > r["user_speech_end_ms"]]
        end = min(nxt) if nxt else len(db) * FRAME_MS
        seg = db[int(r["first_reply_audio_ms"] // FRAME_MS): int(end // FRAME_MS)] > ONSET_DBFS
        on = np.nonzero(seg)[0]
        if len(on) > 1:
            gaps = np.diff(on) * FRAME_MS
            dead += [int(g) for g in gaps if g > DEAD_AIR_MS]
    return {"turns": turns, "dead_air_ms": dead, "problems": problems, "warnings": warnings}


def main():
    run = sys.argv[1]
    scen_dir = sys.argv[sys.argv.index("--scenarios-dir") + 1] if "--scenarios-dir" in sys.argv else \
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenarios")
    out, lat, bad = {}, [], False
    for sid in sorted(os.listdir(run)):
        d = os.path.join(run, sid)
        sp = os.path.join(scen_dir, sid, "scenario.json")
        if not os.path.isdir(d) or not os.path.exists(sp):
            continue
        s = score_scenario(d, json.load(open(sp)))
        out[sid] = s
        bad |= bool(s["problems"])
        for t in s.get("turns", []):
            v = t.get("v2v_latency_ms")
            if v is not None and not t["barge_in"]:
                lat.append(v)
            print(f"{sid} turn {t['turn']}: v2v={t.get('v2v_latency_ms')} endpoint={t.get('endpoint_delay_ms')} "
                  f"stt={t.get('stt_delay_ms')} stop={t.get('stop_delay_ms')} cutoffs={t.get('false_cutoffs')}")
        for p in s["problems"]:
            print(f"{sid}: PROBLEM {p}")
    pos = [v for v in lat if v >= 0]
    summ = {"turns_scored": len(lat),
            "v2v_p50_ms": float(np.median(pos)) if pos else None,
            "v2v_p90_ms": float(np.percentile(pos, 90)) if pos else None,
            "replies_before_user_finished": sum(1 for v in lat if v < 0)}
    out["_summary"] = summ
    json.dump(out, open(os.path.join(run, "score.json"), "w"), indent=2)
    print(json.dumps(summ))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
