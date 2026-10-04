import { cls, fmt, ist, pnlColor } from '../lib/api'
import type { StopRule, StopRuleCell, StopRuleGrid as Grid, StopRuleRow } from '../types'
import { Badge } from './Ui'

// Every book against its two stop-rule mirrors (operator, 2026-10-04: "do i also see the bifurcation in
// overview? per combination"): the same trades under the current stop, stop E and the adaptive stop. A trade
// is counted once it has closed under all three, so the columns stay like for like.

export const RULES: StopRule[] = ['current', 'E', 'A']
export const RULE_LABEL: Record<StopRule, string> = { current: 'current stop', E: 'stop E', A: 'stop adaptive' }
export const RULE_TONE: Record<StopRule, string> = { current: 'text-slate-200', E: 'text-sky-300', A: 'text-violet-300' }

export function RuleBadge({ rule }: { rule: StopRule | undefined }) {
  if (!rule) return <span className="text-slate-600">—</span>
  return (
    <span className="whitespace-nowrap">
      <Badge tone={rule === 'E' ? 'blue' : rule === 'A' ? 'violet' : 'slate'}>{RULE_LABEL[rule]}</Badge>
    </span>
  )
}

function Cell({ c, rule, best }: { c: StopRuleCell | undefined; rule: StopRule; best: boolean }) {
  if (!c) return <td className="px-2 py-1.5 text-slate-600">—</td>
  return (
    <td className={cls('px-2 py-1.5 align-top tabular-nums', best && 'bg-emerald-950/40')}>
      {c.closed ? (
        <div className={cls('font-semibold', pnlColor(c.net))}>
          {fmt.signedInr(c.net)}
          {rule !== 'current' && (
            <span className={cls('ml-1.5 text-[10px] font-normal', pnlColor(c.diff))} title={`${c.better} better · ${c.worse} worse · ${c.same} the same as the current stop`}>
              {fmt.signedInr(c.diff)} vs current
            </span>
          )}
        </div>
      ) : (
        <div className="text-slate-600">no trade closed under all three</div>
      )}
      {c.closed > 0 && (
        <div className="text-[10px] text-slate-500">
          {c.closed} closed · {Math.round((c.wins / c.closed) * 100)}% won · {c.stops} stopped
          {rule !== 'current' && ` · ${c.better}↑ ${c.worse}↓ ${c.same}=`}
        </div>
      )}
      {(c.open > 0 || c.waiting > 0) && (
        <div className="text-[10px] text-slate-400">
          {c.open > 0 && (
            <>
              {c.open} open <span className={pnlColor(c.openGross)}>{c.openGross == null ? '' : `${fmt.signedInr(c.openGross)} gross`}</span>
            </>
          )}
          {c.open > 0 && c.waiting > 0 && ' · '}
          {c.waiting > 0 && <span title="closed under this rule, still open under another: counted once all three are closed">{c.waiting} closed, waiting on another rule</span>}
        </div>
      )}
    </td>
  )
}

function Row({ label, cells, total }: { label: React.ReactNode; cells: Partial<Record<StopRule, StopRuleCell>>; total?: boolean }) {
  const closed = RULES.filter((r) => (cells[r]?.closed ?? 0) > 0)
  const top = closed.length > 1 ? Math.max(...closed.map((r) => cells[r]!.net)) : null
  return (
    <tr className={cls('border-b border-slate-900', total && 'border-t-2 border-t-slate-700')}>
      <td className={cls('whitespace-nowrap px-2 py-1.5 align-top', total ? 'font-semibold text-slate-100' : 'text-slate-300')}>{label}</td>
      {RULES.map((r) => (
        <Cell key={r} c={cells[r]} rule={r} best={top != null && cells[r]?.closed ? cells[r]!.net === top : false} />
      ))}
    </tr>
  )
}

/** The grid: one row per book with any trade in the window, the trading books' total beneath them, the
 *  shadow books after. The best net of a row is shaded. */
