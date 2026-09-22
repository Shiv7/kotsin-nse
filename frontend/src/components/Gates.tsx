import type { GateResult } from '../types'

/** Gate chips: green = passed, rose = failed, amber = the input was missing (fail-closed or open
 * depending on the gate). Hover for value vs threshold. Shared by Signals and the backtest debugger. */
export function Gates({ gates }: { gates: GateResult[] }) {
  return (
    <div className="flex flex-wrap gap-1">
      {gates.map((g) => (
        <span
          key={g.name}
          title={`${g.name}: value ${g.value ?? 'missing'} vs ${g.threshold ?? '—'} ${g.note}`}
          className={
            'rounded px-1 py-0.5 text-[10px] ' +
            (g.missing ? 'bg-amber-900/50 text-amber-300' : g.passed ? 'bg-emerald-900/40 text-emerald-400' : 'bg-rose-900/50 text-rose-300')
          }
        >
          {g.name}
          {g.missing && '?'}
        </span>
      ))}
    </div>
  )
}
