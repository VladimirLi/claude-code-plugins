import type { EngineInterface, Register } from 'claude-code'

import type { Analysis, Answer, Question } from './logic'
import { AGENT_DECIDES, analysisPrompt, decide, gapsIn, parseAnalysis, parseList, rewritePrompt, rewriteSystem, templateFor } from './logic'

const HELP = `/braindump                      show the settings
/braindump confirm|auto|off|on  set the mode (on = confirm)
/braindump min <words>          set the length that counts as a brain dump
/braindump sections <a, b, c>   set the required sections for a request no intent fits
/braindump rounds <n>           how many times gaps are checked and asked
/braindump questions on|off     ask about gaps or not
/braindump conversation on|off  find gaps with the conversation as context
/braindump review on|off        put the rewrite in the prompt box, or send it at once
/braindump history on|off       keep a local log of brain dumps and rewrites, or not
/braindump original             put your last original brain dump back in the prompt box`

type Cfg = { model: string; maxQuestions: number; required: string[]; useConversation: boolean }

// The engine does not keep a dropped prompt, so this opt-in history is the only record of what was said next to its rewrite.
async function logHistory($: EngineInterface, entry: Record<string, unknown>): Promise<unknown> {
  const line = JSON.stringify({ ts: new Date().toISOString(), session: await $.session.id().catch(() => ''), ...entry })
  return $.process.run(['/bin/sh', '-c', 'mkdir -p "$HOME/.claude/brain-dump" && printf "%s\\n" "$1" >> "$HOME/.claude/brain-dump/history.jsonl"', 'sh', line]).catch(() => undefined)
}

// A dismissed dialog answers undefined, so a closed question never blocks the prompt.
function ask($: EngineInterface, q: Question) {
  return $.ui.ask(q.question, { options: q.options, header: q.header }).catch(() => undefined)
}

// The batch askAll opens: $.ui.ask takes one question (a plugin may not call AskUserQuestion itself), so the
// tool.call hook in register widens that dialog to every question in tabs and keeps all the answers here.
let tabs: { questions: readonly Question[]; answers?: Record<string, string> } | undefined

// Asks the questions as tabs of one dialog (4 per dialog, the tool's limit); false once the person closes one, which stops all further asking.
async function askAll($: EngineInterface, questions: readonly Question[], answers: Answer[]): Promise<boolean> {
  for (let i = 0; i < questions.length; i += 4) {
    const batch = questions.slice(i, i + 4)
    tabs = { questions: batch }
    const first = await ask($, batch[0]!)
    const given = tabs.answers ?? (first === undefined ? undefined : { [batch[0]!.question]: first })
    tabs = undefined
    if (given === undefined) return false
    // A question left unanswered stays a gap for the next round.
    for (const q of batch) {
      const answer = given[q.question]
      if (typeof answer === 'string') answers.push({ question: q.question, answer, header: q.header })
    }
  }
  return true
}

// A fork reads the conversation from the prompt cache, so it can resolve references; with nothing to fork, the brain dump alone.
async function analyze($: EngineInterface, dump: string, answers: readonly Answer[], cfg: Cfg): Promise<Analysis> {
  if (cfg.useConversation) {
    const f = await $.model.fork({ prompt: analysisPrompt(dump, answers, cfg.required, cfg.maxQuestions, true) })
    if (f.isAnswered) return parseAnalysis(f.text, cfg.maxQuestions)
  }
  const r = await $.model.complete({ model: cfg.model, prompt: analysisPrompt(dump, answers, cfg.required, cfg.maxQuestions, false), maxTokens: 2000, timeoutMs: 60000 })
  // Without the conversation there is nothing a reference could be resolved from.
  return r.isAnswered ? { ...parseAnalysis(r.text, cfg.maxQuestions), resolved: [] } : { intent: 'general', resolved: [], questions: [] }
}