export function StopRuleGridTable({ data }: { data: Grid | null }) {
  if (!data) return <div className="text-xs text-slate-500">loading…</div>
  const active = data.books.filter((b) => RULES.some((r) => { const c = b.cells[r]; return c && (c.closed || c.open || c.waiting) }))
  if (!data.since || active.length === 0) {
    return <div className="text-xs text-slate-500">{data.scope === 'today' ? 'no trade under the three stops yet today' : 'the mirrors have not traded yet'}</div>
  }
  const trading = active.filter((b) => !b.shadow)
  const shadows = active.filter((b) => b.shadow)
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b border-slate-800 text-left text-slate-500">
            <th className="px-2 py-1.5 font-medium">Strategy</th>
            {RULES.map((r) => (
              <th key={r} className={cls('px-2 py-1.5 font-medium', RULE_TONE[r])}>{RULE_LABEL[r]}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {trading.map((b) => <Row key={b.book} label={b.label} cells={b.cells} />)}
          {data.total && trading.length > 0 && <Row label="all trading books" cells={data.total} total />}
          {shadows.map((b) => <Row key={b.book} label={<span className="text-slate-500">{b.label}</span>} cells={b.cells} />)}
        </tbody>
      </table>
      <div className="mt-2 text-[10px] text-slate-500">
        Net after charges, {data.scope === 'today' ? 'trades opened today' : `every trade since the mirrors began (${ist(data.since)})`}. A trade counts once it has closed
        under all three stops. On the same trade the mirror did ↑ better, ↓ worse, or = within ₹1 of the current stop. The best net in a row is shaded.
      </div>
    </div>
  )
}

/** The max drawdown cell: ₹ on top, R beneath. */
export function MaxDd({ inr, r, small }: { inr: number | null | undefined; r: number | null | undefined; small?: boolean }) {
  if (inr == null || r == null) return <span className="text-slate-600">—</span>
  return (
    <span className={cls('whitespace-nowrap', small && 'text-[11px]')}>
      <span className={r > 0 ? 'text-rose-400' : 'text-slate-400'}>{r > 0 ? fmt.signedInr(inr) : '₹0'}</span>
      <span className="ml-1 text-[10px] text-slate-500">{r > 0 ? `−${r.toFixed(2)}R` : ''}</span>
    </span>
  )
}

/** An open position's mirror, as a sub-row of the Overview's open positions (14 columns). */
export function MirrorSubRow({ r, rungs }: { r: StopRuleRow; rungs: number }) {
  const pnl = r.status === 'EXITED' ? r.net : r.openGross
  const t = r.live?.through
  return (
    <tr className="border-b border-slate-900/60 bg-slate-950/40 text-[11px]">
      <td className={cls('whitespace-nowrap py-1 pl-5 pr-2', RULE_TONE[r.rule])}>↳ {r.label}</td>
      <td colSpan={2} className="px-2 py-1 text-slate-400">
        {r.status === 'NONE' && <span className="text-slate-600">not mirrored (its purse refused it, or the trade predates the mirrors)</span>}
        {r.status === 'OPEN' && (t ? (
          <span className="text-amber-300" title="the stock is through its stop: stop E sells after 60 s through; the adaptive stop once (% through × seconds) reaches its need, 60 s at most">
            stock through its stop {t.seconds}s{t.areaNeeded != null ? ` · ${t.area.toFixed(2)} of ${t.areaNeeded} %·s` : ` of ${t.maxSeconds}s`}
          </span>
        ) : (
          'open, stock inside its stop'
        ))}
        {r.status === 'EXITED' && (
          <>
            sold {r.exitReason} {fmt.n(r.exitPrice)} at {r.closedTs ? ist(r.closedTs) : '—'}
            {r.stop && (
              <span className="ml-1 text-slate-500">
                (stop {fmt.n(r.stop.level)} → {r.stop.triggerOn === 'underlying' ? 'stock' : r.stop.triggerOn} {fmt.n(r.stop.triggerPrice)} → bid {fmt.n(r.stop.bidAtTrigger)} → fill {fmt.n(r.stop.fill)})
              </span>
            )}
          </>
        )}
      </td>
      <td className="px-2 py-1 text-slate-400">
        {r.status === 'NONE' ? '' : r.qtyRemaining}
        {r.status !== 'NONE' && r.qtyRemaining !== r.qty && <span className="text-slate-600">/{r.qty}</span>}
      </td>
      <td className="px-2 py-1 text-slate-600">{r.status === 'NONE' ? '' : fmt.n(r.entry)}</td>
      <td className="px-2 py-1 text-slate-600">·</td>
      <td className={cls('px-2 py-1', pnlColor(pnl))}>
        {r.status === 'NONE' ? '' : fmt.signedInr(pnl)}
        {r.status === 'EXITED' && <span className="ml-1 text-[9px] text-slate-500">net</span>}
      </td>
      <td className="px-2 py-1" />
      <td className="px-2 py-1">{r.live ? <MaxDd inr={r.live.maxDdInr} r={r.live.maxDdR} small /> : ''}</td>
      <td className="px-2 py-1 text-rose-400/60" title="a mirror stops on the stock; the option bid sells it only through the 25 % premium cap, or once the trail has lifted the option stop">
        {r.live ? (r.live.trailStop != null ? `trail ${fmt.n(r.live.trailStop)}` : `cap ${fmt.n(r.live.premiumCap)}`) : ''}
      </td>
      <td className="px-2 py-1 text-rose-400/80">{r.live ? fmt.n(r.live.equitySl) : ''}</td>
      <td className="px-2 py-1 text-slate-500">{r.status === 'NONE' ? '' : `${r.targetsHit ?? 0}/${rungs}`}</td>
      <td className="px-2 py-1" colSpan={2} />
    </tr>
  )
}
