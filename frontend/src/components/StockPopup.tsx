import { createChart, LineStyle, type UTCTimestamp } from 'lightweight-charts'
import { useEffect, useRef, useState } from 'react'
import { FlowPanel } from './FlowPanel'
import { FnoPanel } from './FnoPanel'
import { cls, fmt, istChartOptions, istHm } from '../lib/api'
import { usePoll } from '../lib/usePoll'
import type { BoardRow, LevelsData, OHLCV, PivotCluster, ProfileData, StructureData, VolumeProfileData } from '../types'

export interface TradeMarks {
  entry_ts: number
  entry: number
  exits: { ts: number; price: number; why: string }[]
  /** when the setup was taken — the structure is drawn as it stood then */
  signal_at: string
  /** the trade's own plan (stop, targets), drawn as lines */
  lines?: { price: number; title: string; color: string }[]
}

interface Props {
  row: BoardRow
  /** replay: the day and the minute the board stands at; live: undefined */
  day?: string
  at?: string
  trade?: TradeMarks
  onClose: () => void
}

interface LiveBars {
  bars: { ts: number; o: number; h: number; l: number; c: number; v: number }[]
}

/** One stock: 5-minute candles up to the board's minute, VWAP, volume, and the board's reading. */
export function StockPopup({ row, day, at, trade, onClose }: Props) {
  const path = day
    ? `/api/history/bars/${row.symbol}?day=${day}&tf=5m${at ? `&until=${at}` : ''}`
    : `/api/bars/${row.symbol}?tf=5m&n=80`
  const { data, error } = usePoll<{ bars: OHLCV[] } | LiveBars>(path, day ? 600000 : 10000)
  const structure = usePoll<StructureData>(
    day
      ? `/api/structure/${encodeURIComponent(row.symbol)}?day=${day}&at=${trade?.signal_at ?? at ?? '15:30'}`
      : `/api/structure/${encodeURIComponent(row.symbol)}`,
    day ? 600000 : 30000,
  )
  const profile = usePoll<ProfileData>(
    day ? `/api/profile/${encodeURIComponent(row.symbol)}?day=${day}${at ? `&at=${at}` : ''}&days=5` : `/api/profile/${encodeURIComponent(row.symbol)}`,
    day ? 600000 : 30000,
  )
  const levels = usePoll<LevelsData>(
    day
      ? `/api/levels/${encodeURIComponent(row.symbol)}?day=${day}&at=${trade?.signal_at ?? at ?? '15:30'}`
      : `/api/levels/${encodeURIComponent(row.symbol)}`,
    day ? 600000 : 60000,
  )
  const [allPivots, setAllPivots] = useState(false)
  const box = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!box.current || !data?.bars?.length) return
    const bars = data.bars
    const c = createChart(box.current, {
      layout: { background: { color: '#020617' }, textColor: '#94a3b8' },
      grid: { vertLines: { color: '#0f172a' }, horzLines: { color: '#0f172a' } },
      ...istChartOptions,
      rightPriceScale: { borderColor: '#334155' },
      height: 340,
      width: box.current.clientWidth,
    })
    const candles = c.addCandlestickSeries({
      upColor: '#10b981', downColor: '#f43f5e', wickUpColor: '#10b981', wickDownColor: '#f43f5e', borderVisible: false,
    })
    candles.setData(bars.map((b) => ({ time: b.ts as UTCTimestamp, open: b.o, high: b.h, low: b.l, close: b.c })))
    const vol = c.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: 'vol', lastValueVisible: false, priceLineVisible: false })
    c.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } })
    vol.setData(bars.map((b) => ({ time: b.ts as UTCTimestamp, value: b.v, color: b.c >= b.o ? 'rgba(16,185,129,0.35)' : 'rgba(244,63,94,0.35)' })))
    // session VWAP, typical price weighted by volume — the board's own definition. It restarts
    // each IST day: 80 five-minute bars reach into the previous session, and carrying its volume
    // over put GOLD's line at 146,837 on 2026-10-20 against a true session VWAP of 146,392
    let pv = 0
    let vv = 0
    let day = -1
    const vw = c.addLineSeries({ color: '#38bdf8', lineWidth: 1, lineStyle: LineStyle.Dashed, priceLineVisible: false, lastValueVisible: true, title: 'VWAP' })
    vw.setData(
      bars.map((b) => {
        const d = Math.floor((b.ts + 19800) / 86400) // IST is UTC+5:30, no DST
        if (d !== day) {
          day = d
          pv = 0
          vv = 0
        }
        pv += ((b.h + b.l + b.c) / 3) * b.v
        vv += b.v
        return vv > 0 ? { time: b.ts as UTCTimestamp, value: pv / vv } : { time: b.ts as UTCTimestamp }
      }),
    )
    // structure: the current setup's lines, its zone's edges, the liquidity met on the way, and
    // a marker at every break of structure and every sweep
    const st = structure.data
    if (st) {
      const line = (price: number, color: string, title: string, style = LineStyle.Solid, width: 1 | 2 = 1) =>
        candles.createPriceLine({ price, color, lineWidth: width, lineStyle: style, axisLabelVisible: true, title })
      const su = st.setup
      if (su) {
        const zn = st.zones[su.zone_index]
        const tone = su.side === 'BULLISH' ? '#10b981' : '#f43f5e'
        if (zn) {
          line(zn.high, tone, '', LineStyle.Dotted)
          line(zn.low, tone, '', LineStyle.Dotted)
        }
        line(su.z, tone, `Z ${su.state.toLowerCase()}`, LineStyle.Solid, 2)
        line(su.s1, '#f59e0b', 'S1 (wrong)')
        if (su.r1 != null) line(su.r1, '#e2e8f0', 'R1', LineStyle.Dashed)
        if (su.r2 != null) line(su.r2, '#e2e8f0', 'R2', LineStyle.Dashed)
        for (const x of su.extra) line(x.price, 'rgba(245,158,11,0.6)', x.kind.toLowerCase(), LineStyle.SparseDotted)
      }
      const marks = [
        ...st.zones.map((z) => ({
          time: z.bos_ts as UTCTimestamp,
          position: (z.side === 'BULLISH' ? 'belowBar' : 'aboveBar') as 'belowBar' | 'aboveBar',
          color: z.state === 'BROKEN' ? '#64748b' : z.side === 'BULLISH' ? '#10b981' : '#f43f5e',
          shape: (z.side === 'BULLISH' ? 'arrowUp' : 'arrowDown') as 'arrowUp' | 'arrowDown',
          text: `BOS${z.bos_kind === 'OPENING_RANGE' ? ' OR' : ''}${z.state === 'BROKEN' ? ' ✕' : ''}`,
        })),
        // one dot per bar that swept something; only the last three are labelled
        ...[...new Map(st.sweeps.map((w) => [Math.floor(w.ts / 300) * 300, w])).values()].map((w, i, all) => ({
          time: (Math.floor(w.ts / 300) * 300) as UTCTimestamp,
          position: (w.side === 'BULLISH' ? 'belowBar' : 'aboveBar') as 'belowBar' | 'aboveBar',
          color: '#a78bfa',
          shape: 'circle' as const,
          text: i >= all.length - 3 ? `sweep ${w.kind.toLowerCase().replace('_', ' ')}` : '',
        })),
        ...(trade
          ? [
              {
                time: (Math.floor((trade.entry_ts - 60) / 300) * 300) as UTCTimestamp,
                position: 'inBar' as const,
                color: '#facc15',
                shape: 'square' as const,
                text: `IN ${trade.entry}`,
              },
              ...trade.exits.map((e) => ({
                time: (Math.floor((e.ts - 60) / 300) * 300) as UTCTimestamp,
                position: 'inBar' as const,
                color: e.why === 'R1' || e.why === 'R2' ? '#22c55e' : e.why === 'EOD' ? '#94a3b8' : '#ef4444',
                shape: 'square' as const,
                text: `${e.why} ${e.price}`,
              })),
            ]
          : []),
      ].sort((a, b) => (a.time as number) - (b.time as number))
      candles.setMarkers(marks)
    }
    if (trade) {
      candles.createPriceLine({ price: trade.entry, color: '#facc15', lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: true, title: 'entry' })
      for (const l of trade.lines ?? []) candles.createPriceLine({ price: l.price, color: l.color, lineWidth: 2, lineStyle: LineStyle.Solid, axisLabelVisible: true, title: l.title })
    }
    // pivot clusters: the nearest three support and three resistance zones, line weight = zone
    // weight (monthly 3, weekly 2, daily 1 per member); every single pivot only on request
    // A zone more than a normal day beyond the candles is listed above the chart, not drawn: a price
    // line pulls the scale with it, and a 6 % gap squashed WIPRO's whole session into a sliver.
    const lv = levels.data
    if (lv) {
      const pad = lv.adr ?? 0
      const lo = Math.min(...bars.map((b) => b.l)) - pad
      const hi = Math.max(...bars.map((b) => b.h)) + pad
      for (const z of nearestZones(lv.clusters).filter((z) => z.high >= lo && z.low <= hi)) {
        const tone = z.kind === 'SUPPORT' ? '16,185,129' : z.kind === 'RESISTANCE' ? '244,63,94' : '250,204,21'
        const width = (z.weight >= 6 ? 4 : z.weight >= 4 ? 3 : 2) as 2 | 3 | 4
        candles.createPriceLine({
          price: z.mid, color: `rgba(${tone},0.55)`, lineWidth: width, lineStyle: LineStyle.Solid, axisLabelVisible: true,
          title: `${z.kind === 'SUPPORT' ? 'S' : z.kind === 'RESISTANCE' ? 'R' : '='} ${z.members.join('·')}`,
        })
        if (z.high - z.low > 0) {
          for (const edge of [z.low, z.high])
            candles.createPriceLine({ price: edge, color: `rgba(${tone},0.25)`, lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: false, title: '' })
        }
      }
      if (allPivots) {
        for (const p of lv.levels.filter((p) => p.price >= lo && p.price <= hi))
          candles.createPriceLine({
            price: p.price, color: p.frame === 'M' ? 'rgba(251,191,36,0.5)' : p.frame === 'W' ? 'rgba(148,163,184,0.5)' : 'rgba(100,116,139,0.35)',
            lineWidth: 1, lineStyle: LineStyle.SparseDotted, axisLabelVisible: false, title: p.name,
          })
      }
    }
    const vp = profile.data?.volume
    if (vp) {
      candles.createPriceLine({ price: vp.poc, color: '#c084fc', lineWidth: 2, lineStyle: LineStyle.Solid, axisLabelVisible: true, title: 'POC' })
      candles.createPriceLine({ price: vp.vah, color: 'rgba(192,132,252,0.6)', lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: false, title: 'VAH' })
      candles.createPriceLine({ price: vp.val, color: 'rgba(192,132,252,0.6)', lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: false, title: 'VAL' })
    }
    c.timeScale().fitContent()
    return () => c.remove()
  }, [data, structure.data, profile.data, levels.data, allPivots])

  const bull = row.side === 'BULLISH'
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/70 p-4" onClick={onClose}>
      <div className="w-full max-w-4xl rounded-lg border border-slate-700 bg-slate-950 p-4" onClick={(e) => e.stopPropagation()}>
        <div className="mb-3 flex flex-wrap items-baseline gap-3">
          <span className="text-lg font-bold text-slate-100">{row.symbol}</span>
          <span className="text-xs text-slate-500">{row.sector}</span>
          <span className="font-mono text-sm text-slate-200">{fmt.n(row.last)}</span>
          <span className={cls('font-mono text-sm', (row.pct ?? 0) >= 0 ? 'text-emerald-400' : 'text-rose-400')}>{fmt.pct(row.pct)}</span>
          {row.side !== 'NONE' && (
            <span className={cls('rounded px-2 py-0.5 text-[11px] font-semibold', bull ? 'bg-emerald-900/60 text-emerald-300' : 'bg-rose-900/60 text-rose-300')}>
              {row.side} #{row.rank}
            </span>
          )}
          {row.event_risk && <span className="rounded bg-amber-900/60 px-2 py-0.5 text-[11px] text-amber-300">event risk</span>}
          <button onClick={onClose} className="ml-auto rounded px-2 py-1 text-xs text-slate-400 hover:bg-slate-800">
            close ✕
          </button>
        </div>
        <div className="mb-3 grid grid-cols-2 gap-2 font-mono text-[11px] md:grid-cols-4">
          {structure.data?.setup && (
            <>
              <Cell k="SETUP" v={`${structure.data.setup.side} · ${structure.data.setup.state.toLowerCase()}`} />
              <Cell k="Z / S1" v={`${fmt.n(structure.data.setup.z)} / ${fmt.n(structure.data.setup.s1)}`} />
              <Cell k="R1 / R2" v={`${fmt.n(structure.data.setup.r1)} / ${fmt.n(structure.data.setup.r2)}`} />
              <Cell k="R:R" v={structure.data.setup.rr != null ? `1 : ${structure.data.setup.rr}` : '—'} />
            </>
          )}
          <Cell k="STRENGTH" v={`${row.score >= 0 ? '+' : ''}${row.score.toFixed(2)}× day`} />
          <Cell k="PERCENTILE" v={row.percentile != null ? `p${row.percentile}` : '—'} />
          <Cell k="VWAP" v={row.above_vwap == null ? '—' : row.above_vwap ? '▲ above' : '▼ below'} />
          <Cell
            k="OI"
            v={
              row.oi_build_up
                ? `${row.oi_build_up} ${fmt.pct(row.oi_change_pct)}${row.oi_since ? ` since ${istHm(row.oi_since)}` : ''}`
                : '—'
            }
          />
          <Cell k="RULE" v={row.rule || '—'} wide />
        </div>
        {levels.data && <LevelsStrip lv={levels.data} allPivots={allPivots} onToggle={() => setAllPivots((v) => !v)} />}
        {error && <div className="text-xs text-rose-400">{error}</div>}
        <div className="flex gap-2">
          <div ref={box} className="min-w-0 flex-1" />
          {profile.data?.volume && <ProfileBars vp={profile.data.volume} composite={profile.data.composite ?? null} />}
        </div>
        {!day && <FnoPanel symbol={row.symbol} />}
        {!day && <FlowPanel symbol={row.symbol} />}
        {profile.data?.market && (
          <div className="mt-2 flex flex-wrap gap-3 font-mono text-[11px] text-slate-400">
            <span>
              market profile: <span className="text-slate-200">{profile.data.market.day_type.replace('_', ' ').toLowerCase()}</span>
            </span>
            {profile.data.market.ib_low != null && profile.data.market.ib_high != null && (
              <span>
                first hour {fmt.n(profile.data.market.ib_low)}–{fmt.n(profile.data.market.ib_high)}
              </span>
            )}
            <span>
              TPO value {fmt.n(profile.data.market.val)}–{fmt.n(profile.data.market.vah)} · POC {fmt.n(profile.data.market.poc)}
            </span>
            <span>{profile.data.market.single_prints.length} single prints</span>
            {profile.data.market.poor_high && <span className="text-amber-400">poor high</span>}
            {profile.data.market.poor_low && <span className="text-amber-400">poor low</span>}
            {profile.data.volume && (
              <span>
                volume POC {fmt.n(profile.data.volume.poc)} · value {fmt.n(profile.data.volume.val)}–{fmt.n(profile.data.volume.vah)}
              </span>
            )}
          </div>
        )}
        <p className="mt-2 text-[10px] text-slate-600">
          5-minute candles{day ? ` up to ${at ?? 'the close'} on ${day}` : ''} · blue dashed = VWAP · Z = where the setup acts, S1 = where it is wrong, R1/R2 =
          liquidity ahead · ▲▼ = break of structure (✕ = zone since broken) · ● = sweep · thick green / red = pivot support /
          resistance zone (where daily, weekly and monthly pivots meet; thicker = more and higher-timeframe pivots; a zone
          more than a normal day away is listed above, not drawn)
        </p>
      </div>
    </div>
  )
}