export const register: Register = (on, options) => {
  const mode = String(options.mode ?? 'confirm')
  const minWords = Number(options.minWords ?? 120)
  const forcePrefix = String(options.forcePrefix ?? 'bd')
  const skipPrefix = String(options.skipPrefix ?? 'raw')
  const askQuestions = options.askQuestions !== false
  const maxRounds = Math.max(1, Number(options.maxRounds ?? 2))
  const review = options.review !== false
  const keepHistory = options.keepHistory === true
  const cfg: Cfg = {
    model: String(options.model ?? 'sonnet'),
    maxQuestions: Math.max(1, Number(options.maxQuestions ?? 3)),
    required: parseList(String(options.requiredSections ?? 'Task, Done when, Scope')),
    useConversation: options.useConversation !== false,
  }

  // Set while a rewrite waits in the prompt box: the next submission is the reviewed prompt, edited or not, and is sent as is.
  let isReviewing = false

  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'braindump', description: 'Brain dump rewriter: show or change its settings', argumentHint: '[confirm|auto|off|min N|sections a, b|rounds N|questions on|off|conversation on|off|review on|off|history on|off|original]' })
    return next(e)
  })

  on('tool.call', { tool: 'AskUserQuestion' }, async ($, e, next) => {
    const batch = tabs
    if (batch === undefined) return next(e)
    const r = await next({ ...e, questions: batch.questions.map(q => ({ question: q.question, header: q.header, multiSelect: false, options: q.options.map(label => ({ label, description: '' })) })) })
    if (r.result) batch.answers = (r.result as { answers: Record<string, string> }).answers
    return r
  }).catch(($, e, next) => next(e)) // a failure shows the question as the model asked it

  on('prompt.submit', async ($, e, next) => {
    // Only what the person typed or dictated: the terminal, or Remote Control from a phone.
    if (e.command || e.isMeta || (e.origin.kind !== 'composer' && e.origin.kind !== 'bridge')) return next(e)
    if (isReviewing) {
      isReviewing = false
      return next(e)
    }

    const d = decide(e.text, { mode, minWords, forcePrefix, skipPrefix })
    if (!d.rewrite) return d.text === e.text ? next(e) : next({ ...e, text: d.text })

    if (d.by === 'length' && mode === 'confirm') {
      const choice = await ask($, { question: `This looks like a brain dump (${d.words} words). Rewrite it into a structured prompt first?`, header: 'Brain dump', options: ['Rewrite', 'Send as is'] })
      if (choice !== 'Rewrite') {
        if (keepHistory) void logHistory($, { action: 'declined', original: e.text })
        return next(e)
      }
    }

    await $.store.set('lastOriginal', d.text)
    try {
      const answers: Answer[] = []
      // The analysis also picks the intent, so it runs even when no questions are asked.
      // Says what set it off, so an unexpected rewrite is easy to explain and avoid next time.
      const why = d.by === 'phrase' ? 'you said it is a brain dump' : d.by === 'prefix' ? `"${forcePrefix}"` : `${d.words} words`
      $.ui.status(`brain-dump: reading the brain dump (${why})…`)
      const a = await analyze($, d.text, answers, cfg)
      const template = templateFor(a.intent, cfg.required)
      const resolved = a.resolved
      let isAsking = askQuestions && (await askAll($, a.questions, answers))

      let rewritten = ''
      for (let round = 1; ; round++) {
        $.ui.status('brain-dump: writing the prompt…')
        const r = await $.model.complete({ model: cfg.model, system: rewriteSystem(template), prompt: rewritePrompt(d.text, answers, resolved), maxTokens: 4000, timeoutMs: 120000 })
        if (!r.isAnswered || r.text.trim() === '') {
          $.ui.toast(`brain-dump: rewrite failed (${r.isAnswered ? 'empty reply' : r.reason}); sent your original`)
          return next({ ...e, text: d.text })
        }
        rewritten = r.text.trim()
        // The finished rewrite is checked: a required section it lacks, or a point it could not settle, goes back to the person.
        const gaps = gapsIn(rewritten, template.required, answers).slice(0, cfg.maxQuestions)
        if (gaps.length === 0 || !isAsking || round >= maxRounds) break
        const before = answers.length
        isAsking = await askAll($, gaps, answers)
        if (answers.length === before) break
      }

      if (keepHistory) void logHistory($, { action: `rewrite:${d.by}`, original: d.text, intent: a.intent, answers, rewrite: rewritten })
      const left = gapsIn(rewritten, template.required, answers).length
      const delegated = answers.filter(a => a.answer === AGENT_DECIDES).length
      if (left > 0 || delegated > 0) {
        $.ui.toast(`brain-dump: ${left > 0 ? `${left} gap(s) still open, see "Unresolved" or the missing sections` : ''}${left > 0 && delegated > 0 ? ' · ' : ''}${delegated > 0 ? `${delegated} left to the agent` : ''}`, { timeoutMs: 8000 })
      }
      if (!review) {
        $.ui.toast(`brain-dump: read as a ${template.label}`)
        return next({ ...e, text: rewritten })
      }

      isReviewing = true
      // After the drop settles, so the engine's own clearing of the box does not wipe the rewrite.
      $.clock.after(150, () => void $.prompt.fill({ text: rewritten, mode: 'replace' }))
      return { drop: `brain-dump: read as a ${template.label}. The rewrite is in the prompt box. Edit it, then press Enter. /braindump original restores what you said.` }
    } finally {
      $.ui.status(undefined)
    }
  }).catch(($, e, next) => next(e)) // a failure sends the prompt as typed, never loses it

  on('command.run', { command: 'braindump' }, async ($, e) => {
    const args = e.args.trim()
    const [word = '', value = ''] = args.split(/\s+/)
    const rest = args.slice(word.length).trim()
    const set = async (field: string, v: string | number | boolean) => {
      await $.config.set({ key: `brain-dump.${field}`, value: v })
      return { text: `brain-dump: ${field} = ${v}` }
    }
    const onOff = value === 'on' || value === 'off'
    if (['confirm', 'auto', 'off'].includes(word)) return set('mode', word)
    // People type "on" for the opposite of off; the default mode is what on means.
    if (word === 'on') return set('mode', 'confirm')
    if (word === 'min' && Number(value) > 0) return set('minWords', Math.round(Number(value)))
    if (word === 'rounds' && Number(value) >= 1) return set('maxRounds', Math.round(Number(value)))
    if (word === 'sections' && parseList(rest).length > 0) return set('requiredSections', parseList(rest).join(', '))
    if (word === 'questions' && onOff) return set('askQuestions', value === 'on')
    if (word === 'conversation' && onOff) return set('useConversation', value === 'on')
    if (word === 'review' && onOff) return set('review', value === 'on')
    if (word === 'history' && onOff) return set('keepHistory', value === 'on')
    if (word === 'original') {
      const original = await $.store.get('lastOriginal')
      if (typeof original !== 'string') return { text: 'brain-dump: no original saved yet.' }
      isReviewing = true
      await $.prompt.fill({ text: original, mode: 'replace' })
      return { text: 'brain-dump: your original is back in the prompt box; it is sent as is.' }
    }
    const force = forcePrefix.trim() === '' ? 'say "this is a brain dump"' : `"${forcePrefix}" first or last, or say "this is a brain dump"`
    const skip = skipPrefix.trim() === '' ? 'none' : `"${skipPrefix}" first`
    const status = `mode ${mode} · rewrite at ${minWords}+ words · force: ${force} · skip: ${skip} · required (no intent) ${cfg.required.join(', ')} · questions ${askQuestions ? `on (max ${cfg.maxQuestions} per round, ${maxRounds} rounds, conversation ${cfg.useConversation ? 'on' : 'off'})` : 'off'} · review ${review ? 'on' : 'off'} · history ${keepHistory ? 'on' : 'off'} · model ${cfg.model}`
    return { text: word === '' || word === 'status' ? `brain-dump: ${status}` : `brain-dump: unknown option "${args}"\n${HELP}` }
  })
}
