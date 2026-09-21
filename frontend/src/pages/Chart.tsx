import { createChart, type IChartApi, type IPriceLine, type ISeriesApi, type SeriesMarker, type UTCTimestamp } from 'lightweight-charts'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Card, ErrorLine, Table } from '../components/Ui'
import { fmt, ist } from '../lib/api'
import { usePoll } from '../lib/usePoll'
import type { BarsResponse, IndicatorsResponse, SignalRow, UniverseRow } from '../types'

const TFS = ['1m', '2m', '3m', '5m', '15m', '30m', '1d'] as const
type Tf = (typeof TFS)[number]

/**
 * Candles, the SAME Bollinger/SuperTrend the strategy decides on, every signal as a marker, and a
 * forming candle that ticks from the engine's WebSocket.
 *
 * The indicator lines come from /api/indicators, which calls the strategy's own `bollinger` and
 * `supertrend` with its live config. They are not a charting-library reimplementation: if a line
 * here disagrees with a signal, the signal is wrong, not the chart.
 */
export function Chart() {
  const universe = usePoll<UniverseRow[]>('/api/universe', 30000)
  const [symbol, setSymbol] = useState('')
  const [tf, setTf] = useState<Tf>('30m')
  const first = universe.data?.[0]?.symbol
  const active = symbol || first || ''
  const { data, error } = usePoll<BarsResponse>(active ? `/api/bars/${active}?tf=${tf}&n=400` : null, 5000)
  const ind = usePoll<IndicatorsResponse>(active && tf !== '1d' ? `/api/indicators/${active}?tf=${tf}&n=400` : null, 5000)
  const signals = usePoll<SignalRow[]>('/api/signals?limit=400', 10000)
  const [live, setLive] = useState<{ ts: number; c: number; v: number } | null>(null)
  const [wsState, setWsState] = useState<'connecting' | 'open' | 'closed'>('connecting')

  const box = useRef<HTMLDivElement>(null)
  const chart = useRef<IChartApi | null>(null)
  const candles = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const bbU = useRef<ISeriesApi<'Line'> | null>(null)
  const bbM = useRef<ISeriesApi<'Line'> | null>(null)
  const bbL = useRef<ISeriesApi<'Line'> | null>(null)
  const stUp = useRef<ISeriesApi<'Line'> | null>(null)
  const stDn = useRef<ISeriesApi<'Line'> | null>(null)
  const lines = useRef<IPriceLine[]>([])
  const lastClosedTs = useRef<number>(0)

  useEffect(() => {
    if (!box.current) return
    const c = createChart(box.current, {
      layout: { background: { color: '#020617' }, textColor: '#94a3b8' },
      grid: { vertLines: { color: '#0f172a' }, horzLines: { color: '#0f172a' } },
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: '#334155' },
      rightPriceScale: { borderColor: '#334155' },
      height: 460,
    })
    candles.current = c.addCandlestickSeries({
      upColor: '#10b981', downColor: '#f43f5e', wickUpColor: '#10b981', wickDownColor: '#f43f5e', borderVisible: false,
    })
    const line = (color: string, width: 1 | 2 = 1, style = 0) =>
      c.addLineSeries({ color, lineWidth: width, lineStyle: style, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false })
    bbU.current = line('rgba(56,189,248,0.7)')
    bbM.current = line('rgba(148,163,184,0.5)', 1, 2)
    bbL.current = line('rgba(56,189,248,0.7)')
    stUp.current = line('#22c55e', 2)
    stDn.current = line('#ef4444', 2)
    chart.current = c
    const onResize = () => c.applyOptions({ width: box.current?.clientWidth ?? 600 })
    onResize()
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      c.remove()
      chart.current = candles.current = null
      bbU.current = bbM.current = bbL.current = stUp.current = stDn.current = null
      lines.current = []
    }
  }, [])

  // closed candles + pivot zones
  useEffect(() => {
    const s = candles.current
    if (!s || !Array.isArray(data?.bars)) return
    s.setData(data.bars.map((b) => ({ time: b.ts as UTCTimestamp, open: b.o, high: b.h, low: b.l, close: b.c })))
    lastClosedTs.current = data.bars.length ? data.bars[data.bars.length - 1].ts : 0
    for (const l of lines.current) s.removePriceLine(l)
    lines.current = (Array.isArray(data.zones) ? data.zones : []).map((z) =>
      s.createPriceLine({
        price: z.price,
        color: z.strength >= 5.2 ? 'rgba(245,158,11,0.8)' : 'rgba(71,85,105,0.6)',
        lineWidth: 1,
        lineStyle: 2,
        axisLabelVisible: z.strength >= 5.2,
        title: z.members.slice(0, 2).join('+'),
      }),
    )
  }, [data])

  // the strategy's own indicators
  useEffect(() => {
    const rows = ind.data?.rows
    if (!Array.isArray(rows) || !bbU.current) return
    const t = (r: { ts: number }) => r.ts as UTCTimestamp
    bbU.current.setData(rows.map((r) => (r.bb_upper != null ? { time: t(r), value: r.bb_upper } : { time: t(r) })))
    bbM.current?.setData(rows.map((r) => (r.bb_middle != null ? { time: t(r), value: r.bb_middle } : { time: t(r) })))
    bbL.current?.setData(rows.map((r) => (r.bb_lower != null ? { time: t(r), value: r.bb_lower } : { time: t(r) })))
    stUp.current?.setData(rows.map((r) => (r.st_value != null && r.st_trend === 1 ? { time: t(r), value: r.st_value } : { time: t(r) })))
    stDn.current?.setData(rows.map((r) => (r.st_value != null && r.st_trend === -1 ? { time: t(r), value: r.st_value } : { time: t(r) })))
  }, [ind.data])

  // every signal on this symbol, as a marker
  useEffect(() => {
    const s = candles.current
    if (!s || !Array.isArray(signals.data) || tf === '1d') return
    const bucket = { '1m': 60, '2m': 120, '3m': 180, '5m': 300, '15m': 900, '30m': 1800 }[tf]
    const marks: SeriesMarker<UTCTimestamp>[] = signals.data
      .filter((x) => x.symbol === active)
      .map((x) => ({
        time: (Math.floor(x.ts / bucket) * bucket) as UTCTimestamp,
        position: (x.direction === 'BULLISH' ? 'belowBar' : 'aboveBar') as 'belowBar' | 'aboveBar',
        color: x.strategy === 'FUKAA' ? '#a78bfa' : '#38bdf8',
        shape: (x.direction === 'BULLISH' ? 'arrowUp' : 'arrowDown') as 'arrowUp' | 'arrowDown',
        text: `${x.strategy} ${x.grade || ''}${x.decision && x.decision !== 'PAPER_FILLED' && x.decision !== 'SUBMITTED' ? ' ✕' : ''}`,
      }))
      .sort((a, b) => (a.time as number) - (b.time as number))
    s.setMarkers(marks)
  }, [signals.data, active, tf])

  // the forming candle, from the engine's socket
  useEffect(() => {
    if (tf === '1d' || !active) return
    const url = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`
    let ws: WebSocket | null = null
    let stopped = false
    const connect = () => {
      if (stopped) return
      setWsState('connecting')
      ws = new WebSocket(url)
      ws.onopen = () => setWsState('open')
      ws.onclose = () => {
        setWsState('closed')
        if (!stopped) setTimeout(connect, 2000)
      }
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data as string) as { ts: number; forming: Record<string, { ts: number; o: number; h: number; l: number; c: number; v: number }> }
          const f = msg.forming?.[`${active}:${tf}`]
          if (!f || !candles.current) return
          if (f.ts < lastClosedTs.current) return // a stale bucket cannot go before the last closed bar
          candles.current.update({ time: f.ts as UTCTimestamp, open: f.o, high: f.h, low: f.l, close: f.c })
          setLive({ ts: msg.ts, c: f.c, v: f.v })
        } catch {
          /* a malformed frame is not worth a crash */
        }
      }
    }
    connect()
    return () => {
      stopped = true
      ws?.close()
    }
  }, [active, tf])

  const symbols = useMemo(() => (universe.data ?? []).map((u) => u.symbol), [universe.data])
  const p = ind.data?.params

  return (
    <div className="space-y-4 p-4">
      <ErrorLine error={error ?? universe.error ?? ind.error} />
      <div className="flex flex-wrap items-center gap-2">
        <select value={active} onChange={(e) => setSymbol(e.target.value)} className="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-xs">
          {symbols.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        {TFS.map((t) => (
          <button key={t} onClick={() => setTf(t)} className={'rounded px-2 py-1 text-xs ' + (tf === t ? 'bg-slate-700 text-white' : 'bg-slate-900 text-slate-400')}>
            {t}
          </button>
        ))}
        <span className={'ml-2 rounded px-1.5 py-0.5 text-[10px] font-semibold ' + (wsState === 'open' ? 'bg-emerald-900/60 text-emerald-300' : 'bg-slate-800 text-slate-400')}>
          {tf === '1d' ? 'daily' : wsState === 'open' ? '● LIVE' : wsState}
        </span>
        {live && tf !== '1d' && (
          <span className="text-xs text-slate-400">
            forming {fmt.n(live.c)} · vol {fmt.int(live.v)} · {ist(live.ts, true).slice(11)} IST
          </span>
        )}
        {p && (
          <span className="ml-auto text-[11px] text-slate-500">
            <span className="text-sky-400">BB({p.bb_period},{p.bb_mult})</span> · <span className="text-emerald-400">ST</span>
            <span className="text-rose-400">({p.st_atr_period},{p.st_mult})</span> — the strategy's own config
          </span>
        )}
      </div>

      {!symbols.length && (
        <Card title="No symbols">
          <p className="text-[11px] leading-relaxed text-slate-500">The universe is empty — it is built from the scrip master, which needs a broker session.</p>
        </Card>
      )}

      <Card title={`${active || '—'} ${tf}`} right={`${data?.bars?.length ?? 0} bars · ▲▼ = signals (✕ = not filled) · amber = wall`}>
        <div ref={box} />
      </Card>

      <Card title="Confluence zones" right="stops and targets are drawn from these — walls in amber">
        <Table head={['Price', 'Strength', 'Wall?', 'Members']} empty="no zones — needs 25+ daily bars">
          {(Array.isArray(data?.zones) ? data.zones : []).map((z) => (
            <tr key={z.price} className="border-b border-slate-900">
              <td className="px-2 py-1.5">{fmt.n(z.price)}</td>
              <td className="px-2 py-1.5">{fmt.n(z.strength, 1)}</td>
              <td className="px-2 py-1.5">{z.strength >= 5.2 ? <span className="text-amber-400">wall</span> : '—'}</td>
              <td className="px-2 py-1.5 text-slate-400">{z.members.join(', ')}</td>
            </tr>
          ))}
        </Table>
      </Card>
    </div>
  )
}
