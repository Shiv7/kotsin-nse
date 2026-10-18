import { useMemo, useState } from 'react'
import { cls, fmt, ist } from '../lib/api'
import { TradeChart, type MtfTrade } from './TradeChart'
import { Card, Stat, Table } from './Ui'

export interface MtfSummary {
  trades: number
  win_rate: number | null
  avg_r: number | null
  total_r: number
  net_inr: number
  costs_inr: number
  costs_r_per_trade: number | null
  t_by_week: number | null
  weeks: number
  months_positive: number
  months_negative: number
  avg_sessions_held: number | null
  by_month: Record<string, number>
  by_exit: Record<string, { n: number; avg_r: number; total_r: number }>
  by_setup: Record<string, { n: number; avg_r: number; total_r: number }>
  by_side: Record<string, { n: number; avg_r: number; total_r: number }>
}

export interface MtfRunData {
  id: string
  label: string
  params: Record<string, string | number | boolean>
  range: string[]
  candidates: number
  summary: MtfSummary
  trades: MtfTrade[]
}

/** A multi-timeframe sweep run: the numbers, R by month, and every trade one click from its chart. */
export function MtfRun({ run }: { run: MtfRunData }) {
  const sm = run.summary
  const [filter, setFilter] = useState('')
  const [open, setOpen] = useState<MtfTrade | null>(null)
  const trades = useMemo(
    () => run.trades.filter((t) => !filter || t.symbol.includes(filter.toUpperCase()) || t.exit_reason === filter.toUpperCase() || t.setup.includes(filter.toUpperCase())),
    [run.trades, filter],
  )
  const months = Object.entries(sm.by_month)
  const max = Math.max(1, ...months.map(([, v]) => Math.abs(v)))

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-8">
        <Stat label="Trades" value={sm.trades} sub={`${run.candidates} candidates`} />
        <Stat label="Win rate" value={sm.win_rate != null ? `${sm.win_rate}%` : 'DM'} />
        <Stat label="Avg R" value={fmt.r(sm.avg_r)} tone={(sm.avg_r ?? 0) >= 0 ? 'text-emerald-400' : 'text-rose-400'} />
        <Stat label="Total R" value={sm.total_r.toFixed(1)} tone={sm.total_r >= 0 ? 'text-emerald-400' : 'text-rose-400'} />
        <Stat label="Net" value={fmt.signedInr(sm.net_inr)} tone={sm.net_inr >= 0 ? 'text-emerald-400' : 'text-rose-400'} sub={`costs ${fmt.inr(sm.costs_inr)}`} />
        <Stat label="Costs / trade" value={sm.costs_r_per_trade != null ? `${sm.costs_r_per_trade} R` : 'DM'} />
        <Stat label="t (by week)" value={sm.t_by_week ?? 'DM'} sub={`${sm.weeks} weeks · |t| < 2 = could be luck`} />
        <Stat label="Months ±" value={`${sm.months_positive} / ${sm.months_negative}`} sub={`held ${sm.avg_sessions_held} sessions avg`} />
      </div>

      <Card title="R by month" right={run.range.join(' → ')}>
        <svg viewBox="0 0 900 140" className="w-full">
          <line x1={0} x2={900} y1={70} y2={70} stroke="#334155" />
          {months.map(([m, v], i) => {
            const bw = 900 / months.length
            const h = (Math.abs(v) / max) * 60
            return (
              <g key={m}>
                <rect x={i * bw + 2} width={Math.max(2, bw - 4)} y={v >= 0 ? 70 - h : 70} height={h} fill={v >= 0 ? '#10b981' : '#f43f5e'} opacity={0.8}>
                  <title>{`${m}: ${v.toFixed(1)} R`}</title>
                </rect>
                <text x={i * bw + bw / 2} y={136} fontSize="8" textAnchor="middle" fill="#475569">
                  {m.slice(2)}
                </text>
              </g>
            )
          })}
        </svg>
      </Card>

      <div className="grid gap-4 md:grid-cols-3">
        {(
          [
            ['By setup', sm.by_setup],
            ['By exit', sm.by_exit],
            ['By side', sm.by_side],
          ] as const
        ).map(([title, g]) => (
          <Card key={title} title={title}>
            <Table head={['', 'n', 'avg R', 'total R']}>
              {Object.entries(g).map(([k, v]) => (
                <tr key={k} className="cursor-pointer border-b border-slate-900 hover:bg-slate-900" onClick={() => setFilter(k.split(' ').pop() ?? k)}>
                  <td className="px-2 py-1 font-mono text-[11px]">{k}</td>
                  <td className="px-2 py-1">{v.n}</td>
                  <td className={cls('px-2 py-1 font-mono', v.avg_r >= 0 ? 'text-emerald-400' : 'text-rose-400')}>{v.avg_r.toFixed(2)}</td>
                  <td className="px-2 py-1 font-mono">{v.total_r.toFixed(1)}</td>
                </tr>
              ))}
            </Table>
          </Card>
        ))}
      </div>

      <Card title="Rules of this run">
        <div className="grid grid-cols-2 gap-1 font-mono text-[11px] md:grid-cols-4">
          {Object.entries(run.params).map(([k, v]) => (
            <div key={k}>
              <span className="text-slate-500">{k}</span> <span className="text-slate-200">{String(v)}</span>
            </div>
          ))}
        </div>
      </Card>

      <Card
        title={`Trades (${trades.length})`}
        right={<input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="filter: symbol, exit or level" className="rounded border border-slate-700 bg-slate-900 px-2 py-0.5 text-xs" />}
      >
        <Table head={['Entry', 'Stock', 'Side', 'Setup', 'Entry px', 'Stop', 'T1 / T2', 'R:R', 'Exit', 'Held', 'Best / worst', 'R', 'Net']} empty="no trades">
          {trades.slice(0, 800).map((t, i) => (
            <tr key={i} onClick={() => setOpen(t)} className="cursor-pointer border-b border-slate-900 hover:bg-slate-900">
              <td className="whitespace-nowrap px-2 py-1 font-mono text-[11px] text-slate-400">{ist(t.signal_ts, false)}</td>
              <td className="px-2 py-1 font-semibold">{t.symbol}</td>
              <td className={cls('px-2 py-1 text-[10px]', t.side === 'BULLISH' ? 'text-emerald-400' : 'text-rose-400')}>{t.side === 'BULLISH' ? 'LONG' : 'SHORT'}</td>
              <td className="px-2 py-1 text-[10px] text-sky-300">{t.setup}</td>
              <td className="px-2 py-1 font-mono">{fmt.n(t.entry)}</td>
              <td className="px-2 py-1 font-mono text-amber-400">{fmt.n(t.stop)}</td>
              <td className="px-2 py-1 font-mono text-[11px]">
                {fmt.n(t.t1)} / {fmt.n(t.t2)}
              </td>
              <td className="px-2 py-1 font-mono">{t.rr}</td>
              <td className="px-2 py-1 font-mono text-[11px]">{t.exit_reason}</td>
              <td className="px-2 py-1">{t.sessions_held}d</td>
              <td className="px-2 py-1 font-mono text-[11px] text-slate-400">
                {t.mfe_r.toFixed(1)} / {t.mae_r.toFixed(1)}
              </td>
              <td className={cls('px-2 py-1 font-mono', t.r >= 0 ? 'text-emerald-400' : 'text-rose-400')}>{t.r.toFixed(2)}</td>
              <td className={cls('px-2 py-1 font-mono', t.net >= 0 ? 'text-emerald-400' : 'text-rose-400')}>{fmt.signedInr(t.net)}</td>
            </tr>
          ))}
        </Table>
        {trades.length > 800 && <div className="mt-2 text-[11px] text-slate-500">showing the first 800 — filter to narrow</div>}
      </Card>

      {open && <TradeChart t={open} onClose={() => setOpen(null)} />}
    </div>
  )
}
