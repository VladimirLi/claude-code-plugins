export type Clock = {
  lastAt: number
  keepUntil: number
  isWarned: boolean
  pings: number
}

declare module 'claude-code' {
  interface PluginState {
    'cache-clock': { clock: Shaped<Clock> }
  }
}
