import { useState } from 'react'
import { usePoll } from '../lib/usePoll'

// Ported from trading-dashboard's HotStocksPage/HotStocksCard. Two books side by side: CAN1
// (positional, ranked on the exchange's end-of-day data) on the left, the live book on the right.
//
// The header shows dataAsOf, NOT the response time. The original shipped generatedAt there and a
// list computed by the 05:45 enrichment read as two minutes old at any hour of the day.

type Card = {
  scripCode: string
  symbol: string
  sector: string
  fnoEligible: boolean
  ltp: number
  ltpYesterday: number
  change1dPct: number
  change5dPct: number
  change20dPct: number
  vsSectorIndexPct: number
  vsSectorLabel: string
  sectorIndex: string | null
  vsNifty50Pct: number
  vsNiftyLabel: string
  bulkDealCount: number
  blockDealCount: number
  smartBuyCr: number
  smartSellCr: number
  smartBuyClients: string[]
  smartSellClients: string[]
  dominantFlow: string
  conviction: number
  flowKnown: boolean
  deliveryPctLatest: number | null
  deliveryPctAvg5d: number | null
  deliveryTrend: string
  deliveryInstitutional: boolean
  above50dmaPct: number | null
  above200dmaPct: number | null
  trendState: string
  rsi14: number | null
  weekly52PositionPct: number | null
  priceRegime: string
  volumeRatio5d20d: number
  volumeRegime: string
  avgTurnover20dCr: number
  liquidityTier: string
  swingLow20d: number
  swingHigh20d: number
  entryZoneLow: number
  entryZoneHigh: number
  suggestedSlPrice: number | null
  oiChangePct5d: number | null
  v2Score: number
  v2Tier: string
  v2ScoreWithoutVolume: number
  v2DataConfidence: number
  v2Bucket1: number
  v2Bucket2: number
  v2Bucket3: number
  v2Bucket4: number
  v2Bucket5: number
  v2Clamps: string[]
}

type ListResponse = {
  fno: Card[]
  nonFno: Card[]
  dataAsOf: number | null
  dataAgeMinutes: number | null
  deliveryDay: string | null
  unavailable: string[]
  sources: Record<string, string>
}

type Position = {
  symbol: string
  strategy: string
  contract: string
  entryPrice: number
  sl: number
  initialSl: number
  slPct: number | null
  qty: number
  ltp: number | null
  unrealizedPct: number | null
  rNow: number | null
  grade: string
  direction: string
  openedIst: string | null
  equityEntry: number
  equitySl: number
}

type Can2 = {
  positions: Position[]
  wallets: { strategy: string; balance: number; deployed: number; realizedPnl: number; trades: number }[]
  mode: string
}

const num = (v: number | null | undefined, d = 2) =>
  v === null || v === undefined ? '—' : v.toFixed(d)

const signed = (v: number | null | undefined, d = 2) =>
  v === null || v === undefined ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(d)}`

function tone(v: number | null | undefined) {
  if (v === null || v === undefined) return 'text-slate-500'
  return v > 0 ? 'text-emerald-400' : v < 0 ? 'text-rose-400' : 'text-slate-300'
}

function scoreTone(s: number) {
  if (s >= 60) return 'bg-emerald-500/15 text-emerald-300 border-emerald-500/40'
  if (s >= 30) return 'bg-emerald-500/10 text-emerald-400 border-emerald-500/25'
  if (s > -30) return 'bg-slate-700/40 text-slate-300 border-slate-600/40'
  if (s > -60) return 'bg-rose-500/10 text-rose-400 border-rose-500/25'
  return 'bg-rose-500/15 text-rose-300 border-rose-500/40'
}

function rsTone(label: string) {
  if (label === 'LEADING') return 'text-emerald-400'
  if (label === 'LAGGING') return 'text-rose-400'
  if (label === 'UNKNOWN') return 'text-slate-600'
  return 'text-slate-400'
}

/** A value the exchange did not publish. Rendered as absent, never as zero. */
function Unknown({ what }: { what: string }) {
  return <span className="text-slate-600 italic">no {what}</span>
}

function Bucket({ label, value, cap }: { label: string; value: number; cap: number }) {
  const pct = Math.min(100, (Math.abs(value) / cap) * 100)
  return (
    <div className="flex items-center gap-1.5">
      <span className="w-16 shrink-0 text-[10px] text-slate-500">{label}</span>
      <div className="h-1.5 flex-1 overflow-hidden rounded bg-slate-800">
        <div
          className={`h-full ${value >= 0 ? 'bg-emerald-500/60' : 'bg-rose-500/60'}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className={`w-7 shrink-0 text-right text-[10px] tabular-nums ${tone(value)}`}>
        {value >= 0 ? '+' : ''}{value}
      </span>
    </div>
  )
}

