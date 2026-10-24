import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Need, Status } from '../types'
import { FOCUS, FORK_PROMPT, INSTRUCTION, RESUME, afterPrompt, gateChoice, draftFor, markOf, mergeAnswer, parseStatus, progressOf, stripStatus, toMarkdown, toStatus } from './status'

const PANE = 'session-status'
const HOTKEYS = '123456789'
const status = atom({ plugin: 'session-status', key: 'status' } as const, null as Status | null, { shape: 'status-1' })
const updatedAt = atom({ plugin: 'session-status', key: 'updatedAt' } as const, 0)
const isFromFork = atom({ plugin: 'session-status', key: 'isFromFork' } as const, false)
const isStale = atom({ plugin: 'session-status', key: 'isStale' } as const, false)
const lastPromptAt = atom({ plugin: 'session-status', key: 'lastPromptAt' } as const, 0)
const isCompacted = atom({ plugin: 'session-status', key: 'isCompacted' } as const, false)
const PAUSE_MS = 2 * 3_600_000

// Set from the usageLog option at each load: an opt-in local log of how the gate and resume are used.
let isLogging = false

async function logUse($: EngineInterface, action: string): Promise<unknown> {
  if (!isLogging) return
  const line = JSON.stringify({ ts: new Date().toISOString(), session: await $.session.id().catch(() => ''), plugin: 'session-status', action })
  return $.process.run(['/bin/sh', '-c', 'printf "%s\\n" "$1" >> "$HOME/.claude/usage.jsonl"', 'sh', line]).catch(() => undefined)
}

// Measures the focus gate: what Claude recommended, and what the user chose.
async function logGateAnswer($: EngineInterface, s: Status | null, text: string) {
  if (text.trimStart().toLowerCase().startsWith('now:')) return void logUse($, `gate:skip:${s?.kind ?? 'unknown'}`)
  if (!s?.gate) return
  const choice = gateChoice(s.gate, text)
  void logUse($, `gate:choice:${s.kind ?? 'unknown'}:${s.gate.recommend}:${choice ?? 'own-words'}`)
}

const isOff = async ($: EngineInterface) => (await $.store.get('isOff')) === true

async function save($: EngineInterface, s: Status, fromFork: boolean) {
  await update($, status, () => s)
  await update($, updatedAt, () => Date.now())
  await update($, isFromFork, () => fromFork)
  await update($, isStale, () => false)
}

// The fallback: a fork reads the conversation from the prompt cache and writes the status the reply left out.
async function forkStatus($: EngineInterface) {
  const r = await $.model.fork({ prompt: FORK_PROMPT })
  if (!r.isAnswered) return
  const start = r.text.indexOf('{')
  const end = r.text.lastIndexOf('}')
  if (start < 0 || end <= start) return
  try {
    const s = toStatus(JSON.parse(r.text.slice(start, end + 1)))
    if (s) await save($, s, true)
  } catch {}
}

const ago = (ms: number) => (ms < 60_000 ? 'just now' : ms < 3_600_000 ? `${Math.round(ms / 60_000)}m ago` : `${Math.round(ms / 3_600_000)}h ago`)

// Appends the answer on a line of its own, then, where the draft is readable, drops an earlier answer to the same question.
async function answer($: EngineInterface, need: Need, option: string) {
  // The pane holds the keys, so it closes before the draft goes into the prompt box.
  await $.ui.close({ id: PANE }).catch(() => undefined)
  // A slash command runs only as the whole prompt, so it replaces the draft instead of joining it.
  if (option.trim().startsWith('/')) {
    await $.prompt.fill({ text: option.trim(), mode: 'replace' })
    return
  }
  const filled = await $.prompt.fill({ text: `\n${draftFor(need, option)}`, mode: 'append' })
  if (!filled.isFilled || !filled.text.includes(draftFor(need, option))) return
  const merged = mergeAnswer(filled.text, need, option)
  if (merged !== filled.text) await $.prompt.fill({ text: merged, mode: 'replace' })
}

// One row per decision the reply asked for, each ready answer a hotkey Button that drafts the reply.
function needRows($: EngineInterface, needs: readonly Need[], els: ReturnType<EngineInterface['ui']['resolve']>) {
  const { Box, Text, Button } = els
  let key = 0
  return needs.slice(0, 4).map((need, i) => (
    <Box key={`need-${i}`} flexDirection="row" gap={1} flexWrap="wrap">
      <Text color="warning">?</Text>
      <Text>{need.ask}</Text>
      {need.options.map((option, j) => {
        const hotkey = HOTKEYS[key++]
        return (
          <Button key={`opt-${i}-${j}`} plain {...(hotkey ? { hotkey } : {})} onPress={() => void answer($, need, option)}>
            {option}
          </Button>
        )
      })}
    </Box>
  ))
}

