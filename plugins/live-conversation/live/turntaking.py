"""turntaking piece: what to do once a user turn has ended, and filler policy.

Interface:

    tt = TurnTaking(cfg)
    tt.decide(TurnContext) -> Action
        Action.kind: "respond" | "ignore"
        Action.filler: optional short text to speak before the brain's reply
    tt.on_user_resumed(ctx) -> str   "merge" (keep the earlier words and wait),
                                     or "new" (start a fresh turn)
    tt.observe_content_latency(ms, after_interrupt=False)   optional calibration

TurnContext carries the final transcript, whether the assistant was speaking
when the user started, and the last reply text.

Policy (v1):

- Ignore: empty text, pure hesitations ("um", "uh": the user still holds the
  floor), and backchannels ("mm-hmm", "yeah", "right") while the assistant is
  still speaking, so the reply goes on instead of restarting.
- Respond to everything else.
- Filler: a turn-initial acknowledgement ("Oh okay, got it, so,") that fills
  the wait until the brain's first sentence is audible, the way people (and
  the reference voice model, "Ah, good follow-up!") open a turn. It is spoken
  only when that wait is long (`expected_content_ms - filler_lead_ms >=
  min_wait_ms`), and its length (estimate_ms) is picked to end just as the
  content would start: a filler that ends early leaves a silence after it
  ("Okay, so," then nothing), one that ends late only delays the answer a
  little. Fillers are composed from an interjection and a body that fit the
  kind of turn (a correction gets "got it", a request for the short version
  gets "short version"), never repeat inside a session (text, interjection or
  body, while fresh ones are left), never echo the user's first word, end with
  a comma so they lead into the answer, and vary across sessions with the
  wording of the turn.
  The orchestrator may call observe_content_latency() after each reply so the
  expected wait follows the real pipeline.
- Merge: when the user starts again before any reply audio, keep their
  earlier words (one turn).
"""
from __future__ import annotations

import re
import zlib
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TurnContext:
    transcript: str
    assistant_was_speaking: bool = False
    last_reply: str = ""
    history: list = field(default_factory=list)


@dataclass
class Action:
    kind: str                 # "respond" | "ignore"
    filler: Optional[str] = None
    reason: str = ""          # why (for logs and tests)


DEFAULTS = {
    "fillers": True,
    # ms from decide() to the first audible filler sample (TTS start-up for a short text).
    "filler_lead_ms": 150.0,
    # ms from decide() to the first audible sample of the brain's reply, normal turn.
    "expected_content_ms": 1300.0,
    # same, for a turn that follows an interruption (the brain first cancels the old reply).
    "expected_content_after_interrupt_ms": 1900.0,
    # speak a filler only if the content would otherwise start this long after the filler could.
    "min_wait_ms": 500.0,
    # an early filler end (silence after it) costs this many times more than a late one.
    "early_penalty": 2.5,
    # pick among fillers whose fit is within this many ms of the best fit (variety).
    "fit_tolerance_ms": 400.0,
    # TTS engine the filler lengths are estimated for ("kokoro" | "say"), and a scale for other voices.
    "engine": "kokoro",
    "duration_scale": 1.0,
    # EWMA weight for observe_content_latency().
    "learn_rate": 0.3,
}

_HESITATIONS = {"um", "uh", "er", "erm", "uhm", "umm", "uhh", "hm", "ehm"}
_BACKCHANNELS = {
    "mm hmm", "mhm", "mm", "mmm", "hmm", "uh huh", "yeah", "yep", "yes", "yup", "right", "okay", "ok",
    "sure", "got it", "i see", "oh", "ah", "ahh", "cool", "nice", "huh", "wow", "uh hm", "mm hm", "mhmm",
    "alright", "all right", "true", "exactly", "makes sense", "oh okay", "ah okay", "oh i see", "yeah yeah",
    "okay okay", "right right", "mm yeah", "oh nice", "oh wow", "interesting", "go on", "go ahead", "uh yeah",
    "ok ok", "yeah okay", "okay yeah", "oh right", "ah right", "i see i see",
}