function HotCard({ c }: { c: Card }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="rounded-lg border border-slate-700/40 bg-slate-900/50 p-3">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-baseline gap-2">
            <span className="truncate font-semibold text-slate-100">{c.symbol}</span>
            <span className={`text-sm tabular-nums ${tone(c.change1dPct)}`}>
              {num(c.ltp)} <span className="text-xs">({signed(c.change1dPct)}%)</span>
            </span>
          </div>
          <div className="truncate text-[11px] text-slate-500">
            {c.sector || 'sector unknown'} · {c.liquidityTier} · ₹{num(c.avgTurnover20dCr, 0)}cr/day
          </div>
        </div>
        <div className={`shrink-0 rounded border px-2 py-1 text-center ${scoreTone(c.v2Score)}`}>
          <div className="text-base font-bold leading-none tabular-nums">
            {c.v2Score >= 0 ? '+' : ''}{c.v2Score}
          </div>
          <div className="mt-0.5 text-[9px] uppercase tracking-wide opacity-80">{c.v2Tier}</div>
        </div>
      </div>

      <div className="mt-2 grid grid-cols-3 gap-x-3 gap-y-1 text-[11px]">
        <div>
          <span className="text-slate-500">5d </span>
          <span className={tone(c.change5dPct)}>{signed(c.change5dPct)}%</span>
        </div>
        <div>
          <span className="text-slate-500">20d </span>
          <span className={tone(c.change20dPct)}>{signed(c.change20dPct)}%</span>
        </div>
        <div>
          <span className="text-slate-500">RSI </span>
          <span className="text-slate-300">{num(c.rsi14, 0)}</span>
        </div>
        <div>
          <span className="text-slate-500">50d </span>
          <span className={tone(c.above50dmaPct)}>{signed(c.above50dmaPct, 1)}%</span>
        </div>
        <div>
          <span className="text-slate-500">200d </span>
          <span className={tone(c.above200dmaPct)}>{signed(c.above200dmaPct, 1)}%</span>
        </div>
        <div>
          <span className="text-slate-500">52w </span>
          <span className="text-slate-300">{num(c.weekly52PositionPct, 0)}%</span>
        </div>
      </div>

      <div className="mt-2 flex flex-wrap gap-1 text-[10px]">
        <span className="rounded bg-slate-800 px-1.5 py-0.5 text-slate-400">{c.trendState}</span>
        <span className="rounded bg-slate-800 px-1.5 py-0.5 text-slate-400">{c.priceRegime}</span>
        <span className="rounded bg-slate-800 px-1.5 py-0.5 text-slate-400">
          vol {c.volumeRegime} ×{num(c.volumeRatio5d20d, 2)}
        </span>
        <span className={`rounded bg-slate-800 px-1.5 py-0.5 ${rsTone(c.vsSectorLabel)}`}>
          sector {c.vsSectorLabel === 'UNKNOWN' ? '—' : `${signed(c.vsSectorIndexPct, 1)}%`}
        </span>
        <span className={`rounded bg-slate-800 px-1.5 py-0.5 ${rsTone(c.vsNiftyLabel)}`}>
          nifty {c.vsNiftyLabel === 'UNKNOWN' ? '—' : `${signed(c.vsNifty50Pct, 1)}%`}
        </span>
      </div>

      {c.v2Clamps.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {c.v2Clamps.map((k) => (
            <span
              key={k}
              className="rounded border border-amber-500/40 bg-amber-500/10 px-1.5 py-0.5 text-[10px] text-amber-300"
            >
              {k}
            </span>
          ))}
        </div>
      )}

      <div className="mt-2 grid grid-cols-2 gap-x-3 text-[11px]">
        <div>
          <span className="text-slate-500">delivery </span>
          {c.deliveryPctLatest === null ? (
            <Unknown what="bhavcopy" />
          ) : (
            <span className={c.deliveryInstitutional ? 'text-emerald-400' : 'text-slate-300'}>
              {num(c.deliveryPctLatest, 1)}%{' '}
              <span className="text-slate-600">
                ({c.deliveryTrend.toLowerCase()}, 5d {num(c.deliveryPctAvg5d, 1)}%)
              </span>
            </span>
          )}
        </div>
        <div>
          <span className="text-slate-500">deals </span>
          {!c.flowKnown ? (
            <Unknown what="disclosure feed" />
          ) : c.bulkDealCount + c.blockDealCount === 0 ? (
            <span className="text-slate-600">none in 7d</span>
          ) : (
            <span className="text-slate-300">
              {c.bulkDealCount}bulk/{c.blockDealCount}block ·{' '}
              <span className={tone(c.smartBuyCr - c.smartSellCr)}>
                {signed(c.smartBuyCr - c.smartSellCr, 1)}cr
              </span>
            </span>
          )}
        </div>
        <div>
          <span className="text-slate-500">entry </span>
          <span className="text-slate-300">
            {num(c.entryZoneLow)}–{num(c.entryZoneHigh)}
          </span>
        </div>
        <div>
          <span className="text-slate-500">SL </span>
          <span className="text-slate-300">{num(c.suggestedSlPrice)}</span>
        </div>
      </div>

      <button
        onClick={() => setOpen((v) => !v)}
        className="mt-2 text-[11px] text-slate-500 hover:text-slate-300"
      >
        {open ? '▾' : '▸'} score breakdown
      </button>
      {open && (
        <div className="mt-2 space-y-1 rounded bg-slate-950/50 p-2">
          <Bucket label="flow" value={c.v2Bucket1} cap={30} />
          <Bucket label="momentum" value={c.v2Bucket2} cap={25} />
          <Bucket label="OI" value={c.v2Bucket3} cap={20} />
          <Bucket label="rel str" value={c.v2Bucket4} cap={15} />
          <Bucket label="volume" value={c.v2Bucket5} cap={10} />
          <div className="pt-1 text-[10px] text-slate-500">
            without volume bucket: {c.v2ScoreWithoutVolume >= 0 ? '+' : ''}
            {c.v2ScoreWithoutVolume} · inputs known {Math.round(c.v2DataConfidence * 100)}%
            {c.oiChangePct5d !== null && ` · OI 5d ${signed(c.oiChangePct5d, 1)}%`}
          </div>
          {(c.smartBuyClients.length > 0 || c.smartSellClients.length > 0) && (
            <div className="pt-1 text-[10px] leading-relaxed">
              {c.smartBuyClients.length > 0 && (
                <div className="text-emerald-400/80">buy: {c.smartBuyClients.join(', ')}</div>
              )}
              {c.smartSellClients.length > 0 && (
                <div className="text-rose-400/80">sell: {c.smartSellClients.join(', ')}</div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function LiveBook({ data }: { data: Can2 | null }) {
  if (!data) return <div className="text-sm text-slate-500">live book unavailable</div>
  return (
    <div>
      <div className="mb-2 flex items-baseline gap-2 border-b border-sky-500/25 pb-1">
        <h2 className="text-lg font-semibold text-sky-300">Live book</h2>
        <span className="rounded border border-sky-500/30 bg-sky-500/15 px-1.5 py-0.5 text-[10px] text-sky-400">
          {data.positions.length}
        </span>
        <span className="text-[10px] text-slate-500">{data.mode}</span>
      </div>
      <p className="mb-3 text-[11px] text-slate-500">
        This engine's own open positions — FUDKII/FUKAA, option leg, trailing stop. The old CAN2 was
        a separate momentum consumer; it has no equivalent here, so this is not labelled as one.
      </p>

      <div className="mb-3 grid grid-cols-2 gap-2">
        {data.wallets.map((w) => (
          <div key={w.strategy} className="rounded border border-slate-700/40 bg-slate-900/50 p-2">
            <div className="text-[11px] font-semibold text-slate-300">{w.strategy}</div>
            <div className="text-[11px] tabular-nums text-slate-400">
              ₹{w.balance.toLocaleString('en-IN', { maximumFractionDigits: 0 })}
            </div>
            <div className="text-[10px] text-slate-500">
              deployed ₹{w.deployed.toLocaleString('en-IN', { maximumFractionDigits: 0 })} ·{' '}
              {w.trades} trades
            </div>
          </div>
        ))}
      </div>

      {data.positions.length === 0 ? (
        <div className="rounded border border-slate-700/40 bg-slate-900/40 p-4 text-sm text-slate-500">
          No open positions.
        </div>
      ) : (
        <div className="space-y-3">
          {data.positions.map((p) => (
            <div key={p.symbol + p.contract} className="rounded-lg border border-slate-700/40 bg-slate-900/50 p-3">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="flex items-baseline gap-2">
                    <span className="font-semibold text-slate-100">{p.symbol}</span>
                    <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-400">
                      {p.strategy}
                    </span>
                    <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-400">
                      {p.direction}
                    </span>
                    <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-400">
                      {p.grade}
                    </span>
                  </div>
                  <div className="truncate text-[11px] text-slate-500">{p.contract}</div>
                </div>
                <div className="shrink-0 text-right">
                  <div className={`text-sm font-semibold tabular-nums ${tone(p.unrealizedPct)}`}>
                    {signed(p.unrealizedPct, 1)}%
                  </div>
                  <div className="text-[10px] text-slate-500">
                    {p.rNow === null ? '—' : `${signed(p.rNow, 2)}R`}
                  </div>
                </div>
              </div>
              <div className="mt-2 grid grid-cols-4 gap-x-2 text-[11px]">
                <div>
                  <span className="text-slate-500">entry </span>
                  <span className="text-slate-300">{num(p.entryPrice)}</span>
                </div>
                <div>
                  <span className="text-slate-500">ltp </span>
                  <span className="text-slate-300">{num(p.ltp)}</span>
                </div>
                <div>
                  <span className="text-slate-500">SL </span>
                  <span className="text-slate-300">
                    {num(p.sl)}
                    {p.sl !== p.initialSl && (
                      <span className="text-slate-600"> ←{num(p.initialSl)}</span>
                    )}
                  </span>
                </div>
                <div>
                  <span className="text-slate-500">qty </span>
                  <span className="text-slate-300">{p.qty}</span>
                </div>
              </div>
              <div className="mt-1 text-[10px] text-slate-600">
                underlying {num(p.equityEntry)} · stop {num(p.equitySl)} · opened {p.openedIst ?? '—'}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export function HotStocks() {
  const { data, error } = usePoll<ListResponse>('/api/hot-stocks', 60_000)
  const { data: live } = usePoll<Can2>('/api/can2', 5_000)
  const [showNonFno, setShowNonFno] = useState(false)
  const [limit, setLimit] = useState(25)

  if (error) {
    return (
      <div className="p-6">
        <h1 className="mb-4 text-2xl font-semibold text-slate-100">Hot Stocks</h1>
        <div className="text-sm text-rose-400">Failed to load Hot Stocks: {String(error)}</div>
      </div>
    )
  }

  if (!data) {
    return (
      <div className="p-6">
        <h1 className="mb-4 text-2xl font-semibold text-slate-100">Hot Stocks</h1>
        <div className="text-sm text-slate-500">
          Ranking — fetching the exchange's bhavcopy, deal disclosures and index levels…
        </div>
      </div>
    )
  }

  const ranked = new Date(data.dataAsOf ?? 0)
  return (
    <div className="p-6">
      <div className="mb-4 flex items-baseline justify-between gap-4">
        <h1 className="text-2xl font-semibold text-slate-100">Hot Stocks</h1>
        <div className="text-right text-xs text-slate-500">
          {data.dataAsOf ? (
            <>
              <div>
                Ranked{' '}
                {ranked.toLocaleTimeString('en-IN', { hour12: false, timeZone: 'Asia/Kolkata' })} IST
                {typeof data.dataAgeMinutes === 'number' && (
                  <span className={data.dataAgeMinutes > 240 ? 'text-amber-400' : 'text-slate-500'}>
                    {' '}· {data.dataAgeMinutes < 60
                      ? `${data.dataAgeMinutes}m old`
                      : `${Math.floor(data.dataAgeMinutes / 60)}h ${data.dataAgeMinutes % 60}m old`}
                  </span>
                )}
              </div>
              <div className="text-[10px] text-slate-600">
                delivery from bhavcopy {data.deliveryDay ?? '—'}
              </div>
            </>
          ) : (
            <span className="text-amber-400">ranking time unknown</span>
          )}
        </div>
      </div>

      {data.unavailable.length > 0 && (
        <div className="mb-4 rounded border border-amber-500/30 bg-amber-500/10 p-2 text-[11px] text-amber-300">
          Exchange data unavailable: {data.unavailable.join(', ')}. Those buckets score zero rather
          than being guessed — the score is correspondingly less informed, not wrong.
        </div>
      )}

      <div className="grid grid-cols-1 items-start gap-6 lg:grid-cols-2">
        <section>
          <div className="mb-2 flex items-baseline gap-2 border-b border-pink-500/25 pb-1">
            <h2 className="text-lg font-semibold text-pink-300">CAN1 — Positional</h2>
            <span className="rounded border border-pink-500/30 bg-pink-500/15 px-1.5 py-0.5 text-[10px] text-pink-400">
              {data.fno.length}
            </span>
          </div>
          <p className="mb-3 text-[11px] text-slate-500">
            Ranked on the v2 signed score · flow + momentum + OI + relative strength + volume, then
            the clamp ladder · F&amp;O names, strongest accumulation first
          </p>
          <div className="grid grid-cols-1 gap-3">
            {data.fno.slice(0, limit).map((c) => (
              <HotCard key={c.scripCode} c={c} />
            ))}
          </div>
          {data.fno.length > limit && (
            <button
              onClick={() => setLimit((v) => v + 25)}
              className="mt-3 text-sm text-slate-400 hover:text-slate-200"
            >
              Show more ({data.fno.length - limit} left)
            </button>
          )}
        </section>

        <section>
          <LiveBook data={live ?? null} />
        </section>
      </div>

      {data.nonFno.length > 0 && (
        <div className="mt-8">
          <button
            onClick={() => setShowNonFno((v) => !v)}
            className="text-sm text-slate-400 hover:text-slate-200"
          >
            {showNonFno ? '▾' : '▸'} Non-F&amp;O Picks ({data.nonFno.length})
          </button>
          {showNonFno && (
            <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-2">
              {data.nonFno.map((c) => (
                <HotCard key={c.scripCode} c={c} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
