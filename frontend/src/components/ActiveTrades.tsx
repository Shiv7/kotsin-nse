import { Fragment } from 'react'
import { cls, contractName, fmt, ist, pnlColor } from '../lib/api'
import type { MirrorAlone, Overview, StopRule } from '../types'
import { MaxDd, MirrorSubRow, RULE_LABEL } from './StopRuleGrid'
import { GradeBadge, StrategyBadge, Table } from './Ui'

// Every active trade, grouped by the strategy that holds it (operator, 2026-10-05: "where is list of all active
// trades for all respective strategies ... i want to see it on dashboard"): the trade, then the same trade
// under stop E and the adaptive stop, then any mirror still running after its real trade closed.

/** The books in the order the tabs show them; the shadows after the books that trade. */
const BOOKS: [string, string][] = [
  ['FUDKII', 'FUDKII'],
  ['FUDKII_RT_X', 'FUDKII-RT-X'],
  ['FUDKII_RT_N', 'FUDKII-RT-N'],
  ['FUDKII_RT_Y', 'FUDKII-RT-Y'],
  ['FUDKII_CT_X', 'FUDKII-CT-X'],
  ['FUDKII_CT_Y', 'FUDKII-CT-Y'],
  ['FUDKII_RT_MCX', 'FUDKII-RT-MCX'],
  ['FUKAA', 'FUKAA'],
  ['FUDKII_RT_Y_F', 'RT-Y · graded F (shadow)'],
  ['FUDKII_RT_Y_W1', 'RT-Y · wide stop (shadow)'],
  ['FUDKII_CT_M', 'FUDKII-CT-M (shadow)'],
]

const HEAD = ['Book', 'Underlying', 'Instrument', 'Qty', 'Entry', 'LTP', 'Unreal', 'R', 'Max DD', 'Opt SL', 'Eq SL', 'T hit', 'Grade', 'Opened']

export const MAX_DD_TIP =
  'Max drawdown since the trade opened: the most it has fallen from its best point so far (the entry counting as the first) to a later low, marked on what it could be sold for (the bid; the last trade when there is none). In ₹ on the full size, then in R. A trade that has only ever risen shows ₹0.'

const TIPS: Record<string, string> = {
  'Max DD': MAX_DD_TIP,
  Unreal: 'The trade: at its last traded price. A stop-rule mirror (the lines under it): at the bid/ask mid, the price its own stop is judged on.',
}

type Position = Overview['positions'][number]

function sum(xs: (number | null | undefined)[]): number | null {
  const v = xs.filter((x): x is number => x != null)
  return v.length ? v.reduce((a, b) => a + b, 0) : null
}

/** A strategy's header: its open trades and their open P&L under each stop rule. */
function GroupHead({ label, trades, alone }: { label: string; trades: Position[]; alone: MirrorAlone[] }) {
  const cur = sum(trades.map((p) => p.unrealized))
  const mirror = (rule: StopRule) =>
    sum([
      ...trades.flatMap((p) => (p.stopRules ?? []).filter((r) => r.rule === rule && r.status === 'OPEN').map((r) => r.openGross)),
      ...alone.filter((m) => m.rule === rule).map((m) => m.stopRule?.openGross ?? m.unrealized),
    ])
  const worst = Math.min(0, ...[...trades, ...alone].map((p) => p.max_dd_inr ?? 0))
  return (
    <tr className="border-b border-slate-800 bg-slate-900/80">
      <td colSpan={HEAD.length} className="px-2 py-2">
        <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
          <span className="text-sm font-semibold text-slate-100">{label}</span>
          <span className="text-xs text-slate-400">
            {trades.length} open{alone.length ? ` · ${alone.length} mirror${alone.length > 1 ? 's' : ''} still running` : ''}
          </span>
          <span className="text-xs text-slate-400">
            open P&L now: current <b className={pnlColor(cur)}>{fmt.signedInr(cur)}</b>
            {(['E', 'A'] as StopRule[]).map((rule) => {
              const v = mirror(rule)
              return (
                <span key={rule}>
                  {' · '}
                  {RULE_LABEL[rule]} <b className={pnlColor(v)}>{v == null ? '—' : fmt.signedInr(v)}</b>
                </span>
              )
            })}
          </span>
          {worst < 0 && <span className="text-xs text-slate-500">worst max DD {fmt.signedInr(worst)}</span>}
        </div>
      </td>
    </tr>
  )
}

