import { useState } from 'react'
import { ActiveTradesTable } from '../components/ActiveTrades'
import { DepthCheckTable } from '../components/DepthCheck'
import { StopRuleGridTable } from '../components/StopRuleGrid'
import { Card, ErrorLine, Notes, Stat, StrategyBadge, Table } from '../components/Ui'
import { cls, fmt, pnlColor } from '../lib/api'
import { useEngine } from '../lib/engine'
import { usePoll } from '../lib/usePoll'
import type { DepthCheckView, Overview as OverviewData, StopRuleGrid } from '../types'

export function Overview() {
  const { data, error } = usePoll<OverviewData>('/api/overview', 3000)
  const { data: grid } = usePoll<StopRuleGrid>('/api/stop-rules?since=today', 10000)
  // paper fills against the recorded book (operator, 2026-10-09) — the trading books, today or since the log began
  const [depthSince, setDepthSince] = useState<'today' | 'start'>('today')
  const { data: depth } = usePoll<DepthCheckView>(`/api/depth-check?since=${depthSince}&books=trading`, 15000)
  // the twin's active trades, read-only through /api/peer (the same switch as Live Alerts)
  const engine = useEngine()
  const [side, setSide] = useState<'self' | 'peer'>('self')
  const peer = side === 'peer' && engine?.peer ? engine.peer : null
  const { data: twin, error: twinError } = usePoll<OverviewData>(peer ? '/api/peer/overview' : null, 3000)
  if (!data) return <div className="p-4 text-sm text-slate-500">{error ?? 'loading…'}</div>
  const shown = peer ? twin : data

  return (
    <div className="space-y-4 p-4">
      <ErrorLine error={error} />
      <Notes notes={data.boot_notes} />

      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <Stat label="Capital" value={fmt.inr(data.capital)} sub={`${data.universe} symbols tracked`} />
        <Stat label="Day P&L" value={fmt.signedInr(data.day_pnl)} tone={pnlColor(data.day_pnl)} />
        <Stat label="Open" value={data.positions.length} sub={data.mirrors_open ? `positions · ${data.mirrors_open} stop-rule mirrors beside them` : 'positions'} />
        <Stat
          label="Gross exposure"
          value={fmt.inr(data.exposure.gross)}
          sub={`${data.exposure.gross_pct.toFixed(1)}% of capital`}
        />
        <Stat
          label="Halt"
          value={data.halted ? 'HALTED' : 'clear'}
          tone={data.halted ? 'text-rose-400' : 'text-emerald-400'}
          sub={data.halt_reason || undefined}
        />
      </div>

      <Card
        title={`Active trades · by strategy${engine?.peer ? ` · ${peer ? peer.name : engine.name}` : ''}`}
        right={
          engine?.peer ? (
            <span className="flex items-center gap-1">
              {([['self', engine.name || 'this engine'], ['peer', engine.peer.name || 'twin']] as const).map(([k, label]) => (
                <button
                  key={k}
                  onClick={() => setSide(k)}
                  className={cls('rounded px-2 py-0.5 text-[11px]', side === k ? 'bg-violet-800 text-white' : 'bg-slate-800 text-slate-400 hover:text-white')}
                >
                  {label}
                </button>
              ))}
              {peer && <span className="ml-1 text-violet-300">read-only</span>}
            </span>
          ) : (
            'each trade, then the same trade under stop E and the adaptive stop'
          )
        }
      >
        {peer && !twin ? (
          <div className="text-xs text-slate-500">{twinError ?? `loading ${peer.name}…`}</div>
        ) : shown ? (
          <ActiveTradesTable data={shown} />
        ) : null}
      </Card>

      <Card title="Strategy × stop rule · today" right="the same trades under the current stop, stop E and the adaptive stop">
        <StopRuleGridTable data={grid} />
      </Card>

      <Card
        title="Depth check · paper fills against the recorded book"
        right={
          <span className="flex items-center gap-1">
            {([['today', 'today'], ['start', 'since the log began']] as const).map(([k, label]) => (
              <button
                key={k}
                onClick={() => setDepthSince(k)}
                className={cls('rounded px-2 py-0.5 text-[11px]', depthSince === k ? 'bg-slate-700 text-white' : 'bg-slate-800 text-slate-400 hover:text-white')}
              >
                {label}
              </button>
            ))}
          </span>
        }
      >
        <DepthCheckTable data={depth} />
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Wallets" right="one per book; the stop-rule mirrors' purses are in the grid above">
          <Table head={['Book', 'Balance', 'Deployed', 'Day P&L', 'Trades', 'Win %', 'Charges', 'State']}>
            {data.wallets.map((w) => (
              <tr key={w.strategy} className="border-b border-slate-900">
                <td className="px-2 py-1.5">
                  <StrategyBadge k={w.strategy} />
                </td>
                <td className="px-2 py-1.5">{fmt.inr(w.balance)}</td>
                <td className="px-2 py-1.5 text-slate-400">{fmt.inr(w.deployed)}</td>
                <td className={`px-2 py-1.5 ${pnlColor(w.balance - w.day_start_balance)}`}>
                  {fmt.signedInr(w.balance - w.day_start_balance)}
                </td>
                <td className="px-2 py-1.5">{w.trades}</td>
                <td className="px-2 py-1.5">{w.trades ? `${((w.wins / w.trades) * 100).toFixed(0)}%` : 'DM'}</td>
                <td className="px-2 py-1.5 text-amber-400/80">{fmt.inr(w.charges_paid)}</td>
                <td className="px-2 py-1.5">
                  {w.halted ? <span className="text-rose-400">{w.halt_reason}</span> : <span className="text-emerald-500">ok</span>}
                </td>
              </tr>
            ))}
          </Table>
        </Card>

        <Card title="Exposure by underlying" right="aggregated across both books">
          <Table head={['Underlying', 'Outlay', '% of capital']} empty="no open exposure">
            {Object.entries(data.exposure.by_underlying).map(([sym, v]) => (
              <tr key={sym} className="border-b border-slate-900">
                <td className="px-2 py-1.5 font-medium">{sym}</td>
                <td className="px-2 py-1.5">{fmt.inr(v.outlay)}</td>
                <td className="px-2 py-1.5 text-slate-400">{v.pct.toFixed(2)}%</td>
              </tr>
            ))}
          </Table>
        </Card>
      </div>

    </div>
  )
}
