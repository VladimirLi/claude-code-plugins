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

## Use your own Chrome

By default the plugin runs its own headless Chrome with a separate profile. To have it work in your everyday Chrome instead, with your tabs, logins and extensions, like Claude in Chrome:

1. In Chrome, open `chrome://inspect/#remote-debugging` and switch on "Allow remote debugging for this browser instance".
2. Turn on "Use my own Chrome" in `/plugin` (jev-browser, Configure), or set `JEVB_ATTACH=1` for the CLI (`JEVB_ATTACH=1 jevb --session me open https://example.com`).
3. On the first connection Chrome asks you to Allow it. Click Allow.

Each session opens its own window in your Chrome. Pages keep your real window size, language and user agent. `close` and `stop` close only the tabs the plugin opened, never Chrome itself. This mode has its own daemon (`daemon-attach.sock`), so it never mixes with the separate Chrome. Every site you are logged in to is reachable in this mode; the irreversible-action gate still applies.

## CLI

```
jevb [--session NAME] [--fresh] <command>
  open <url> [--full]            prints a summary; --full adds the element table (as observe full)
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
| Observer | One injected script per call: interactive elements through open shadow roots and same-origin iframes, labels, state, nearby text, a covered flag from a hit test, visible text, and a summary of visible tables (headers marked with their sort order, so Jev can see that a column is sorted). |
| Ids | Sticky: an element keeps its id while its role and name stay the same. Ids are never reused within a session, so an id from a page that navigated away fails as "gone". |
| Executor | Re-resolves the id, scrolls it into view, runs a hit test, then sends CDP `Input` events at fresh coordinates (a link that wraps onto two lines is aimed at its largest line box). Short text is typed as key events (date pickers and masks listen to keys). Mutations are never retried. |
| Settler | Two animation frames. If the action caused DOM or network activity, it waits until both are quiet for 150 ms (requests older than 2 s are ignored). Navigation waits for `DOMContentLoaded`, not `load`. |
| Overlays | A covering overlay with a plain close/accept control is closed (at most twice per URL), then the target is checked again. |
| Decider | One Jev request per step: operation, target, value key, and done/blocked/error/login/irreversible scores. Pages with more than 240 elements use two stages (groups, then elements). |
| Stopping | Jev's done score never ends a run alone. Every ending asks a separate `complete` question about the page only, with no action history, because a history of repeated clicks made Jev say yes too early. `done` needs `complete` of 0.65 or more, and 0.45 to 0.65 gives `likely_done`. When Jev picks no action and the goal is not met, its next-best action runs instead. Before an action repeats on the same element (a toggle such as a sort header, or a drag swap), `complete` is asked first. For drags, scrolls and keys, a second question checks whether the action the goal names was done, and a yes ends the run as `likely_done`. A `stuck`, `max_actions` or `ambiguous` ending asks both questions, so a met goal is reported as met. Re-entering a value after every value has been entered ends the run as `likely_done`. The same `select` or `type` on the same element never runs twice in one `do`. A `select` runs only with an option the goal or value names; a missing option ends as `not_found` before the page changes. A search goal whose words were typed but not submitted gets Enter pressed in the field before any ending. An `open`/`go to` goal whose naming words appear nowhere on the page (whole text, links, labels) ends as `not_found` instead of scrolling. An `open`/`go to` goal whose last click was a link that moved to a page whose title, url or top text carries the link's name ends as `done` once Jev wants no further action, since the page alone rarely proves "the second book listed there". A `scroll to <place>` goal ends as `done` when the words naming the place come into view, and never ends as met while they are out of view. The action budget (default 10 plus the number of values, at least 20, at most 30) is a backstop: a sure step (tool and target p >= 0.8, no repeat building up, the page changed in the last 2 rounds) runs up to 5 past it, and a run that still ends as `max_actions` names the planned action in a `next:` line. A first look with no action is asked again only while the page is still loading. A scroll toward a place the page already has takes one step: an off-screen element Jev targets and the goal names is scrolled into view, and a `scroll to <place>` goal jumps to the heading that names it; wheel steps are left for content that is not loaded yet (infinite scroll). In a goal like "scroll down to the X link and open it", once X is in view its next action runs instead of another scroll. |
| Statuses | `done`, `likely_done`, `needs_confirmation`, `needs_login`, `needs_value` (the next step types into a field and no `values` were given; a `field:` line names it and nothing is typed), `needs_human` (captcha), `not_found` (the dropdown option or the page the goal names does not exist; the page is left unchanged), `blocked`, `stuck`, `ambiguous`, `error`, `max_actions`. |

## Safety

- Page content is untrusted. Claude gets it between `<<<untrusted page content>>>` markers. Jev gets it only in `state`, never in question instructions.
- Irreversible actions are gated in code. Buy, pay, place order, delete, send money and similar labels always pause. Finish, confirm and submit pause on checkout or payment pages. Risky confirm dialogs pause too. Enter in a field is judged by the form's default button (each field carries it as `submitter`), and Enter or Space with no target by the focused element. Submitting a form that collects contact or payment details pauses in any language, whatever its button says ("Skicka bokning"): fields of type email or tel, or whose name, id, autocomplete or label names an email, phone, address, card (`cc-*`) or IBAN. Sign-in forms (a password and one other field), search fields, and a name or postal code alone do not count. A capture-phase `submit` listener in every page also stops any form submit whose button hits the rule, or whose form collects such details, however it started (a click, Enter, or a page script calling `requestSubmit`). The result is `needs_confirmation` with a `resume:` line that is the exact call to proceed, and only `allow_irreversible` proceeds. Not covered: a script that calls `form.submit()` (no submit event fires) or sends the order with `fetch` from a plain button. Jev's own `irreversible` score (0.6 or more) can add a pause but never remove one: `do` asks it every step, and `act` asks it once per call, before the first step that submits a form.
- Secrets: `values` keys that look secret (password, token, card, ...) are shown to Jev by key only, typed locally, and scrubbed from every output. Password field values are never read.
- `JEVB_ALLOWED_DOMAINS` (comma separated) is an optional allow-list. It is a guard rail, not a boundary.
- The daemon's socket is readable only by your user. The Chrome profile is the plugin's own, never your everyday profile.

## Images

Jev accepts images: base64 JPEG/PNG assets with the `TypeSafe-Danger-Zone-Images: true` header ([docs](https://docs.typesafe.ai/images)). `do --vision` and `check --vision` attach a viewport screenshot. Without the flag, `do` attaches one only on signals from its loop: the page has no elements, the last action hit a covering element ("covered by …"), the text decision has a weak target (p < 0.4), or, before the first action, a word the goal names is nowhere in the visible text (with a target p < 0.6 or no action). The screenshot carries each in-view element's `[i]` mark, and each element sent with it carries its `box` [x, y, w, h], so Jev can name a target from the image. The image's done judge never ends a run before the first action. No screenshot is taken while a secret value typed into a plain (non-password) field is in view.

When the goal names something the page does not show yet, `do` watches for a page change for up to 800 ms while its first decision runs (an ad or modal opened by a timer), and looks again if one comes.

## Files

`~/.claude/plugins/data/jev-browser-vladimirli-plugins/`: `daemon.sock`, `daemon.log` (operation names and timings only), `chrome-profile/`, `shots/`, and optionally `.env`. `JEVB_HOME` moves all of them.
