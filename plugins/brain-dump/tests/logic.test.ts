import { expect, test } from 'claude-code/testing'

import { AGENT_DECIDES, INTENTS, LEAVE_OUT, decide, gapsIn, parseAnalysis, parseSections, rewriteSystem, templateFor } from '../hooks/logic'

const T = { mode: 'confirm', minWords: 5, forcePrefix: 'bd', skipPrefix: 'raw' }
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
