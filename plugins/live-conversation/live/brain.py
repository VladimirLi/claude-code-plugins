"""brain piece: Claude Code itself, as a persistent headless process.

Interface:

    brain = Brain(cfg)                cfg: model, cwd, tools, system_prompt, prompt_extra, warm, pool, probes,
                                      pool_batches, fast_ms, effort, disable_hooks, clean, extra_args
    await brain.start()               spawn `claude -p` (stream-json) and warm it up
    async for delta in brain.respond(text, interrupted=None):   reply text pieces
        ...                           `interrupted` = Interrupted(spoken_text) if the
                                      previous reply was cut off by the user
    await brain.cancel()              stop the reply in flight (stream-json interrupt)
    await brain.close()

Replies always come from Claude Code. History lives in the claude process.

What makes the reply speakable fast:
- one warm `claude -p` process per session (no spawn per turn), prompt cache primed
  by a warm-up turn, hooks and slash-command/skill listings disabled so every
  request is small and nothing runs before it;
- start() warms a small pool of processes, times each one's first text on probe
  turns and keeps the two fastest; each turn is raced on both and the first to
  start its reply wins (the other is interrupted before any tool runs, and gets
  the exchange it missed as a context note on its next turn);
- effort "low", which mostly skips adaptive thinking before the first word;
- a voice system prompt (VOICE_PROMPT) that asks for a short first sentence, spoken
  register and no markdown;
- when a text block ends (for example "Let me check." before a tool call) the brain
  yields a trailing space, so a sentence chunker can release that sentence at once
  instead of waiting for the text after the tool call;
- `spoken()` cleans any markdown that slips through, so the TTS never reads symbols.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from typing import AsyncIterator, Optional

VOICE_PROMPT = """You are Claude, on a live voice call with the user. You run inside their Claude Code \
session, in the project folder {cwd}, so you can read its files and run commands there. Everything you \
write is turned into speech the moment you write it. The user hears it, they never see it.

How you talk:
- Like a sharp, friendly colleague on a call, not an assistant writing a document. Contractions, everyday \
words, short sentences, one idea per sentence. React like a person when it fits ("Oh nice.", "Yeah, \
fair.", "Hmm."), but don't open every reply the same way.
- Your first sentence is short, under eight words, and already answers or reacts. "Canberra." is a \
perfect start. Never open with filler like "Great question", "Sure thing", "Absolutely" or "Of course", \
and don't announce what you're about to say ("Here's the short version", "Here's the core idea"). Just \
say it.
- Fit the length to the moment. A quick fact: one or two sentences. Small talk: one or two sentences, \
often with a question back. An explanation or advice: three or four short sentences, about fifty \
words, the key idea first. Leave the rest for the user to ask about. Never a lecture, never a \
summary at the end like "So in short".
- Plain speech only. No markdown, lists, bullets, numbering, headings, code blocks, backticks, emoji, \
or symbols such as slashes, tildes, dashes or arrows. Don't read out file paths or URLs; name the thing \
("the readme", "the brain module"). Say a command the way a developer says it out loud, as one \
phrase, for example "git reset dash dash soft HEAD tilde one".
- Give your own view when asked what you'd do. Be direct and specific, not "it depends".
- Most replies end without a question. Ask one only when you really want to know something, or \
when there's a concrete next step you could do for them. Don't tack on "Want me to go deeper?" or \
"Anything else?". If something is vague, ask one quick question instead of guessing. When the user \
answers your question, even with just "yeah" or "no", take it as the answer and carry on from there.
- When the user corrects you, don't apologise. Say "Right," or "Ah, got it," and give the answer that \
fits what they meant.
- Never invent facts about the project. If you need to know, look.

Doing work with tools:
- Never start a reply with a tool call: the user would hear dead silence. Always write one short \
sentence first that you're on it ("Let me take a look.", "Okay, running them now."), then use the tools without narrating each step, then give the result in two or three short spoken \
sentences: the answer and what matters, not everything you saw. Look only at the few things you need.

