"""tasks: the voice brain hands real coding work to the Claude Code session.

The voice brain is a small, fast `claude -p`. Work that edits code or takes long
goes to the person's own Claude Code session instead, and the result is spoken
when it is done. Standard library only: `bin/live-task` imports this module
without the plugin's venv.

The run folder (`--run-dir`, made by the /live command) holds one file per task
state, so every step is a rename or a new file:

    tasks/<n>.request.json   written by `bin/live-task` (the brain's Bash tool)  {task, text, t}
    tasks/<n>.handed.json    the daemon told the mod (event `task_handoff`)
    tasks/<n>.result.json    written by the mod when the session's turn ended   {task, reason, answer, durationMs}
    tasks/<n>.done.json      the daemon passed the result to the brain (event `task_result`)

Interface:

    request(run_dir, text) -> int             new task, numbered 1, 2, ... (safe against races)
    status(run_dir) -> list[dict]             every task with its state
    TaskBridge(run_dir, log).poll() -> list[dict]   one step: hands requests, returns new results
    await TaskBridge(...).run(on_result)      poll forever (every poll_s)
    handoff_prompt(command) -> str            system prompt section for the brain
    result_prompt(result) -> str              the brain turn that speaks a result
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import time
from typing import Callable, Optional

MAX_ANSWER = 3000            # characters of the session's answer the brain gets
_NAME = re.compile(r"^(\d+)\.(request|handed|result|done)\.json$")
_ORDER = {"request": 0, "handed": 1, "result": 2, "done": 3}


def _dir(run_dir: str) -> str:
    d = os.path.join(run_dir, "tasks")
    os.makedirs(d, exist_ok=True)
    return d


def _scan(d: str) -> dict[int, tuple[str, str]]:
    """task number -> (latest state, file name)."""
    out: dict[int, tuple[str, str]] = {}
    for name in os.listdir(d):
        m = _NAME.match(name)
        if not m:
            continue
        n, st = int(m.group(1)), m.group(2)
        if n not in out or _ORDER[st] > _ORDER[out[n][0]]:
            out[n] = (st, name)
    return out


def _read(path: str) -> Optional[dict]:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def request(run_dir: str, text: str) -> int:
    """Write a new task request; returns its number."""
    text = " ".join(text.split())
    if not text:
        raise ValueError("empty task")
    d = _dir(run_dir)
    tmp = os.path.join(d, f".req-{os.getpid()}-{time.monotonic_ns()}.tmp")
    n = max(_scan(d), default=0) + 1
    try:
        while True:
            with open(tmp, "w") as f:
                json.dump({"task": n, "text": text, "t": time.time()}, f)
            try:
                os.link(tmp, os.path.join(d, f"{n}.request.json"))   # atomic, fails if taken
                return n
            except FileExistsError:
                n += 1
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def status(run_dir: str) -> list[dict]:
    d = _dir(run_dir)
    out = []
    for n, (st, name) in sorted(_scan(d).items()):
        req = _read(os.path.join(d, f"{n}.request.json")) or _read(os.path.join(d, f"{n}.handed.json")) or {}
        out.append({"task": n, "state": {"request": "waiting", "handed": "running"}.get(st, "finished"),
                    "text": req.get("text", "")})
    return out


class TaskBridge:
    """The daemon's side: hands new requests to the mod, collects results."""

    def __init__(self, run_dir: str, log, poll_s: float = 0.25) -> None:
        self.run_dir = run_dir
        self.d = _dir(run_dir)
        self.log = log
        self.poll_s = poll_s

    def poll(self) -> list[dict]:
        results = []
        for n, (st, name) in sorted(_scan(self.d).items()):
            path = os.path.join(self.d, name)
            if st == "request":
                req = _read(path)
                if req is None:
                    continue
                os.replace(path, os.path.join(self.d, f"{n}.handed.json"))
                # the mod reads this event from stderr and submits the task to the session
                self.log.emit("task_handoff", None, task=n, text=req.get("text", ""))
            elif st == "result":
                res = _read(path)
                if res is None:
                    continue                      # still being written
                res.setdefault("task", n)
                req = _read(os.path.join(self.d, f"{n}.handed.json")) or {}
                res.setdefault("text", req.get("text", ""))
                os.replace(path, os.path.join(self.d, f"{n}.done.json"))
                self.log.emit("task_result", None, task=n, reason=res.get("reason", "answer"))
                results.append(res)
        return results

    async def run(self, on_result: Callable[[dict], None]) -> None:
        while True:
            try:
                for res in self.poll():
                    on_result(res)
            except OSError:
                pass
            await asyncio.sleep(self.poll_s)


def command(python: str, script: str, run_dir: str) -> str:
    """The shell command the brain runs to hand off a task (the task text goes after it)."""
    return " ".join(shlex.quote(x) for x in (python, script, "--run-dir", run_dir))


def handoff_prompt(cmd: str) -> str:
    return f"""

Handing work to Claude Code:
- The user's own Claude Code session runs next to this call, with a stronger model and full tools. \
Hand it real coding work: any change to files (edit, fix, refactor, write tests, commit), or anything \
that takes more than a minute or two. You can still look things up yourself with your tools for quick \
questions.
- To hand off, first say one short sentence such as "Okay, handing that to Claude Code.", then run this \
Bash command with the whole task in one quoted argument: {cmd} "the task"
- Write the task for a developer who did not hear the call: what to do, where, and what done looks like. \
Write it as text, not speech: flags, file names and code as they are written (--verbose, not "dash dash \
verbose").
- The command returns at once with a task number. Then your reply is over: add nothing, or at most a \
few words. Don't wait for the task and don't do the work yourself.
- To check on tasks, run the same command with only --status in place of the task.
- "[Task N finished ...]" brings you Claude Code's report. Tell the user the outcome in two or three \
spoken sentences: what changed and whether it worked. Name files by what they are, never read a path or \
file name letter by letter. If it failed or asks a question, say so plainly."""


def result_prompt(res: dict) -> str:
    n = res.get("task", "?")
    reason = res.get("reason", "answer")
    answer = " ".join(str(res.get("answer") or "").split())
    if len(answer) > MAX_ANSWER:
        answer = answer[:MAX_ANSWER] + " ..."
    task = res.get("text", "")
    if reason == "aborted":
        head = f'[Task {n} finished: the user stopped it in Claude Code before it was done. The task was: "{task}".'
    elif reason in ("error", "refusal"):
        head = f'[Task {n} finished with an error ({reason}). The task was: "{task}".'
    else:
        head = f'[Task {n} finished. The task was: "{task}".'
    body = f' Claude Code\'s report: "{answer}"]' if answer else " Claude Code gave no report.]"
    return head + body + " Tell the user now, briefly."
