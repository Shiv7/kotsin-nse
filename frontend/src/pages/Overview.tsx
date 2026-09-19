import { Card, ErrorLine, GradeBadge, Notes, Stat, StrategyBadge, Table } from '../components/Ui'
import { fmt, pnlColor } from '../lib/api'
import { usePoll } from '../lib/usePoll'
import type { Overview as OverviewData } from '../types'

export function Overview() {
  const { data, error } = usePoll<OverviewData>('/api/overview', 3000)
  if (!data) return <div className="p-4 text-sm text-slate-500">{error ?? 'loading…'}</div>

  return (
    <div className="space-y-4 p-4">
      <ErrorLine error={error} />
      <Notes notes={data.boot_notes} />

      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <Stat label="Capital" value={fmt.inr(data.capital)} sub={`${data.universe} symbols tracked`} />
        <Stat label="Day P&L" value={fmt.signedInr(data.day_pnl)} tone={pnlColor(data.day_pnl)} />
        <Stat label="Open" value={data.positions.length} sub="positions" />
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

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Wallets" right="one per book, as the old stack had">
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

      <Card title="Open positions" right="levels are on the underlying; the trade is the option">
        <Table
          head={['Book', 'Underlying', 'Instrument', 'Qty', 'Entry', 'LTP', 'Unreal', 'R', 'Opt SL', 'Eq SL', 'T hit', 'Grade', 'Opened']}
          empty="flat"
        >
          {data.positions.map((p) => (
            <tr key={p.id} className="border-b border-slate-900">
              <td className="px-2 py-1.5">
                <StrategyBadge k={p.strategy} />
              </td>
              <td className="px-2 py-1.5 font-medium">
                {p.symbol}
                <span className={`ml-1 text-[10px] ${p.direction === 'BULLISH' ? 'text-emerald-500' : 'text-rose-500'}`}>
                  {p.direction === 'BULLISH' ? '▲' : '▼'}
                </span>
              </td>
              <td className="px-2 py-1.5 text-slate-400">{p.instrument.name || p.instrument.scrip_code}</td>
              <td className="px-2 py-1.5">
                {p.qty_remaining}
                {p.qty_remaining !== p.qty && <span className="text-slate-600">/{p.qty}</span>}
              </td>
              <td className="px-2 py-1.5">{fmt.n(p.entry)}</td>
              <td className="px-2 py-1.5">{fmt.n(p.ltp)}</td>
              <td className={`px-2 py-1.5 ${pnlColor(p.unrealized)}`}>{fmt.signedInr(p.unrealized)}</td>
              <td className={`px-2 py-1.5 ${pnlColor(p.r_now)}`}>{fmt.r(p.r_now)}</td>
              <td className="px-2 py-1.5 text-rose-400/80">{fmt.n(p.option_sl)}</td>
              <td className="px-2 py-1.5 text-rose-400/60">{fmt.n(p.equity_sl)}</td>
              <td className="px-2 py-1.5 text-slate-400">
                {p.targets_hit}/{p.option_targets.length}
              </td>
              <td className="px-2 py-1.5">
                <GradeBadge grade={p.grade} />
              </td>
              <td className="px-2 py-1.5 text-slate-500">{p.opened_ist}</td>
            </tr>
          ))}
        </Table>
      </Card>
    </div>
  )
}