The conversation:
- The user's words come from speech recognition, so read them charitably.
- "[The user interrupted you ...]" means they cut you off and heard only the quoted part. Don't repeat \
it and don't apologise. Answer what they say now, and fold in anything they missed only if they need it.
- A short "mm-hmm", "uh-huh" or "right" after you explained something means "go on": continue with \
the next point in a sentence or two. Don't start over.
- "[Context: ...]" lists replies of yours that the user heard but that are missing from your history. \
Treat them as things you said, and never mention the note."""

WARM_TEXT = "(Audio check before the call starts. Reply with only the word: ready)"
PROBE_TEXT = "(Audio check {n}. Reply with only the word: ready)"


@dataclass
class Interrupted:
    spoken_text: str


DEFAULTS = {
    "model": "haiku",
    "tools": "Read,Grep,Glob,Bash",
    "warm": True,
    "pool": 4,                   # warm processes started; the fastest one is kept
    "probes": 2,                 # timed probe turns per process after the warm-up turn
    "pool_batches": 1,           # start another pool if the kept ones are not under fast_ms
    "fast_ms": 900,
    "race": 2,                   # warm processes kept; each turn goes to all, the first to start wins
    "narrate_after_s": 6.0,      # after a tool call, speak a narration line only after this much silence
    "effort": "low",             # low effort mostly skips adaptive thinking (0.3-1 s before the first word)
    "disable_hooks": True,       # hooks of installed mods must not delay or alter voice turns
    "clean": True,               # strip markdown that slips through (see spoken())
    "extra_args": [],
}


# -- markdown to speech ---------------------------------------------------------------
_MARK_AT_LINE_START = re.compile(r"[ \t]*(?:[-*+•]|\d{1,2}[.)]|#{1,6})[ \t]+")
_MAYBE_MARK = re.compile(r"[ \t]*(?:[-*+•]|\d{1,2}[.)]?|#{1,6})?[ \t]*")
_DASH = re.compile(r"\s*[—–]\s*")


def _inline(t: str) -> str:
    t = t.replace("`", "").replace("*", "")
    t = _DASH.sub(", ", t)
    return t.replace("\n", " ")


def spoken(text: str) -> str:
    """One whole reply text made safe to speak: no markdown marks, list markers or headings."""
    c = _Cleaner()
    return (c.push(text) + c.flush()).strip()


class _Cleaner:
    """Streaming markdown stripper. Drops backticks and asterisks, list and heading
    markers at the start of a line, and turns dashes and newlines into pauses.

    It never holds text back except a possible marker at a line start (at most one
    short delta), so it adds no delay to normal speech."""

    def __init__(self) -> None:
        self.buf = ""
        self.line_start = True
        self.last = "."

    def push(self, delta: str) -> str:
        self.buf += delta
        out = []
        while self.buf:
            if self.line_start:
                if _MAYBE_MARK.fullmatch(self.buf) and len(self.buf) < 8:
                    break                                   # wait: could still be "- " or "1. "
                m = _MARK_AT_LINE_START.match(self.buf)
                if m:
                    self.buf = self.buf[m.end():]
                self.line_start = False
                continue
            nl = self.buf.find("\n")
            piece = _inline(self.buf if nl < 0 else self.buf[:nl])
            if piece.strip():
                self.last = piece.rstrip()[-1]
            out.append(piece)
            if nl < 0:
                self.buf = ""
            else:
                # a line that ends without punctuation (a list item, a heading) ends a sentence
                out.append(". " if self.last.isalnum() else " ")
                self.last = "."
                self.buf = self.buf[nl + 1:]
                self.line_start = True
        return "".join(out)

    def flush(self) -> str:
        rest, self.buf = self.buf, ""
        return "" if _MAYBE_MARK.fullmatch(rest) else _inline(rest)


_SENTENCE = re.compile(r"[.!?](\s|$)")


class _Turn:
    """Text of one reply, from the stream events to the queue respond() reads.

    - Text before the first tool call streams straight through (the "Let me look."
      sentence). When a text block ends, a space goes out so a sentence chunker can
      release its last sentence without waiting for the text after the tool call.
    - After a tool call, a text block is held until it is clearly the answer (two
      sentences or 30 words, or the message ends without another tool call). A block
      that only leads into the next tool call ("I'll run pytest now.") is narration
      and is dropped, unless nothing was said for narrate_after_s, so a long task
      still gets one progress line."""

    def __init__(self, q: asyncio.Queue, narrate_after_s: float = 6.0) -> None:
        self.q = q
        self.narrate_after_s = narrate_after_s
        self.got_text = False
        self.after_tool = False
        self.in_text = False
        self.held: Optional[list[str]] = None     # text of a block after a tool call, not yet released
        self.held_closed = False                  # that block has ended; waiting for what comes next
        self.new_block = False
        self.ends_space = False
        self.last_out = time.monotonic()
        self.t0 = time.monotonic()
        self.started = asyncio.Event()           # the model began its reply (any content block)
        self.started_at: Optional[float] = None

    def _out(self, t: str) -> None:
        if t and self.ends_space and t[:1].isspace():
            t = t.lstrip()
        if t:
            self.q.put_nowait(t)
            self.got_text = True
            self.ends_space = t[-1:].isspace()
            self.last_out = time.monotonic()

    def _release(self) -> None:
        if self.held is not None:
            text = "".join(self.held)
            self.held = None
            if self.got_text and text and not text[:1].isspace():
                text = " " + text
            self._out(text + (" " if self.held_closed else ""))
        self.held_closed = False

    def _drop_or_release(self) -> None:
        if self.held is not None and time.monotonic() - self.last_out >= self.narrate_after_s:
            self._release()
        self.held, self.held_closed = None, False

    def on_event(self, ev: dict) -> None:
        et = ev.get("type")
        if et == "content_block_start":
            if not self.started.is_set():
                self.started_at = time.monotonic()
                self.started.set()
            kind = ev.get("content_block", {}).get("type")
            if kind == "text":
                if self.held_closed:
                    self._release()
                self.in_text = True
                if self.after_tool:
                    self.held = []
            elif kind in ("tool_use", "server_tool_use"):
                if self.held_closed:
                    self._drop_or_release()
                self.after_tool = True
        elif et == "content_block_delta" and ev.get("delta", {}).get("type") == "text_delta":
            t = ev["delta"]["text"]
            if self.held is not None:
                if not self.held and self.got_text:
                    t = t.lstrip()
                self.new_block = False
                self.held.append(t)
                text = "".join(self.held)
                if len(_SENTENCE.findall(text)) >= 2 or len(text.split()) >= 30:
                    self.held = None                 # clearly the answer: stream from here on
                    self._out(text if not self.got_text or text[:1].isspace() else " " + text)
                return
            if not self.in_text or not t:
                return
            if self.got_text and self.new_block:
                t = t.lstrip()
            self.new_block = False
            self._out(t)
        elif et == "content_block_stop" and self.in_text:
            self.in_text = False
            self.new_block = True
            if self.held is not None:
                self.held_closed = True
            elif self.got_text:
                self._out(" ")
        elif et in ("message_delta", "message_stop"):
            if self.held_closed and (et == "message_stop" or ev.get("delta", {}).get("stop_reason") != "tool_use"):
                self._release()

    def finish(self) -> None:
        if self.held:
            self._release()
        if not self.started.is_set():
            self.started_at = time.monotonic()
            self.started.set()
        self.q.put_nowait(None)


class _Session:
    """One persistent `claude -p` stream-json process: one conversation."""

    def __init__(self, argv: list[str], cwd: str, clean: bool = True) -> None:
        self.argv, self.cwd, self.clean = argv, cwd, clean
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._queue: Optional[asyncio.Queue] = None
        self._turn: Optional[_Turn] = None
        self._reader: Optional[asyncio.Task] = None
        self._busy = asyncio.Event()
        self._busy.set()      # set = idle
        self._req = 0
        self._interrupting = False
        self.narrate_after_s = 6.0
        self.ttft_ms: list[float] = []        # first text delta after the turn was written

    async def start(self, warm: bool) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv, cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=1 << 24)
        self._reader = asyncio.create_task(self._read())
        if warm:
            await self.probe(WARM_TEXT)

    async def probe(self, text: str) -> float:
        """One throwaway turn; returns its time to first text in ms (inf if it failed)."""
        t0 = time.monotonic()
        first = None
        try:
            async for _ in self.respond(text):
                if first is None:
                    first = (time.monotonic() - t0) * 1000.0
        except Exception:
            return float("inf")
        return first if first is not None else float("inf")

    async def _read(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                if self._turn is not None:
                    self._turn.finish()
                self._turn = None
                self._queue = None
                self._busy.set()
                return
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            ty = o.get("type")
            tr = self._turn
            if ty == "stream_event" and tr is not None:
                tr.on_event(o.get("event", {}))
            elif ty == "result":
                if tr is not None:
                    tr.finish()
                self._turn = None
                self._queue = None
                self._interrupting = False
                self._busy.set()

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None and self._reader is not None \
            and not self._reader.done()

    async def submit(self, text: str) -> _Turn:
        """Send one user turn; returns its _Turn (read it with stream())."""
        assert self.proc and self.proc.stdin
        await self._busy.wait()
        if not self.alive:
            raise RuntimeError("claude process exited")
        self._busy.clear()
        q: asyncio.Queue = asyncio.Queue()
        self._queue = q
        turn = _Turn(q, self.narrate_after_s)
        self._turn = turn
        msg = {"type": "user", "message": {"role": "user", "content": text}}
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self.proc.stdin.drain()
        return turn

    async def stream(self, turn: _Turn) -> AsyncIterator[str]:
        clean = _Cleaner() if self.clean else None
        finished = False
        first = True
        try:
            while True:
                t = await turn.q.get()
                if t is None:
                    finished = True
                    if clean is not None:
                        rest = clean.flush()
                        if rest:
                            yield rest
                    return
                if first:
                    first = False
                    self.ttft_ms.append((time.monotonic() - turn.t0) * 1000.0)
                if clean is not None:
                    t = clean.push(t)
                if t:
                    yield t
        finally:
            if not finished and self._turn is turn and not self._busy.is_set():
                # the caller stopped listening (barge-in, merged turn): stop generating too
                asyncio.ensure_future(self.cancel())

    async def respond(self, text: str) -> AsyncIterator[str]:
        turn = await self.submit(text)
        async for d in self.stream(turn):
            yield d

    async def cancel(self) -> None:
        if self.proc is None or self.proc.stdin is None or self._busy.is_set():
            return
        if not self._interrupting:
            self._interrupting = True
            self._req += 1
            req = {"type": "control_request", "request_id": f"int{self._req}", "request": {"subtype": "interrupt"}}
            try:
                self.proc.stdin.write((json.dumps(req) + "\n").encode())
                await self.proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                self._busy.set()
                return
        try:
            await asyncio.wait_for(self._busy.wait(), 5)
        except asyncio.TimeoutError:
            if self._turn is not None:
                self._turn.finish()
            self._turn = None
            self._queue = None
            self._interrupting = False
            self._busy.set()

    async def close(self) -> None:
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.stdin.close()
                await asyncio.wait_for(self.proc.wait(), 3)
            except Exception:
                self.proc.kill()
        if self._reader:
            self._reader.cancel()


class Brain:
    """The voice brain.

    start() spawns a small pool of warm claude processes, times each one's first text
    on probe turns and keeps the `race` fastest. Every user turn then goes to all
    kept processes at once; the first to begin its reply is streamed and the others
    are interrupted at once (before any tool can run). A process that lost a turn
    gets the exchanges it missed as a short context note on its next turn, so all of
    them keep the same conversation.

    Measured on this machine (2026-10-29): time to first text varies a lot between
    processes and requests (about 0.45 s to over 3 s for the same short turn), and
    adaptive thinking adds 0.3 to 1 s when it kicks in (effort "low" mostly avoids
    it). Racing two warm processes takes the faster of the two on every turn."""

    def __init__(self, cfg: Optional[dict] = None) -> None:
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.sessions: list[_Session] = []
        self.session: Optional[_Session] = None        # the one that answered last
        self.startup: dict = {}
        self.wins: list[int] = []                      # index of the winning session per turn
        self._closing: list[asyncio.Task] = []
        self._missed: dict[int, list[tuple[str, str]]] = {}   # id(session) -> (user said, user heard)

    def system_prompt(self) -> str:
        p = (self.cfg.get("system_prompt") or VOICE_PROMPT) + (self.cfg.get("prompt_extra") or "")
        return p.replace("{cwd}", self.cfg.get("cwd") or os.getcwd())

    def _argv(self) -> list[str]:
        c = self.cfg
        argv = [c.get("claude_bin") or "claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--include-partial-messages", "--model", c["model"],
                "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config",
                "--disable-slash-commands", "--system-prompt", self.system_prompt()]
        if c.get("disable_hooks"):
            argv += ["--settings", json.dumps({"disableAllHooks": True})]
        if c.get("effort"):
            argv += ["--effort", c["effort"]]
        if c["tools"]:
            argv += ["--tools", c["tools"], "--allowedTools", c["tools"]]
        else:
            argv += ["--tools", ""]
        return argv + list(c.get("extra_args") or [])

    def _new_session(self) -> _Session:
        s = _Session(self._argv(), self.cfg.get("cwd") or os.getcwd(), self.cfg.get("clean", True))
        s.narrate_after_s = float(self.cfg.get("narrate_after_s", 6.0))
        return s

    async def start(self) -> None:
        c = self.cfg
        pool = max(1, int(c.get("pool", 1)))
        if not c["warm"]:
            self.session = self._new_session()
            self.sessions = [self.session]
            await self.session.start(warm=False)
            return
        keep = max(1, min(int(c.get("race", 1)), pool))
        scored: list[tuple[float, int, _Session]] = []
        tried: list[list[float]] = []
        for _batch in range(max(1, int(c.get("pool_batches", 1)))):
            cands = [self._new_session() for _ in range(pool)]
            await asyncio.gather(*(s.start(warm=True) for s in cands), return_exceptions=True)
            scores: dict[int, list[float]] = {id(s): [] for s in cands}
            for k in range(int(c.get("probes", 0))):
                text = PROBE_TEXT.format(n=k + 2)
                res = await asyncio.gather(*(s.probe(text) for s in cands))
                for s, r in zip(cands, res):
                    scores[id(s)].append(r)
            for s in cands:
                xs = scores[id(s)] or (s.ttft_ms[-1:] if s.alive else [float("inf")])
                sc = sorted(xs)[len(xs) // 2] if s.alive and xs else float("inf")
                tried.append([round(x) if x != float("inf") else -1 for x in xs])
                scored.append((sc, len(scored), s))
            scored.sort(key=lambda x: (x[0], x[1]))
            if scored[min(keep, len(scored)) - 1][0] <= float(c.get("fast_ms", 900)):
                break
        self.sessions = [s for _, _, s in scored[:keep]]
        self.session = self.sessions[0]
        self.startup = {"kept_ttft_ms": [round(x) if x != float("inf") else -1 for x, _, _ in scored[:keep]],
                        "candidates_ttft_ms": tried}
        self._closing = [asyncio.ensure_future(s.close()) for _, _, s in scored[keep:]]
        if os.environ.get("LIVE_BRAIN_DEBUG"):
            import sys
            print(f"brain startup: {json.dumps(self.startup)}", file=sys.stderr, flush=True)

    def _message(self, s: _Session, text: str, interrupted: Optional[Interrupted]) -> str:
        notes = []
        missed = self._missed.get(id(s), [])
        if missed:
            # this process lost the last turn(s): give it what the user said and heard
            parts = [f'the user said "{u}" and you answered "{a}"' if a else f'the user said "{u}" and you said nothing yet'
                     for u, a in missed[-4:]]
            notes.append("[Context: replies from you that are not in your history yet: " + "; then ".join(parts) + ".]")
        if interrupted is not None:
            heard = interrupted.spoken_text.strip()
            notes.append(f'[The user interrupted you. They heard only: "{heard}"]' if heard
                         else "[The user interrupted you before you said anything]")
        return " ".join(notes + [text])

    async def respond(self, text: str, interrupted: Optional[Interrupted] = None) -> AsyncIterator[str]:
        assert self.session is not None, "start() first"
        live = [s for s in self.sessions if s.alive] or [self.session]
        idle = [s for s in live if s._busy.is_set()]
        if not idle:
            # all still stopping the last reply: take whichever is free first
            waits = {asyncio.ensure_future(s._busy.wait()): s for s in live}
            done, pending = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            for x in pending:
                x.cancel()
            idle = [waits[x] for x in done]
        live = idle      # a process still busy sits this turn out; it gets a context note later
        turns = list(await asyncio.gather(*(s.submit(self._message(s, text, interrupted)) for s in live)))
        w = 0
        if len(live) > 1:
            waits = [asyncio.ensure_future(t.started.wait()) for t in turns]
            try:
                await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for x in waits:
                    x.cancel()
            started = [(t.started_at, i) for i, t in enumerate(turns) if t.started_at is not None]
            w = min(started)[1]
            for i, s in enumerate(live):
                if i != w:
                    asyncio.ensure_future(s.cancel())
        winner = live[w]
        self.session = winner
        self.wins.append(self.sessions.index(winner) if winner in self.sessions else 0)
        out: list[str] = []
        done = False
        try:
            async for d in winner.stream(turns[w]):
                out.append(d)
                yield d
            done = True
        finally:
            said = " ".join("".join(out).split()) if done else ""
            if interrupted is not None and not done:
                said = ""
            self._missed[id(winner)] = []
            for s in self.sessions:
                if s is not winner:
                    # a loser's interrupted turn is not in its history: keep it as a note
                    self._missed.setdefault(id(s), []).append((text, said))

    async def cancel(self) -> None:
        await asyncio.gather(*(s.cancel() for s in self.sessions), return_exceptions=True)

    async def close(self) -> None:
        await asyncio.gather(*(s.close() for s in self.sessions), return_exceptions=True)
        if self._closing:
            await asyncio.gather(*self._closing, return_exceptions=True)
            self._closing = []
