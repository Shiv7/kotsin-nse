import { createChart, type IChartApi, type ISeriesApi, type UTCTimestamp } from 'lightweight-charts'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Card, ErrorLine } from '../components/Ui'
import { fmt, ist } from '../lib/api'
import { usePoll } from '../lib/usePoll'
import type { BarsResponse, UniverseRow } from '../types'

const TFS = ['1m', '2m', '3m', '5m', '15m', '30m', '1d'] as const
type Tf = (typeof TFS)[number]

/** Candles and volume from the engine's bar store, plus the forming candle from its WebSocket. */
export function Chart() {
  const universe = usePoll<UniverseRow[]>('/api/universe', 30000)
  const [symbol, setSymbol] = useState('')
  const [tf, setTf] = useState<Tf>('5m')
  const active = symbol || universe.data?.[0]?.symbol || ''
  const { data, error } = usePoll<BarsResponse>(active ? `/api/bars/${active}?tf=${tf}&n=500` : null, 5000)
  const [live, setLive] = useState<{ ts: number; c: number; v: number } | null>(null)
  const [wsState, setWsState] = useState<'connecting' | 'open' | 'closed'>('connecting')

  const box = useRef<HTMLDivElement>(null)
  const chart = useRef<IChartApi | null>(null)
  const candles = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const volume = useRef<ISeriesApi<'Histogram'> | null>(null)
  const lastClosedTs = useRef<number>(0)

  useEffect(() => {
    if (!box.current) return
    const c = createChart(box.current, {
      layout: { background: { color: '#020617' }, textColor: '#94a3b8' },
      grid: { vertLines: { color: '#0f172a' }, horzLines: { color: '#0f172a' } },
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: '#334155' },
      rightPriceScale: { borderColor: '#334155' },
      height: 520,
    })
    candles.current = c.addCandlestickSeries({
      upColor: '#10b981', downColor: '#f43f5e', wickUpColor: '#10b981', wickDownColor: '#f43f5e', borderVisible: false,
    })
    volume.current = c.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: 'vol', lastValueVisible: false, priceLineVisible: false })
    c.priceScale('vol').applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } })
    chart.current = c
    const onResize = () => c.applyOptions({ width: box.current?.clientWidth ?? 600 })
    onResize()
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      c.remove()
      chart.current = candles.current = volume.current = null
    }
  }, [])

  useEffect(() => {
    if (!candles.current || !volume.current || !Array.isArray(data?.bars)) return
    const t = (ts: number) => ts as UTCTimestamp
    candles.current.setData(data.bars.map((b) => ({ time: t(b.ts), open: b.o, high: b.h, low: b.l, close: b.c })))
    volume.current.setData(
      data.bars.map((b) => ({ time: t(b.ts), value: b.v, color: b.c >= b.o ? 'rgba(16,185,129,0.4)' : 'rgba(244,63,94,0.4)' })),
    )
    lastClosedTs.current = data.bars.length ? data.bars[data.bars.length - 1].ts : 0
  }, [data])

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
          if (!f || !candles.current || f.ts < lastClosedTs.current) return
          candles.current.update({ time: f.ts as UTCTimestamp, open: f.o, high: f.h, low: f.l, close: f.c })
          volume.current?.update({ time: f.ts as UTCTimestamp, value: f.v, color: f.c >= f.o ? 'rgba(16,185,129,0.4)' : 'rgba(244,63,94,0.4)' })
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

  return (
    <div className="space-y-4 p-4">
      <ErrorLine error={error ?? universe.error} />
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
      </div>

      {!symbols.length && (
        <Card title="No symbols">
          <p className="text-[11px] leading-relaxed text-slate-500">The universe is empty — it is built from the scrip master, which needs a broker session.</p>
        </Card>
      )}

      <Card title={`${active || '—'} ${tf}`} right={`${data?.bars?.length ?? 0} bars`}>
        <div ref={box} />
      </Card>
    </div>
  )
}
