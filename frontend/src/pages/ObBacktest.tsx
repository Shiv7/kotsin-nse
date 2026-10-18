import { useMemo, useState } from 'react'
import { MtfRun, type MtfRunData } from '../components/MtfRun'
import { StockPopup } from '../components/StockPopup'
import { Card, ErrorLine, Stat, Table } from '../components/Ui'
import { cls, fmt, ist } from '../lib/api'
import { usePoll } from '../lib/usePoll'
import type { BoardRow } from '../types'

interface RunSummary {
  trades: number
  days: number
  win_rate: number | null
  avg_r: number | null
  total_r: number
  net_inr: number
  costs_inr: number
  t_by_day: number | null
  days_positive: number
  days_negative: number
  by_exit: Record<string, { n: number; r: number }>
  by_side: Record<string, { n: number; avg_r: number | null }>
  by_day: Record<string, number>
}

interface RunHead {
  id: string
  label: string
  created_ts: number
  seconds: number
  params: Record<string, string | number | boolean>
  range: string[]
  summary: RunSummary
}

interface ObTrade {
  day: string
  symbol: string
  side: 'BULLISH' | 'BEARISH'
  signal_ts: number
  entry_ts?: number
  entry: number
  // order-block runs
  z?: number
  s1?: number
  r1?: number | null
  r2?: number | null
  rr_planned?: number | null
  align?: number | null
  rank?: number | null
  // sweep / value-area runs
  setup?: string
  stop?: number
  t1?: number
  t2?: number
  rr?: number
  qty: number
  exits: { ts: number; price: number; frac: number; why: string }[]
  exit_reason: string
  r: number
  gross: number
  costs: number
  net: number
  mfe_r: number
  mae_r: number
}

const hhmm = (ts: number) => ist(ts, false).slice(11, 16)

