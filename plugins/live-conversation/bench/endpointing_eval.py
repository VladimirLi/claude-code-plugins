#!/usr/bin/env python3
"""Endpointing alone: scenario turns -> Endpointer in real time -> SPEC events + piece metrics.

Faster than `run_ours.py --mode e2e` (no brain, no TTS), for tuning the
endpointing piece. Every turn of every scenario is fed as the mic, in real time
(the probes run in threads, so their latency counts), with `gap_s` of the bench
noise floor between turns. Writes `<out>/<scenario>/events.jsonl` (SPEC
format, read by piece_metrics.py) and `<out>/<scenario>/endpointer_trace.json`.

Usage (from the plugin folder):
  .venv/bin/python bench/endpointing_eval.py [--scenarios s05_um_pause,...] [--out DIR]
                                             [--config cfg.json] [--reps N] [--fast]
--fast feeds without real-time pacing (probe latency then does not count; for smoke tests only).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import multiprocessing as mp
import os
import sys
import time
import wave

import numpy as np

BENCH = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(BENCH)
sys.path.insert(0, PLUGIN)
sys.path.insert(0, BENCH)

SCEN = os.path.join(BENCH, "scenarios")
SR, FRAME = 24000, 480


def read_wav(p: str) -> np.ndarray:
    with wave.open(p) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").copy()


def run_scenario(args) -> str:
    sid, out, cfg, realtime, lead_s, gap_s = args
    from live.endpointing import Endpointer
    sc = json.load(open(os.path.join(SCEN, sid, "scenario.json")))
    noise = read_wav(os.path.join(SCEN, "noise_floor_24k.wav"))
    ep = Endpointer(cfg)
    time.sleep(1.5)                                   # warm-up runs in the background, like before a session
    parts, ev = [], [{"t_ms": 0.0, "event": "session_start", "turn": None}]
    pos = int(lead_s * SR)
    parts.append(("noise", pos))
    for t in sc["turns"]:
        x = read_wav(os.path.join(SCEN, sid, t["audio"]["24k"]["file"]))
        a = t["audio"]["24k"]
        ev.append({"t_ms": (pos + a["speech_start_sample"]) * 1000.0 / SR, "event": "user_speech_start", "turn": t["turn"]})
        ev.append({"t_ms": (pos + a["speech_end_sample"]) * 1000.0 / SR, "event": "user_speech_end", "turn": t["turn"]})
        parts.append(("wav", x))
        pos += len(x)
        g = int(gap_s * SR)
        parts.append(("noise", g))
        pos += g
    mic = []
    npos = 0
    for kind, v in parts:
        if kind == "wav":
            mic.append(v)
        else:
            idx = (np.arange(v) + npos) % len(noise)
            npos += v
            mic.append(noise[idx])
    mic = np.concatenate(mic)
    mic = mic[: len(mic) - len(mic) % FRAME]
    t0 = time.monotonic()
    for i in range(0, len(mic), FRAME):
        if realtime:
            due = t0 + (i + FRAME) / SR
            d = due - time.monotonic()
            if d > 0:
                time.sleep(d)
        t_ms = (i + FRAME) * 1000.0 / SR
        for d in ep.feed(mic[i:i + FRAME], t_ms):
            if d.kind == "turn_end":
                ev.append({"t_ms": round(d.t_ms, 2), "event": "turn_end_detected", "turn": None, "source": d.source})
            else:
                ev.append({"t_ms": round(d.t_ms, 2), "event": "x_speech_start_detected", "turn": None})
    d = os.path.join(out, sid)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "events.jsonl"), "w") as f:
        for e in sorted(ev, key=lambda e: e["t_ms"]):
            f.write(json.dumps(e) + "\n")
    json.dump(ep.trace, open(os.path.join(d, "endpointer_trace.json"), "w"), indent=1, default=str)
    return sid


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--config", default="")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--fast", action="store_true")
    ap.add_argument("--gap-s", type=float, default=3.5)
    ap.add_argument("--jobs", type=int, default=7)
    a = ap.parse_args()
    import piece_metrics
    sids = a.scenarios.split(",") if a.scenarios else [s["id"] for s in json.load(open(os.path.join(SCEN, "index.json")))]
    cfg = json.load(open(a.config)).get("endpointing", {}) if a.config else {}
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = a.out or os.path.join(BENCH, "runs", f"{stamp}-endpointing")
    summaries = []
    for rep in range(1, a.reps + 1):
        out = base if a.reps == 1 else f"{base}-rep{rep}"
        with mp.get_context("spawn").Pool(min(a.jobs, len(sids))) as pool:
            list(pool.imap_unordered(run_scenario, [(s, out, cfg, not a.fast, 1.0, a.gap_s) for s in sids]))
        res = piece_metrics.compute("endpointing", out, use_whisper=False)
        json.dump(res, open(os.path.join(out, "endpointing_metrics.json"), "w"), indent=2, default=float)
        for sid, s in sorted(res["scenarios"].items()):
            for t in s["turns"]:
                print(f"rep{rep} {sid:22s} t{t['turn']} delay={t['endpoint_delay_ms']} cutoffs={t['false_cutoffs']}"
                      f" missed={t['missed']}")
        print(f"rep{rep}", json.dumps(res["summary"]))
        summaries.append(res["summary"])
        print(out)
    if a.reps > 1:
        print("median endpoint_delay over reps:", float(np.median([s["endpoint_delay_ms_median"] for s in summaries])),
              "cutoffs:", sum(s["false_cutoffs_total"] for s in summaries))


if __name__ == "__main__":
    main()
