import type { Gate, Kind, Need, Status, Step } from '../types'

const TAG = /<status>([\s\S]*?)<\/status>/g

// What the model is told on every prompt while the mod is on.
export const INSTRUCTION = `Session status: when work spans several steps, finishes a long task, or resumes after a pause, end the reply with exactly one line
<status>{"goal":"...","kind":"build|research|planning|requirements|queue","done":"...","now":"...","needs":[{"ask":"...","options":["...","..."]}],"next":"...","plan":[{"item":"...","state":"done|next|waiting"}]}</status>
and no status summary in prose. goal: one line on what the session is for. kind: build, research, planning, requirements, or queue (several independent items such as triage or PR review). done: one checkable "done when" line (build: works or merged; research: the question answered with sources; planning: the decision made; requirements: the spec covers named areas; queue: ""). now: one line on where things stand. needs: each decision or command the user must give, with 1-4 short ready answers in options (a command to paste is one option); [] when nothing is needed. next: what you will do next and what it waits on. plan: the checklist, kept current; [] for one-step work. Name every PR, issue or ticket with a few words on what it is, never the number alone. The tag is hidden from the user and drawn as an interactive band, so do not repeat its content in prose. Nothing after the tag.`

// The focus rules ride with the status instruction: the user's attention is the scarce resource, and side threads are fine when chosen.
export const FOCUS = `Focus: protect the user's attention without making the plan rigid.
- Goal: the first reply of a session always ends with a status, so goal, kind and done are set before any side request. Keep goal, kind and done fixed once set; never widen done to cover a new request. Change them only when the user says so; when they widen the scope, propose the change as a need ("Change goal to …?") instead of drifting into it. A sharper research question is a goal update, not drift.
- Gate: when answering the user's prompt would need a new run that done does not need (an experiment, research across several sources, a new artifact such as a file, PR, issue, skill, mod or prototype, or a sub-investigation over several turns), do not start it. Reply in at most 4 lines: what the goal still needs, what this really is, its size, and one recommendation. Add "gate":{"thread":"3-6 words","size":"e.g. ~1h","recommend":"Fork|Park|Now|Goal"} to the status; the band draws its choices, so add no need for it. Fork: a background agent in its own worktree, only for work that needs no input from the user. Park: you write a to-do or GitHub issue with where it stopped and the next step. Now: do it here. Goal: make it the new goal. A request for a new artifact that done does not name is gated even when it is related to the goal. Gate when unsure. Never gate a question you can answer in one reply, work the goal needs, a prompt starting with "now:", or a queue session.
- Proportion: before deep research or a design proposal, name the cheapest option that meets done and its size. In an interview, ask the core questions first, keep a running total of what was approved, say once when the core is decided, and offer to park the edge cases.
- Noticed: put your own off-goal findings in one closing line "Noticed (parked): …", not as offers.
- Close: when done is met, say so and stop; list parked, forked and noticed items once; ask: next goal, new session, or close. A retro files issues; it builds nothing unless the user says "build it now".`

// Added to a prompt that comes after a long pause or a compaction.
export const RESUME = 'Resume: the user is back after a pause or a compaction. Start this reply with at most 5 lines: the goal, decisions so far, the open question, and the next step.'

export const GATE_OPTIONS = { Fork: 'Fork', Park: 'Park', Now: 'Now', Goal: 'Make it the goal' } as const

// The gate as a decision row, the recommendation first.
export function gateNeed(g: Gate): Need {
  const first = GATE_OPTIONS[g.recommend as keyof typeof GATE_OPTIONS] ?? GATE_OPTIONS.Park
  return { ask: `Side: ${g.thread}${g.size ? ` (${g.size})` : ''}`, options: [first, ...Object.values(GATE_OPTIONS).filter(o => o !== first)] }
}

// The gate option a prompt picked, if it answers the gate's row.
export function gateChoice(g: Gate, prompt: string): string | undefined {
  const head = `${gateNeed(g).ask} → `
  const line = prompt.split('\n').find(l => l.startsWith(head))
  return line?.slice(head.length).trim() || undefined
}

const KINDS: readonly Kind[] = ['build', 'research', 'planning', 'requirements', 'queue']

export const FORK_PROMPT = `Write the current session status for the user as JSON only, no prose, in this shape:
{"goal":"...","now":"...","needs":[{"ask":"...","options":["..."]}],"next":"...","plan":[{"item":"...","state":"done|next|waiting"}]}
goal: what the session is for. now: where things stand after the last reply. needs: decisions or commands the user must still give, with short ready answers. next: what happens next and what it waits on. plan: the checklist. Base it only on the conversation.`

