import { Fragment } from 'react'
import { MaxDd, MirrorSubRow, RULE_TONE, StopRuleGridTable } from '../components/StopRuleGrid'
import { Card, ErrorLine, GradeBadge, Notes, Stat, StrategyBadge, Table } from '../components/Ui'
import { contractName, fmt, ist, pnlColor } from '../lib/api'
import { usePoll } from '../lib/usePoll'
import type { Overview as OverviewData, StopRuleGrid } from '../types'

/** Max drawdown, on hover (operator, 2026-10-04: "max drawdown since the time it has been trading"). */
const MAX_DD_TIP =
  'Max drawdown since the trade opened: the most it has fallen from its best point so far (the entry counting as the first) to a later low, marked on what it could be sold for (the bid; the last trade when there is none). In ₹ on the full size, then in R. A trade that has only ever risen shows ₹0.'

export function Overview() {
  const { data, error } = usePoll<OverviewData>('/api/overview', 3000)
  const { data: grid } = usePoll<StopRuleGrid>('/api/stop-rules?since=today', 10000)
  if (!data) return <div className="p-4 text-sm text-slate-500">{error ?? 'loading…'}</div>

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

      <Card title="Strategy × stop rule · today" right="the same trades under the current stop, stop E and the adaptive stop">
        <StopRuleGridTable data={grid} />
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

      <Card title="Open positions" right="levels are on the underlying; the trade is the option · under each, the same trade under stop E and the adaptive stop">
        <Table
          head={['Book', 'Underlying', 'Instrument', 'Qty', 'Entry', 'LTP', 'Unreal', 'R', 'Max DD', 'Opt SL', 'Eq SL', 'T hit', 'Grade', 'Opened']}
          tips={{ 'Max DD': MAX_DD_TIP }}
          empty="flat"
        >
          {data.positions.map((p) => (
            <Fragment key={p.id}>
            <tr className="border-b border-slate-900">
              <td className="px-2 py-1.5">
                <StrategyBadge k={p.strategy} />
              </td>
              <td className="px-2 py-1.5 font-medium">
                {p.symbol}
                <span className={`ml-1 text-[10px] ${p.direction === 'BULLISH' ? 'text-emerald-500' : 'text-rose-500'}`}>
                  {p.direction === 'BULLISH' ? '▲' : '▼'}
                </span>
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
              <td className="px-2 py-1.5"><MaxDd inr={p.max_dd_inr} r={p.max_dd_r} /></td>
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
            {(p.stopRules ?? []).map((r) => (
              <MirrorSubRow key={r.rule} r={r} rungs={p.option_targets.length} />
            ))}
            </Fragment>
          ))}
        </Table>
      </Card>

      {(data.mirrors_alone?.length ?? 0) > 0 && (
        <Card title="Stop-rule mirrors still running" right="the real trade is closed; each mirror keeps its own rule until it exits">
          <Table head={['Strategy', 'Stop rule', 'Underlying', 'Instrument', 'Qty', 'Entry', 'LTP', 'Unreal', 'Max DD', 'Stop now', 'The real trade']} tips={{ 'Max DD': MAX_DD_TIP }}>
            {data.mirrors_alone!.map((m) => {
              const t = m.stopRule?.live?.through
              return (
                <tr key={m.id} className="border-b border-slate-900">
                  <td className="px-2 py-1.5"><StrategyBadge k={m.source} label={m.sourceLabel} /></td>
                  <td className={`px-2 py-1.5 ${RULE_TONE[m.rule]}`}>{m.rule === 'E' ? 'stop E' : 'stop adaptive'}</td>
                  <td className="px-2 py-1.5 font-medium">{m.symbol}</td>
                  <td className="px-2 py-1.5 text-slate-400">{contractName(m.instrument.name) || m.instrument.scrip_code}</td>
                  <td className="px-2 py-1.5">{m.qty_remaining}{m.qty_remaining !== m.qty && <span className="text-slate-600">/{m.qty}</span>}</td>
                  <td className="px-2 py-1.5">{fmt.n(m.entry)}</td>
                  <td className="px-2 py-1.5">{fmt.n(m.ltp)}</td>
                  <td className={`px-2 py-1.5 ${pnlColor(m.unrealized)}`}>{fmt.signedInr(m.unrealized)}</td>
                  <td className="px-2 py-1.5"><MaxDd inr={m.max_dd_inr} r={m.max_dd_r} /></td>
                  <td className="px-2 py-1.5 text-slate-400">
                    stock {fmt.n(m.equity_sl)} · {m.stopRule?.live?.trailStop != null ? `trail ${fmt.n(m.stopRule.live.trailStop)}` : `cap ${fmt.n(m.stopRule?.live?.premiumCap)}`}
                    {t && <div className="text-[10px] text-amber-300">through {t.seconds}s{t.areaNeeded != null ? ` · ${t.area.toFixed(2)} of ${t.areaNeeded} %·s` : ` of ${t.maxSeconds}s`}</div>}
                  </td>
                  <td className="px-2 py-1.5 text-slate-400">
                    {m.realExit ? (
                      <>
                        {m.realExit.byOperator ? 'you closed it' : m.realExit.exitReason} {fmt.n(m.realExit.exitPrice)}
                        {m.realExit.closedTs ? ` at ${ist(m.realExit.closedTs)}` : ''}
                        <span className={`ml-1 ${pnlColor(m.realExit.net)}`}>{fmt.signedInr(m.realExit.net)}</span>
                      </>
                    ) : (
                      'closed'
                    )}
                  </td>
                </tr>
              )
            })}
          </Table>
        </Card>
      )}
    </div>
  )
}
