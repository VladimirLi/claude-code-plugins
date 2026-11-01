"""Smart Turn v3.2 end-of-turn model, run with numpy only (part of the endpointing piece).

Smart Turn (pipecat-ai/smart-turn, BSD-2) is a Whisper-tiny encoder plus a
linear head. It reads the last 8 s of the user's audio (16 kHz) and returns
P(turn is complete). It hears prosody and grammar: "I was wondering if ..." or
"I want to, um ..." score low; "... handles errors." scores high.

onnxruntime has no wheel for the venv's Python (3.15), so this file holds:

- a minimal protobuf reader for the .onnx file,
- a numpy interpreter for the 17 ops the fp32 graph uses,
- the Whisper log-mel feature extractor (same math as transformers'
  WhisperFeatureExtractor(chunk_length=8), as smart-turn's inference.py uses).

The fp32 model (32 MB) is downloaded once to ~/.cache/live-conversation/ from a
pinned Hugging Face revision and checked against its sha256.
"""
from __future__ import annotations

import hashlib
import math
import os
import struct
import urllib.request
from typing import Optional

import numpy as np

MODEL_REV = "f766f81d3cfdf7737ac64aad813d91bbfd56bf93"
MODEL_FILE = "smart-turn-v3.2-gpu.onnx"          # fp32 weights; the "cpu" file is int8
MODEL_SHA256 = "ab8dc64b88713f90b571c15b714bd1330e6c883cad8763dacf65c9376dc539be"
MODEL_URL = f"https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/{MODEL_REV}/{MODEL_FILE}"
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "live-conversation")

SR16 = 16000
N_SECONDS = 8


# -- model file -------------------------------------------------------------------
def model_path(download: bool = True) -> Optional[str]:
    """Local path of the model, downloading it once if needed. None if unavailable."""
    p = os.environ.get("LIVE_SMART_TURN_MODEL") or os.path.join(CACHE_DIR, MODEL_FILE)
    if os.path.exists(p):
        return p
    if not download:
        return None
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".part"
    with urllib.request.urlopen(MODEL_URL, timeout=60) as r, open(tmp, "wb") as f:
        h = hashlib.sha256()
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            h.update(b)
            f.write(b)
    if h.hexdigest() != MODEL_SHA256:
        os.remove(tmp)
        raise RuntimeError("smart-turn model checksum mismatch")
    os.replace(tmp, p)
    return p


# -- protobuf ---------------------------------------------------------------------
def _varint(b, i):
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        s += 7
        if c < 0x80:
            return r, i


def _fields(b):
    i, n = 0, len(b)
    while i < n:
        k, i = _varint(b, i)
        f, w = k >> 3, k & 7
        if w == 0:
            v, i = _varint(b, i)
        elif w == 1:
            v = b[i:i + 8]
            i += 8
        elif w == 5:
            v = b[i:i + 4]
            i += 4
        elif w == 2:
            ln, i = _varint(b, i)
            v = b[i:i + ln]
            i += ln
        else:
            raise ValueError(f"protobuf wire type {w}")
        yield f, w, v


def _packed_varints(v, w):
    if w == 0:
        return [v]
    out, i = [], 0
    while i < len(v):
        x, i = _varint(v, i)
        out.append(x)
    return out


def _signed(x):
    return x - (1 << 64) if x >= 1 << 63 else x


_DTYPES = {1: np.float32, 2: np.uint8, 3: np.int8, 6: np.int32, 7: np.int64, 9: np.bool_,
           10: np.float16, 11: np.float64}


