import { useState } from 'react'
import { Badge, Card, ErrorLine, Table } from '../components/Ui'
import { cls, fmt } from '../lib/api'
import { usePoll } from '../lib/usePoll'
import type { UniverseRow } from '../types'

interface Leg {
  scrip_code: string
  ltp: number | null
  bid: number | null
  ask: number | null
  spread_pct: number | null
  age_s: number | null
  delta_est: number | null
}

interface ChainRow {
  strike: number
  lot_size: number
  CE?: Leg
  PE?: Leg
}

interface Pick {
  ok: boolean
  reason: string
  anchor: number
  strike: number | null
  scrip_code: string | null
  name: string | null
  premium: number
  spread_pct: number | null
}

interface Chain {
  symbol: string
  spot: number | null
  expiry: string
  expiries: string[]
  rows: ChainRow[]
  selection: Record<string, Pick>
  policy: Record<string, number | null>
}

function LegCell({ leg, spot, strike, side }: { leg?: Leg; spot: number | null; strike: number; side: 'CE' | 'PE' }) {
  if (!leg) return <td className="px-2 py-1.5 text-slate-700">—</td>
  const itm = spot != null && (side === 'CE' ? strike < spot : strike > spot)
  const stale = leg.age_s != null && leg.age_s > 30
  return (
    <td className={cls('px-2 py-1.5', itm && 'bg-slate-800/40')}>
      <span className={stale ? 'text-amber-500' : 'text-slate-200'}>{fmt.n(leg.ltp)}</span>
      <span className="ml-2 text-[10px] text-slate-500">
        {fmt.n(leg.bid)}/{fmt.n(leg.ask)}
        {leg.spread_pct != null && <span className={leg.spread_pct > 8 ? ' text-rose-400' : ''}> · {leg.spread_pct}%</span>}
        {leg.delta_est != null && <span className="text-slate-600"> · Δ{leg.delta_est}</span>}
        {stale && <span className="text-amber-500"> · {leg.age_s}s old</span>}
      </span>
    </td>
  )
}

/**
 * The chain, and — the part that matters — which strike the engine would actually buy.
 *
 * The rule is "OTM at entry, roughly ATM at the confluence T1", and it decides what a signal costs.
 * In the old stack this step ran inline inside the trigger, took 3–23 seconds on a cache miss and
 * blocked publication; nobody could see which strike it had picked or why it had refused. Here the
 * real `select_option` runs against live quotes for both directions and reports the choice or the
 * reason there isn't one.
 */
export function Options() {
  const universe = usePoll<UniverseRow[]>('/api/universe', 30000)
  const [symbol, setSymbol] = useState('')
  const [expiry, setExpiry] = useState('')
  const active = symbol || universe.data?.[0]?.symbol || ''
  const { data, error } = usePoll<Chain>(
    active ? `/api/chain/${active}${expiry ? `?expiry=${expiry}` : ''}` : null,
    5000,
  )

  const picks = data?.selection ?? {}

  return (
    <div className="space-y-4 p-4">
      <ErrorLine error={error ?? universe.error} />

      <div className="flex flex-wrap items-center gap-2">
        <select
          value={active}
          onChange={(e) => {
            setSymbol(e.target.value)
            setExpiry('')
          }}
          className="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-xs"
        >
          {(universe.data ?? []).map((u) => (
            <option key={u.symbol} value={u.symbol}>
              {u.symbol}
            </option>
          ))}
        </select>
        <select
          value={expiry || data?.expiry || ''}
          onChange={(e) => setExpiry(e.target.value)}
          className="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-xs"
        >
          {(data?.expiries ?? []).map((e) => (
            <option key={e} value={e}>
              {e}
            </option>
          ))}
        </select>
        {data?.spot != null && <span className="text-xs text-slate-400">spot {fmt.n(data.spot)}</span>}
        <span className="text-xs text-slate-600">{data?.rows.length ?? 0} strikes</span>
      </div>

      {!data && !error && (
        <Card title="No chain">
          <p className="text-[11px] leading-relaxed text-slate-500">
            The chain is built from the scrip master, which needs a broker session. Check the boot
            notes on Overview.
          </p>
        </Card>
      )}

      {Object.keys(picks).length > 0 && (
        <div className="grid gap-3 md:grid-cols-2">
          {(['BULLISH', 'BEARISH'] as const).map((dir) => {
            const p = picks[dir]
            if (!p) return null
            return (
              <Card
                key={dir}
                title={
                  <span className="flex items-center gap-2">
                    <Badge tone={dir === 'BULLISH' ? 'green' : 'red'}>{dir}</Badge>
                    <span className="text-slate-400">would buy</span>
                  </span>
                }
                right={p.ok ? 'tradeable' : 'no strike'}
              >
                {p.ok ? (
                  <div className="space-y-1">
                    <div className="text-sm font-semibold text-slate-100">{p.name}</div>
                    <div className="text-xs text-slate-400">
                      strike {fmt.n(p.strike)} · premium {fmt.n(p.premium)}
                      {p.spread_pct != null && ` · spread ${p.spread_pct}%`}
                    </div>
                    <div className="text-[11px] text-slate-500">
                      anchored at {fmt.n(p.anchor)} — with no live target this is spot, i.e. plain ATM;
                      on a real signal it is the confluence T1
                    </div>
                  </div>
                ) : (
                  <div className="text-xs text-rose-300">{p.reason}</div>
                )}
              </Card>
            )
          })}
        </div>
      )}

      <Card title={`${active} ${data?.expiry ?? ''}`} right="shaded = in the money · amber = stale quote">
        <Table head={['CE  ltp  bid/ask · spread · Δ', 'Strike', 'PE  ltp  bid/ask · spread · Δ', 'Lot']} empty="no strikes">
          {(data?.rows ?? []).map((r) => {
            const atm =
              data?.spot != null &&
              Math.abs(r.strike - data.spot) ===
                Math.min(...(data.rows ?? []).map((x) => Math.abs(x.strike - (data.spot ?? 0))))
            return (
              <tr key={r.strike} className={cls('border-b border-slate-900', atm && 'bg-sky-950/30')}>
                <LegCell leg={r.CE} spot={data?.spot ?? null} strike={r.strike} side="CE" />
                <td className={cls('px-2 py-1.5 text-center font-medium', atm && 'text-sky-300')}>
                  {fmt.n(r.strike, 0)}
                  {atm && <span className="ml-1 text-[9px] text-sky-500">ATM</span>}
                </td>
                <LegCell leg={r.PE} spot={data?.spot ?? null} strike={r.strike} side="PE" />
                <td className="px-2 py-1.5 text-slate-500">{r.lot_size}</td>
              </tr>
            )
          })}
        </Table>
      </Card>

      {data?.policy && (
        <Card title="Selection policy" right="a strike outside these is not tradeable">
          <Table head={['Rule', 'Value']}>
            {Object.entries(data.policy).map(([k, v]) => (
              <tr key={k} className="border-b border-slate-900">
                <td className="px-2 py-1 font-mono text-[11px] text-slate-400">{k}</td>
                <td className="px-2 py-1 font-mono text-[11px]">{v === null ? 'none' : String(v)}</td>
              </tr>
            ))}
          </Table>
        </Card>
      )}
    </div>
  )
}
