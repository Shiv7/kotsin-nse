import { createChart, LineStyle, type UTCTimestamp } from 'lightweight-charts'
import { useEffect, useRef, useState } from 'react'
import { cls, fmt, ist, istChartOptions } from '../lib/api'
import { usePoll } from '../lib/usePoll'
import type { OHLCV } from '../types'

export interface MtfTrade {
  symbol: string
  side: 'BULLISH' | 'BEARISH'
  setup: string
  sweep_ts: number
  level: number
  wick: number
  signal_ts: number
  entry: number
  stop: number
  t1: number
  t2: number
  rr: number
  qty: number
  day: string
  exits: { ts: number; price: number; frac: number; why: string }[]
  exit_reason: string
  exit_ts: number
  r: number
  costs: number
  net: number
  mfe_r: number
  mae_r: number
  sessions_held: number
}

const DAY = 86400

/** One multi-day trade on its own chart: the higher-timeframe sweep, the lower-timeframe entry,
 * the plan (stop, T1, T2) and every exit — on 1h, 15m or 5m candles from the 5-minute cache. */
export function TradeChart({ t, onClose }: { t: MtfTrade; onClose: () => void }) {
  const [tf, setTf] = useState<'240m' | '60m' | '15m' | '5m'>(t.setup.startsWith('240m') ? '240m' : '60m')
  const span = tf === '5m' ? [2 * DAY, DAY] : tf === '15m' ? [4 * DAY, 2 * DAY] : [12 * DAY, 4 * DAY]
  const { data, error } = usePoll<{ bars: OHLCV[] }>(
    `/api/history/range/${encodeURIComponent(t.symbol)}?start=${t.sweep_ts - span[0]}&end=${t.exit_ts + span[1]}&tf=${tf}`,
    600000,
  )
  const box = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!box.current || !data?.bars?.length) return
    const c = createChart(box.current, {
      layout: { background: { color: '#020617' }, textColor: '#94a3b8' },
      grid: { vertLines: { color: '#0f172a' }, horzLines: { color: '#0f172a' } },
      ...istChartOptions,
      rightPriceScale: { borderColor: '#334155' },
      height: 440,
      width: box.current.clientWidth,
    })
    const s = c.addCandlestickSeries({ upColor: '#10b981', downColor: '#f43f5e', wickUpColor: '#10b981', wickDownColor: '#f43f5e', borderVisible: false })
    s.setData(data.bars.map((b) => ({ time: b.ts as UTCTimestamp, open: b.o, high: b.h, low: b.l, close: b.c })))
    const line = (price: number, color: string, title: string, style = LineStyle.Solid, width: 1 | 2 = 1) =>
      s.createPriceLine({ price, color, lineWidth: width, lineStyle: style, axisLabelVisible: true, title })
    line(t.level, '#a78bfa', 'swept level', LineStyle.Dashed)
    line(t.entry, '#facc15', 'entry', LineStyle.Dotted)
    line(t.stop, '#f59e0b', 'stop', LineStyle.Solid, 2)
    line(t.t1, '#e2e8f0', 'T1', LineStyle.Dashed)
    line(t.t2, '#e2e8f0', 'T2', LineStyle.Solid, 2)
    // a marker sits on the bar that CONTAINS the moment: the last bar starting at or before it
    const at = (ts: number) => {
      let best = data.bars[0].ts
      for (const b of data.bars) if (b.ts <= ts) best = b.ts
      return best as UTCTimestamp
    }
    const bull = t.side === 'BULLISH'
    s.setMarkers(
      [
        { time: at(t.sweep_ts - 1), position: (bull ? 'belowBar' : 'aboveBar') as 'belowBar' | 'aboveBar', color: '#a78bfa', shape: 'circle' as const, text: 'sweep' },
        { time: at(t.signal_ts - 1), position: (bull ? 'belowBar' : 'aboveBar') as 'belowBar' | 'aboveBar', color: '#facc15', shape: (bull ? 'arrowUp' : 'arrowDown') as 'arrowUp' | 'arrowDown', text: `IN ${t.entry}` },
        ...t.exits.map((e) => ({
          time: at(e.ts - 1),
          position: (bull ? 'aboveBar' : 'belowBar') as 'aboveBar' | 'belowBar',
          color: e.why.startsWith('T') && e.why !== 'TIME' ? '#22c55e' : e.why === 'TIME' ? '#94a3b8' : '#ef4444',
          shape: 'square' as const,
          text: `${e.why} ${e.price}`,
        })),
      ].sort((a, b) => (a.time as number) - (b.time as number)),
    )
    c.timeScale().fitContent()
    return () => c.remove()
  }, [data, t])

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/70 p-4" onClick={onClose}>
      <div className="w-full max-w-5xl rounded-lg border border-slate-700 bg-slate-950 p-4" onClick={(e) => e.stopPropagation()}>
        <div className="mb-3 flex flex-wrap items-baseline gap-3">
          <span className="text-lg font-bold text-slate-100">{t.symbol}</span>
          <span className={cls('rounded px-2 py-0.5 text-[11px] font-semibold', t.side === 'BULLISH' ? 'bg-emerald-900/60 text-emerald-300' : 'bg-rose-900/60 text-rose-300')}>
            {t.side === 'BULLISH' ? 'LONG' : 'SHORT'}
          </span>
          <span className="text-xs text-sky-300">{t.setup}</span>
          <span className={cls('font-mono text-sm', t.r >= 0 ? 'text-emerald-400' : 'text-rose-400')}>
            {t.r.toFixed(2)} R · {fmt.signedInr(t.net)}
          </span>
          <div className="ml-auto flex gap-1">
            {(['240m', '60m', '15m', '5m'] as const).map((x) => (
              <button key={x} onClick={() => setTf(x)} className={cls('rounded px-2 py-0.5 text-xs', tf === x ? 'bg-slate-700 text-white' : 'bg-slate-900 text-slate-400')}>
                {x === '240m' ? '4h' : x === '60m' ? '1h' : x}
              </button>
            ))}
            <button onClick={onClose} className="ml-2 rounded px-2 py-0.5 text-xs text-slate-400 hover:bg-slate-800">
              close ✕
            </button>
          </div>
        </div>
        <div className="mb-3 grid grid-cols-2 gap-2 font-mono text-[11px] md:grid-cols-4">
          <Cell k="SWEEP" v={`${fmt.n(t.level)} (wick ${fmt.n(t.wick)}) · ${ist(t.sweep_ts, false)}`} />
          <Cell k="ENTRY" v={`${fmt.n(t.entry)} · ${ist(t.signal_ts, false)}`} />
          <Cell k="STOP / T1 / T2" v={`${fmt.n(t.stop)} / ${fmt.n(t.t1)} / ${fmt.n(t.t2)}`} />
          <Cell k="PLAN R:R" v={`1 : ${t.rr} · qty ${t.qty}`} />
          <Cell k="EXIT" v={`${t.exit_reason} · ${ist(t.exit_ts, false)} · ${t.sessions_held} sessions`} />
          <Cell k="BEST / WORST" v={`+${t.mfe_r.toFixed(2)} R / −${t.mae_r.toFixed(2)} R`} />
          <Cell k="COSTS" v={fmt.inr(t.costs)} />
        </div>
        {error && <div className="text-xs text-rose-400">{error}</div>}
        <div ref={box} />
        <p className="mt-2 text-[10px] text-slate-600">
          ● sweep = the higher-timeframe candle that took the level and closed back · ▲▼ IN = the lower-timeframe close that broke structure the same way ·
          squares = exits
        </p>
      </div>
    </div>
  )
}

function Cell({ k, v }: { k: string; v: string }) {
  return (
    <div className="rounded border border-slate-800 bg-slate-900/60 px-2 py-1">
      <span className="text-slate-500">{k} </span>
      <span className="text-slate-200">{v}</span>
    </div>
  )
}
