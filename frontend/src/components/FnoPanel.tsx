import { useState } from 'react'
import { cls, fmt, istHm } from '../lib/api'
import { usePoll } from '../lib/usePoll'

interface Leg {
  price_chg_pct: number
  oi_chg: number
  oi_chg_pct: number | null
  state: 'LB' | 'SC' | 'SB' | 'LU'
  weak: boolean
}

interface Reading {
  fut: string
  call: string
  put: string
  score: number
  bias: string
  premium: string
  row: number
  weak: string[]
}

interface Side {
  ltp: number | null
  chg_pct: number | null
  oi: number | null
  oi_chg: number | null
  iv: number | null
  /** Black-76 on this expiry's future: delta, gamma (per ₹1), vega (₹ per IV point), theta (₹ per day) */
  delta?: number
  gamma?: number
  vega?: number
  theta?: number
}

interface Wall {
  strike: number
  value: number
}

interface Expiry {
  expiry: string
  days: number
  future: { code: string; name: string; expiry: string; ltp: number }
  strikes: number
  atm?: number
  walls_cover?: [number, number]
  straddle?: number
  straddle_prev?: number
  straddle_chg_pct?: number
  implied_move_pct?: number
  iv?: number | null
  iv_prev?: number | null
  call_oi?: number
  put_oi?: number
  call_oi_chg?: number
  put_oi_chg?: number
  pcr_oi?: number | null
  pcr_oi_chg?: number | null
  call_wall?: Wall | null
  put_wall?: Wall | null
  call_add?: Wall | null
  put_add?: Wall | null
  rows?: { strike: number; dist_pct?: number; sd?: number | null; far?: boolean; CE: Side | null; PE: Side | null }[]
}

interface RecentLeg {
  oi_chg: number
  oi_chg_pct: number | null
  strikes: number
}

/** The last 15 minutes / hour: futures build-up, and calls / puts OI change over the same strikes */
interface Recent {
  window: string
  futures: { price_chg_pct: number; oi_chg_pct: number; oi_chg: number; build_up: 'LB' | 'SC' | 'SB' | 'LU' | 'FLAT' }
  calls: RecentLeg | null
  puts: RecentLeg | null
  pcr_then?: number
  pcr_now?: number
}

interface Fno {
  symbol: string
  future: { name: string; expiry: string; ltp: number | null; prev_close: number | null; oi: number | null; oi_ref: number | null }
  legs: { futures: Leg | null; calls: Leg | null; puts: Leg | null }
  reading: Reading | null
  oi_since: number | null
  recent?: Recent[]
  missing: string[]
  expiries: Expiry[]
}

const LONG: Record<string, string> = { LB: 'long build-up', SC: 'short covering', SB: 'short build-up', LU: 'long unwinding' }
const biasColor = (b: string) => (b.includes('Bull') ? 'text-emerald-300' : b.includes('Bear') ? 'text-rose-300' : 'text-slate-300')
const signed = (v: number | null | undefined) => (v == null ? 'DM' : `${v >= 0 ? '+' : ''}${fmt.int(v)}`)

const writers = (leg: RecentLeg | null, word: string) =>
  !leg ? `${word} —` : `${word} ${signed(leg.oi_chg)}${leg.oi_chg_pct != null ? ` (${fmt.pct(leg.oi_chg_pct, 1)})` : ''}`

