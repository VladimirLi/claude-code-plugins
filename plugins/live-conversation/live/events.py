"""Shared clock, audio constants and the event log. Owned by the orchestrator piece.

Every module stamps times with `Clock.ms`: the mic sample clock (samples fed
since session start / 24000 * 1000), as bench/SPEC.md defines it.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

SR = 24000            # every module speaks 24 kHz mono int16
FRAME = 480           # 20 ms mic frame
FRAME_MS = 20.0


def dbfs(x: np.ndarray) -> float:
    """RMS level of an int16 (or float in [-1, 1]) block in dBFS."""
    if len(x) == 0:
        return -120.0
    f = x.astype(np.float64)
    if x.dtype == np.int16:
        f /= 32768.0
    return float(20 * np.log10(np.sqrt(np.mean(f * f)) + 1e-12))


class Clock:
    """Mic sample clock. Only the Mic implementation advances it."""

    def __init__(self) -> None:
        self.samples = 0

    def advance(self, n: int) -> None:
        self.samples += n

    @property
    def ms(self) -> float:
        return self.samples * 1000.0 / SR


@dataclass
class Event:
    t_ms: float
    event: str
    turn: Optional[int]
    extra: dict

    def to_json(self) -> dict:
        d = {"t_ms": round(self.t_ms, 2), "event": self.event, "turn": self.turn}
        d.update(self.extra)
        return d


class EventLog:
    """Collects SPEC events. `emit` stamps with the clock unless t_ms is given."""

    def __init__(self, clock: Clock, echo: bool = False,
                 listener: Optional[Callable[[Event], None]] = None) -> None:
        self.clock = clock
        self.events: list[Event] = []
        self.echo = echo
        self.listener = listener

    def emit(self, event: str, turn: Optional[int] = None, t_ms: Optional[float] = None, **extra) -> Event:
        e = Event(self.clock.ms if t_ms is None else t_ms, event, turn, extra)
        self.events.append(e)
        if self.echo:
            print(json.dumps(e.to_json()), file=sys.stderr, flush=True)
        if self.listener:
            self.listener(e)
        return e

    def write(self, path: str) -> None:
        with open(path, "w") as f:
            for e in sorted(self.events, key=lambda e: e.t_ms):
                f.write(json.dumps(e.to_json()) + "\n")
