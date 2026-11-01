"""Echo score: how much of the mic is the reply's own audio (FLOOR-SPEC.md "Words", step 1).

    echo_corr(mic, ref, max_lag) -> 0..1

`ref` is what the sink played, starting `max_lag` samples before `mic` starts (len(mic) + max_lag
samples). The score is the best normalized correlation of the mic with the speaker signal 0 to
max_lag samples earlier: near 1 when the mic only hears the speaker (no echo cancellation), low
when someone talks over it. The same measure as bin/live-review's echo_corr, by FFT.
"""
from __future__ import annotations

import numpy as np


def echo_corr(mic: np.ndarray, ref: np.ndarray, max_lag: int) -> float:
    m = mic.astype(np.float64)
    r = ref.astype(np.float64)
    n = len(m)
    if n == 0 or len(r) < n + max_lag:
        return 0.0
    r = r[:n + max_lag]
    m = m - m.mean()
    r = r - r.mean()
    em = float((m * m).sum())
    if em <= 0:
        return 0.0
    size = 1 << int(np.ceil(np.log2(len(r) + n)))
    c = np.fft.irfft(np.fft.rfft(r, size) * np.conj(np.fft.rfft(m, size)), size)[:max_lag + 1]
    sq = np.concatenate([[0.0], np.cumsum(r * r)])
    er = sq[n:n + max_lag + 1] - sq[:max_lag + 1]          # energy of r[k:k+n], k = 0..max_lag
    with np.errstate(divide="ignore", invalid="ignore"):
        corr = np.where(er > 0, c / np.sqrt(em * er), 0.0)
    return float(max(0.0, np.nanmax(corr)))
