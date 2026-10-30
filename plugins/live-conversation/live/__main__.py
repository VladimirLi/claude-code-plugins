"""Live mode: `python -m live [--cwd DIR] [--config FILE] [--run-dir DIR]`. Talk; Ctrl-C to stop.

Prints one JSON event per line on stderr (the /live command shows them).

With --run-dir (the /live command passes one) the voice brain can hand coding
tasks to the Claude Code session (live/tasks.py), and the daemon writes its pid
to <run-dir>/daemon.pid. SIGTERM stops it cleanly, with its claude processes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys

from . import tasks
from .audio_io import LiveAudio
from .events import Clock, EventLog
from .orchestrator import Orchestrator, build
from .recorder import Recorder

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def main() -> None:
    ap = argparse.ArgumentParser(prog="live")
    ap.add_argument("--cwd", default=os.getcwd(), help="folder Claude Code works in")
    ap.add_argument("--config", help="JSON file with per-module config sections")
    ap.add_argument("--run-dir", help="folder shared with the /live mod (task handoff, pid)")
    a = ap.parse_args()
    cfg = json.load(open(a.config)) if a.config else {}
    cfg.setdefault("brain", {})["cwd"] = a.cwd
    if a.run_dir:
        os.makedirs(a.run_dir, exist_ok=True)
        with open(os.path.join(a.run_dir, "daemon.pid"), "w") as f:
            f.write(str(os.getpid()))
        cmd = tasks.command(sys.executable, os.path.join(PLUGIN, "bin", "live-task"), a.run_dir)
        cfg["brain"]["prompt_extra"] = (cfg["brain"].get("prompt_extra") or "") + tasks.handoff_prompt(cmd)

    me = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            loop.add_signal_handler(sig, me.cancel)
        except (NotImplementedError, RuntimeError):
            pass

    rec = Recorder.open(a.cwd, cfg)
    log = EventLog(Clock(), echo=True, listener=rec.on_event if rec else None)
    if rec:
        log.emit("x_recording", None, t_ms=0.0, path=rec.path)
    orch = Orchestrator(LiveAudio(log, recorder=rec), log, build(cfg), cfg)
    bridge = None
    try:
        await orch.start()
        if a.run_dir:
            bridge = asyncio.create_task(
                tasks.TaskBridge(a.run_dir, log).run(lambda res: orch.announce(tasks.result_prompt(res))))
        await orch.run()
    except asyncio.CancelledError:
        pass
    finally:
        if bridge is not None:
            bridge.cancel()
        await asyncio.shield(orch.close())
        log.emit("session_end", None)
        if rec:
            rec.close()
        if a.run_dir:
            try:
                os.unlink(os.path.join(a.run_dir, "daemon.pid"))
            except OSError:
                pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