/** The three nearest support and resistance zones, plus any zone the price sits in. */
function nearestZones(cl: PivotCluster[]): PivotCluster[] {
  const by = (k: PivotCluster['kind']) => cl.filter((c) => c.kind === k).sort((a, b) => Math.abs(a.distance_pct ?? 0) - Math.abs(b.distance_pct ?? 0))
  return [...by('AT_PRICE'), ...by('SUPPORT').slice(0, 3), ...by('RESISTANCE').slice(0, 3)]
}

/** Nearest zones as a line under the cells: resistance above, support below, distance from price. */
function LevelsStrip({ lv, allPivots, onToggle }: { lv: LevelsData; allPivots: boolean; onToggle: () => void }) {
  const zones = nearestZones(lv.clusters)
  const res = zones.filter((z) => z.kind === 'RESISTANCE').sort((a, b) => b.mid - a.mid)
  const at = zones.filter((z) => z.kind === 'AT_PRICE')
  const sup = zones.filter((z) => z.kind === 'SUPPORT').sort((a, b) => b.mid - a.mid)
  const zone = (z: PivotCluster, tone: string) => (
    <span key={`${z.low}-${z.high}`} className={cls('rounded border px-2 py-0.5', tone)} title={`weight ${z.weight}`}>
      {fmt.n(z.low)}{z.high > z.low ? `–${fmt.n(z.high)}` : ''} <span className="text-slate-400">{z.members.join('·')}</span>
      {z.distance_pct != null && z.kind !== 'AT_PRICE' && <span className="text-slate-500"> {z.distance_pct > 0 ? '+' : ''}{z.distance_pct.toFixed(2)}%</span>}
    </span>
  )
  // with no zone on a side, the nearest single pivot there is still the next level to watch
  const last = lv.last ?? 0
  const single = (above: boolean) => {
    const p = lv.levels.filter((x) => (above ? x.price > last : x.price < last)).sort((a, b) => (above ? a.price - b.price : b.price - a.price))[0]
    return (
      <span className="text-slate-500">
        no zone{p ? ` · next pivot ${p.name} ${fmt.n(p.price)} (${((p.price / last - 1) * 100).toFixed(2)}%)` : ''}
      </span>
    )
  }
  return (
    <div className="mb-3 space-y-1 font-mono text-[11px]">
      <div className="flex flex-wrap items-center gap-2">
        <span className="w-24 text-slate-500">resistance</span>
        {res.length ? res.map((z) => zone(z, 'border-rose-900 bg-rose-950/40 text-rose-300')) : single(true)}
      </div>
      {at.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          <span className="w-24 text-slate-500">at price</span>
          {at.map((z) => zone(z, 'border-amber-900 bg-amber-950/40 text-amber-300'))}
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <span className="w-24 text-slate-500">support</span>
        {sup.length ? sup.map((z) => zone(z, 'border-emerald-900 bg-emerald-950/40 text-emerald-300')) : single(false)}
        <button onClick={onToggle} className="ml-auto rounded px-2 py-0.5 text-slate-400 hover:bg-slate-800">
          {allPivots ? 'hide' : 'show'} every pivot
        </button>
      </div>
    </div>
  )
}

/** Volume at price, top = high: today's bars, and the multi-day composite as a faint outline. */
function ProfileBars({ vp, composite }: { vp: VolumeProfileData; composite: VolumeProfileData | null }) {
  const H = 340
  const W = 110
  const lo = Math.min(vp.prices[0], composite?.prices[0] ?? Infinity)
  const hi = Math.max(vp.prices[vp.prices.length - 1], composite?.prices[composite.prices.length - 1] ?? -Infinity)
  const span = Math.max(hi - lo, vp.step)
  const y = (p: number) => H - 8 - ((p - lo) / span) * (H - 16)
  const max = Math.max(...vp.volumes, 1)
  const cmax = composite ? Math.max(...composite.volumes, 1) : 1
  const h = Math.max(1, ((H - 16) / span) * vp.step - 0.5)
  return (
    <svg width={W} height={H} className="shrink-0">
      {composite?.prices.map((p, i) => (
        <rect key={`c${p}`} x={0} y={y(p) - h / 2} width={(composite.volumes[i] / cmax) * (W - 10)} height={h} fill="rgba(148,163,184,0.12)" />
      ))}
      {vp.prices.map((p, i) => (
        <rect
          key={p}
          x={0}
          y={y(p) - h / 2}
          width={(vp.volumes[i] / max) * (W - 10)}
          height={h}
          fill={p === vp.poc ? '#c084fc' : p >= vp.val && p <= vp.vah ? 'rgba(192,132,252,0.55)' : 'rgba(100,116,139,0.5)'}
        />
      ))}
      <text x={2} y={10} fontSize="9" fill="#64748b">
        volume at price
      </text>
    </svg>
  )
}

function Cell({ k, v, wide }: { k: string; v: string; wide?: boolean }) {
  return (
    <div className={cls('rounded border border-slate-800 bg-slate-900/60 px-2 py-1', wide && 'col-span-2')}>
      <span className="text-slate-500">{k} </span>
      <span className="text-slate-200">{v}</span>
    </div>
  )
}
