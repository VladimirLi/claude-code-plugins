#!/usr/bin/env python3
"""Build bench/scenarios/ from bench/scenarios.src.json.

For every user turn: synthesise each segment with macOS `say`, trim the
synthesiser's own leading/trailing silence, join the segments with real
silences, add a quiet microphone noise floor, and write PCM16 mono WAVs at
24 kHz and 16 kHz. The exact sample where speech ends (ground truth "user
stopped speaking") is measured on the clean speech before noise is added.

Needs: macOS `say`, sox, python3 with numpy. Deterministic (fixed noise seed).
Usage: python3 bench/make_scenarios.py [--only s05_um_pause]
"""
import json, os, subprocess, sys, tempfile, wave, shutil
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "scenarios.src.json")
OUT = os.path.join(HERE, "scenarios")

SR = 24000
SR16 = 16000
LEAD_IN_MS = 200          # noise before the first word of a turn WAV
TAIL_MS = 300             # noise after the last word of a turn WAV
NOISE_DBFS = -65.0        # RMS of the mic noise floor
SPEECH_THRESH = 10 ** (-48 / 20)  # |x| above this (full scale = 1.0) counts as speech
TRIM_KEEP_MS = 30         # keep this much of the synthesiser's decay after the last speech sample


def say_to_array(text, voice, rate):
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "seg.wav")
        subprocess.run(["say", "-v", voice, "-r", str(rate), "--file-format=WAVE",
                        f"--data-format=LEI16@{SR}", "-o", p, text], check=True)
        with wave.open(p) as w:
            assert w.getframerate() == SR and w.getnchannels() == 1 and w.getsampwidth() == 2
            x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64) / 32768.0
    return x


def speech_bounds(x):
    """First and last+1 sample index whose magnitude exceeds the speech threshold."""
    idx = np.nonzero(np.abs(x) > SPEECH_THRESH)[0]
    if len(idx) == 0:
        raise RuntimeError("no speech found in synthesised segment")
    return int(idx[0]), int(idx[-1]) + 1


def noise(n, rng):
    sigma = 10 ** (NOISE_DBFS / 20)
    return rng.normal(0.0, sigma, n)


def to_pcm16(x):
    return np.clip(np.round(x * 32768.0), -32768, 32767).astype("<i2")


def write_wav(path, x, sr):
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(to_pcm16(x).tobytes())


def resample_16k(path24, path16):
    subprocess.run(["sox", "-D", path24, "-r", str(SR16), path16, "rate", "-v"], check=True)


def ms(samples, sr):
    return round(samples * 1000.0 / sr, 3)


def build_turn(turn, voice, rate, rng):
    keep = int(SR * TRIM_KEEP_MS / 1000)
    clean = [np.zeros(int(SR * LEAD_IN_MS / 1000))]
    pos = len(clean[0])
    segs, pauses = [], []
    for i, seg in enumerate(turn["segments"]):
        x = say_to_array(seg["text"], seg.get("voice", voice), seg.get("rate_wpm", rate))
        a, b = speech_bounds(x)
        x = x[a:min(len(x), b + keep)]
        start, end = pos, pos + (b - a)
        segs.append({"text": seg["text"], "speech_start_sample": start, "speech_end_sample": end})
        clean.append(x); pos += len(x)
        if i < len(turn["segments"]) - 1:
            p = int(SR * seg.get("pause_ms", 0) / 1000)
            pauses.append({"after_segment": i, "silence_start_sample": end,
                           "silence_end_sample": pos + p,
                           "silence_ms": ms(pos + p - end, SR), "scripted_pause_ms": seg.get("pause_ms", 0)})
            clean.append(np.zeros(p)); pos += p
    clean.append(np.zeros(int(SR * TAIL_MS / 1000)))
    clean = np.concatenate(clean)
    # Re-measure the true end on the assembled clean signal (guards against off-by-one in assembly).
    _, true_end = speech_bounds(clean)
    assert abs(true_end - segs[-1]["speech_end_sample"]) <= 1, (true_end, segs[-1])
    return clean + noise(len(clean), rng), segs, pauses


def main():
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    src = json.load(open(SRC))
    os.makedirs(OUT, exist_ok=True)
    rng_floor = np.random.default_rng(7)
    write_wav(os.path.join(OUT, "noise_floor_24k.wav"), noise(SR * 10, rng_floor), SR)
    resample_16k(os.path.join(OUT, "noise_floor_24k.wav"), os.path.join(OUT, "noise_floor_16k.wav"))
    index = []
    for sc in src["scenarios"]:
        if only and sc["id"] != only:
            continue
        d = os.path.join(OUT, sc["id"])
        shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
        voice = sc.get("voice", src["default_voice"]); rate = sc.get("rate_wpm", src["default_rate_wpm"])
        rng = np.random.default_rng(sum(map(ord, sc["id"])))
        turns_out = []
        for n, turn in enumerate(sc["turns"], 1):
            x, segs, pauses = build_turn(turn, voice, rate, rng)
            f24, f16 = f"turn_{n:02d}_24k.wav", f"turn_{n:02d}_16k.wav"
            write_wav(os.path.join(d, f24), x, SR)
            resample_16k(os.path.join(d, f24), os.path.join(d, f16))
            first, last = segs[0]["speech_start_sample"], segs[-1]["speech_end_sample"]
            t = {
                "turn": n,
                "text": " ".join(s["text"] for s in segs),
                "segments": [dict(s, speech_start_ms=ms(s["speech_start_sample"], SR),
                                  speech_end_ms=ms(s["speech_end_sample"], SR)) for s in segs],
                "pauses": [dict(p, silence_start_ms=ms(p["silence_start_sample"], SR),
                                silence_end_ms=ms(p["silence_end_sample"], SR)) for p in pauses],
                "start": turn["start"],
                "barge_in": bool(turn.get("barge_in", False)),
                "backchannel": bool(turn.get("backchannel", False)),
                "voice": voice, "rate_wpm": rate,
                "audio": {
                    "24k": {"file": f24, "sample_rate": SR, "num_samples": len(x),
                            "speech_start_sample": first, "speech_end_sample": last},
                    "16k": {"file": f16, "sample_rate": SR16,
                            "num_samples": int(round(len(x) * SR16 / SR)),
                            "speech_start_sample": int(round(first * SR16 / SR)),
                            "speech_end_sample": int(round(last * SR16 / SR))},
                },
                "speech_start_ms": ms(first, SR),
                "speech_end_ms": ms(last, SR),
            }
            turns_out.append(t)
        meta = {"id": sc["id"], "kind": sc["kind"], "description": sc["description"],
                "turns": turns_out,
                "generator": {"tool": "macOS say + sox", "noise_floor_dbfs": NOISE_DBFS,
                              "speech_threshold_dbfs": -48, "lead_in_ms": LEAD_IN_MS, "tail_ms": TAIL_MS}}
        json.dump(meta, open(os.path.join(d, "scenario.json"), "w"), indent=2)
        index.append({"id": sc["id"], "kind": sc["kind"], "turns": len(turns_out),
                      "speech_ms": [round(t["speech_end_ms"] - t["speech_start_ms"]) for t in turns_out]})
        print(sc["id"], [t["speech_end_ms"] for t in turns_out])
    if not only:
        json.dump(index, open(os.path.join(OUT, "index.json"), "w"), indent=2)


if __name__ == "__main__":
    main()