/** The turn a since-the-open label hides: who positioned in the last 15 minutes and the last hour. */
function RecentStrip({ rows }: { rows: Recent[] }) {
  if (!rows.length) return <div className="mt-2 text-[10px] text-slate-600">last 15 min / 1 hour: not enough data yet</div>
  return (
    <div className="mt-2 grid gap-2 md:grid-cols-2">
      {rows.map((r) => {
        const b = r.futures.build_up
        const tone = b === 'LB' || b === 'SC' ? 'text-emerald-300' : b === 'FLAT' ? 'text-slate-400' : 'text-rose-300'
        return (
          <div key={r.window} className="rounded border border-slate-800 bg-slate-900/40 p-2 font-mono text-[11px]">
            <div className="flex justify-between">
              <span className="text-slate-400">last {r.window}</span>
              <span className={tone}>
                futures {b}
                {b !== 'FLAT' && ` · ${LONG[b]}`}
              </span>
            </div>
            <div className="mt-1 text-slate-300">
              price {fmt.pct(r.futures.price_chg_pct)} · fut OI {fmt.pct(r.futures.oi_chg_pct, 2)}
            </div>
            <div className="text-slate-300">
              <span className={cls((r.puts?.oi_chg ?? 0) > 0 && 'text-emerald-300')}>{writers(r.puts, 'puts')}</span>
              {' · '}
              <span className={cls((r.calls?.oi_chg ?? 0) > 0 && 'text-rose-300')}>{writers(r.calls, 'calls')}</span>
              {r.pcr_then != null && r.pcr_now != null && (
                <span className="text-slate-500">
                  {' '}
                  · PCR {r.pcr_then.toFixed(2)}→{r.pcr_now.toFixed(2)}
                </span>
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}

function LegCard({ name, leg, priceWord }: { name: string; leg: Leg | null; priceWord: string }) {
  if (!leg) return <div className="rounded border border-slate-800 p-2 text-[11px] text-slate-600">{name}: no data</div>
  const up = leg.state === 'LB' || leg.state === 'SC'
  return (
    <div className={cls('rounded border p-2 font-mono text-[11px]', leg.weak ? 'border-amber-900/60' : 'border-slate-800')}>
      <div className="flex justify-between">
        <span className="text-slate-400">{name}</span>
        <span className={up ? 'text-emerald-300' : 'text-rose-300'}>
          {leg.state} · {LONG[leg.state]}
        </span>
      </div>
      <div className="mt-1 text-slate-300">
        {priceWord} {fmt.pct(leg.price_chg_pct)} · OI {signed(leg.oi_chg)} ({fmt.pct(leg.oi_chg_pct, 1)})
      </div>
      {leg.weak && <div className="text-[10px] text-amber-500">OI barely moved — unconfirmed</div>}
    </div>
  )
}

function Ladder({ x }: { x: Expiry }) {
  const [view, setView] = useState<'market' | 'greeks'>('market')
  const rows = x.rows ?? []
  const maxOi = Math.max(1, ...rows.flatMap((r) => [r.CE?.oi ?? 0, r.PE?.oi ?? 0]))
  const bar = (v: number | null | undefined, right: boolean, color: string) => (
    <div className={cls('flex h-2.5 w-16', right ? 'justify-start' : 'justify-end')}>
      <div style={{ width: `${((v ?? 0) / maxOi) * 100}%` }} className={color} />
    </div>
  )
  const chg = (v: number | null | undefined) => <span className={(v ?? 0) >= 0 ? 'text-emerald-400' : 'text-rose-400'}>{signed(v)}</span>
  const g = (v: number | undefined, d: number) => (v == null ? '—' : v.toFixed(d))
  // the strike, and how far it is from the future: % and standard deviations of the move the ATM
  // IV prices in to expiry — beyond 1σ is outside the expected range ("far")
  const strikeCell = (r: (typeof rows)[number], atm: boolean) => (
    <td className={cls('px-2 text-center', atm ? 'font-bold text-sky-300' : r.far ? 'text-slate-500' : 'text-slate-300')}>
      {fmt.n(r.strike, 0)}
      {r.dist_pct != null && (
        <div className={cls('text-[9px] font-normal', r.far ? 'text-amber-600' : 'text-slate-500')}>
          {r.dist_pct >= 0 ? '+' : ''}
          {r.dist_pct.toFixed(1)}%{r.sd != null ? ` · ${Math.abs(r.sd).toFixed(1)}σ` : ''}
          {r.far ? ' far' : ''}
        </div>
      )}
    </td>
  )
  const toggle = (
    <div className="mt-2 flex gap-1 text-[10px]">
      {(['market', 'greeks'] as const).map((v) => (
        <button key={v} onClick={() => setView(v)} className={cls('rounded px-2 py-0.5', view === v ? 'bg-sky-900 text-sky-100' : 'bg-slate-900 text-slate-400')}>
          {v === 'market' ? 'OI · price · IV' : 'Greeks Δ Γ ν Θ'}
        </button>
      ))}
      <span className="ml-2 self-center text-slate-600">σ = standard deviations of the move priced in to expiry; far = outside 1σ</span>
    </div>
  )
  if (view === 'greeks')
    return (
      <>
        {toggle}
        <table className="mt-1 w-full font-mono text-[10px]">
          <thead className="text-slate-500">
            <tr>
              <th className="text-right" title="change in premium per ₹1 of the future">call Δ</th>
              <th className="px-1 text-right" title="change in delta per ₹1 of the future">Γ</th>
              <th className="px-1 text-right" title="₹ change in premium per 1 point of IV">ν</th>
              <th className="px-1 text-right" title="₹ of premium lost per calendar day">Θ/day</th>
              <th className="px-1 text-right">LTP</th>
              <th className="px-2 text-center text-slate-300">strike</th>
              <th className="px-1 text-left">LTP</th>
              <th className="px-1 text-left">Θ/day</th>
              <th className="px-1 text-left">ν</th>
              <th className="px-1 text-left">Γ</th>
              <th className="text-left">put Δ</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const atm = r.strike === x.atm
              return (
                <tr key={r.strike} className={cls(atm && 'bg-slate-800/60', r.far && 'opacity-60')}>
                  <td className="py-0.5 text-right text-slate-200">{g(r.CE?.delta, 2)}</td>
                  <td className="px-1 text-right text-slate-400">{g(r.CE?.gamma, 4)}</td>
                  <td className="px-1 text-right text-slate-400">{g(r.CE?.vega, 2)}</td>
                  <td className="px-1 text-right text-rose-300">{g(r.CE?.theta, 2)}</td>
                  <td className="px-1 text-right text-slate-200">{fmt.n(r.CE?.ltp)}</td>
                  {strikeCell(r, atm)}
                  <td className="px-1 text-left text-slate-200">{fmt.n(r.PE?.ltp)}</td>
                  <td className="px-1 text-left text-rose-300">{g(r.PE?.theta, 2)}</td>
                  <td className="px-1 text-left text-slate-400">{g(r.PE?.vega, 2)}</td>
                  <td className="px-1 text-left text-slate-400">{g(r.PE?.gamma, 4)}</td>
                  <td className="py-0.5 text-left text-slate-200">{g(r.PE?.delta, 2)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </>
    )
  return (
    <>
    {toggle}
    <table className="mt-1 w-full font-mono text-[10px]">
      <thead className="text-slate-500">
        <tr>
          <th className="text-right">call OI</th>
          <th className="px-1 text-right">ΔOI</th>
          <th className="px-1 text-right">%</th>
          <th className="px-1 text-right">LTP</th>
          <th className="px-1 text-right">IV</th>
          <th className="px-2 text-center text-slate-300">strike</th>
          <th className="px-1 text-left">IV</th>
          <th className="px-1 text-left">LTP</th>
          <th className="px-1 text-left">%</th>
          <th className="px-1 text-left">ΔOI</th>
          <th className="text-left">put OI</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => {
          const atm = r.strike === x.atm
          return (
            <tr key={r.strike} className={cls(atm && 'bg-slate-800/60', r.far && 'opacity-60')}>
              <td className="py-0.5">
                <div className="flex items-center justify-end gap-1">
                  <span className={cls(r.strike === x.call_wall?.strike && 'text-rose-300')}>{fmt.int(r.CE?.oi)}</span>
                  {bar(r.CE?.oi, false, 'bg-rose-500/60')}
                </div>
              </td>
              <td className="px-1 text-right">{chg(r.CE?.oi_chg)}</td>
              <td className="px-1 text-right text-slate-400">{r.CE?.chg_pct != null ? fmt.pct(r.CE.chg_pct, 1) : '—'}</td>
              <td className="px-1 text-right text-slate-200">{fmt.n(r.CE?.ltp)}</td>
              <td className="px-1 text-right text-slate-500">{r.CE?.iv != null ? r.CE.iv.toFixed(1) : '—'}</td>
              {strikeCell(r, atm)}
              <td className="px-1 text-left text-slate-500">{r.PE?.iv != null ? r.PE.iv.toFixed(1) : '—'}</td>
              <td className="px-1 text-left text-slate-200">{fmt.n(r.PE?.ltp)}</td>
              <td className="px-1 text-left text-slate-400">{r.PE?.chg_pct != null ? fmt.pct(r.PE.chg_pct, 1) : '—'}</td>
              <td className="px-1 text-left">{chg(r.PE?.oi_chg)}</td>
              <td className="py-0.5">
                <div className="flex items-center gap-1">
                  {bar(r.PE?.oi, true, 'bg-emerald-500/60')}
                  <span className={cls(r.strike === x.put_wall?.strike && 'text-emerald-300')}>{fmt.int(r.PE?.oi)}</span>
                </div>
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
    </>
  )
}

/** Futures and options read together, live: each leg's build-up, the 64-state reading, and per
 * expiry the walls, put-call ratio, straddle (the move priced in) and IV, over a strike ladder. */
export function FnoPanel({ symbol }: { symbol: string }) {
  const { data, error } = usePoll<Fno>(`/api/fno/${encodeURIComponent(symbol)}`, 3000)
  const [pick, setPick] = useState(0)
  if (error) return <div className="mt-3 text-[11px] text-slate-600">futures & options: {error}</div>
  if (!data) return null
  const r = data.reading
  const x = data.expiries[Math.min(pick, Math.max(0, data.expiries.length - 1))]
  return (
    <div className="mt-3 rounded border border-slate-800 p-3">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-[11px]">
        <span className="font-semibold text-slate-200">Futures & options</span>
        {r ? (
          <>
            <span className="font-mono text-slate-300">
              64-state #{r.row} · {r.fut} / {r.call} / {r.put}
            </span>
            <span className={cls('font-mono font-semibold', biasColor(r.bias))}>
              {r.score >= 0 ? '+' : ''}
              {r.score} {r.bias}
            </span>
            <span className={cls('font-mono', r.premium === 'Divergence' ? 'text-amber-400' : 'text-slate-400')}>{r.premium}</span>
            {r.weak.length > 0 && <span className="text-amber-500">unconfirmed: {r.weak.join(', ')}</span>}
          </>
        ) : (
          <span className="text-slate-500">no reading yet</span>
        )}
        {data.oi_since != null && <span className="text-slate-500">OI change since {istHm(data.oi_since)} (no recorded close)</span>}
      </div>
      {data.missing.length > 0 && <div className="mt-1 text-[10px] text-amber-600">missing: {data.missing.join(' · ')}</div>}
      {data.recent && <RecentStrip rows={data.recent} />}
      <div className="mt-2 text-[10px] text-slate-500">since the previous close</div>
      <div className="mt-1 grid gap-2 md:grid-cols-3">
        <LegCard name={`Futures ${data.future.expiry.slice(5)}`} leg={data.legs.futures} priceWord="price" />
        <LegCard name="Calls near the money" leg={data.legs.calls} priceWord="premium" />
        <LegCard name="Puts near the money" leg={data.legs.puts} priceWord="premium" />
      </div>
      {data.expiries.length > 0 && x && (
        <div className="mt-3">
          <div className="flex flex-wrap gap-2 text-[11px]">
            {data.expiries.map((e, i) => (
              <button
                key={e.expiry}
                onClick={() => setPick(i)}
                className={cls('rounded px-2 py-0.5', e.expiry === x.expiry ? 'bg-slate-700 text-slate-100' : 'bg-slate-900 text-slate-400')}
              >
                {e.expiry} · {e.days}d
              </button>
            ))}
          </div>
          <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 font-mono text-[11px] text-slate-400">
            <span>
              on {x.future.name} {x.future.expiry.slice(5)} <span className="text-slate-200">{fmt.n(x.future.ltp)}</span>
            </span>
            <span>
              ATM {fmt.n(x.atm, 0)} · straddle <span className="text-slate-200">{fmt.n(x.straddle)}</span>
              {x.implied_move_pct != null && <> (±{x.implied_move_pct.toFixed(2)}% to expiry)</>}
              {x.straddle_chg_pct != null && <> {fmt.pct(x.straddle_chg_pct, 1)}</>}
            </span>
            <span>
              IV <span className="text-slate-200">{x.iv != null ? `${x.iv.toFixed(1)}%` : 'DM'}</span>
              {x.iv_prev != null && <> (prev close {x.iv_prev.toFixed(1)}%)</>}
            </span>
            <span>
              PCR OI <span className="text-slate-200">{x.pcr_oi != null ? x.pcr_oi.toFixed(2) : 'DM'}</span>
              {x.pcr_oi_chg != null && <> · ΔOI {x.pcr_oi_chg.toFixed(2)}</>}
            </span>
            <span>
              call wall <span className="text-rose-300">{x.call_wall ? `${fmt.n(x.call_wall.strike, 0)} (${fmt.int(x.call_wall.value)})` : 'DM'}</span> · put wall{' '}
              <span className="text-emerald-300">{x.put_wall ? `${fmt.n(x.put_wall.strike, 0)} (${fmt.int(x.put_wall.value)})` : 'DM'}</span>
            </span>
            <span>
              adding: calls {x.call_add ? `${fmt.n(x.call_add.strike, 0)} +${fmt.int(x.call_add.value)}` : '—'} · puts{' '}
              {x.put_add ? `${fmt.n(x.put_add.strike, 0)} +${fmt.int(x.put_add.value)}` : '—'}
            </span>
          </div>
          <Ladder x={x} />
          <p className="mt-1 text-[10px] text-slate-600">
            walls are the largest OI among the {x.strikes} strikes subscribed
            {x.walls_cover && ` (${fmt.n(x.walls_cover[0], 0)}–${fmt.n(x.walls_cover[1], 0)})`} · options are priced on the future of their own expiry ·
            IV is Black-76 · the 64-state is a reading of positioning, not a tested signal
          </p>
        </div>
      )}
    </div>
  )
}
