import { expect, test } from 'claude-code/testing'

import { AGENT_DECIDES, INTENTS, LEAVE_OUT, analysisPrompt, decide, fillGauntlet, gapsIn, parseAnalysis, parseSections, rewriteSystem, templateFor } from '../hooks/logic'

const T = { mode: 'confirm', minWords: 5, forcePrefix: 'bd', skipPrefix: 'raw', gauntletPrefix: 'gl' }
const REQUIRED = ['Task', 'Done when', 'Scope']

test('length, prefixes and mode decide when a prompt is rewritten', async () => {
  expect(decide('fix the bug', T)).toEqual({ rewrite: false, text: 'fix the bug' })
  expect(decide('one two three four five', T)).toEqual({ rewrite: true, text: 'one two three four five', by: 'length', words: 5 })
  expect(decide('bd: fix it', T)).toEqual({ rewrite: true, text: 'fix it', by: 'prefix', words: 2 })
  expect(decide('raw one two three four five', T)).toEqual({ rewrite: false, text: 'one two three four five' })
  expect(decide('one two three four five', { ...T, mode: 'off' }).rewrite).toBe(false)
  expect(decide('bd fix it', { ...T, mode: 'off' }).rewrite).toBe(true)
  // Dictated prefixes come out spelled.
  expect(decide('B D. Okay, how do I test this?', T)).toEqual({ rewrite: true, text: 'Okay, how do I test this?', by: 'prefix', words: 6 })
  expect(decide('b.d. fix it', T).rewrite).toBe(true)
  // A word that only starts with the prefix is not the prefix.
  expect(decide('bdx', T)).toEqual({ rewrite: false, text: 'bdx' })
})

test('the force prefix also works as the last word, and a trigger with nothing else is not rewritten', async () => {
  expect(decide('so yeah, do you understand what I mean? bd', T)).toEqual({ rewrite: true, text: 'so yeah, do you understand what I mean?', by: 'prefix', words: 8 })
  expect(decide('fix it. B. D.', T)).toEqual({ rewrite: true, text: 'fix it.', by: 'prefix', words: 2 })
  // Part of a word is not the prefix.
  expect(decide('fix abd', T).rewrite).toBe(false)
  expect(decide('bd', T)).toEqual({ rewrite: false, text: 'bd' })
  expect(decide('This is a brain dump.', T)).toEqual({ rewrite: false, text: 'This is a brain dump.' })
})

const DECLARED = [
  'This is a brain dump. Fix it',
  'this is a brain dump: fix it',
  'THIS IS A BRAIN DUMP. fix it',
  'It’s a brain dump. Fix it',
  "It's a braindump, fix it",
  'it is a brain-dump. fix it',
  'Here’s a brain dump. Fix it',
  'Okay, so this is just a brain dump. Fix it',
  'Um, this is gonna be a long brain dump. Fix it',
  'This will be a quick brain dump. Fix it',
  'Fix the login. This is my brain dump about it',
  'Hi.\nThis is a brain dump\nfix it',
  'This is the brain dump. Fix it',
]
const MENTIONED = [
  'the brain dump mod is broken',
  'can you fix brain dump',
  'when I say this is a brain dump it should trigger',
  'make "this is a brain dump" trigger it',
  "This isn't a brain dump, fix it",
  'This is not a brain dump. fix it',
  'Is this a brain dump? fix it',
  'this is a brain dumpster fire',
  'about my brain dump: fix it',
]

test('saying it is a brain dump forces a rewrite; mentioning a brain dump does not', async () => {
  // Length and mode cannot hide a declaration, and minWords is out of reach so only the phrase can trigger.
  const t = { ...T, mode: 'off', minWords: 100 }
  for (const text of DECLARED) expect([text, decide(text, t)]).toEqual([text, { rewrite: true, text, by: 'phrase', words: expect.any(Number) }])
  for (const text of MENTIONED) expect([text, decide(text, { ...T, minWords: 100 }).rewrite]).toEqual([text, false])
  expect(decide('raw this is a brain dump. fix it', T)).toEqual({ rewrite: false, text: 'this is a brain dump. fix it' })
})

