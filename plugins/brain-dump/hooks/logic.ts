export type Trigger = {
  mode: string
  minWords: number
  forcePrefix: string
  skipPrefix: string
  gauntletPrefix: string
}

export type Decision =
  | { rewrite: false; text: string }
  | { rewrite: true; text: string; by: 'prefix' | 'phrase' | 'length'; words: number; intent?: 'gauntlet' }

export type Question = { question: string; header: string; options: string[] }
export type Answer = { question: string; answer: string; header?: string }
export type Analysis = { intent: string; resolved: string[]; questions: Question[] }

// Offered with every question, so a gap is never filled by a guess the person did not choose.
export const AGENT_DECIDES = 'Let the agent decide'
export const LEAVE_OUT = 'Leave it out'

// Every section any template may write, with what it holds. "Unresolved" is the rewrite's own report of what it could not settle.
const SECTION_TEXT: Record<string, string> = {
  Task: 'what to do, in one to three sentences.',
  Question: 'what the user wants to know, in one to three sentences.',
  Target: 'what to review: the diff, branch, PR or files.',
  Symptom: 'what goes wrong, as the user sees it.',
  Evidence: 'error text, logs or steps that reproduce it, kept verbatim.',
  Expected: 'what should happen instead.',
  Why: 'the goal or context behind it.',
  Scope: 'which repo, branch, files or area, and what to leave alone.',
  Steps: 'numbered steps in the order the user gave, for each item when there are several.',
  Checkpoints: 'bullets for where the agent stops and waits for the user before going on.',
  'Pattern to follow':'an existing feature, file or pattern in the codebase to copy.',
  'Behavior to keep': 'bullets for what must not change for users or callers.',
  'Where to look': 'bullets for the files, history, docs or sources to read, and which sources to trust.',
  Criteria: 'bullets for what to check it against.',
  'What counts as a finding': 'what to report and what to ignore.',
  'Requirements and guardrails': 'bullets for constraints, preferences and what not to do.',
  Deliverable: 'the form of the result: a reply, a file, an issue, a proposal.',
  'After that': 'what to do once the answer is in, only if the user said so.',
  'Done when': 'bullets for how the result will be checked.',
  'Agent decides': 'bullets for decisions the user left to the agent.',
  Goal: 'the opening instruction, one or two sentences that start with a verb ("Build a landing page for...", "Write a 2000-word explainer on..."), with every concrete detail the user gave about the result.',
  Bar: 'the one named, fetchable thing the result must beat, as a short noun phrase with its URL, product, repo, author or title ("Nike\'s current running campaign page"). Never a category.',
  'Getting the bar': 'one sentence on how to get the real bar, only if the user said or it is clear from the bar ("Screenshot it at desktop and mobile.", "Pull three of her actual posts.").',
  'Judged on': 'what the critic decides between ours and the bar, as a clause that starts with "which" ("which one a non-engineer would understand faster").',
  Pieces: 'the pieces the user named to split the work into, as a comma-separated list. Only pieces the user named.',
  'Measurable half': 'one sentence naming a number the result must also beat (load time, benchmark, pass rate), only if the user named one.',
  Budget: 'the budget or cost ceiling as a short noun phrase ("$30 of API spend"), only if the user named one.',
  'Tools needed': 'the tools the goal needs as a comma-separated list ("image generation, a browser"), only if the user named them.',
  Unresolved: 'bullets, only for what you could not settle.',
}
export const SECTIONS = Object.keys(SECTION_TEXT)

// Each intent has its own sections: a bug needs its symptom, a question needs no "Done when".
export type Template = { label: string; when: string; sections: string[]; required: string[]; ask: string }