export const register: Register = (on, options) => {
  const fallbackMs = Math.max(0, Number(options.fallbackMinSeconds ?? 120)) * 1000
  const focus = options.focusRules !== false
  isLogging = options.usageLog === true

  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'standing', description: 'Session status: open the full status, copy it, or turn the mod off/on', argumentHint: '[copy|off|on]' })
    return next(e)
  })

  // Every prompt of the person's carries the replacement instruction, so it also holds in a conversation that began before the mod.
  on('prompt.submit', async ($, e, next) => {
    if (e.command || e.isMeta || (e.origin.kind !== 'composer' && e.origin.kind !== 'bridge') || (await isOff($))) return next(e)
    await logGateAnswer($, await read($, status), e.text).catch(() => undefined)
    const away = Date.now() - (await read($, lastPromptAt))
    const isResume = (await read($, lastPromptAt)) > 0 && (away >= PAUSE_MS || (await read($, isCompacted)))
    await update($, lastPromptAt, () => Date.now())
    await update($, isCompacted, () => false)
    if (isResume) void logUse($, 'resume')
    let stale = false
    await update($, status, s => {
      if (!s) return s
      const r = afterPrompt(s, e.text)
      stale = r.isStale
      return r.status
    }).catch(() => undefined)
    if (stale) await update($, isStale, () => true).catch(() => undefined)
    return next({ ...e, context: [...(e.context ?? []), INSTRUCTION, ...(focus ? [FOCUS] : []), ...(isResume ? [RESUME] : [])] })
  }).catch(($, e, next) => next(e)) // a failure sends the prompt without the instruction, never drops it

  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined && e.reason === 'answer' && !(await isOff($))) {
      const s = parseStatus(e.answer)
      if (s) await save($, s, false)
      if (s?.gate) void logUse($, `gate:${s.kind ?? 'unknown'}:${s.gate.recommend}`)
      else if (fallbackMs > 0 && e.durationMs >= fallbackMs) void forkStatus($)
    }
    return next(e)
  })

  on('session.compact', async ($, e, next) => {
    if (e.agentId === undefined) await update($, isCompacted, () => true).catch(() => undefined)
    return next(e)
  }).catch(($, e, next) => next(e))

  // The tag is for the mod: the reply is drawn without it; the model's transcript keeps it.
  on('ui.render', { component: 'AssistantMessage' }, async ($, e, next) => {
    if (!e.props.text.includes('<status>')) return next(e)
    const text = stripStatus(e.props.text)
    if (text !== '') return next({ ...e, props: { ...e.props, text } })
    const { Text } = $.ui.resolve(e)
    return <Text dimColor>status updated above the prompt · /standing</Text>
  })

  on('command.run', { command: 'standing' }, async ($, e) => {
    const arg = e.args.trim()
    if (arg === 'off' || arg === 'on') {
      await $.store.set('isOff', arg === 'off')
      return { text: arg === 'off' ? 'session-status: off. Replies no longer end with a status.' : 'session-status: on.' }
    }
    const s = await read($, status)
    if (arg === 'copy') {
      if (!s) return { text: 'session-status: no status yet.' }
      const copied = await $.ui.copy({ text: toMarkdown(s) })
      return { text: copied.isCopied ? 'session-status: copied as Markdown.' : `session-status: copy failed (${copied.reason}).\n\n${toMarkdown(s)}` }
    }
    if (arg !== '') return { text: 'session-status: usage /standing [copy|off|on]' }
    const opened = await $.ui.open({ id: PANE, title: 'Session status', focus: true, closeOnEscape: true })
    return { text: opened.isPlaced ? '' : s ? toMarkdown(s) : 'session-status: no status yet.' }
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const s = await read($, status)
    if (e.props.hasSurvey || !s || (await isOff($))) return next(e)
    const below = await next(e).catch(() => undefined)
    const els = $.ui.resolve(e)
    const { Box, Text } = els
    const age = ago(Date.now() - (await read($, updatedAt)))
    const inferred = (await read($, isFromFork)) ? ' · inferred' : ''
    const progress = progressOf(s.plan)
    const old = await read($, isStale)
    const mine = (
      <Box flexDirection="column" width={e.props.bodyColumns}>
        <Text wrap="truncate-end">
          {old ? <Text dimColor>◇ before your last prompt: {s.now}</Text> : <Text bold>◆ {s.now}</Text>}
          <Text dimColor>
            {'  '}
            {progress && `${progress} · `}
            {age}
            {inferred} · /standing
          </Text>
        </Text>
        {needRows($, s.needs, els)}
      </Box>
    )
    return below ? (
      <Box flexDirection="column">
        {mine}
        {below}
      </Box>
    ) : (
      mine
    )
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const els = $.ui.resolve(e)
    const { Box, Text } = els
    const s = await read($, status)
    if (!s) return <Text dimColor>No status yet: it appears after a reply that ends a step.</Text>
    return (
      <Box flexDirection="column" width={e.props.bodyColumns} gap={1}>
        <Text>
          <Text bold>Goal </Text>
          {s.goal}
          {s.kind && <Text dimColor> · {s.kind}</Text>}
        </Text>
        {s.done && (
          <Text>
            <Text bold>Done when </Text>
            {s.done}
          </Text>
        )}
        <Text>
          <Text bold>Now </Text>
          {s.now}
        </Text>
        <Box flexDirection="column">
          <Text bold>Needs you</Text>
          {s.needs.length === 0 ? <Text dimColor>Nothing.</Text> : needRows($, s.needs, els)}
        </Box>
        <Text>
          <Text bold>Next </Text>
          {s.next}
        </Text>
        {s.plan.length > 0 && (
          <Box flexDirection="column">
            <Text bold>Plan <Text dimColor>{progressOf(s.plan)}</Text></Text>
            {s.plan.map((p, i) => (
              <Text key={`plan-${i}`} dimColor={p.state === 'done'}>
                {markOf(p.state)} {p.item}
              </Text>
            ))}
          </Box>
        )}
        <Text dimColor>A key puts that answer in the prompt box · /standing copy · Esc closes</Text>
      </Box>
    )
  })
}
