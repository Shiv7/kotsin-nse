import { Card, ErrorLine, Stat, StrategyBadge, Table } from '../components/Ui'
import { fmt } from '../lib/api'
import { usePoll } from '../lib/usePoll'
import type { StrategyView } from '../types'

function ConfigTable({ config }: { config: Record<string, unknown> }) {
  return (
    <Table head={['Parameter', 'Value']}>
      {Object.entries(config).map(([k, v]) => (
        <tr key={k} className="border-b border-slate-900">
          <td className="px-2 py-1 font-mono text-[11px] text-slate-400">{k}</td>
          <td className="px-2 py-1 font-mono text-[11px] text-slate-200">
            {v === null ? <span className="text-amber-400">OFF</span> : String(v)}
          </td>
        </tr>
      ))}
    </Table>
  )
}

/**
 * The page that answers "why is this book quiet?".
 *
 * A conjunction of gates can strangle a strategy silently: one book in the previous stack had six
 * mandatory gates and produced two signals in its entire lifetime, and nothing recorded which gate
 * was binding. Every rejection here is attributed.
 */
export function Strategies() {
  const { data, error } = usePoll<{ fudkii: StrategyView; fukaa: StrategyView }>('/api/strategies', 6000)
  if (!data) return <div className="p-4 text-sm text-slate-500">{error ?? 'loading…'}</div>

  return (
    <div className="space-y-6 p-4">
      <ErrorLine error={error} />
      {[data.fudkii, data.fukaa].map((s) => {
        const totalBinding = s.binding.reduce((a, b) => a + b.count, 0)
        return (
          <div key={s.key} className="space-y-3">
            <div className="flex items-center gap-2">
              <StrategyBadge k={s.key} />
              <span className="text-xs text-slate-500">
                {s.gates.candidates} candidates evaluated · {s.gates.passed} passed
              </span>
            </div>

            <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
              <Stat label="Signals emitted" value={s.gates.passed} sub="the cheapest liveness test there is" />
              <Stat label="Candidates" value={s.gates.candidates} />
              <Stat label="Wallet" value={fmt.inr(s.wallet.balance)} sub={`${s.wallet.trades} trades`} />
              <Stat
                label="State"
                value={s.wallet.halted ? 'HALTED' : 'live'}
                tone={s.wallet.halted ? 'text-rose-400' : 'text-emerald-400'}
                sub={s.wallet.halt_reason || undefined}
              />
            </div>

            <div className="grid gap-4 lg:grid-cols-2">
              <Card title="Which gate is binding" right={`${totalBinding} rejections attributed`}>
                <Table head={['Gate', 'Binding', 'Share']} empty="nothing rejected yet">
                  {s.binding.map((b) => (
                    <tr key={b.gate} className="border-b border-slate-900">
                      <td className="px-2 py-1.5 font-mono text-[11px]">{b.gate}</td>
                      <td className="px-2 py-1.5">{b.count}</td>
                      <td className="px-2 py-1.5">
                        <div className="h-2 w-40 rounded bg-slate-800">
                          <div
                            className="h-2 rounded bg-rose-600"
                            style={{ width: `${totalBinding ? (b.count / totalBinding) * 100 : 0}%` }}
                          />
                        </div>
                      </td>
                    </tr>
                  ))}
                </Table>
              </Card>

              <Card title="Per-gate counters" right="evaluated / rejected / missing">
                <Table head={['Gate', 'Evaluated', 'Rejected', 'Missing input']} empty="no evaluations yet">
                  {Object.entries(s.gates.by_gate).map(([name, g]) => (
                    <tr key={name} className="border-b border-slate-900">
                      <td className="px-2 py-1.5 font-mono text-[11px]">{name}</td>
                      <td className="px-2 py-1.5">{g.evaluated}</td>
                      <td className="px-2 py-1.5 text-rose-400/80">{g.rejected}</td>
                      <td className="px-2 py-1.5 text-amber-400/80">{g.missing}</td>
                    </tr>
                  ))}
                </Table>
              </Card>
            </div>

            <Card
              title={`${s.key} parameters`}
              right={s.multipliers ? `volume multiplier — N ${s.multipliers.N}× · M ${s.multipliers.M}× · C ${s.multipliers.C}×` : 'every value here is read by the code'}
            >
              <ConfigTable config={s.config} />
            </Card>
          </div>
        )
      })}
    </div>
  )
}
