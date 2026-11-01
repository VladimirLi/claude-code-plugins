#!/usr/bin/env python3
"""Make a blind A/B folder for one piece: ours vs the cached reference ("the bar").

Usage:
  python3 bench/blind.py --piece <key> --round <n> --ours-label A|B --out <dir> --mapping-out <file>
                         [--ours-run DIR] [--bar-run DIR] [--scenarios id,id] [--config cfg.json] [--no-whisper]
  python3 bench/blind.py <run_1> <run_2> <out>      (legacy: random labels, key next to <out>)

Pieces: endpointing, stt, brain, tts, bargein, turntaking, e2e.

Steps:
 1. Unless --ours-run is given, runs bench/run_ours.py for the piece (mode and
    scenarios from bench/piece_metrics.py) into bench/runs/.
 2. Finds the newest cached reference run (run.json "system": "bar") that has
    the needed scenario folders, unless --bar-run is given. If there is none,
    it writes nothing to <out>, records the ours run and its metrics in
    --mapping-out with "reference_missing": true, and exits with code 3.
 3. Copies the scenarios both sides have into <out>/A and <out>/B with identical
    file names. WAVs are decoded and rewritten as plain 24 kHz mono PCM16 (no
    tags); events and transcripts keep only SPEC fields; names of models,
    voices, vendors and file paths are replaced by "[redacted]". run.json,
    score.json, piece/ and logs are not copied.
 4. Computes <out>/A/metrics.json and <out>/B/metrics.json with the same code
    (bench/piece_metrics.py) on the anonymized copies, and writes <out>/TASK.md
    with the piece's section of SPEC.md.
 5. Writes the label mapping only to --mapping-out.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import wave

import numpy as np

BENCH = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(BENCH)
sys.path.insert(0, BENCH)
import piece_metrics as pm  # noqa: E402

SR = 24000
KEEP_EVENTS = {"session_start", "user_speech_start", "user_speech_end", "turn_end_detected", "final_transcript",
               "first_reply_text", "first_reply_audio", "reply_audio_end", "barge_in_detected", "playback_stopped",
               "x_interim_transcript", "x_no_reply", "x_reply_shorter_than_offset", "x_turn_submitted",
               "x_first_sentence", "x_reply_done", "x_text_available"}
KEEP_EVENT_FIELDS = {"t_ms", "event", "turn", "text", "filler", "spoken_text"}
KEEP_TURN_FIELDS = {"role", "turn", "reply_to", "script", "asr_text", "text_generated", "text_spoken",
                    "interrupted", "fillers"}
REDACT = re.compile(
    r"\b(claude(?: code)?|anthropic|openai|chat ?gpt|gpt[-\w.]*|azure|realtime api|haiku|sonnet|opus|"
    r"samantha|alloy|shimmer|"
    r"gpt-live-1|gpt-realtime)\b", re.I)
PATHS = re.compile(r"(/Users/[^\s\"']+|/private/[^\s\"']+|/tmp/[^\s\"']+)")


def redact(s):
    if not isinstance(s, str):
        return s
    return REDACT.sub("[redacted]", PATHS.sub("[path]", s))


# -- finding runs -----------------------------------------------------------------------
def needed(piece: str, only: list | None) -> list[str]:
    if only:
        return only
    if piece == "tts":
        ids = [t["id"] for t in json.load(open(os.path.join(BENCH, "tts_texts.json")))["texts"]]
        return ids + [i + "_stream" for i in ids]
    return pm.PIECE_SCENARIOS.get(piece) or [s["id"] for s in json.load(open(os.path.join(pm.SCEN, "index.json")))]


def serves(run: str, sid: str, mode: str) -> bool:
    d = os.path.join(run, sid)
    if not os.path.isdir(d):
        return False
    has = lambda f: os.path.exists(os.path.join(d, f))
    if mode == "e2e":
        return has("assistant.wav") and has("mic.wav") and has("events.jsonl")
    if mode == "tts":
        return has("assistant.wav")
    return has("transcript.json") and not has("mic.wav")      # brain: text only


def find_bar(piece: str, ids: list[str], runs_dir: str) -> str | None:
    mode = pm.PIECE_MODE[piece]
    best, best_n = None, 0
    if not os.path.isdir(runs_dir):
        return None
    cands = []
    for name in os.listdir(runs_dir):
        run = os.path.join(runs_dir, name)
        try:
            meta = json.load(open(os.path.join(run, "run.json")))
        except Exception:
            continue
        if meta.get("system") != "bar":
            continue
        cands.append((os.path.getmtime(os.path.join(run, "run.json")), run))
    for _, run in sorted(cands, reverse=True):
        n = sum(serves(run, s, mode) for s in ids)
        if n > best_n:
            best, best_n = run, n
    return best


def run_ours(piece: str, ids: list[str], config: str | None, runs_dir: str, rnd) -> str:
    mode = pm.PIECE_MODE[piece]
    run_id = f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-ours-{piece}-r{rnd}"
    argv = [sys.executable, os.path.join(BENCH, "run_ours.py"), "--mode", mode, "--run-id", run_id,
            "--runs-dir", runs_dir]
    if pm.PIECE_SCENARIOS.get(piece) or ids:
        base = sorted({i[:-7] if i.endswith("_stream") else i for i in ids})
        argv += ["--scenarios", ",".join(base)]
    if config:
        argv += ["--config", config]
    r = subprocess.run(argv, cwd=PLUGIN, stdout=subprocess.PIPE, text=True)
    if r.returncode != 0:
        sys.exit(f"run_ours.py failed ({r.returncode})")
    return r.stdout.strip().splitlines()[-1]


# -- anonymizing -------------------------------------------------------------------------
def decode_wav(path: str) -> np.ndarray:
    try:
        with wave.open(path) as w:
            if w.getsampwidth() == 2:
                x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
                if w.getnchannels() > 1:
                    x = x.reshape(-1, w.getnchannels()).mean(axis=1)
                if w.getframerate() != SR:
                    n = int(round(len(x) * SR / w.getframerate()))
                    x = np.interp(np.arange(n) * (w.getframerate() / SR), np.arange(len(x)), x.astype(np.float64))
                return np.clip(np.round(x), -32768, 32767).astype(np.int16)
    except wave.Error:
        pass
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "x.raw")
        subprocess.run(["ffmpeg", "-loglevel", "error", "-i", path, "-ac", "1", "-ar", str(SR), "-f", "s16le",
                        "-map_metadata", "-1", out], check=True)
        return np.fromfile(out, dtype="<i2")


def write_plain_wav(path: str, x: np.ndarray) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(x.astype("<i2").tobytes())


def anonymize(src: str, dst: str) -> None:
    os.makedirs(dst, exist_ok=True)
    for f in sorted(os.listdir(src)):
        p = os.path.join(src, f)
        if not os.path.isfile(p):
            continue                       # piece/ and other folders are not copied
        if f.endswith(".wav"):
            write_plain_wav(os.path.join(dst, f), decode_wav(p))
        elif f == "events.jsonl":
            rows = []
            for line in open(p):
                if not line.strip():
                    continue
                e = json.loads(line)
                if e.get("event") not in KEEP_EVENTS:
                    continue
                e = {k: (redact(v) if k in ("text", "spoken_text") else v) for k, v in e.items() if k in KEEP_EVENT_FIELDS}
                # 10 ms grid for both sides, so clock granularity does not tell the systems apart
                e["t_ms"] = float(round(float(e["t_ms"]) / 10.0) * 10)
                if e["event"] == "session_start":
                    e["turn"] = None
                rows.append(e)
            rows.sort(key=lambda e: e["t_ms"])
            with open(os.path.join(dst, f), "w") as o:
                for e in rows:
                    o.write(json.dumps(e) + "\n")
        elif f == "transcript.json":
            t = json.load(open(p))
            turns = []
            for row in t.get("turns", []):
                r = {k: v for k, v in row.items() if k in KEEP_TURN_FIELDS}
                for k in ("asr_text", "text_generated", "text_spoken", "script"):
                    if k in r:
                        r[k] = redact(r[k])
                if "fillers" in r:
                    r["fillers"] = [redact(x) for x in r["fillers"] or []]
                turns.append(r)
            json.dump({"turns": turns}, open(os.path.join(dst, f), "w"), indent=2)
        # anything else (run.json, score.json, logs, notes) is dropped


def spec_section(piece: str) -> str:
    spec = open(os.path.join(BENCH, "SPEC.md")).read()
    head = "End-to-end evaluation" if piece == "e2e" else piece
    m = re.search(rf"^#{{2,3}} {re.escape(head)}\n(.*?)(?=^#{{2,3}} )", spec + "\n## ", re.S | re.M)
    body = m.group(1).strip() if m else ""
    return (f"# Blind comparison: {piece}\n\nTwo systems, A and B, got the same input. Judge which one feels more "
            f"natural as a live conversation partner for this piece, by measurement. Each side has metrics.json "
            f"(same code for both) and per-scenario folders (assistant.wav, mic.wav, timeline.wav at 24 kHz PCM16, "
            f"events.jsonl, transcript.json where the piece has them). Times are ms on the mic sample clock.\n\n"
            f"## Piece definition\n\n{body}\n")


def main() -> None:
    if len(sys.argv) == 4 and not sys.argv[1].startswith("-"):
        r1, r2, out = sys.argv[1:4]
        label = random.SystemRandom().choice("AB")
        key = os.path.abspath(out) + ".key.json"
        sys.argv = [sys.argv[0], "--piece", "e2e", "--round", "0", "--ours-label", label, "--out", out,
                    "--mapping-out", key, "--ours-run", r1, "--bar-run", r2]
    ap = argparse.ArgumentParser()
    ap.add_argument("--piece", required=True, choices=sorted(pm.PIECE_MODE))
    ap.add_argument("--round", required=True)
    ap.add_argument("--ours-label", required=True, choices=["A", "B"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--mapping-out", required=True)
    ap.add_argument("--ours-run")
    ap.add_argument("--bar-run")
    ap.add_argument("--scenarios")
    ap.add_argument("--config")
    ap.add_argument("--runs-dir", default=os.path.join(BENCH, "runs"))
    ap.add_argument("--no-whisper", action="store_true")
    a = ap.parse_args()
    if os.path.exists(a.out) and os.listdir(a.out):
        sys.exit(f"{a.out} exists and is not empty; use a fresh folder")
    only = a.scenarios.split(",") if a.scenarios else None
    if a.piece == "tts" and only:
        only = sorted(set(only) | {i + "_stream" for i in only if not i.endswith("_stream")})
    ids = needed(a.piece, only)
    ours = a.ours_run or run_ours(a.piece, ids, a.config, a.runs_dir, a.round)
    bar = a.bar_run or find_bar(a.piece, ids, a.runs_dir)
    mode = pm.PIECE_MODE[a.piece]
    mapping = {"piece": a.piece, "round": a.round, "ours_run": os.path.abspath(ours),
               "bar_run": os.path.abspath(bar) if bar else None, "created": dt.datetime.now(dt.timezone.utc).isoformat()}
    common = [s for s in ids if serves(ours, s, mode) and bar and serves(bar, s, mode)]
    if not common:
        mapping["reference_missing"] = True
        mapping["ours_metrics"] = pm.compute(a.piece, ours, use_whisper=not a.no_whisper,
                                             only=[s for s in ids if serves(ours, s, mode)])
        os.makedirs(os.path.dirname(os.path.abspath(a.mapping_out)), exist_ok=True)
        json.dump(mapping, open(a.mapping_out, "w"), indent=2, default=float)
        print(f"no reference run with scenarios {ids} for piece {a.piece}; ours run: {ours}; "
              f"ours metrics in {a.mapping_out}", file=sys.stderr)
        print(json.dumps(mapping["ours_metrics"]["summary"], default=float))
        sys.exit(3)
    other = "B" if a.ours_label == "A" else "A"
    sides = {a.ours_label: ours, other: bar}
    os.makedirs(a.out, exist_ok=True)
    summaries = {}
    for label in ("A", "B"):
        root = os.path.join(a.out, label)
        os.makedirs(root, exist_ok=True)
        for s in common:
            anonymize(os.path.join(sides[label], s), os.path.join(root, s))
        m = pm.compute(a.piece, root, use_whisper=not a.no_whisper)
        json.dump(m, open(os.path.join(root, "metrics.json"), "w"), indent=2, default=float)
        summaries[label] = m["summary"]
    open(os.path.join(a.out, "TASK.md"), "w").write(spec_section(a.piece))
    mapping.update({a.ours_label: "ours", other: "bar", "scenarios": common,
                    "skipped": [s for s in ids if s not in common]})
    os.makedirs(os.path.dirname(os.path.abspath(a.mapping_out)), exist_ok=True)
    json.dump(mapping, open(a.mapping_out, "w"), indent=2)
    print(json.dumps({"out": a.out, "scenarios": common, "summary": summaries}, default=float))


if __name__ == "__main__":
    main()
