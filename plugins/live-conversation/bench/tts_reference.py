#!/usr/bin/env python3
"""Record the bar for the tts piece: the realtime voice model says each text in bench/tts_texts.json.

Usage (from the plugin folder):
  .venv/bin/python bench/tts_reference.py [--ids t01_short,...] [--reps 2] [--out bench/reference/tts]

Per SPEC "tts": the session is opened and configured first (warm connection,
no turn detection), then the clock starts and `response.create` is sent with
instructions "Say exactly this, nothing else: <text>". Audio deltas go to a
wall-clock speaker (each delta at max(arrival, end of queue)), the same sink
bench/run_ours.py uses for ours. A take whose audio transcript has WER > 5%
against the text is re-recorded (up to 4 tries; the best take is kept).

Output: <out>/<id>/run<k>/ (assistant.wav, events.jsonl, transcript.json,
server_events.log) and bench/runs/<date>-bar-tts-rep<k>/ with run.json and one
symlink per text, so blind.py --piece tts finds it. Only whole-text folders are
recorded: the realtime model cannot be fed text word by word, so the `_stream`
variant has no bar counterpart.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import datetime
import json
import os
import subprocess
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import piece_metrics as pm  # noqa: E402
import reference_runner as rr  # noqa: E402

SR = 24000


async def take(text: str) -> dict:
    ws, dep, _created = await rr.connect()
    log = []
    try:
        await ws.send(json.dumps({"type": "session.update", "session": {
            "type": "realtime",
            "audio": {"input": {"format": {"type": "audio/pcm", "rate": SR}, "turn_detection": None},
                      "output": {"format": {"type": "audio/pcm", "rate": SR}}}}}))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), 15))
            if m["type"] == "session.updated":
                break
            if m["type"] == "error":
                raise RuntimeError(f"session.update failed: {m}")
        t0 = time.perf_counter()
        ms = lambda: (time.perf_counter() - t0) * 1000.0
        await ws.send(json.dumps({"type": "response.create", "response": {
            "instructions": f"Say exactly this, nothing else: {text}"}}))
        buf = np.zeros(SR * 60, np.int16)
        end, first, transcript = 0, None, ""
        events = [{"t_ms": 0.0, "event": "x_text_available", "turn": 1}]
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), 60))
            t = ms()
            ty = m["type"]
            if ty in ("response.output_audio.delta", "response.audio.delta"):
                pcm = np.frombuffer(base64.b64decode(m["delta"]), dtype="<i2")
                start = max(int(t * SR / 1000), end)
                if start + len(pcm) > len(buf):
                    buf = np.concatenate([buf, np.zeros(start + len(pcm) - len(buf) + SR, np.int16)])
                buf[start:start + len(pcm)] = pcm
                end = start + len(pcm)
                if first is None:
                    first = start * 1000.0 / SR
                    events.append({"t_ms": round(first, 2), "event": "first_reply_audio", "turn": 1, "filler": False})
                log.append({"t_ms": round(t, 1), "type": ty, "bytes": len(pcm) * 2})
                continue
            log.append({"t_ms": round(t, 1), **{k: v for k, v in m.items() if k != "delta" or ty.endswith("transcript.delta")}})
            if ty in ("response.output_audio_transcript.done", "response.audio_transcript.done"):
                transcript = m.get("transcript", "")
            elif ty == "response.done":
                break
            elif ty == "error":
                raise RuntimeError(f"server error: {m}")
        events.append({"t_ms": round(end * 1000.0 / SR, 2), "event": "reply_audio_end", "turn": 1})
        return {"deployment": dep, "audio": buf[: end + SR // 2], "events": events, "transcript": transcript,
                "server_log": log}
    finally:
        await ws.close()


def save(d: str, text: str, r: dict) -> None:
    os.makedirs(d, exist_ok=True)
    rr.write_wav(os.path.join(d, "assistant.wav"), r["audio"])
    with open(os.path.join(d, "events.jsonl"), "w") as f:
        for e in r["events"]:
            f.write(json.dumps(e) + "\n")
    json.dump({"turns": [{"role": "assistant", "reply_to": 1, "text_generated": r["transcript"],
                          "text_spoken": r["transcript"], "interrupted": False, "fillers": []}]},
              open(os.path.join(d, "transcript.json"), "w"), indent=2)
    with open(os.path.join(d, "server_events.log"), "w") as f:
        for e in r["server_log"]:
            f.write(json.dumps(e) + "\n")
    json.dump({"deployment": r["deployment"], "api_version": rr.API_VERSION, "text": text,
               "wer": r.get("wer")}, open(os.path.join(d, "run_meta.log"), "w"), indent=2)


async def amain() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids")
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--out", default=os.path.join(HERE, "reference", "tts"))
    a = ap.parse_args()
    texts = json.load(open(os.path.join(HERE, "tts_texts.json")))["texts"]
    ids = a.ids.split(",") if a.ids else None
    date = datetime.date.today().isoformat()
    for k in range(1, a.reps + 1):
        rep = os.path.join(HERE, "runs", f"{date}-bar-tts-rep{k}")
        os.makedirs(rep, exist_ok=True)
        dep = None
        for item in texts:
            if ids and item["id"] not in ids:
                continue
            best = None
            for _ in range(4):
                r = await take(item["text"])
                r["wer"] = pm.wer(item["text"], r["transcript"])[0]
                if best is None or r["wer"] < best["wer"]:
                    best = r
                if r["wer"] <= 0.05:
                    break
            dep = best["deployment"]
            d = os.path.join(a.out, item["id"], f"run{k}")
            save(d, item["text"], best)
            dst = os.path.join(rep, item["id"])
            if os.path.islink(dst):
                os.unlink(dst)
            os.symlink(os.path.relpath(d, rep), dst)
            fa = next(e["t_ms"] for e in best["events"] if e["event"] == "first_reply_audio")
            print(f"rep{k} {item['id']}: wer {best['wer']:.3f} first delta {fa:.0f} ms", flush=True)
        try:
            commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=HERE, text=True).strip()
        except Exception:
            commit = None
        json.dump({"system": "bar", "created": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   "git_commit": commit, "config": {"mode": "tts", "deployment": dep, "api_version": rr.API_VERSION,
                                                    "instructions": "Say exactly this, nothing else: <text>"}},
                  open(os.path.join(rep, "run.json"), "w"), indent=2)


if __name__ == "__main__":
    asyncio.run(amain())
