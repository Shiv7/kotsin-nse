import { cls, ist } from '../lib/api'
import type { Health } from '../types'

const TONE: Record<string, string> = {
  SHADOW: 'bg-slate-800 text-slate-300',
  PAPER: 'bg-sky-900 text-sky-200',
  LIVE_CAPPED: 'bg-amber-800 text-amber-100',
  LIVE: 'bg-rose-800 text-rose-100',
}

/**
 * Mode is always on screen. The single most expensive failure in the previous system was a book
 * that ran in paper for eight weeks while everyone believed it was live; the only tell was one
 * line in a boot banner nobody re-read.
 */
export function ModeBanner({ health, error }: { health: Health | null; error: string | null }) {
  const mode = health?.mode ?? 'SHADOW'
  const armed = health?.armed_until ?? null
  return (
    <div className="flex items-center gap-3 border-b border-slate-800 bg-slate-900 px-4 py-2 text-xs">
      <span className={cls('rounded px-2 py-0.5 font-bold tracking-wide', TONE[mode] ?? TONE.SHADOW)}>{mode}</span>
      {armed && <span className="text-amber-300">armed until {ist(armed, true)} IST</span>}
      {health?.halted && <span className="rounded bg-rose-900 px-2 py-0.5 font-semibold text-rose-100">HALTED</span>}
      {health && (
        <span className={health.status === 'ok' ? 'text-emerald-400' : 'text-amber-400'}>
          {health.status === 'ok' ? 'healthy' : `degraded: ${health.degraded.join(', ')}`}
        </span>
      )}
      {health && (
        <span className="text-slate-500">
          feed {health.feed.connected ? 'up' : 'down'} · {health.feed.ticks.toLocaleString()} ticks ·{' '}
          {health.positions_open} open
        </span>
      )}
      <span className="ml-auto text-slate-600">{error ? <span className="text-rose-400">{error}</span> : 'kotsin-nse'}</span>
    </div>
  )
}
