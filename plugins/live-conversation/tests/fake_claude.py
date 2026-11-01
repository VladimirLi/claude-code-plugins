#!/usr/bin/env python3
"""Stand-in for `claude -p --input-format stream-json --output-format stream-json` in brain tests.

Each process takes the next delay from FAKE_DELAYS (comma list of ms, via the counter
file FAKE_COUNTER) as its time to first text. A user turn containing "tool" answers
with a text block, a tool_use block, then a second text block. "long" streams slowly
so a test can interrupt it.
"""
import json, os, sys, time, threading

def next_delay():
    """Delay for this process: the i-th process started takes FAKE_DELAYS[i]. FAKE_COUNTER
    is a folder; each process claims the first free slot file atomically."""
    delays = [int(x) for x in os.environ.get("FAKE_DELAYS", "10").split(",")]
    folder = os.environ.get("FAKE_COUNTER")
    i = 0
    if folder:
        os.makedirs(folder, exist_ok=True)
        while True:
            try:
                os.close(os.open(os.path.join(folder, str(i)), os.O_CREAT | os.O_EXCL))
                break
            except FileExistsError:
                i += 1
    return delays[i % len(delays)] / 1000.0


DELAY = next_delay()
stop = threading.Event()
lock = threading.Lock()

def out(o):
    with lock:
        sys.stdout.write(json.dumps(o) + "\n")
        sys.stdout.flush()

def ev(e):
    out({"type": "stream_event", "event": e})

def text_block(i, parts, gap=0.0):
    ev({"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}})
    for p in parts:
        if stop.is_set():
            return False
        ev({"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": p}})
        time.sleep(gap)
    ev({"type": "content_block_stop", "index": i})
    return True

def answer(text):
    stop.clear()
    time.sleep(DELAY)
    ev({"type": "message_start", "message": {}})
    if "tool" in text:
        text_block(0, ["Let me ", "check."])
        ev({"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "name": "Read"}})
        ev({"type": "content_block_stop", "index": 1})
        time.sleep(0.6)
        text_block(0, ["It ", "works."])
    elif "report" in text:
        text_block(0, ["Let me ", "run them."])
        ev({"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "name": "Bash"}})
        ev({"type": "content_block_stop", "index": 1})
        ev({"type": "message_delta", "delta": {"stop_reason": "tool_use"}})
        ev({"type": "message_stop"})
        ev({"type": "message_start", "message": {}})
        text_block(0, ["One test", " is broken.", " It", " expected three.", " The rest", " pass."])
        ev({"type": "message_delta", "delta": {"stop_reason": "end_turn"}})
        ev({"type": "message_stop"})
    elif "narrate" in text:
        # starts with a tool call, narrates between tools, then answers
        ev({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "name": "Bash"}})
        ev({"type": "content_block_stop", "index": 0})
        ev({"type": "message_delta", "delta": {"stop_reason": "tool_use"}})
        ev({"type": "message_stop"})
        time.sleep(0.05)
        ev({"type": "message_start", "message": {}})
        text_block(0, ["I'll run ", "pytest now."])
        ev({"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "name": "Bash"}})
        ev({"type": "content_block_stop", "index": 1})
        ev({"type": "message_delta", "delta": {"stop_reason": "tool_use"}})
        ev({"type": "message_stop"})
        time.sleep(0.05)
        ev({"type": "message_start", "message": {}})
        text_block(0, ["All ", "pass."])
        ev({"type": "message_delta", "delta": {"stop_reason": "end_turn"}})
        ev({"type": "message_stop"})
    elif "long" in text:
        text_block(0, ["word "] * 200, gap=0.02)
    elif "markdown" in text:
        text_block(0, ["Two things:\n", "- **one", "** thing\n", "- `two`\n"])
    else:
        text_block(0, ["ready"])
    out({"type": "result", "subtype": "success"})

worker = None
for line in sys.stdin:
    o = json.loads(line)
    if o.get("type") == "user":
        worker = threading.Thread(target=answer, args=(o["message"]["content"],))
        worker.start()
    elif o.get("type") == "control_request":
        stop.set()
        if worker:
            worker.join()
        out({"type": "control_response", "response": {"subtype": "success", "request_id": o["request_id"]}})
if worker:
    worker.join()
