import { fmt } from '../lib/api'
import { usePoll } from '../lib/usePoll'

interface FlowRow {
  ts: number
  buy: number
  sell: number
  delta: number
  cum_delta: number
  imbalance: number | null
  oi: number | null
}

interface FlowData {
  /** false for an MCX root: its underlying is the front future, shown once */
  has_cash: boolean
  equity: FlowRow[]
  future: FlowRow[]
  estimate: string
}

/** Today's estimated delta (bars) and cumulative delta (line) for the stock and its future, plus
 * the future's OI — live only, recorded from the feed's snapshots. */
export function FlowPanel({ symbol }: { symbol: string }) {
  const { data, error } = usePoll<FlowData>(`/api/flow/${encodeURIComponent(symbol)}?tf=5`, 5000)
  if (error) return <div className="mt-2 text-[11px] text-slate-600">order flow: {error}</div>
  if (!data || (!data.equity.length && !data.future.length)) {
    return <div className="mt-2 text-[11px] text-slate-600">order flow: nothing recorded yet today — it builds from the live feed</div>
  }
  return (
    <div className="mt-3 grid gap-3 md:grid-cols-2">
      {data.has_cash && <Series title="Cash — estimated delta" rows={data.equity} />}
      <Series title="Front future — estimated delta, OI" rows={data.future} showOi />
      <p className="text-[10px] text-slate-600 md:col-span-2">{data.estimate}: each snapshot's new volume is called a buy at or above the previous ask, a sell at or below the previous bid</p>
    </div>
  )
}

function Series({ title, rows, showOi }: { title: string; rows: FlowRow[]; showOi?: boolean }) {
  const W = 420
  const H = 110
  if (!rows.length) return <div className="text-[11px] text-slate-600">{title}: no data</div>
  const maxD = Math.max(1, ...rows.map((r) => Math.abs(r.delta)))
  const cds = rows.map((r) => r.cum_delta)
  const cmin = Math.min(...cds, 0)
  const cmax = Math.max(...cds, 0)
  const ois = rows.map((r) => r.oi).filter((x): x is number => x != null)
  const omin = Math.min(...ois)
  const omax = Math.max(...ois)
  const bw = W / rows.length
  const last = rows[rows.length - 1]
  return (
    <div>
      <div className="mb-1 flex justify-between text-[11px] text-slate-400">
        <span>{title}</span>
        <span className="font-mono">
          CVD {fmt.int(last.cum_delta)}
          {last.imbalance != null && ` · book ${(last.imbalance * 100).toFixed(0)}%`}
          {showOi && last.oi != null && ` · OI ${fmt.int(last.oi)}`}
        </span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full rounded border border-slate-800 bg-slate-950">
        <line x1={0} x2={W} y1={H / 2} y2={H / 2} stroke="#1e293b" />
        {rows.map((r, i) => {
          const h = (Math.abs(r.delta) / maxD) * (H / 2 - 4)
          return <rect key={r.ts} x={i * bw + 0.5} width={Math.max(1, bw - 1)} y={r.delta >= 0 ? H / 2 - h : H / 2} height={h} fill={r.delta >= 0 ? '#10b981' : '#f43f5e'} opacity={0.7} />
        })}
        <polyline
          fill="none"
          stroke="#e2e8f0"
          strokeWidth={1.2}
          points={rows.map((r, i) => `${i * bw + bw / 2},${H - 4 - ((r.cum_delta - cmin) / Math.max(1, cmax - cmin)) * (H - 8)}`).join(' ')}
        />
        {showOi && ois.length > 1 && (
          <polyline
            fill="none"
            stroke="#c084fc"
            strokeDasharray="3 2"
            strokeWidth={1}
            points={rows
              .map((r, i) => (r.oi != null ? `${i * bw + bw / 2},${H - 4 - ((r.oi - omin) / Math.max(1, omax - omin)) * (H - 8)}` : null))
              .filter(Boolean)
              .join(' ')}
          />
        )}
      </svg>
    </div>
  )
}