export const INTENTS: Record<string, Template> = {
  bug: {
    label: 'bug fix',
    when: 'something is broken, failing or wrong',
    sections: ['Symptom', 'Evidence', 'Expected', 'Scope', 'Requirements and guardrails', 'Done when'],
    required: ['Symptom', 'Done when'],
    ask: 'Ask for the error text or steps to reproduce when they are missing. A likely "Done when" option is "A failing test reproduces it, then passes".',
  },
  feature: {
    label: 'new feature',
    when: 'build or add something new',
    sections: ['Task', 'Why', 'Scope', 'Pattern to follow', 'Requirements and guardrails', 'Done when'],
    required: ['Task', 'Scope', 'Done when'],
    ask: 'Ask whether an existing feature or file is the pattern to follow when none is named.',
  },
  change: {
    label: 'refactor or change',
    when: 'refactor, simplify, rename, migrate or replace existing code',
    sections: ['Task', 'Why', 'Behavior to keep', 'Scope', 'Requirements and guardrails', 'Done when'],
    required: ['Task', 'Behavior to keep'],
    ask: 'A likely "Done when" option is "The existing tests pass unchanged".',
  },
  question: {
    label: 'question or research',
    when: 'explain, investigate, research or propose, without a code change asked yet',
    sections: ['Question', 'Why', 'Where to look', 'Requirements and guardrails', 'Deliverable', 'After that'],
    required: ['Question', 'Deliverable'],
    ask: 'Never ask for "Done when". A likely "Deliverable" option is "An answer in the chat".',
  },
  review: {
    label: 'review',
    when: 'review a diff, PR, branch or files',
    sections: ['Target', 'Criteria', 'What counts as a finding', 'Requirements and guardrails'],
    required: ['Target'],
    ask: 'A likely "What counts as a finding" option is "Only what affects correctness or the stated requirements".',
  },
  workflow: {
    label: 'workflow',
    when: 'run a multi-step process, often over several items (PRs, issues, decisions), with points where the user approves',
    sections: ['Task', 'Steps', 'Checkpoints', 'Scope', 'Requirements and guardrails', 'Done when'],
    required: ['Task', 'Steps'],
    ask: 'Ask where the agent should stop and wait for the user when the brain dump does not say.',
  },
  // Only a gauntlet trigger picks it: its prompt is the Gauntlet Loop template, filled by fillGauntlet.
  gauntlet: {
    label: 'gauntlet loop',
    when: 'a prompt that makes an agent loop a builder and a harsh critic until the work beats a named reference',
    sections: ['Goal', 'Bar', 'Getting the bar', 'Judged on', 'Pieces', 'Measurable half', 'Budget', 'Tools needed'],
    required: ['Goal', 'Bar', 'Judged on'],
    ask: `The bar must be named (a specific thing, not a category), fetchable (the critic can screenshot, read, run or open it) and comparable (the two can sit side by side and a judge can pick one). When the brain dump and the conversation name no bar that passes all three, or name a vague one ("award-winning sites"), ask for it with header "Bar" and offer 2 or 3 candidate bars that pass, one short line each with its URL when it has one, hardest reachable first. Each candidate is itself a real, named thing; never a placeholder, a category or "name one". When nothing published fits, a named, fetchable spec, test suite or benchmark counts. Ask for "Judged on" when the goal does not make it clear, offering the likely answer. Ask about a measurable half (load time, benchmark, word count, pass rate) when the goal has one and the user named none. Never ask about pieces, architecture, stack, round counts, budget or tools.`,
  },
}

// A dump that does not trigger a gauntlet loop is never read as one.
const AUTO_INTENTS = Object.keys(INTENTS).filter(k => k !== 'gauntlet')

// The fallback for a brain dump no intent fits, with the person's own required sections.
export function templateFor(intent: string, generalRequired: readonly string[]): Template {
  if (Object.hasOwn(INTENTS, intent)) return INTENTS[intent]!
  return { label: 'general request', when: 'none of the above fits', sections: ['Task', 'Why', 'Scope', 'Requirements and guardrails', 'Done when'], required: [...generalRequired], ask: '' }
}

const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

