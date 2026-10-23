import { cls, fmt, istHm } from '../lib/api'
import { usePoll } from '../lib/usePoll'

interface MicroSide {
  frames: number
  mid?: number
  microprice?: number
  micro_bps?: number
  queue_imbalance?: number | null
  spread_bps?: number
  spread_z?: number
  book_skew?: number | null
  bid_depth?: number
  ask_depth?: number
  levels?: number
  walls?: { side: string; price: number; size: number; age_s: number }[]
  kyle_lambda_bps_per_10k?: number | null
  vpin?: number
  vpin_buckets?: number
  efficiency_ratio?: number | null
  realised_vol_bps_min?: number | null
  roll_spread?: number | null
  amihud?: number
  walls_pulled: number
  walls_filled: number
  events: { ts: number; kind: string; text: string }[]
}

interface Micro {
  symbol: string
  cash: MicroSide | null
  future: MicroSide | null
  future_name: string | null
}

const tone = (v: number | null | undefined, good = 0) => ((v ?? 0) > good ? 'text-emerald-300' : (v ?? 0) < -good ? 'text-rose-300' : 'text-slate-300')

/** One reading: its value, coloured where a sign means something, and what it means on hover. */
function Row({ k, v, cls: c, help }: { k: string; v: string; cls?: string; help: string }) {
  return (
    <div className="flex justify-between gap-2" title={help}>
      <span className="text-slate-500">{k}</span>
      <span className={cls('text-right', c ?? 'text-slate-200')}>{v}</span>
    </div>
  )
}

function Side({ name, m }: { name: string; m: MicroSide | null }) {
  if (!m) return <div className="text-[11px] text-slate-600">{name}: nothing yet</div>
  const n = (v: number | null | undefined, d = 2) => (v == null ? '—' : v.toFixed(d))
  const er = m.efficiency_ratio
  const vp = m.vpin
  return (
    <div className="space-y-0.5 font-mono text-[11px]">
      <div className="mb-1 text-slate-300">{name}</div>
      <Row k="microprice − mid" v={`${n(m.micro_bps, 2)} bps`} cls={tone(m.micro_bps, 0.05)} help="Microprice tilts the mid toward the thinner side: above the mid, the next tick is more likely up" />
      <Row k="queue imbalance" v={n(m.queue_imbalance, 2)} cls={tone(m.queue_imbalance, 0.2)} help="(best bid size − best ask size) / both: one of the most reliable very-short-term predictors" />
      <Row k="book skew (5 levels)" v={n(m.book_skew, 2)} cls={tone(m.book_skew, 0.2)} help="Visible bid vs ask size. Avellaneda–Stoikov makers lean their quotes against their inventory: the skew is its visible trace" />
      <Row k="spread" v={`${n(m.spread_bps, 2)} bps${m.spread_z != null ? ` · z ${m.spread_z.toFixed(1)}` : ''}`} cls={(m.spread_z ?? 0) > 2 ? 'text-amber-300' : undefined} help="Glosten–Milgrom: the spread is the makers' insurance against informed flow. z > 2 = wider than usual: they smell informed orders" />
      <Row k="Kyle λ" v={m.kyle_lambda_bps_per_10k != null ? `${n(m.kyle_lambda_bps_per_10k, 3)} bps / 10k sh` : '—'} help="Price impact: how far the mid moves per 10,000 shares of net (estimated) flow. High = fragile: breakouts run and stops get hit" />
      <Row k="VPIN (toxicity)" v={vp != null ? `${vp.toFixed(2)} (${m.vpin_buckets} buckets)` : '—'} cls={(vp ?? 0) > 0.6 ? 'text-amber-300' : undefined} help="Mean |buy − sell| / volume over equal-volume buckets. High = one-sided, toxic flow: makers widen or pull, big moves follow" />
      <Row k="efficiency (chop)" v={er != null ? `${er.toFixed(2)}${er < 0.3 ? ' chop' : er > 0.6 ? ' trending' : ''}` : '—'} cls={er != null && er < 0.3 ? 'text-amber-300' : undefined} help="Kaufman: |net move| / sum of moves over the window. Under 0.3 the stock is chopping" />
      <Row k="realised vol" v={m.realised_vol_bps_min != null ? `${n(m.realised_vol_bps_min, 1)} bps/√min` : '—'} help="Standard deviation of 1-minute mid returns" />
      <Row k="Roll spread" v={m.roll_spread != null ? fmt.n(m.roll_spread, 3) : '— (trending)'} help="2√(−cov of successive price changes): the bid-ask bounce implied by trade prices" />
      <Row k="Amihud" v={m.amihud != null ? n(m.amihud, 3) : '—'} help="|1-minute return| per ₹1 crore traded (×10⁴): how much price moves for the money traded" />
      <Row k="walls pulled / filled" v={`${m.walls_pulled} / ${m.walls_filled}`} cls={m.walls_pulled > m.walls_filled + 2 ? 'text-amber-300' : undefined} help="Large resting orders (≥ 3× the median level): pulled before price reached them vs traded through. Many pulls = spoof-like walls" />
      {m.walls && m.walls.length > 0 && (
        <div className="text-[10px] text-slate-400">
          walls now: {m.walls.map((w) => `${w.side.toLowerCase()} ${fmt.n(w.price)} × ${fmt.int(w.size)} (${w.age_s.toFixed(0)}s)`).join(' · ')}
        </div>
      )}
      <div className="text-[10px] text-slate-600">
        {m.frames} frames · {m.levels ?? 0} levels a side · book {fmt.int(m.bid_depth)} / {fmt.int(m.ask_depth)}
      </div>
    </div>
  )
}

/** Microstructure of the stock and its front future, live: what the book and the flow say. */
export function MicroPanel({ symbol }: { symbol: string }) {
  const { data, error } = usePoll<Micro>(`/api/micro/${encodeURIComponent(symbol)}`, 2000)
  if (error) return <div className="mt-2 text-[11px] text-slate-600">microstructure: {error}</div>
  if (!data) return null
  const events = [...(data.cash?.events ?? []).map((e) => ({ ...e, on: 'cash' })), ...(data.future?.events ?? []).map((e) => ({ ...e, on: 'fut' }))]
    .sort((a, b) => b.ts - a.ts)
    .slice(0, 8)
  return (
    <div className="mt-3 rounded border border-slate-800 p-3">
      <div className="mb-2 flex items-baseline justify-between">
        <span className="text-sm font-semibold text-slate-200">Microstructure</span>
        <span className="text-[10px] text-slate-500">last 30 min · 5-level book · flow sides estimated from snapshots · hover a row for what it means</span>
      </div>
      <div className="grid gap-4 md:grid-cols-2">
        <Side name="Cash" m={data.cash} />
        <Side name={data.future_name ?? 'Front future'} m={data.future} />
      </div>
      <div className="mt-2 border-t border-slate-800 pt-2 font-mono text-[11px]">
        <div className="mb-1 text-slate-400">book events</div>
        {events.length === 0 && <div className="text-slate-600">none yet: walls pulled or filled, absorption, liquidity vacuums appear here</div>}
        {events.map((e, i) => (
          <div key={i} className="flex gap-2">
            <span className="text-slate-500">{istHm(e.ts)}</span>
            <span className="text-slate-500">{e.on}</span>
            <span className={cls(e.kind === 'ABSORPTION' ? 'text-sky-300' : e.kind === 'VACUUM' ? 'text-amber-300' : e.kind === 'WALL_PULLED' ? 'text-rose-300' : 'text-emerald-300')}>{e.text}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