function TradeRow({ p }: { p: Position }) {
  return (
    <tr className="border-b border-slate-900">
      <td className="px-2 py-1.5">
        <StrategyBadge k={p.strategy} />
      </td>
      <td className="px-2 py-1.5 font-medium">
        {p.symbol}
        <span className={`ml-1 text-[10px] ${p.direction === 'BULLISH' ? 'text-emerald-500' : 'text-rose-500'}`}>{p.direction === 'BULLISH' ? '▲' : '▼'}</span>
      </td>
      <td className="px-2 py-1.5 text-slate-400">{contractName(p.instrument.name) || p.instrument.scrip_code}</td>
      <td className="px-2 py-1.5">
        {p.qty_remaining}
        {p.qty_remaining !== p.qty && <span className="text-slate-600">/{p.qty}</span>}
      </td>
      <td className="px-2 py-1.5">{fmt.n(p.entry)}</td>
      <td className="px-2 py-1.5">{fmt.n(p.ltp)}</td>
      <td className={`px-2 py-1.5 ${pnlColor(p.unrealized)}`}>{fmt.signedInr(p.unrealized)}</td>
      <td className={`px-2 py-1.5 ${pnlColor(p.r_now)}`}>{fmt.r(p.r_now)}</td>
      <td className="px-2 py-1.5">
        <MaxDd inr={p.max_dd_inr} r={p.max_dd_r} />
      </td>
      <td className="px-2 py-1.5 text-rose-400/80">{fmt.n(p.option_sl)}</td>
      <td className="px-2 py-1.5 text-rose-400/60">{fmt.n(p.equity_sl)}</td>
      <td className="px-2 py-1.5 text-slate-400">
        {p.targets_hit}/{p.option_targets.length}
      </td>
      <td className="px-2 py-1.5">
        <GradeBadge grade={p.grade} />
      </td>
      <td className="px-2 py-1.5 text-slate-500">{p.opened_ist.slice(11, 19)}</td>
    </tr>
  )
}

/** A real trade that has closed while its mirrors run on: what closed it, then the mirrors. */
function ClosedRealRow({ ms }: { ms: MirrorAlone[] }) {
  const m = ms[0]
  const x = m.realExit
  return (
    <>
      <tr className="border-b border-slate-900">
        <td className="px-2 py-1.5">
          <StrategyBadge k={m.source} label={m.sourceLabel} />
        </td>
        <td className="px-2 py-1.5 font-medium">
          {m.symbol}
          <span className={`ml-1 text-[10px] ${m.direction === 'BULLISH' ? 'text-emerald-500' : 'text-rose-500'}`}>{m.direction === 'BULLISH' ? '▲' : '▼'}</span>
        </td>
        <td className="px-2 py-1.5 text-slate-400">{contractName(m.instrument.name) || m.instrument.scrip_code}</td>
        <td colSpan={HEAD.length - 4} className="px-2 py-1.5 text-slate-400">
          <span className="text-amber-300/90">real trade closed</span>
          {x ? (
            <>
              {' — '}
              {x.byOperator ? 'you closed it' : x.exitReason} {fmt.n(x.exitPrice)}
              {x.closedTs ? ` at ${ist(x.closedTs)}` : ''} <b className={pnlColor(x.net)}>{fmt.signedInr(x.net)}</b>
            </>
          ) : null}
          <span className="text-slate-500"> · its mirrors keep their own stop rule</span>
        </td>
      </tr>
      {ms.map((mm) => (mm.stopRule ? <MirrorSubRow key={mm.id} r={mm.stopRule} rungs={mm.option_targets.length} /> : null))}
    </>
  )
}

export function ActiveTradesTable({ data }: { data: Overview }) {
  const alone = data.mirrors_alone ?? []
  const groups = BOOKS.map(([key, label]) => {
    const trades = data.positions.filter((p) => p.strategy === key)
    const mine = alone.filter((m) => m.source === key)
    // one row per real trade the mirrors were copied from
    const pairs = Object.values(
      mine.reduce<Record<string, MirrorAlone[]>>((acc, m) => {
        const k = `${m.symbol}|${m.opened_ist}`
        ;(acc[k] ||= []).push(m)
        return acc
      }, {}),
    )
    return { key, label, trades, mine, pairs }
  })
  const known = new Set(BOOKS.map(([k]) => k))
  const others = data.positions.filter((p) => !known.has(p.strategy))
  const active = groups.filter((g) => g.trades.length || g.mine.length)
  const idle = groups.filter((g) => !g.trades.length && !g.mine.length)
  return (
    <div className="space-y-2">
      <Table head={HEAD} tips={TIPS} empty="no active trade">
        {active.map((g) => (
          <Fragment key={g.key}>
            <GroupHead label={g.label} trades={g.trades} alone={g.mine} />
            {g.trades.map((p) => (
              <Fragment key={p.id}>
                <TradeRow p={p} />
                {(p.stopRules ?? []).map((r) => (
                  <MirrorSubRow key={r.rule} r={r} rungs={p.option_targets.length} />
                ))}
              </Fragment>
            ))}
            {g.pairs.map((ms) => (
              <ClosedRealRow key={ms[0].id} ms={ms} />
            ))}
          </Fragment>
        ))}
        {others.map((p) => (
          <TradeRow key={p.id} p={p} />
        ))}
      </Table>
      {idle.length > 0 && (
        <div className={cls('text-[11px] text-slate-500')}>
          no active trade: {idle.map((g) => g.label).join(' · ')}
        </div>
      )}
    </div>
  )
}