// Dictation spells a short prefix as letters ("B D." for "bd"), so dots and spaces between its letters are allowed.
const spell = (word: string) => [...word].map(escape).join('[\\s.]*')

// The prefix as a whole first word, followed by a space or punctuation: "bd: ..." or "bd ...".
function stripPrefix(text: string, prefix: string): string | undefined {
  const p = prefix.trim()
  if (p === '') return undefined
  const m = text.match(new RegExp(`^\\s*${spell(p)}(?:[\\s:,.;-]+|$)`, 'i'))
  return m ? text.slice(m[0].length) : undefined
}

// The force prefix said last instead, as dictation often ends with it: "... what I mean? bd".
function stripSuffix(text: string, word: string): string | undefined {
  const w = word.trim()
  if (w === '') return undefined
  const m = text.match(new RegExp(`(?:^|[\\s.,;:!?])${spell(w)}[\\s.!?]*$`, 'i'))
  return m ? text.slice(0, m.index).trimEnd() : undefined
}

// "This is a brain dump" declares one only at the start of a sentence or clause, after a few filler words at most:
// "So, this is a brain dump." counts, while "when I say this is a brain dump" or a quoted "this is a brain dump" is a mention.
// Negations ("this isn't a brain dump") never match, and dictation may type a curly apostrophe.
const PHRASE =
  /(?:^|[.!?;:,\n])\s*(?:(?:ok(?:ay)?|so|alright|right|well|hi|hey|um+|uh+|and|anyway|now)[\s,]+){0,3}(?:this|it|here)(?:\s+is|\s+will\s+be|['’](?:s|ll\s+be))\s+(?:(?:just|basically|really|kind\s+of|sort\s+of|more\s+of|going\s+to\s+be|gonna\s+be)\s+)?(?:a|the|my|another)\s+(?:quick\s+|long\s+|big\s+)?brain[\s-]?dump\b/i

export const countWords = (text: string) => text.split(/\s+/).filter(Boolean).length
const hasWords = (text: string) => /[\p{L}\p{N}]/u.test(text)

// "Make a gauntlet loop" asks for one; "the gauntlet loop skill" only mentions it.
const GAUNTLET_PHRASE = /\b(?:make|create|build|write|set\s+up)\s+(?:me\s+|us\s+)?(?:a|an|another)\s+gauntlet[\s-]?loop\b/i

export function decide(text: string, t: Trigger): Decision {
  const skipped = stripPrefix(text, t.skipPrefix)
  if (skipped !== undefined) return { rewrite: false, text: skipped }
  const gauntlet = stripPrefix(text, t.gauntletPrefix) ?? stripSuffix(text, t.gauntletPrefix)
  if (gauntlet !== undefined) return hasWords(gauntlet) ? { rewrite: true, text: gauntlet, by: 'prefix', words: countWords(gauntlet), intent: 'gauntlet' } : { rewrite: false, text }
  if (GAUNTLET_PHRASE.test(text)) return { rewrite: true, text, by: 'phrase', words: countWords(text), intent: 'gauntlet' }
  const forced = stripPrefix(text, t.forcePrefix) ?? stripSuffix(text, t.forcePrefix)
  // A forced prompt with nothing in it but the trigger has nothing to rewrite.
  if (forced !== undefined) return hasWords(forced) ? { rewrite: true, text: forced, by: 'prefix', words: countWords(forced) } : { rewrite: false, text }
  // The phrase stays in the text: it reads naturally mid-dictation and tells the rewrite what it is reading.
  const phrase = text.match(PHRASE)
  if (phrase) return hasWords(text.replace(phrase[0], ' ')) ? { rewrite: true, text, by: 'phrase', words: countWords(text) } : { rewrite: false, text }
  const words = countWords(text)
  if (t.mode === 'off' || words < t.minWords) return { rewrite: false, text }
  return { rewrite: true, text, by: 'length', words }
}

export const parseList = (csv: string) =>
  csv
    .split(',')
    .map(s => s.trim())
    .filter(Boolean)

// The model is asked for JSON; take the first object in its reply and keep only well-formed questions.
export function parseAnalysis(reply: string, max: number): Analysis {
  const start = reply.indexOf('{')
  const end = reply.lastIndexOf('}')
  if (start < 0 || end <= start) return { intent: 'general', resolved: [], questions: [] }
  let parsed: { intent?: unknown; questions?: unknown; resolved?: unknown }
  try {
    parsed = JSON.parse(reply.slice(start, end + 1))
  } catch {
    return { intent: 'general', resolved: [], questions: [] }
  }
  const intent = typeof parsed.intent === 'string' && Object.hasOwn(INTENTS, parsed.intent) ? parsed.intent : 'general'
  const resolved = Array.isArray(parsed.resolved) ? parsed.resolved.filter((r): r is string => typeof r === 'string' && r !== '') : []
  const list = Array.isArray(parsed.questions) ? parsed.questions : []
  const questions = list
    .filter((q): q is Question => typeof q?.question === 'string' && q.question !== '')
    .slice(0, Math.max(0, max))
    .map(q => toQuestion(q.question, String(q.header ?? 'Brain dump'), Array.isArray(q.options) ? q.options : []))
  return { intent, resolved, questions }
}

// The model's suggested answers (at most two), then the two ways out; typing an answer is the dialog's "Other".
// A gauntlet bar with candidates offers only them (and Other): a bar left to a guess is how the loop fails.
export function toQuestion(question: string, header: string, suggested: readonly unknown[]): Question {
  const valid = suggested.filter((o): o is string => typeof o === 'string' && o !== '' && o !== AGENT_DECIDES && o !== LEAVE_OUT)
  if (header === 'Bar' && valid.length >= 2) return { question, header, options: valid.slice(0, 3) }
  const own = valid.slice(0, 2)
  return { question, header: header.slice(0, 12), options: [...own, AGENT_DECIDES, ...(own.length < 2 ? [LEAVE_OUT] : [])] }
}

// The rewrite's sections by heading: a line "Heading:" opens one, its text runs to the next heading.
export function parseSections(text: string): Map<string, string> {
  const heads = SECTIONS.map(escape).join('|')
  const re = new RegExp(`^\\s*(?:#+\\s*)?\\**(${heads})\\**\\s*:\\**[ \\t]*`, 'gim')
  const marks = [...text.matchAll(re)]
  const out = new Map<string, string>()
  marks.forEach((m, i) => {
    const name = SECTIONS.find(s => s.toLowerCase() === m[1]!.toLowerCase())!
    const body = text.slice(m.index! + m[0].length, marks[i + 1]?.index ?? text.length).trim()
    out.set(name, body)
  })
  return out
}

const bullets = (body: string) =>
  body
    .split('\n')
    .map(l => l.replace(/^\s*(?:[-*•]|\d+[.)])\s*/, '').trim())
    .filter(Boolean)

// A required section counts as covered when it has text, or the person already left it to the agent or left it out.
function isCovered(section: string, sections: Map<string, string>, answers: readonly Answer[]): boolean {
  const name = SECTIONS.find(s => s.toLowerCase() === section.toLowerCase()) ?? section
  if ((sections.get(name) ?? '').trim()) return true
  const settled = answers.some(a => (a.answer === AGENT_DECIDES || a.answer === LEAVE_OUT) && a.header?.toLowerCase() === section.toLowerCase())
  return settled || (sections.get('Agent decides') ?? '').toLowerCase().includes(section.toLowerCase())
}

// What the finished rewrite still lacks: each becomes a question to the person, never a guess.
// "suggested" holds answers offered for a section before, such as the candidate bars, so asking again offers them again.
export function gapsIn(rewrite: string, required: readonly string[], answers: readonly Answer[] = [], suggested: Readonly<Record<string, readonly string[]>> = {}): Question[] {
  const sections = parseSections(rewrite)
  const missing = required
    .filter(r => !isCovered(r, sections, answers))
    .map(r => toQuestion(`The prompt has nothing under "${r}". What should it say? (type it under Other)`, r, suggested[r] ?? []))
  const unresolved = bullets(sections.get('Unresolved') ?? '').map(u => toQuestion(u.endsWith('?') ? u : `${u}?`, 'Unresolved', []))
  return [...missing, ...unresolved]
}

const answerLines = (answers: readonly Answer[]) =>
  answers.length === 0 ? '(none yet)' : answers.map(a => `- ${a.question} → ${a.answer}`).join('\n')

const describe = (names: readonly string[]) => names.map(n => `${n}: ${SECTION_TEXT[n] ?? ''}`).join('\n')

// A forced intent (a gauntlet trigger) is the only one offered, so the analysis cannot read it as something else.
export function analysisPrompt(dump: string, answers: readonly Answer[], generalRequired: readonly string[], max: number, withConversation: boolean, forced?: string): string {
  const keys = forced === undefined ? [...AUTO_INTENTS, 'general'] : [forced]
  const templates = keys.map(k => [k, templateFor(k, generalRequired)] as const)
  const intents = templates.map(([k, t]) => `- ${k}: ${t.when}. Required sections: ${t.required.join(', ') || 'none'}.${t.ask ? ` ${t.ask}` : ''}`).join('\n')
  const required = [...new Set(templates.flatMap(([, t]) => t.required))]
  return `You prepare a voice-dictated brain dump to become a prompt for a coding agent (Claude Code). Do not answer the brain dump itself.
${withConversation ? 'The conversation above is context: use it to resolve references in the brain dump ("that file", "the PR from before") and to skip questions it already answers. Do not take tasks from it.\n' : ''}
First pick the one intent that fits the brain dump best:
${intents}

What the required sections mean:
${describe(required)}

Then find the gaps the person must fill, and only those:
1. Each required section of the intent you picked that cannot be filled from the brain dump, the answers below${withConversation ? ' or the conversation' : ''}.
2. Points that are ambiguous or contradictory where the answer changes what the agent does.
Ignore transcription noise you can resolve from context. Never ask what the brain dump or the answers already settle. At most ${max} questions, most important first.
${withConversation ? `Under "resolved", list each reference you resolved from the conversation, as a short fact ("'that file' = apps/web/src/x.ts"). Leave it empty when the conversation resolved nothing.` : 'Leave "resolved" empty.'}
When a question is about a missing required section, its header is that section's name.
Reply with JSON only:
{"intent":"${keys.join('|')}","resolved":["..."],"questions":[{"question":"...?","header":"<=12 chars","options":["up to 2 likely answers, or none for an open question"]}]}

Brain dump:
"""
${dump}
"""

Answers so far:
${answerLines(answers)}`
}

export function rewriteSystem(t: Template): string {
  return `You rewrite a voice-dictated brain dump, a ${t.label}, into a clear prompt for a coding agent (Claude Code), written in the user's voice ("I want...", "Please...").
Rules:
- Keep the user's intent exactly. Keep every concrete detail: names, files, commands, numbers, URLs, constraints, preferences.
- Fix transcription errors only where the meaning is clear from context. Keep technical terms as the user meant them.
- Drop filler, repetition and false starts. Use the user's answers to settle what was unclear. Use the resolved references to replace vague references.
- Never guess, and never add requirements, scope or steps the user did not ask for.
- Keep the user's own words for conditions and criteria ("if there are issues" stays that, never "if it is risky"). A section's description never adds content of its own.
- Say each point once, in the section that fits it best.
- An answer "${AGENT_DECIDES}" goes under "Agent decides" as one bullet naming the decision. An answer "${LEAVE_OUT}" means the point is left out entirely.
- When the user left a whole section to the agent, write that section as exactly "Left to the agent.", with nothing added; when they chose "${LEAVE_OUT}" for it, write "None.".
- Anything still ambiguous or contradictory goes under "Unresolved" as one question per bullet; never settle it yourself.
- Plain, concise English.
Use these sections, each as a line "Name:" followed by its text, in this order:
${describe([...t.sections, 'Agent decides', 'Unresolved'])}
Write a section only from what the user actually said or answered. Restating the task, or generic text such as "in the codebase you work in" or "a check shows it is faster", does not count: leave that section out instead, so the user is asked for it. Leave out any section that would be empty.
Reply with the rewritten prompt only, no preamble.`
}

export function rewritePrompt(dump: string, answers: readonly Answer[], resolved: readonly string[]): string {
  const qa = answers.length === 0 ? '' : `\n\nThe user's answers to clarifying questions:\n${answerLines(answers)}`
  const refs = resolved.length === 0 ? '' : `\n\nResolved references:\n${resolved.map(r => `- ${r}`).join('\n')}`
  return `Brain dump:\n"""\n${dump}\n"""${qa}${refs}`
}

const LEFT = /^(?:left to the agent|none)\.?$/i

// A section's text as one line; a section left to the agent or left out counts as empty.
function line(sections: Map<string, string>, name: string): string {
  const text = bullets(sections.get(name) ?? '').join(', ')
  return LEFT.test(text) ? '' : text
}

const sentence = (s: string) => (s === '' || /[.!?]$/.test(s) ? s : `${s}.`)
const clause = (s: string) => s.replace(/[.!?]+$/, '')

// The Gauntlet Loop template (github.com/robonuggets/gauntlet-loop), filled from the rewrite's sections.
// The fixed lines are written here, never by the model, so none can be dropped or softened.
export function fillGauntlet(rewrite: string): string {
  const s = parseSections(rewrite)
  const bar = clause(line(s, 'Bar'))
  const pieces = line(s, 'Pieces')
  const judged = clause(line(s, 'Judged on')) || 'which one is better'
  const tools = clause(line(s, 'Tools needed'))
  const budget = clause(line(s, 'Budget'))
  const extras = [tools && `Use ${tools}.`, budget && `Stay within a budget of ${budget}.`].filter(Boolean).join(' ')
  const unresolved = bullets(s.get('Unresolved') ?? '')
  return [
    sentence(line(s, 'Goal')),
    [
      bar ? `The bar is ${bar.replace(/^(?:A|An|The)\b/, w => w.toLowerCase())}.` : 'Before you build, pick the hardest named reference you can actually get, a specific page, piece, repo or title, and make it the bar.',
      sentence(line(s, 'Measurable half')),
      line(s, 'Getting the bar') ? `${sentence(line(s, 'Getting the bar'))} Compare against it directly, not against a description of it.` : 'Get the real thing first and compare against it directly, not against a description of it.',
    ].filter(Boolean).join(' '),
    `Break this into the smallest pieces that can be improved and judged on their own${pieces ? ` - ${clause(pieces)}` : ''}. For each piece, fan out a builder and a separate critic with fresh context. The critic inspects the actual output, puts it next to the bar blind with the labels stripped, says ${judged}, and names the single biggest remaining gap. Then it goes back to the builder.`,
    extras,
    'The critic should be a harsh critic. Praise is not useful. If ours does not win, it keeps going.',
    '/loop on each piece until the critic picks ours blind. Do not stop before that.',
    'Keep a live progress page updating as the work evolves so I can watch it.',
    'Fan out subagents and ultracode.',
    // Left in for the person to settle while reviewing; gapsIn reads them from the rewrite, not from here.
    unresolved.length > 0 ? `Unresolved:\n${unresolved.map(u => `- ${u}`).join('\n')}` : '',
  ]
    .filter(Boolean)
    .join('\n\n')
}