/** Every backtest run of the order-block playbook, and every trade in it, one click from its chart. */
export function ObBacktest() {
  const runs = usePoll<RunHead[]>('/api/obtests', 30000)
  const [sel, setSel] = useState('')
  const active = sel || runs.data?.[0]?.id || ''
  const run = usePoll<RunHead & { trades: ObTrade[]; kind?: string }>(active ? `/api/obtests/${active}` : null, 600000)
  const [filter, setFilter] = useState('')
  const [open, setOpen] = useState<ObTrade | null>(null)

  const trades = useMemo(
    () => (run.data?.trades ?? []).filter((t) => !filter || t.symbol.includes(filter.toUpperCase()) || t.exit_reason === filter.toUpperCase()),
    [run.data, filter],
  )
  const isMtf = run.data?.kind === 'mtf'
  const sm = isMtf ? undefined : run.data?.summary

  return (
    <div className="space-y-4 p-4">
      <ErrorLine error={runs.error ?? run.error} />
      {!runs.data?.length && (
        <Card title="No runs yet">
          <p className="text-xs text-slate-500">
            Run one from the backend: <code className="text-slate-300">uv run kotsin-nse ob-backtest</code> (all cached days), or add{' '}
            <code className="text-slate-300">--set min_rr=2</code> to change one rule.
          </p>
        </Card>
      )}

      <div className="grid gap-4 xl:grid-cols-[300px_1fr]">
        <Card title="Runs" right="newest first">
          <div className="space-y-1">
            {(runs.data ?? []).map((r) => (
              <button
                key={r.id}
                onClick={() => setSel(r.id)}
                className={cls('w-full rounded px-2 py-1.5 text-left text-xs', r.id === active ? 'bg-slate-800' : 'hover:bg-slate-900')}
              >
                <div className="flex justify-between">
                  <span className="font-mono text-slate-300">{r.id}</span>
                  <span className={cls('font-mono', r.summary.total_r >= 0 ? 'text-emerald-400' : 'text-rose-400')}>{r.summary.total_r.toFixed(1)}R</span>
                </div>
                <div className="truncate text-[10px] text-slate-500">
                  {r.label || '—'} · {r.summary.trades} trades · {r.range.join(' → ')}
                </div>
              </button>
            ))}
          </div>
        </Card>

        {isMtf && run.data && <MtfRun run={run.data as unknown as MtfRunData} />}
        {sm && run.data && (
          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-3 md:grid-cols-6">
              <Stat label="Trades" value={sm.trades} sub={`${sm.days} days`} />
              <Stat label="Win rate" value={sm.win_rate != null ? `${sm.win_rate}%` : 'DM'} />
              <Stat label="Avg R" value={fmt.r(sm.avg_r)} tone={(sm.avg_r ?? 0) >= 0 ? 'text-emerald-400' : 'text-rose-400'} />
              <Stat label="Net" value={fmt.signedInr(sm.net_inr)} tone={sm.net_inr >= 0 ? 'text-emerald-400' : 'text-rose-400'} sub={`costs ${fmt.inr(sm.costs_inr)}`} />
              <Stat label="t (by day)" value={sm.t_by_day ?? 'DM'} sub="|t| < 2 = could be luck" />
              <Stat label="Days ±" value={`${sm.days_positive} / ${sm.days_negative}`} sub="positive / negative" />
            </div>

            <Card title="R by day" right="green = the day made money, after costs">
              <DayBars byDay={sm.by_day} />
            </Card>

            <div className="grid gap-4 md:grid-cols-2">
              <Card title="How trades ended">
                <Table head={['Exit', 'Trades', 'Total R', 'Avg R']}>
                  {Object.entries(sm.by_exit).map(([k, v]) => (
                    <tr key={k} className="cursor-pointer border-b border-slate-900 hover:bg-slate-900" onClick={() => setFilter(k)}>
                      <td className="px-2 py-1 font-mono">{k}</td>
                      <td className="px-2 py-1">{v.n}</td>
                      <td className={cls('px-2 py-1 font-mono', v.r >= 0 ? 'text-emerald-400' : 'text-rose-400')}>{v.r.toFixed(2)}</td>
                      <td className="px-2 py-1 font-mono">{(v.r / v.n).toFixed(2)}</td>
                    </tr>
                  ))}
                </Table>
              </Card>
              <Card title="Rules of this run">
                <div className="grid grid-cols-2 gap-1 font-mono text-[11px]">
                  {Object.entries(run.data.params).map(([k, v]) => (
                    <div key={k}>
                      <span className="text-slate-500">{k}</span> <span className="text-slate-200">{String(v)}</span>
                    </div>
                  ))}
                </div>
              </Card>
            </div>

            <Card
              title="Trades"
              right={
                <input
                  value={filter}
                  onChange={(e) => setFilter(e.target.value)}
                  placeholder="filter: symbol or exit"
                  className="rounded border border-slate-700 bg-slate-900 px-2 py-0.5 text-xs"
                />
              }
            >
              <Table head={['Day', 'Signal', 'In', 'Stock', 'Setup', 'Side', 'Entry', 'Z / Stop', 'T1 / T2', 'Plan R:R', 'Align', 'Exit', 'MFE / MAE', 'R', 'Net']} empty="no trades">
                {trades.map((t, i) => (
                  <tr key={i} onClick={() => setOpen(t)} className="cursor-pointer border-b border-slate-900 hover:bg-slate-900">
                    <td className="px-2 py-1 text-slate-400">{t.day}</td>
                    <td className="px-2 py-1 font-mono text-slate-500">{hhmm(t.signal_ts)}</td>
                    <td className="px-2 py-1 font-mono">{hhmm(t.entry_ts ?? t.signal_ts)}</td>
                    <td className="px-2 py-1 font-semibold">{t.symbol}</td>
                    <td className="px-2 py-1 text-[10px] text-sky-300">{t.setup ?? 'ORDER BLOCK'}</td>
                    <td className={cls('px-2 py-1 text-[10px]', t.side === 'BULLISH' ? 'text-emerald-400' : 'text-rose-400')}>{t.side === 'BULLISH' ? 'LONG' : 'SHORT'}</td>
                    <td className="px-2 py-1 font-mono">{fmt.n(t.entry)}</td>
                    <td className="px-2 py-1 font-mono text-[11px]">
                      {t.z != null ? fmt.n(t.z) : '—'} / <span className="text-amber-400">{fmt.n(t.s1 ?? t.stop)}</span>
                    </td>
                    <td className="px-2 py-1 font-mono text-[11px]">
                      {fmt.n(t.r1 ?? t.t1)} / {fmt.n(t.r2 ?? t.t2)}
                    </td>
                    <td className="px-2 py-1 font-mono">{t.rr_planned ?? t.rr ?? '—'}</td>
                    <td className="px-2 py-1">{t.align != null ? `${t.align}/4` : '—'}</td>
                    <td className="px-2 py-1 font-mono text-[11px]">
                      {t.exit_reason} {t.exits.length ? hhmm(t.exits[t.exits.length - 1].ts) : ''}
                    </td>
                    <td className="px-2 py-1 font-mono text-[11px] text-slate-400">
                      {t.mfe_r.toFixed(1)} / {t.mae_r.toFixed(1)}
                    </td>
                    <td className={cls('px-2 py-1 font-mono', t.r >= 0 ? 'text-emerald-400' : 'text-rose-400')}>{t.r.toFixed(2)}</td>
                    <td className={cls('px-2 py-1 font-mono', t.net >= 0 ? 'text-emerald-400' : 'text-rose-400')}>{fmt.signedInr(t.net)}</td>
                  </tr>
                ))}
              </Table>
            </Card>
          </div>
        )}
      </div>

      {open && (
        <StockPopup
          row={asRow(open)}
          day={open.day}
          at={open.exits.length ? hhmm(open.exits[open.exits.length - 1].ts + 900) : '15:14'}
          trade={{
            entry_ts: open.entry_ts ?? open.signal_ts,
            entry: open.entry,
            exits: open.exits,
            signal_at: hhmm(open.signal_ts),
            lines:
              open.stop != null
                ? [
                    { price: open.stop, title: 'stop', color: '#f59e0b' },
                    ...(open.t1 != null ? [{ price: open.t1, title: 'T1', color: '#e2e8f0' }] : []),
                    ...(open.t2 != null ? [{ price: open.t2, title: 'T2', color: '#e2e8f0' }] : []),
                  ]
                : undefined,
          }}
          onClose={() => setOpen(null)}
        />
      )}
    </div>
  )
}

