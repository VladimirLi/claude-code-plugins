import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Clock } from '../types'

const MINUTE = 60_000
// Ping this long before the cache would go cold, so a slow fork still lands in time.
const MARGIN = 2 * MINUTE
const PING = 'Keep-alive ping from the cache-clock mod. Reply with only: ok'

const clock = atom({ plugin: 'cache-clock', key: 'clock' } as const, { lastAt: 0, keepUntil: 0, isWarned: false, pings: 0 } as Clock, { shape: 'clock-1' })

const mins = (ms: number) => (ms >= 60 * MINUTE ? `${Math.floor(ms / (60 * MINUTE))}h${String(Math.floor((ms % (60 * MINUTE)) / MINUTE)).padStart(2, '0')}m` : ms < MINUTE ? '<1m' : `${Math.floor(ms / MINUTE)}m`)

function statusText(c: Clock, now: number, ttl: number): string | undefined {
  if (c.lastAt === 0) return undefined
  const left = c.lastAt + ttl - now
  const keep = c.keepUntil > now ? ` · keep-warm ${mins(c.keepUntil - now)}` : ''
  return left > 0 ? `cache warm ${mins(left)}${keep}` : `cache cold${keep}`
}

// One ping over the session's own transcript: the API serves it from the cache, which refreshes the cache's lifetime.
async function ping($: EngineInterface): Promise<void> {
  const r = await $.model.fork({ prompt: PING })
  if (!r.isAnswered) {
    $.ui.toast(`cache-clock: keep-warm ping failed (${r.reason})${r.reason === 'nothing-to-fork' ? '; keep-warm stopped' : ''}`)
    if (r.reason === 'nothing-to-fork') await update($, clock, c => ({ ...c, keepUntil: 0 }))
    return
  }
  const read_ = r.usage.cache_read_input_tokens
  const now = await $.clock.now()
  if (read_ === 0) {
    $.ui.toast('cache-clock: the ping found the cache already cold; it is warm again now, at full price')
  }
  await update($, clock, c => ({ ...c, lastAt: now, isWarned: false, pings: c.pings + 1 }))
  $.ui.log(`cache-clock: kept warm (${read_} cached tokens read, ${r.usage.cache_creation_input_tokens} written)`, { to: 'debug' })
}

type Cfg = { ttl: number; warn: number }
type Flags = { isTurnRunning: boolean; isPinging: boolean }

async function tick($: EngineInterface, cfg: Cfg, flags: Flags) {
  const now = await $.clock.now()
  const c = await read($, clock)
  $.ui.status(statusText(c, now, cfg.ttl))
  if (c.lastAt === 0 || flags.isTurnRunning) return
  const left = c.lastAt + cfg.ttl - now
  if (c.keepUntil > now && left <= MARGIN && !flags.isPinging) {
    flags.isPinging = true
    try {
      await ping($)
    } finally {
      flags.isPinging = false
    }
    return
  }
  if (cfg.warn > 0 && left > 0 && left <= cfg.warn && !c.isWarned && c.keepUntil <= now) {
    await update($, clock, x => ({ ...x, isWarned: true }))
    $.ui.toast(`Cache goes cold in ${mins(left)}. /keepwarm keeps it warm.`, { timeoutMs: 10000 })
  }
}

export const register: Register = (on, options) => {
  const ttl = Math.max(1, Number(options.ttlMinutes ?? 60)) * MINUTE
  const warn = Math.max(0, Number(options.warnMinutes ?? 5)) * MINUTE
  const keepHours = Math.max(0.1, Number(options.keepWarmHours ?? 2))
  const cfg: Cfg = { ttl, warn }
  const flags: Flags = { isTurnRunning: false, isPinging: false }

  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'keepwarm', description: 'Keep this chat\'s prompt cache warm while you are away', argumentHint: '[hours|off]' })
    await $.command.register({ name: 'cacheclock', description: 'Show when this chat\'s prompt cache goes cold' })
    $.clock.every(10_000, () => void tick($, cfg, flags))
    return next(e)
  })

  // /clear starts a conversation with no cache of its own.
  on('session.end', async ($, e, next) => {
    if (e.reason === 'clear') {
      await update($, clock, () => ({ lastAt: 0, keepUntil: 0, isWarned: false, pings: 0 }))
      $.ui.status(undefined)
    }
    return next(e)
  })

  on('prompt.submit', async ($, e, next) => {
    // A prompt the person typed, in the terminal or from the phone, means they are back.
    if (e.origin.kind === 'composer' || e.origin.kind === 'bridge') await update($, clock, c => (c.keepUntil === 0 ? c : { ...c, keepUntil: 0 }))
    return next(e)
  }).catch(($, e, next) => next(e)) // a failure never holds up the prompt

  on('turn.start', async ($, e, next) => {
    if (e.agentId === undefined) flags.isTurnRunning = true
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId !== undefined) return next(e)
    flags.isTurnRunning = false
    const u = e.usage
    if (u) {
      const now = await $.clock.now()
      const before = await read($, clock)
      // The first turn after keep-warm pings tells whether they worked: a warm cache is mostly read, not written.
      if (before.pings > 0 && u.cache_creation_input_tokens > u.cache_read_input_tokens) {
        $.ui.toast(`cache-clock: this turn rebuilt the cache (${u.cache_creation_input_tokens} tokens written) despite ${before.pings} keep-warm ping(s)`, { timeoutMs: 10000 })
      }
      await update($, clock, c => ({ ...c, lastAt: now, isWarned: false, pings: 0 }))
      $.ui.status(statusText({ ...before, lastAt: now }, now, ttl))
    }
    return next(e)
  })

  on('command.run', { command: 'keepwarm' }, async ($, e) => {
    const arg = e.args.trim()
    if (arg === 'off') {
      await update($, clock, c => ({ ...c, keepUntil: 0 }))
      return { text: 'cache-clock: keep-warm off.' }
    }
    const hours = arg === '' ? keepHours : Number(arg)
    if (!(hours > 0)) return { text: 'cache-clock: usage /keepwarm [hours|off]' }
    const now = await $.clock.now()
    const c = await update($, clock, x => ({ ...x, keepUntil: now + hours * 60 * MINUTE }))
    $.ui.status(statusText(c, now, ttl))
    return { text: `cache-clock: keeping the cache warm for ${hours}h, or until your next prompt. Each ping costs a cache read of this chat.` }
  })

  on('command.run', { command: 'cacheclock' }, async $ => {
    const now = await $.clock.now()
    const c = await read($, clock)
    if (c.lastAt === 0) return { text: 'cache-clock: no request yet in this conversation.' }
    const left = c.lastAt + ttl - now
    return {
      text: `cache-clock: last request ${mins(now - c.lastAt)} ago · lifetime ${mins(ttl)} · ${left > 0 ? `cold in ${mins(left)}` : 'cold now'}${c.keepUntil > now ? ` · keep-warm for ${mins(c.keepUntil - now)}` : ''}`,
    }
  })
}
