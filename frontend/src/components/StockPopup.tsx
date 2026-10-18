import { createChart, LineStyle, type UTCTimestamp } from 'lightweight-charts'
import { useEffect, useRef } from 'react'
import { cls, fmt } from '../lib/api'
import { usePoll } from '../lib/usePoll'
import type { BoardRow, OHLCV, StructureData } from '../types'

interface Props {
  row: BoardRow
  /** replay: the day and the minute the board stands at; live: undefined */
  day?: string
  at?: string
  onClose: () => void
}

interface LiveBars {
  bars: { ts: number; o: number; h: number; l: number; c: number; v: number }[]
}

/** One stock: 5-minute candles up to the board's minute, VWAP, volume, and the board's reading. */
export function StockPopup({ row, day, at, onClose }: Props) {
  const path = day
    ? `/api/history/bars/${row.symbol}?day=${day}&tf=5m${at ? `&until=${at}` : ''}`
    : `/api/bars/${row.symbol}?tf=5m&n=80`
  const { data, error } = usePoll<{ bars: OHLCV[] } | LiveBars>(path, day ? 600000 : 10000)
  const structure = usePoll<StructureData>(
    day ? `/api/structure/${encodeURIComponent(row.symbol)}?day=${day}${at ? `&at=${at}` : ''}` : `/api/structure/${encodeURIComponent(row.symbol)}`,
    day ? 600000 : 30000,
  )
  const box = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!box.current || !data?.bars?.length) return
    const bars = data.bars
    const c = createChart(box.current, {
      layout: { background: { color: '#020617' }, textColor: '#94a3b8' },
      grid: { vertLines: { color: '#0f172a' }, horzLines: { color: '#0f172a' } },
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: '#334155' },
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
    // session VWAP, typical price weighted by volume — the board's own definition
    let pv = 0
    let vv = 0
    const vw = c.addLineSeries({ color: '#38bdf8', lineWidth: 1, lineStyle: LineStyle.Dashed, priceLineVisible: false, lastValueVisible: true, title: 'VWAP' })
    vw.setData(
      bars.map((b) => {
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
        ...st.sweeps.map((w) => ({
          time: (Math.floor(w.ts / 300) * 300) as UTCTimestamp,
          position: (w.side === 'BULLISH' ? 'belowBar' : 'aboveBar') as 'belowBar' | 'aboveBar',
          color: '#a78bfa',
          shape: 'circle' as const,
          text: `sweep ${w.kind.toLowerCase()}`,
        })),
      ].sort((a, b) => (a.time as number) - (b.time as number))
      candles.setMarkers(marks)
    }
    c.timeScale().fitContent()
    return () => c.remove()
  }, [data, structure.data])

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
          <Cell k="OI" v={row.oi_build_up ? `${row.oi_build_up} ${fmt.pct(row.oi_change_pct)}` : '—'} />
          <Cell k="RULE" v={row.rule || '—'} wide />
        </div>
        {error && <div className="text-xs text-rose-400">{error}</div>}
        <div ref={box} />
        <p className="mt-2 text-[10px] text-slate-600">
          5-minute candles{day ? ` up to ${at ?? 'the close'} on ${day}` : ''} · blue dashed = VWAP · Z = where the setup acts, S1 = where it is wrong, R1/R2 =
          liquidity ahead · ▲▼ = break of structure (✕ = zone since broken) · ● = sweep
        </p>
      </div>
    </div>
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