function asRow(t: ObTrade): BoardRow {
  return {
    symbol: t.symbol, sector: '', last: t.entry, pct: null, score: 0, side: t.side, rank: t.rank ?? null, percentile: null,
    event_risk: false, above_vwap: null, oi_build_up: null, oi_change_pct: null, pick: true,
    rule: `${t.setup ?? 'ORDER BLOCK'} · ${t.exit_reason} · ${t.r.toFixed(2)} R · net ${fmt.signedInr(t.net)}`, align: t.align ?? null, setup: null,
  }
}

function DayBars({ byDay }: { byDay: Record<string, number> }) {
  const days = Object.entries(byDay)
  const W = 900
  const H = 140
  const max = Math.max(1, ...days.map(([, v]) => Math.abs(v)))
  const bw = W / Math.max(1, days.length)
  let cum = 0
  const pts = days.map(([, v], i) => {
    cum += v
    return [i * bw + bw / 2, cum] as const
  })
  const cmax = Math.max(1, ...pts.map(([, c]) => Math.abs(c)))
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full">
      <line x1={0} x2={W} y1={H / 2} y2={H / 2} stroke="#334155" />
      {days.map(([d, v], i) => (
        <rect key={d} x={i * bw + 1} width={Math.max(1, bw - 2)} y={v >= 0 ? H / 2 - (v / max) * (H / 2 - 6) : H / 2} height={(Math.abs(v) / max) * (H / 2 - 6)} fill={v >= 0 ? '#10b981' : '#f43f5e'} opacity={0.75}>
          <title>{`${d}: ${v.toFixed(2)} R`}</title>
        </rect>
      ))}
      <polyline fill="none" stroke="#e2e8f0" strokeWidth={1.5} points={pts.map(([x, c]) => `${x},${H / 2 - (c / cmax) * (H / 2 - 6)}`).join(' ')} />
      <text x={4} y={12} fontSize="10" fill="#64748b">
        bars = R each day · line = running total (own scale)
      </text>
    </svg>
  )
}
