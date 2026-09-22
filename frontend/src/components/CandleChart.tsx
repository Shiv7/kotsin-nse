import { createChart, type IChartApi, type IPriceLine, type ISeriesApi, type SeriesMarker, type UTCTimestamp } from 'lightweight-charts'
import { useEffect, useRef } from 'react'

export interface ChartBar {
  ts: number
  o: number
  h: number
  l: number
  c: number
  bb_upper?: number | null
  bb_middle?: number | null
  bb_lower?: number | null
  st_value?: number | null
  st_trend?: number | null
}
export interface PriceLine {
  price: number
  color: string
  title?: string
  style?: 0 | 1 | 2 | 3 | 4
  width?: 1 | 2
  label?: boolean
}
export interface Marker {
  ts: number
  position: 'aboveBar' | 'belowBar' | 'inBar'
  color: string
  shape: 'arrowUp' | 'arrowDown' | 'circle' | 'square'
  text?: string
}

/**
 * Candles with the strategy's own Bollinger/SuperTrend lines, price lines for levels and zones,
 * and markers. The same setup the Chart page uses, without the live socket: a component so the
 * backtest debugger and the chart page cannot drift apart in how a bar looks.
 */
export function CandleChart({ bars, priceLines = [], markers = [], height = 380 }: { bars: ChartBar[]; priceLines?: PriceLine[]; markers?: Marker[]; height?: number }) {
  const box = useRef<HTMLDivElement>(null)
  const chart = useRef<IChartApi | null>(null)
  const candles = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const bbU = useRef<ISeriesApi<'Line'> | null>(null)
  const bbM = useRef<ISeriesApi<'Line'> | null>(null)
  const bbL = useRef<ISeriesApi<'Line'> | null>(null)
  const stUp = useRef<ISeriesApi<'Line'> | null>(null)
  const stDn = useRef<ISeriesApi<'Line'> | null>(null)
  const lines = useRef<IPriceLine[]>([])

  useEffect(() => {
    if (!box.current) return
    const c = createChart(box.current, {
      layout: { background: { color: '#020617' }, textColor: '#94a3b8' },
      grid: { vertLines: { color: '#0f172a' }, horzLines: { color: '#0f172a' } },
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: '#334155' },
      rightPriceScale: { borderColor: '#334155' },
      height,
    })
    candles.current = c.addCandlestickSeries({ upColor: '#10b981', downColor: '#f43f5e', wickUpColor: '#10b981', wickDownColor: '#f43f5e', borderVisible: false })
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
  }, [height])

  useEffect(() => {
    const s = candles.current
    if (!s) return
    const t = (b: ChartBar) => b.ts as UTCTimestamp
    s.setData(bars.map((b) => ({ time: t(b), open: b.o, high: b.h, low: b.l, close: b.c })))
    bbU.current?.setData(bars.map((b) => (b.bb_upper != null ? { time: t(b), value: b.bb_upper } : { time: t(b) })))
    bbM.current?.setData(bars.map((b) => (b.bb_middle != null ? { time: t(b), value: b.bb_middle } : { time: t(b) })))
    bbL.current?.setData(bars.map((b) => (b.bb_lower != null ? { time: t(b), value: b.bb_lower } : { time: t(b) })))
    stUp.current?.setData(bars.map((b) => (b.st_value != null && b.st_trend === 1 ? { time: t(b), value: b.st_value } : { time: t(b) })))
    stDn.current?.setData(bars.map((b) => (b.st_value != null && b.st_trend === -1 ? { time: t(b), value: b.st_value } : { time: t(b) })))
    for (const l of lines.current) s.removePriceLine(l)
    lines.current = priceLines.map((p) =>
      s.createPriceLine({ price: p.price, color: p.color, lineWidth: p.width ?? 1, lineStyle: p.style ?? 0, axisLabelVisible: p.label ?? true, title: p.title ?? '' }),
    )
    const marks: SeriesMarker<UTCTimestamp>[] = markers
      .map((m) => ({ time: m.ts as UTCTimestamp, position: m.position, color: m.color, shape: m.shape, text: m.text }))
      .sort((a, b) => (a.time as number) - (b.time as number))
    s.setMarkers(marks)
    chart.current?.timeScale().fitContent()
  }, [bars, priceLines, markers])

  return <div ref={box} />
}
