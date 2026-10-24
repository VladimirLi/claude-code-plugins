export type Need = { ask: string; options: string[] }
export type Step = { item: string; state: 'done' | 'next' | 'waiting' }
export type Kind = 'build' | 'research' | 'planning' | 'requirements' | 'queue'
export type Gate = { thread: string; size: string; recommend: string }
export type Status = { goal: string; now: string; needs: Need[]; next: string; plan: Step[]; kind?: Kind; done?: string; gate?: Gate }

declare module 'claude-code' {
  interface PluginState {
    'session-status': { status: Shaped<Status | null>; updatedAt: number; isFromFork: boolean; isStale: boolean; lastPromptAt: number; isCompacted: boolean }
  }
}
