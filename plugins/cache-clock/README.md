# cache-clock

A Claude Code mod that shows, in the status line, how long this chat's prompt cache stays warm, warns you before it goes cold, and can keep it warm while you are away.

A warm cache makes your next prompt cheaper and faster: the conversation so far is read from the cache instead of being processed again. The cache lives for a fixed time after the last request (60 minutes on a Claude subscription, 5 with an API key).

## Install

At the prompt of a Claude Code terminal session:

```
/plugin install cache-clock --marketplace VladimirLi/claude-code-plugins
```

Answer `y` to add the marketplace, then pick a scope (user scope is first). From a shell:

```
claude plugin install cache-clock --marketplace VladimirLi/claude-code-plugins
```

In the desktop app: click + next to the prompt box, then Plugins, Add marketplace with `VladimirLi/claude-code-plugins`, then Add plugin.

Requires a Claude Code build that runs mods (hooks modules); tested on 2.1.310.

## What it shows

- The status line reads `cache warm 42m`, or `cache cold`, updated every 10 seconds.
- A toast warns you 5 minutes before the cache goes cold.
- `/clear` resets the clock, since a new conversation has no cache of its own.

## Commands

```
/cacheclock         when the last request was, the cache lifetime, and when it goes cold
/keepwarm [hours]   keep the cache warm for that long (2h by default), or until your next prompt
/keepwarm off       stop keeping it warm
```

Keep-warm sends a short hidden ping as a fork of the conversation about 2 minutes before the cache would go cold. Each ping costs a cache read of the whole chat, which is much cheaper than rebuilding the cache, but not free. If a later turn still had to rebuild the cache, a toast tells you.

## Settings

| Setting | Default | What it does |
| --- | --- | --- |
| Cache lifetime (minutes) | 60 | 60 on a Claude subscription, 5 with an API key, or what `promptCacheTtl` sets. |
| Warn before cold (minutes) | 5 | 0: no warning. |
| Keep warm for (hours) | 2 | How long `/keepwarm` lasts when no time is given. |

## Development

```
claude plugin validate .
claude plugin test .
```

## License

MIT