test('every question offers the agent-decides way out, and open ones a leave-out', async () => {
  const reply = 'Sure:\n{"resolved":["\'that file\' = a.ts"],"questions":[{"question":"Which branch?","header":"A very long header","options":["dev","main","feature"]},{"question":"What is out of scope?","header":"Scope","options":[]},{"options":["no question"]}]}'
  expect(parseAnalysis(reply, 3)).toEqual({
    intent: 'general',
    resolved: ["'that file' = a.ts"],
    questions: [
      { question: 'Which branch?', header: 'A very long ', options: ['dev', 'main', AGENT_DECIDES] },
      { question: 'What is out of scope?', header: 'Scope', options: [AGENT_DECIDES, LEAVE_OUT] },
    ],
  })
  expect(parseAnalysis(reply, 1).questions).toHaveLength(1)
  expect(parseAnalysis('not json', 3)).toEqual({ intent: 'general', resolved: [], questions: [] })
})

test('a rewrite missing a required section or holding unresolved points has gaps', async () => {
  const full = 'Task: Fix the login bug.\n\nScope:\n- apps/web only\n\nDone when:\n- the login test passes'
  expect(gapsIn(full, REQUIRED)).toEqual([])

  const partial = '**Task:** Fix the login bug.\n\nUnresolved:\n- Which browser\n- Should I add a test?'
  const gaps = gapsIn(partial, REQUIRED)
  expect(gaps.map(g => g.header)).toEqual(['Done when', 'Scope', 'Unresolved', 'Unresolved'])
  expect(gaps[2]!.question).toBe('Which browser?')
  expect(gaps[3]!.options).toEqual([AGENT_DECIDES, LEAVE_OUT])

  expect(parseSections(partial).get('Task')).toBe('Fix the login bug.')
})

test('the analysis picks a known intent, and anything else falls back to the general template', async () => {
  expect(parseAnalysis('{"intent":"bug","questions":[]}', 3).intent).toBe('bug')
  expect(parseAnalysis('{"intent":"toString","questions":[]}', 3).intent).toBe('general')
  expect(parseAnalysis('{"intent":"poem","questions":[]}', 3).intent).toBe('general')
  expect(templateFor('bug', REQUIRED).required).toEqual(['Symptom', 'Done when'])
  expect(templateFor('general', ['Task']).required).toEqual(['Task'])
})

test('each intent is checked against its own required sections', async () => {
  const question = 'Question: Are per-intent templates better?\n\nDeliverable:\n- an answer in the chat'
  // A question needs no "Done when" or "Scope"; the general template would ask for both.
  expect(gapsIn(question, templateFor('question', REQUIRED).required)).toEqual([])
  expect(gapsIn(question, templateFor('general', REQUIRED).required).map(g => g.header)).toEqual(['Task', 'Done when', 'Scope'])
  expect(gapsIn('Done when:\n- the test passes', templateFor('bug', REQUIRED).required).map(g => g.header)).toEqual(['Symptom'])
  expect(parseSections('Symptom: Login fails.\nEvidence:\n- 401 on refresh').get('Evidence')).toBe('- 401 on refresh')
})

test('every intent writes only sections the parser knows, and its rewrite asks for them', async () => {
  for (const [name, t] of Object.entries(INTENTS)) {
    const system = rewriteSystem(t)
    for (const s of [...t.sections, ...t.required]) {
      expect(parseSections(`${s}: x`).get(s), `${name}: ${s}`).toBe('x')
      expect(system).toContain(`${s}:`)
    }
  }
  expect(rewriteSystem(templateFor('question', REQUIRED))).not.toContain('Done when:')
})

test('"gl" first or last, or asking to make a gauntlet loop, reads the prompt as a gauntlet loop', async () => {
  const t = { ...T, mode: 'off', minWords: 100 }
  expect(decide('gl a pricing page for my SaaS', t)).toEqual({ rewrite: true, text: 'a pricing page for my SaaS', by: 'prefix', words: 6, intent: 'gauntlet' })
  expect(decide('a pricing page for my SaaS. G. L.', t)).toEqual({ rewrite: true, text: 'a pricing page for my SaaS.', by: 'prefix', words: 6, intent: 'gauntlet' })
  for (const text of ['Make a gauntlet loop for a pricing page', 'okay so can you make me a gauntlet loop: an explainer', 'Create a gauntlet-loop for a CLI', 'I want you to set up a gauntlet loop for this essay'])
    expect([text, decide(text, t)]).toEqual([text, { rewrite: true, text, by: 'phrase', words: expect.any(Number), intent: 'gauntlet' }])
  for (const text of ['the gauntlet loop skill is neat', 'how does a gauntlet loop work', 'glad it works', 'gl'])
    expect([text, decide(text, t).rewrite]).toEqual([text, false])
  expect(decide('raw make a gauntlet loop', t)).toEqual({ rewrite: false, text: 'make a gauntlet loop' })
})