# Fillers are composed from two hand-written parts, both fitted to the kind of turn:
#   interjection + body                 "Ah," + " got it, so,"    "Hmm," + " the thing is,"
# Composition gives many distinct texts (no canned repeats), and lets the length fit the wait.
_INTERJ: dict[str, list[str]] = {
    "correction": ["Ah", "Oh", "Oh okay", "Ah okay", "Right"],
    "shorter": ["Oh okay", "Okay", "Right", "Yeah", "Ah okay"],
    "clarify": ["Oh", "Ah", "Ooh", "Right", "Hmm", "Mm"],
    "why": ["Hmm", "Mm", "Oh", "Ah"],
    "compare": ["Hmm", "Ooh", "Oh", "Mm", "Ah"],
    "opinion": ["Hmm", "Ooh", "Mm", "Oh"],
    "action": ["Yeah", "Okay", "Sure", "Right", "Alright"],
    "explain": ["Oh", "Okay", "Ah", "Yeah", "Mm", "Right"],
    "affirm": ["Okay", "Ah", "Oh", "Alright", "Right"],
    "continue": ["Right", "Mm", "Yeah", "Okay"],
    "default": ["Okay", "Oh", "Ah", "Hmm", "Mm", "Right", "Yeah"],
}
_BODY: dict[str, list[str]] = {
    "correction": ["got it, so", "gotcha, so", "I see what you mean, so", "my bad, so", "I see, okay so",
                   "that makes more sense, so", "got it", "gotcha, okay so"],
    "shorter": ["short version", "quick version then", "sure, short version", "sure thing, short version",
                "sure, quickly", "the short version, sure"],
    # No praise ("good question", "great point"): heard on every turn it sounds canned (first live use).
    "clarify": ["so", "okay so", "yeah, so", "I mean", "so basically", "well"],
    "why": ["so basically", "well", "so", "the thing is", "well, basically", "okay so"],
    "compare": ["so", "well", "okay so", "so basically", "let's see", "it depends a bit, but"],
    "opinion": ["honestly", "I'd say", "well, honestly", "I think", "so", "okay so"],
    "action": ["let me check", "one sec", "on it", "let me take a look", "sure, one sec", "on it, one sec"],
    "explain": ["so", "okay so", "let's see, so", "so basically", "alright, so", "yeah, so", "sure, so",
                "well", "sure thing, so", "sure, okay so"],
    "affirm": ["cool, so", "great, so", "cool, okay so", "sounds good, so", "alright, so"],
    "continue": ["anyway", "so anyway", "where was I, so", "so"],
    "default": ["so", "okay so", "got it, so", "sure, so", "alright, so", "well"],
}
_LONG_INTERJ = {"hmm", "mm", "ooh", "oh", "ah"}


def _syllables(word: str) -> int:
    w = word.lower().strip(",.!?'")
    if w in ("hmm", "mm"):
        return 1
    n = len(re.findall(r"[aeiouy]+", w))
    if w.endswith("e") and n > 1 and not w.endswith(("le", "ee")):
        n -= 1
    return max(1, n)


# Spoken length model per TTS engine: ms per syllable, per comma, constant (incl. trailing pause).
# Fitted on composed fillers: Kokoro af_heart speed 1.1 (MAE 88 ms), macOS say Flo 190 wpm (MAE 115 ms).
_DURATION_MODELS = {"kokoro": (121.0, 57.0, 329.0, 0.0), "say": (185.0, 260.0, 145.0, 42.0)}


def estimate_ms(text: str, engine: str = "kokoro") -> float:
    """Spoken length of a filler incl. its trailing pause, for the given TTS engine."""
    words = text.split()
    if not words:
        return 0.0
    syl, comma, base, long_i = _DURATION_MODELS.get(engine, _DURATION_MODELS["kokoro"])
    extra = long_i if words[0].lower().strip(",") in _LONG_INTERJ else 0.0
    return syl * sum(_syllables(w) for w in words) + comma * text.count(",") + base + extra


def compose(kind: str) -> list[tuple[str, str, str, str]]:
    """All fillers for a kind: (text, interjection, body, "")."""
    out = []
    for i in _INTERJ.get(kind, _INTERJ["default"]):
        for body in _BODY.get(kind, _BODY["default"]):
            words = [w.strip(",").lower() for w in (i + " " + body).split()]
            if len(words) != len(set(words)):      # "Okay, okay so," / "Sure, sure thing,"
                continue
            text = f"{i}, {body},"
            out.append((text, i, body, ""))
    return out


# A turn after an interruption usually starts like this.
_INTERRUPT_CUES = ("actually", "wait", "hold on", "hang on", "stop", "sorry", "no no", "just give me",
                   "just tell me", "skip", "never mind", "nevermind")


def normalize(text: str) -> str:
    t = text.lower().replace("’", "'").replace("-", " ")
    t = re.sub(r"[^a-z0-9' ]+", " ", t)
    return " ".join(t.split())


def is_hesitation(norm: str) -> bool:
    words = norm.split()
    return bool(words) and all(w in _HESITATIONS for w in words)


def is_backchannel(norm: str) -> bool:
    if not norm:
        return False
    if norm in _BACKCHANNELS:
        return True
    words = norm.split()
    # short runs of backchannel tokens ("yeah yeah okay", "mm hmm right")
    if len(words) <= 4:
        i = 0
        while i < len(words):
            if i + 1 < len(words) and " ".join(words[i:i + 2]) in _BACKCHANNELS:
                i += 2
            elif words[i] in _BACKCHANNELS or words[i] in _HESITATIONS:
                i += 1
            else:
                return False
        return True
    return False