def _tensor(b) -> tuple[str, np.ndarray]:
    dims, dt, name, raw = [], 1, "", None
    f32, i64, i32 = [], [], []
    for f, w, v in _fields(b):
        if f == 1:
            dims += [_signed(x) for x in _packed_varints(v, w)]
        elif f == 2:
            dt = v
        elif f == 4:
            f32 += list(struct.unpack(f"<{len(v) // 4}f", bytes(v))) if w == 2 else [struct.unpack("<f", bytes(v))[0]]
        elif f == 5:
            i32 += [_signed(x) for x in _packed_varints(v, w)]
        elif f == 7:
            i64 += [_signed(x) for x in _packed_varints(v, w)]
        elif f == 8:
            name = bytes(v).decode()
        elif f == 9:
            raw = bytes(v)
        elif f == 14 and v == 1:
            raise ValueError("external tensor data is not supported")
    dtype = _DTYPES[dt]
    if raw is not None:
        a = np.frombuffer(raw, dtype=dtype).copy()
    elif dtype == np.float32:
        a = np.array(f32, dtype=dtype)
    elif dtype == np.int64:
        a = np.array(i64, dtype=dtype)
    else:
        a = np.array(i32, dtype=dtype)
    return name, a.reshape(dims) if dims else a.reshape(())


def _attr(b):
    name, val, typ = "", None, 0
    ints, floats = [], []
    for f, w, v in _fields(b):
        if f == 1:
            name = bytes(v).decode()
        elif f == 2:
            val = struct.unpack("<f", bytes(v))[0]
        elif f == 3:
            val = _signed(v)
        elif f == 4:
            val = bytes(v).decode()
        elif f == 5:
            val = _tensor(v)[1]
        elif f == 7:
            floats += list(struct.unpack(f"<{len(v) // 4}f", bytes(v))) if w == 2 else [struct.unpack("<f", bytes(v))[0]]
        elif f == 8:
            ints += [_signed(x) for x in _packed_varints(v, w)]
        elif f == 20:
            typ = v
    if typ == 7 or (val is None and ints):
        val = ints
    elif typ == 6 or (val is None and floats):
        val = floats
    return name, val


