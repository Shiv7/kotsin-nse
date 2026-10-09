import { Fragment, useState } from 'react'
import { DEPTH_TIP, DepthCell, DepthDetail } from '../components/DepthCheck'
import { RuleBadge, StopRuleGridTable } from '../components/StopRuleGrid'
import { Badge, Card, ErrorLine, GradeBadge, Stat, StrategyBadge, Table } from '../components/Ui'
import { cls, contractName, fmt, ist, pnlColor } from '../lib/api'
import { usePoll } from '../lib/usePoll'
import type { Pnl, StaleBook, StopRuleGrid, TradeRow } from '../types'

/**
 * Gross, charges and net are shown separately and never merged. On the previous book 81% of the
 * round-trip cost was flat brokerage; a single "P&L" column hides exactly the thing that decided
 * whether the strategy was viable.
 */
const LEDGER_HEAD = ['Closed (IST)', 'Strategy', 'Stop rule', 'Trend', 'Underlying', 'Instrument', 'Qty', 'Entry', 'Exit', 'Gross', 'Charges', 'Net', 'Depth', 'R', 'MFE', 'MAE', 'Exit reason', 'Grade', 'Held']

/** What the R columns mean, on hover. All three are in R: 1R is the premium risked per unit at entry,
 *  entry − the first option stop. R is the result after charges; MFE and MAE are price moves alone. */
const LEDGER_TIPS: Record<string, string> = {
  'Stop rule': 'current stop: the book as it trades. stop E / stop adaptive: a stop-rule mirror — the same fill, judged by the stock\'s stop (E: 60 s through; adaptive: % through × seconds, 60 s at most). Mirrors never place an order and are not in the day\'s money.',
  Trend: 'trend: trades the trigger its own way (the SuperTrend flip + Bollinger break). counter-trend: fades it with the opposite option (CT-X, CT-Y, CT-M).',
  R: 'Net P&L after charges ÷ the money risked at entry (entry − first option stop, × quantity).',
  MFE: 'Maximum favourable excursion: the best the position could have been SOLD for while held — the bid, the last trade when there was none, and every fill — in R: (price − entry) ÷ (entry − first stop), with that price and the rupees open then. Before charges. Rows marked "last" (closed before 4 Oct) used the last trade alone.',
  MAE: 'Maximum adverse excursion: the worst the position could have been SOLD for while held — the bid, the last trade when there was none, and every fill — in R; −1.00R is the first stop. The exit is never below it. Rows marked "last" (closed before 4 Oct) used the last trade alone, which a stop\'s fill can sit below.',
  Depth: DEPTH_TIP,
  'Exit reason': 'For a stop: the LEVEL that fired → the read that breached it (TRIG) → the BID at that instant → the FILL. The gap level→trig is the 1 s read (and a book\'s grace); trig→bid is the spread; bid→fill is the depth walked for the lots. "order book stale": exits the paper matcher refused because the book was older than its limit (6 s, 10 s in the opening window), and a closing fill on a book older than that — read from the stored orders.',
}

/** An excursion: R on top, the option's price then and the rupees open then beneath. */
function Excursion({ r, price, inr, tone, basis }: { r: number | null | undefined; price?: number | null; inr?: number | null; tone: string; basis?: string }) {
  return (
    <td className="px-2 py-1.5">
      <div className={tone}>
        {fmt.r(r)}
        {basis === 'last' && (
          <span className="ml-1 text-[9px] text-slate-600" title="marked on the last trade alone (closed before 4 Oct); a stop's fill can sit below it">
            last
          </span>
        )}
      </div>
      {price != null && (
        <div className="whitespace-nowrap text-[10px] text-slate-500">
          {fmt.n(price)} · {fmt.signedInr(inr)}
        </div>
      )}
    </td>
  )
}

/** A stop's prices, apart: level → trigger read → bid → fill. */
function StopTrail({ s }: { s: NonNullable<TradeRow['stop']> }) {
  return (
    <div className="whitespace-nowrap text-[10px] text-slate-500" title={`level ${fmt.n(s.level)} (${s.triggerOn}) · trig ${fmt.n(s.triggerPrice)} · bid ${fmt.n(s.bidAtTrigger)} · walk ${fmt.n(s.executable)} · fill ${fmt.n(s.fill)}`}>
      SL {fmt.n(s.level)} → trig {fmt.n(s.triggerPrice)}
      {s.triggerOn === 'underlying' ? ' (stock)' : ''} → bid {fmt.n(s.bidAtTrigger)} → fill {fmt.n(s.fill)}
    </div>
  )
}

/** "TRAIL · order book stale (2 exits refused, 14.8 s)" (operator, 2026-10-09: "also in reason for exit: that orderbook was
 *  stale"): read from the stored orders beside the exit reason, which itself is unchanged. */
function StaleNote({ s }: { s: StaleBook }) {
  const tip = [
    s.refused ? `${s.refused} exit order${s.refused > 1 ? 's' : ''} refused: the book was up to ${((s.maxAgeMs ?? 0) / 1000).toFixed(1)} s old${s.limitMs ? ` (limit ${(s.limitMs / 1000).toFixed(0)} s)` : ''}` : null,
    s.fillAgeMs != null ? `the closing exit filled on a ${(s.fillAgeMs / 1000).toFixed(1)} s old book${s.fillLimitMs ? ` (limit ${(s.fillLimitMs / 1000).toFixed(0)} s)` : ''}` : null,
  ].filter(Boolean).join(' · ')
  return (
    <span className="ml-1 whitespace-nowrap text-rose-300" title={tip}>
      · {s.words}
    </span>
  )
}