def classify(norm: str, assistant_was_speaking: bool = False) -> str:
    """Kind of user turn, for picking a fitting filler."""
    words = norm.split()
    first = " ".join(words[:3])
    if is_backchannel(norm):
        if words and words[0] in {"yeah", "yes", "yep", "yup", "sure", "okay", "ok", "alright", "cool"}:
            return "affirm"
        return "continue"
    if re.search(r"\b(short(er)? version|shorter|tl ?dr|in a nutshell|briefly|quick version|summar)", norm):
        return "shorter"
    if (re.match(r"(no|nope|not that|i meant|i mean|that's not)\b", norm) or
            re.search(r"\bi meant\b|\bnot what i\b|\bthat's not what\b", norm)):
        return "correction"
    if (re.match(r"(wait|hold on|hang on|sorry)\b", norm) or
            re.search(r"\b(different from|difference from|compared to|versus|vs)\b", norm) and
            re.match(r"(wait|but|so|and|how|hold)", norm)):
        return "clarify"
    if re.search(r"\bwhy\b", first) or re.search(r"\bwhy is that\b|\bhow come\b", norm):
        return "why"
    if re.search(r"\b(difference between|which one|which is|better|faster|cheaper|slower|versus|vs|compare)\b",
                 norm):
        return "compare"
    if re.match(r"(can|could|would|will) you (run|check|look|open|find|fix|read|show|tell me what|search|grep)",
                norm) or re.match(r"(run|check|look at|open|find|fix|read|search)\b", norm):
        return "action"
    if re.search(r"\b(bad idea|good idea|should i|do you think|worth it|is it ok|would you)\b", norm):
        return "opinion"
    if re.search(r"\b(explain|walk me through|tell me about|how does|how do|what is|what's|what are|how)\b",
                 norm):
        return "explain"
    return "default"


class TurnTaking:
    def __init__(self, cfg: Optional[dict] = None) -> None:
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.used: set[str] = set()
        self.used_parts: set[str] = set()
        self.expected = float(self.cfg["expected_content_ms"])
        self.expected_int = float(self.cfg["expected_content_after_interrupt_ms"])

    # -- decisions -------------------------------------------------------------
    def decide(self, ctx: TurnContext) -> Action:
        norm = normalize(ctx.transcript or "")
        if not norm:
            return Action("ignore", reason="empty")
        if is_hesitation(norm):
            return Action("ignore", reason="hesitation")
        if ctx.assistant_was_speaking and is_backchannel(norm):
            return Action("ignore", reason="backchannel")
        kind = classify(norm, ctx.assistant_was_speaking)
        return Action("respond", filler=self.pick_filler(norm, kind), reason=kind)

    def on_user_resumed(self, ctx: TurnContext) -> str:
        return "merge"

    # -- fillers ---------------------------------------------------------------
    def wait_ms(self, norm: str) -> float:
        """Predicted silence between the earliest filler audio and the brain's first audio."""
        after_int = norm.startswith(_INTERRUPT_CUES)
        exp = self.expected_int if after_int else self.expected
        return exp - float(self.cfg["filler_lead_ms"])

    def pick_filler(self, norm: str, kind: str) -> Optional[str]:
        if not self.cfg["fillers"]:
            return None
        target = self.wait_ms(norm)
        if target < float(self.cfg["min_wait_ms"]):
            return None
        scale = float(self.cfg["duration_scale"])
        early = float(self.cfg["early_penalty"])
        first = norm.split()[0] if norm else ""
        cands = [c for c in compose(kind) if c[0] not in self.used and
                 normalize(c[1]).split()[0] != first]          # don't echo "Okay, so..." with "Okay,"
        if not cands:
            return None

        def cost(c):
            d = estimate_ms(c[0], self.cfg["engine"]) * scale - target
            return -d * early if d < 0 else d

        # Fresh parts first: an interjection or acknowledgement already heard this session
        # makes the next filler sound like a pattern.
        def staleness(c):
            return (c[1] in self.used_parts) + (c[2] in self.used_parts)

        best = min(cost(c) for c in cands)
        near = [c for c in cands if cost(c) <= best + float(self.cfg["fit_tolerance_ms"])]
        least = min(staleness(c) for c in near)
        near = sorted(c for c in near if staleness(c) == least)
        h = zlib.crc32(f"{norm}|{len(self.used)}".encode())
        text, i, a, _ = near[h % len(near)]
        self.used.add(text)
        self.used_parts.update({i, a})
        return text

    def observe_content_latency(self, ms: float, after_interrupt: bool = False) -> None:
        """Optional: the orchestrator reports decide() -> first content audio, to recalibrate."""
        a = float(self.cfg["learn_rate"])
        if after_interrupt:
            self.expected_int = (1 - a) * self.expected_int + a * ms
        else:
            self.expected = (1 - a) * self.expected + a * ms