class OnnxGraph:
    """Loads an ONNX model and runs it with numpy (only the ops Smart Turn uses)."""

    def __init__(self, path: str) -> None:
        with open(path, "rb") as f:
            data = memoryview(f.read())
        graph = next(v for f, _, v in _fields(data) if f == 7)
        self.nodes, self.init, self.inputs, self.outputs = [], {}, [], []
        for f, _, v in _fields(graph):
            if f == 1:
                ins, outs, op, attrs = [], [], "", {}
                for ff, _, vv in _fields(v):
                    if ff == 1:
                        ins.append(bytes(vv).decode())
                    elif ff == 2:
                        outs.append(bytes(vv).decode())
                    elif ff == 4:
                        op = bytes(vv).decode()
                    elif ff == 5:
                        k, a = _attr(vv)
                        attrs[k] = a
                self.nodes.append((op, ins, outs, attrs))
            elif f == 5:
                k, a = _tensor(v)
                self.init[k] = a
            elif f in (11, 12):
                name = next(bytes(vv).decode() for ff, _, vv in _fields(v) if ff == 1)
                (self.inputs if f == 11 else self.outputs).append(name)
        self.inputs = [i for i in self.inputs if i not in self.init]
        for op, *_ in self.nodes:
            if not hasattr(self, "_op_" + op):
                raise ValueError(f"unsupported ONNX op {op}")

    def run(self, feeds: dict) -> list[np.ndarray]:
        env = dict(self.init)
        env.update(feeds)
        for op, ins, outs, attrs in self.nodes:
            args = [env[i] if i else None for i in ins]
            res = getattr(self, "_op_" + op)(attrs, *args)
            if not isinstance(res, tuple):
                res = (res,)
            for k, r in zip(outs, res):
                env[k] = r
        return [env[o] for o in self.outputs]

    # ops ------------------------------------------------------------------------
    @staticmethod
    def _op_Add(a, x, y): return x + y
    @staticmethod
    def _op_Mul(a, x, y): return x * y
    @staticmethod
    def _op_Div(a, x, y): return x / y if x.dtype.kind == "f" else x // y
    @staticmethod
    def _op_MatMul(a, x, y): return np.matmul(x, y)
    @staticmethod
    def _op_Tanh(a, x): return np.tanh(x)
    @staticmethod
    def _op_Sigmoid(a, x): return 1.0 / (1.0 + np.exp(-x))

    @staticmethod
    def _op_Erf(a, x):
        return _erf(x)

    @staticmethod
    def _op_Shape(a, x):
        s = np.array(x.shape, dtype=np.int64)
        return s[a.get("start", 0): a.get("end", len(s))]

    @staticmethod
    def _op_Reshape(a, x, shape):
        shape = [int(d) for d in shape]
        if not a.get("allowzero", 0):
            shape = [x.shape[i] if d == 0 else d for i, d in enumerate(shape)]
        return x.reshape(shape)

    @staticmethod
    def _op_Transpose(a, x):
        return np.transpose(x, a.get("perm"))

    @staticmethod
    def _op_Concat(a, *xs):
        return np.concatenate([x for x in xs if x is not None], axis=a["axis"])

    @staticmethod
    def _op_Slice(a, x, starts, ends, axes=None, steps=None):
        axes = range(len(starts)) if axes is None else axes
        steps = [1] * len(starts) if steps is None else steps
        sl = [slice(None)] * x.ndim
        for s, e, ax, st in zip(starts, ends, axes, steps):
            n = x.shape[int(ax)]
            s, e = int(max(min(s, n), -n - 1)), int(max(min(e, n), -n - 1))
            sl[int(ax)] = slice(s, e, int(st))
        return x[tuple(sl)]

    @staticmethod
    def _op_Softmax(a, x):
        ax = a.get("axis", -1)
        e = np.exp(x - x.max(axis=ax, keepdims=True))
        return e / e.sum(axis=ax, keepdims=True)

    @staticmethod
    def _op_ReduceSum(a, x, axes=None):
        keep = bool(a.get("keepdims", 1))
        if axes is None or len(axes) == 0:
            return x if a.get("noop_with_empty_axes", 0) else x.sum(keepdims=keep)
        return x.sum(axis=tuple(int(i) for i in axes), keepdims=keep)

    @staticmethod
    def _op_LayerNormalization(a, x, scale, bias=None):
        ax = a.get("axis", -1)
        axes = tuple(range(ax if ax >= 0 else x.ndim + ax, x.ndim))
        mu = x.mean(axis=axes, keepdims=True)
        d = x - mu
        var = (d * d).mean(axis=axes, keepdims=True)
        y = d / np.sqrt(var + a.get("epsilon", 1e-5)) * scale
        return y + bias if bias is not None else y

    @staticmethod
    def _op_Gemm(a, x, w, c=None):
        if a.get("transA", 0):
            x = x.T
        if a.get("transB", 0):
            w = w.T
        y = a.get("alpha", 1.0) * (x @ w)
        return y + a.get("beta", 1.0) * c if c is not None else y

    @staticmethod
    def _op_Conv(a, x, w, b=None):
        # 1-D convolution (N, C, L) via im2col; Smart Turn only has Whisper's two conv1d layers.
        if x.ndim != 3 or a.get("group", 1) != 1:
            raise ValueError("only 1-D, group=1 Conv is supported")
        k = w.shape[2]
        st = a.get("strides", [1])[0]
        dil = a.get("dilations", [1])[0]
        p0, p1 = (a.get("pads") or [0, 0])[:2]
        xp = np.pad(x, ((0, 0), (0, 0), (p0, p1)))
        lout = (xp.shape[2] - dil * (k - 1) - 1) // st + 1
        idx = np.arange(lout)[:, None] * st + np.arange(k)[None, :] * dil      # (lout, k)
        cols = xp[:, :, idx]                                                   # (N, C, lout, k)
        cols = cols.transpose(0, 2, 1, 3).reshape(x.shape[0], lout, -1)        # (N, lout, C*k)
        y = cols @ w.reshape(w.shape[0], -1).T                                 # (N, lout, O)
        if b is not None:
            y = y + b
        return np.ascontiguousarray(y.transpose(0, 2, 1))