/** Which books the ledger and its totals show. A stop-rule mirror re-trades its book's fills, so the
 *  books that trade are the default: summing the mirrors in would count each trade three times. */
const BOOK_SETS: [string, string][] = [
  ['trading', 'Trading books'],
  ['mirrors', 'Stop-rule mirrors'],
  ['shadows', 'Other shadows'],
  ['all', 'All'],
]

export function Trades() {
  const [books, setBooks] = useState('trading')
  // depth=1: each trade's fills against the recorded book, for the Depth column (only this page asks for it)
  const { data: trades, error } = usePoll<TradeRow[]>(`/api/trades?limit=200&books=${books}&depth=1`, 8000)
  const { data: pnl } = usePoll<Pnl>(`/api/pnl?books=${books}`, 8000)
  const { data: grid } = usePoll<StopRuleGrid>('/api/stop-rules?since=start', 15000)
  // the rows whose fills are open beneath them (the Depth cell's click)
  const [open, setOpen] = useState<Set<string>>(new Set())
  const toggle = (id: string) =>
    setOpen((s) => {
      const next = new Set(s)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  return (
    <div className="space-y-4 p-4">
      <ErrorLine error={error} />
      <div className="flex flex-wrap items-center gap-1.5">
        {BOOK_SETS.map(([k, label]) => (
          <button
            key={k}
            onClick={() => setBooks(k)}
            className={cls('rounded px-2.5 py-1 text-xs', books === k ? 'bg-slate-700 text-white' : 'bg-slate-900 text-slate-400 hover:text-white')}
          >
            {label}
          </button>
        ))}
        <span className="ml-2 text-[11px] text-slate-500">the totals and the ledger below follow this choice</span>
      </div>
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

      <Card title="Strategy × stop rule · since the mirrors began" right="the same trades under the current stop, stop E and the adaptive stop">
        <StopRuleGridTable data={grid} />
      </Card>

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

      <Card title="Trade ledger" right="MFE / MAE: the best / worst the position could have been sold for while held — in R, then that price and the rupees open — Depth: the fills against the recorded book, click for each — hover a column name">
        <Table head={LEDGER_HEAD} tips={LEDGER_TIPS} empty="no closed trades yet">
          {(trades ?? []).map((t) => (
            <Fragment key={t.id}>
              <tr className="border-b border-slate-900">
                <td className="px-2 py-1.5 text-slate-500">{ist(t.closed_ts)}</td>
                <td className="px-2 py-1.5">
                  {/* a mirror is named by the book whose trade it is: its rule is the next column */}
                  <StrategyBadge k={t.strategy} label={t.stop_rule && t.stop_rule !== 'current' ? t.source_label : t.strategy_label} />
                </td>
                <td className="px-2 py-1.5"><RuleBadge rule={t.stop_rule} /></td>
                <td className="px-2 py-1.5">{t.trend ? <Badge tone={t.trend === 'counter-trend' ? 'amber' : 'slate'}>{t.trend}</Badge> : '—'}</td>
                <td className="px-2 py-1.5 font-medium">{t.underlying}</td>
                <td className="px-2 py-1.5 text-slate-400">{contractName(t.symbol)}</td>
                <td className="px-2 py-1.5">{t.qty}</td>
                <td className="px-2 py-1.5">{fmt.n(t.entry)}</td>
                <td className="px-2 py-1.5">{fmt.n(t.exit)}</td>
                <td className={`px-2 py-1.5 ${pnlColor(t.gross)}`}>{fmt.signedInr(t.gross)}</td>
                <td className="px-2 py-1.5 text-amber-400/80">{fmt.inr(t.charges)}</td>
                <td className={`px-2 py-1.5 font-medium ${pnlColor(t.net)}`}>{fmt.signedInr(t.net)}</td>
                <DepthCell dc={t.depthCheck} open={open.has(t.id)} onToggle={() => toggle(t.id)} />
                <td className={`px-2 py-1.5 ${pnlColor(t.r_multiple)}`}>{fmt.r(t.r_multiple)}</td>
                <Excursion r={t.mfe_r} price={t.mfe_price} inr={t.mfe_inr} tone="text-emerald-500/70" basis={t.mark_basis} />
                <Excursion r={t.mae_r} price={t.mae_price} inr={t.mae_inr} tone="text-rose-500/70" basis={t.mark_basis} />
                <td className="px-2 py-1.5 text-slate-400">
                  {t.exit_reason}
                  {t.staleBook?.stale && <StaleNote s={t.staleBook} />}
                  {t.stop && t.stop.level > 0 && <StopTrail s={t.stop} />}
                </td>
                <td className="px-2 py-1.5">
                  <GradeBadge grade={t.grade} />
                </td>
                <td className="px-2 py-1.5 text-slate-500">{fmt.dur(t.duration_s)}</td>
              </tr>
              {open.has(t.id) && t.depthCheck && (
                <tr className="border-b border-slate-900">
                  <td colSpan={LEDGER_HEAD.length} className="p-0">
                    <DepthDetail dc={t.depthCheck} />
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
        </Table>
      </Card>
    </div>
  )
}
