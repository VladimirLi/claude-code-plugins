# jev-browser

Fast browser tools for Claude. A long-lived daemon drives a dedicated, warm Chrome over raw CDP, and Jev (TypeSafe's decision model, ~0.2 s per call) picks each click and keystroke. Claude plans one outcome at a time and writes all text; Jev never writes text.

The same daemon serves two front ends:

- an MCP server with five tools: `open`, `do`, `act`, `observe`, `check`;
- the `jevb` CLI, for scripts and benchmark drivers, with one tab per `--session`.

No npm dependencies: Node 22.4+ (built-in WebSocket) and Google Chrome at `/Applications/Google Chrome.app` (override with `JEVB_CHROME`).

## Install

```
/plugin install jev-browser --marketplace VladimirLi/claude-code-plugins
```

Then set the TypeSafe key in `/plugin` (jev-browser, Configure), or put `TYPESAFE_API_KEY=...` in `~/.claude/plugins/data/jev-browser-vladimirli-plugins/.env`. A daemon started by the CLI reads that file and its environment (in a development checkout, also the repository's root `.env`). The key is sent only to `api.typesafe.ai` (or an OpenRouter key only to `openrouter.ai`), and is never logged.

## CLI

```
jevb [--session NAME] [--fresh] <command>
  open <url>
  do <goal> [--values JSON] [--max N] [--allow-irreversible] [--vision]
  act <op> [id] [value]          e.g. act click 12 · act type 7 "hello" · act select 5 "Two"
  act '<JSON list>' [--values JSON] [--allow-irreversible]
  observe [full|diff|find <text>|read|screenshot]
  check <yes/no question> [--vision]
  eval <js>                      bench only: run JS in the session's tab, print JSON
  close | sessions | status | stop
```

The first command starts the daemon, and the daemon starts Chrome. Both stay warm (the daemon exits after 8 idle hours, `JEVB_IDLE_HOURS`). `--fresh` on a session's first call gives it an empty browser context instead of the shared profile.

## How it works

| Part | What it does |
| --- | --- |
| Connection | One CDP WebSocket. Each session gets its own window, so its tab is active and renders at full rate (background tabs in headless Chrome barely tick `requestAnimationFrame`). Pop-ups opened by a tab become the session's tab. |
| Observer | One injected script per call: interactive elements through open shadow roots and same-origin iframes, labels, state, nearby text, a covered flag from a hit test, and visible text. |
| Ids | Sticky: an element keeps its id while its role and name stay the same. Ids are never reused within a session, so an id from a page that navigated away fails as "gone". |
| Executor | Re-resolves the id, scrolls it into view, runs a hit test, then sends CDP `Input` events at fresh coordinates. Short text is typed as key events (date pickers and masks listen to keys). Mutations are never retried. |
| Settler | Two animation frames. If the action caused DOM or network activity, it waits until both are quiet for 150 ms (requests older than 2 s are ignored). Navigation waits for `DOMContentLoaded`, not `load`. |
| Overlays | A covering overlay with a plain close/accept control is closed (at most twice per URL), then the target is checked again. |
| Decider | One Jev request per step: operation, target, value key, and done/blocked/error/login/irreversible scores. Pages with more than 240 elements use two stages (groups, then elements). |
| Statuses | `done`, `likely_done`, `needs_confirmation`, `needs_login`, `needs_human` (captcha), `blocked`, `stuck`, `ambiguous`, `error`, `max_actions`. |

## Safety

- Page content is untrusted. Claude gets it between `<<<untrusted page content>>>` markers. Jev gets it only in `state`, never in question instructions.
- Irreversible actions are gated in code. Buy, pay, place order, delete, send money and similar labels always pause. Finish, confirm and submit pause on checkout or payment pages. Risky confirm dialogs pause too. The result is `needs_confirmation`, and only `allow_irreversible` proceeds. Jev's own `irreversible` score (0.6 or more) can add a pause but never remove one.
- Secrets: `values` keys that look secret (password, token, card, ...) are shown to Jev by key only, typed locally, and scrubbed from every output. Password field values are never read.
- `JEVB_ALLOWED_DOMAINS` (comma separated) is an optional allow-list. It is a guard rail, not a boundary.
- The daemon's socket is readable only by your user. The Chrome profile is the plugin's own, never your everyday profile.

## Images

Jev accepts images: base64 JPEG/PNG assets with the `TypeSafe-Danger-Zone-Images: true` header ([docs](https://docs.typesafe.ai/images)). `do --vision` and `check --vision` attach a viewport screenshot. `do` also attaches one by itself when a page has no elements to choose from.

## Files

`~/.claude/plugins/data/jev-browser-vladimirli-plugins/`: `daemon.sock`, `daemon.log` (operation names and timings only), `chrome-profile/`, `shots/`, and optionally `.env`. `JEVB_HOME` moves all of them.