def _erf(x: np.ndarray) -> np.ndarray:
    """Vectorised erf (Numerical Recipes erfc Chebyshev fit, |error| < 1.2e-7: float32 level)."""
    x64 = x.astype(np.float64)
    a = np.abs(x64)
    t = 1.0 / (1.0 + 0.5 * a)
    y = 1.0 - t * np.exp(-a * a - 1.26551223 + t * (1.00002368 + t * (0.37409196 + t * (0.09678418 + t * (
        -0.18628806 + t * (0.27886807 + t * (-1.13520398 + t * (1.48851587 + t * (-0.82215223 + t * 0.17087277)))))))))
    return (np.sign(x64) * y).astype(x.dtype)


# -- features ---------------------------------------------------------------------
def _mel_filters(n_fft: int = 400, n_mels: int = 80, sr: int = SR16) -> np.ndarray:
    """Slaney-style mel filter bank (librosa / transformers norm="slaney", mel_scale="slaney")."""
    def hz_to_mel(f):
        f = np.asarray(f, dtype=np.float64)
        mel = f / (200.0 / 3)
        log_t = f >= 1000.0
        return np.where(log_t, 15.0 + np.log(np.maximum(f, 1e-10) / 1000.0) / (math.log(6.4) / 27.0), mel)

    def mel_to_hz(m):
        m = np.asarray(m, dtype=np.float64)
        f = m * (200.0 / 3)
        return np.where(m >= 15.0, 1000.0 * np.exp((math.log(6.4) / 27.0) * (m - 15.0)), f)

    fft_f = np.linspace(0, sr / 2, 1 + n_fft // 2)
    mel_f = mel_to_hz(np.linspace(hz_to_mel(0.0), hz_to_mel(sr / 2), n_mels + 2))
    fdiff = np.diff(mel_f)
    ramps = mel_f[:, None] - fft_f[None, :]
    lower = -ramps[:-2] / fdiff[:-1, None]
    upper = ramps[2:] / fdiff[1:, None]
    w = np.maximum(0, np.minimum(lower, upper))
    w *= (2.0 / (mel_f[2:n_mels + 2] - mel_f[:n_mels]))[:, None]
    return w                                                                # (n_mels, 201)


_MEL = None
_WINDOW = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(400) / 400)               # periodic hann


def log_mel(audio16: np.ndarray) -> np.ndarray:
    """Whisper input features (1, 80, 800) for 16 kHz float audio, padded/truncated to 8 s on the left."""
    global _MEL
    if _MEL is None:
        _MEL = _mel_filters()
    n = SR16 * N_SECONDS
    x = audio16.astype(np.float64)
    x = x[-n:] if len(x) > n else np.pad(x, (n - len(x), 0))
    x = (x - x.mean()) / np.sqrt(x.var() + 1e-7)                             # do_normalize=True
    xp = np.pad(x, (200, 200), mode="reflect")                               # center=True
    frames = np.lib.stride_tricks.sliding_window_view(xp, 400)[::160]       # (801, 400)
    spec = np.abs(np.fft.rfft(frames * _WINDOW, axis=1)) ** 2               # (801, 201)
    mel = _MEL @ spec.T                                                     # (80, 801)
    lm = np.log10(np.maximum(mel, 1e-10))[:, :-1]
    lm = np.maximum(lm, lm.max() - 8.0)
    return ((lm + 4.0) / 4.0).astype(np.float32)[None]


def resample_24k_to_16k(x: np.ndarray) -> np.ndarray:
    """Band-limited 3:2 resample through the FFT (ideal low-pass at 8 kHz)."""
    x = x.astype(np.float64)
    n = len(x) - len(x) % 3
    if n == 0:
        return np.zeros(0)
    m = n * 2 // 3
    X = np.fft.rfft(x[:n])
    return np.fft.irfft(X[: m // 2 + 1], m) * (m / n)


class SmartTurn:
    """P(turn complete) for the user's audio so far."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.graph = OnnxGraph(path or model_path())

    def predict(self, audio16: np.ndarray) -> float:
        out = self.graph.run({self.graph.inputs[0]: log_mel(audio16)})[0]
        return float(np.asarray(out).reshape(-1)[0])

    def predict_24k(self, pcm24: np.ndarray) -> float:
        x = pcm24.astype(np.float64)
        if pcm24.dtype == np.int16:
            x /= 32768.0
        return self.predict(resample_24k_to_16k(x))