const str = (v: unknown) => (typeof v === 'string' ? v.trim() : '')

export function toStatus(v: unknown): Status | undefined {
  if (typeof v !== 'object' || v === null) return undefined
  const o = v as Record<string, unknown>
  const needs: Need[] = Array.isArray(o.needs)
    ? o.needs
        .map(n => ({ ask: str(n?.ask), options: Array.isArray(n?.options) ? n.options.map(str).filter(Boolean).slice(0, 4) : [] }))
        .filter(n => n.ask !== '')
    : []
  const plan: Step[] = Array.isArray(o.plan)
    ? o.plan
        .map(p => ({ item: str(p?.item), state: ['done', 'next', 'waiting'].includes(p?.state) ? p.state : 'waiting' }))
        .filter(p => p.item !== '')
    : []
  const g = typeof o.gate === 'object' && o.gate !== null ? (o.gate as Record<string, unknown>) : undefined
  const gate: Gate | undefined = g && str(g.thread) ? { thread: str(g.thread), size: str(g.size), recommend: str(g.recommend) } : undefined
  const kind = KINDS.find(k => k === str(o.kind))
  const status: Status = { goal: str(o.goal), now: str(o.now), needs: gate ? [gateNeed(gate), ...needs.filter(n => !(n.options.some(o => /fork/i.test(o)) && n.options.some(o => /park/i.test(o))))].slice(0, 4) : needs, next: str(o.next), plan }
  if (kind) status.kind = kind
  if (str(o.done)) status.done = str(o.done)
  if (gate) status.gate = gate
  return status.goal || status.now ? status : undefined
}

// The last well-formed tag in a reply.
export function parseStatus(text: string): Status | undefined {
  const found = [...text.matchAll(TAG)]
  for (const m of found.reverse()) {
    try {
      const s = toStatus(JSON.parse(m[1]!))
      if (s) return s
    } catch {}
  }
  return undefined
}

// A reply as drawn: every tag gone, and a tag still streaming in cut from its opening.
export function stripStatus(text: string): string {
  return text.replace(TAG, '').replace(/<status>[\s\S]*$/, '').trimEnd()
}

const MARK = { done: '✅', next: '▶', waiting: '⏳' } as const

export function toMarkdown(s: Status): string {
  const needs = s.needs.length === 0 ? 'Nothing.' : s.needs.map(n => `${n.ask}${n.options.length ? ` (${n.options.join(' / ')})` : ''}`).join('; ')
  const plan = s.plan.length ? `\n**Plan:**\n${s.plan.map(p => `- ${MARK[p.state]} ${p.item}`).join('\n')}` : ''
  return `**Goal:** ${s.goal}${s.done ? `\n**Done when:** ${s.done}` : ''}\n**Now:** ${s.now}\n**Needs you:** ${needs}\n**Next:** ${s.next}${plan}`
}

export const markOf = (state: Step['state']) => MARK[state]

export const progressOf = (plan: readonly Step[]) => (plan.length ? `${plan.filter(p => p.state === 'done').length}/${plan.length} done` : '')

// The status once a prompt is sent: a question that prompt answers (its `ask → ` line) leaves Needs you.
export function dropAnswered(s: Status, prompt: string): Status {
  const needs = s.needs.filter(n => !prompt.includes(`${n.ask} → `))
  return needs.length === s.needs.length ? s : { ...s, needs }
}

// A prompt that answers none of the open questions answered them in its own words or moved on: they go, and the status is older than the prompt.
// With no question open, a prompt is a follow-up and the status stands.
export function afterPrompt(s: Status, prompt: string): { status: Status; isStale: boolean } {
  if (s.needs.length === 0) return { status: s, isStale: false }
  const d = dropAnswered(s, prompt)
  return d === s ? { status: { ...s, needs: [] }, isStale: true } : { status: d, isStale: false }
}

// What pressing an answer puts in the prompt box: the question it answers, then the answer, ready to edit.
export const draftFor = (need: Need, option: string) => `${need.ask} → ${option}`

// The draft after an answer was appended: an earlier answer to the same question gives way to the newest, other text stays.
export function mergeAnswer(draft: string, need: Need, option: string): string {
  const head = `${need.ask} → `
  const line = draftFor(need, option)
  const lines = draft.split('\n')
  const last = lines.lastIndexOf(line)
  return lines
    .filter((l, i) => i === last || !l.startsWith(head))
    .join('\n')
    .replace(/^\n+/, '')
}
