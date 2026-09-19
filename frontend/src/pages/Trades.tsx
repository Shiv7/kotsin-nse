import { Card, ErrorLine, GradeBadge, Stat, StrategyBadge, Table } from '../components/Ui'
import { fmt, ist, pnlColor } from '../lib/api'
import { usePoll } from '../lib/usePoll'
import type { Pnl, TradeRow } from '../types'

/**
 * Gross, charges and net are shown separately and never merged. On the previous book 81% of the
 * round-trip cost was flat brokerage; a single "P&L" column hides exactly the thing that decided
 * whether the strategy was viable.
 */
export function Trades() {
  const { data: trades, error } = usePoll<TradeRow[]>('/api/trades?limit=200', 8000)
  const { data: pnl } = usePoll<Pnl>('/api/pnl', 8000)

  return (
    <div className="space-y-4 p-4">
      <ErrorLine error={error} />
      <div className="grid grid-cols-2 gap-3 md:grid-cols-6">
        <Stat label="Trades" value={pnl?.trades ?? 0} />
        <Stat label="Gross" value={fmt.signedInr(pnl?.gross)} tone={pnlColor(pnl?.gross)} />
        <Stat label="Charges" value={fmt.inr(pnl?.charges)} tone="text-amber-400" />
        <Stat label="Net" value={fmt.signedInr(pnl?.net)} tone={pnlColor(pnl?.net)} />
        <Stat
          label="Cost drag"
          value={pnl?.charges_share_of_gross != null ? `${pnl.charges_share_of_gross.toFixed(0)}%` : 'DM'}
          sub="charges ÷ |gross|"
          tone="text-amber-400"
        />
        <Stat label="Avg R" value={pnl?.avg_r != null ? fmt.r(pnl.avg_r) : 'DM'} sub={pnl?.win_rate != null ? `${pnl.win_rate}% win` : undefined} />
      </div>

      {pnl?.by_exit_reason && (
        <Card title="By exit reason" right="which rule is doing the work">
          <Table head={['Reason', 'Trades', 'Net']}>
            {Object.entries(pnl.by_exit_reason).map(([reason, v]) => (
              <tr key={reason} className="border-b border-slate-900">
                <td className="px-2 py-1.5">{reason}</td>
                <td className="px-2 py-1.5">{v.n}</td>
                <td className={`px-2 py-1.5 ${pnlColor(v.net)}`}>{fmt.signedInr(v.net)}</td>
              </tr>
            ))}
          </Table>
        </Card>
      )}

      <Card title="Trade ledger">
        <Table
          head={['Closed (IST)', 'Book', 'Underlying', 'Instrument', 'Qty', 'Entry', 'Exit', 'Gross', 'Charges', 'Net', 'R', 'MFE', 'MAE', 'Exit', 'Grade', 'Held']}
          empty="no closed trades yet"
        >
          {(trades ?? []).map((t) => (
            <tr key={t.id} className="border-b border-slate-900">
              <td className="px-2 py-1.5 text-slate-500">{ist(t.closed_ts)}</td>
              <td className="px-2 py-1.5">
                <StrategyBadge k={t.strategy} />
              </td>
              <td className="px-2 py-1.5 font-medium">{t.underlying}</td>
              <td className="px-2 py-1.5 text-slate-400">{t.symbol}</td>
              <td className="px-2 py-1.5">{t.qty}</td>
              <td className="px-2 py-1.5">{fmt.n(t.entry)}</td>
              <td className="px-2 py-1.5">{fmt.n(t.exit)}</td>
              <td className={`px-2 py-1.5 ${pnlColor(t.gross)}`}>{fmt.signedInr(t.gross)}</td>
              <td className="px-2 py-1.5 text-amber-400/80">{fmt.inr(t.charges)}</td>
              <td className={`px-2 py-1.5 font-medium ${pnlColor(t.net)}`}>{fmt.signedInr(t.net)}</td>
              <td className={`px-2 py-1.5 ${pnlColor(t.r_multiple)}`}>{fmt.r(t.r_multiple)}</td>
              <td className="px-2 py-1.5 text-emerald-500/70">{fmt.r(t.mfe_r)}</td>
              <td className="px-2 py-1.5 text-rose-500/70">{fmt.r(t.mae_r)}</td>
              <td className="px-2 py-1.5 text-slate-400">{t.exit_reason}</td>
              <td className="px-2 py-1.5">
                <GradeBadge grade={t.grade} />
              </td>
              <td className="px-2 py-1.5 text-slate-500">{fmt.dur(t.duration_s)}</td>
            </tr>
          ))}
        </Table>
      </Card>
    </div>
  )
}