test('a missing bar offers its candidate bars and no way out; without candidates it falls back', async () => {
  const reply = '{"intent":"gauntlet","questions":[{"question":"Which bar?","header":"Bar","options":["Stripe pricing page","Linear pricing page","Vercel pricing page","Notion"]}]}'
  expect(parseAnalysis(reply, 3).questions[0]!.options).toEqual(['Stripe pricing page', 'Linear pricing page', 'Vercel pricing page'])
  expect(parseAnalysis(reply.replace(',"Linear pricing page","Vercel pricing page","Notion"', ''), 3).questions[0]!.options).toEqual(['Stripe pricing page', AGENT_DECIDES, LEAVE_OUT])

  const required = templateFor('gauntlet', REQUIRED).required
  expect(required).toEqual(['Goal', 'Bar', 'Judged on'])
  const gaps = gapsIn('Goal: Build a pricing page.\n\nJudged on: which one converts', required, [], { Bar: ['Stripe pricing page', 'Linear pricing page'] })
  expect(gaps).toEqual([{ question: expect.stringContaining('"Bar"'), header: 'Bar', options: ['Stripe pricing page', 'Linear pricing page'] }])
})

test('only a gauntlet trigger offers the gauntlet intent to the analysis', async () => {
  expect(analysisPrompt('x', [], REQUIRED, 3, true)).not.toContain('- gauntlet:')
  const forced = analysisPrompt('x', [], REQUIRED, 3, true, 'gauntlet')
  expect(forced).toContain('- gauntlet:')
  expect(forced).not.toContain('- bug:')
  expect(forced).toContain('{"intent":"gauntlet"')
  expect(forced).toContain('fetchable')
})

const FIXED = [
  'The critic should be a harsh critic. Praise is not useful. If ours does not win, it keeps going.',
  '/loop on each piece until the critic picks ours blind. Do not stop before that.',
  'Keep a live progress page updating as the work evolves so I can watch it.',
  'Fan out subagents and ultracode.',
]

test('the gauntlet prompt fills every slot of the template and always keeps its fixed lines', async () => {
  const full = fillGauntlet(
    [
      'Goal: Build a landing page for a running brand. Green and dark, it has to feel alive.',
      "Bar: Nike's current running campaign page.",
      'Getting the bar: Screenshot it at desktop and mobile',
      'Judged on: which one is more alive.',
      'Pieces:\n- hero\n- motion\n- mobile',
      'Measurable half: It also has to load in under 2 seconds.',
      'Budget: $20 of API spend.',
      'Tools needed: a browser for screenshots',
    ].join('\n\n'),
  )
  expect(full).toBe(
    [
      'Build a landing page for a running brand. Green and dark, it has to feel alive.',
      "The bar is Nike's current running campaign page. It also has to load in under 2 seconds. Screenshot it at desktop and mobile. Compare against it directly, not against a description of it.",
      'Break this into the smallest pieces that can be improved and judged on their own - hero, motion, mobile. For each piece, fan out a builder and a separate critic with fresh context. The critic inspects the actual output, puts it next to the bar blind with the labels stripped, says which one is more alive, and names the single biggest remaining gap. Then it goes back to the builder.',
      'Use a browser for screenshots. Stay within a budget of $20 of API spend.',
      ...FIXED,
    ].join('\n\n'),
  )

  // No budget, tools or pieces unless given; a delegated section is left out, not written as "Left to the agent".
  const minimal = fillGauntlet('Goal: Write an explainer.\n\nBar: three Julia Evans posts\n\nJudged on: Left to the agent.\n\nPieces: Left to the agent.')
  expect(fillGauntlet('Bar: The Stripe pricing page')).toContain('The bar is the Stripe pricing page.')
  expect(minimal).toContain('The bar is three Julia Evans posts. Get the real thing first and compare against it directly')
  expect(minimal).toContain('judged on their own. For each piece')
  expect(minimal).toContain('says which one is better,')
  expect(minimal).not.toMatch(/budget|Left to the agent/i)
  for (const l of FIXED) expect(minimal).toContain(l)
  expect(minimal.endsWith('Fan out subagents and ultracode.')).toBe(true)

  // What is still open stays visible for the person to settle in the prompt box.
  expect(fillGauntlet('Goal: Build it.\n\nUnresolved:\n- Which bar?')).toContain('Unresolved:\n- Which bar?')
})
