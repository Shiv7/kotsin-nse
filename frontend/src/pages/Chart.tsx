import { createChart, type IChartApi, type ISeriesApi } from 'lightweight-charts'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Card, ErrorLine, Table } from '../components/Ui'
import { fmt } from '../lib/api'
import { usePoll } from '../lib/usePoll'
import type { BarsResponse, UniverseRow } from '../types'

const TFS = ['5m', '15m', '30m', '1d'] as const

export function Chart() {
  const universe = usePoll<UniverseRow[]>('/api/universe', 30000)
  const [symbol, setSymbol] = useState('')
  const [tf, setTf] = useState<(typeof TFS)[number]>('30m')
  const first = universe.data?.[0]?.symbol
  const active = symbol || first || ''
  const { data, error } = usePoll<BarsResponse>(active ? `/api/bars/${active}?tf=${tf}&n=300` : '/api/health', 5000)

  const box = useRef<HTMLDivElement>(null)
  const chart = useRef<IChartApi | null>(null)
  const candles = useRef<ISeriesApi<'Candlestick'> | null>(null)

  useEffect(() => {
    if (!box.current) return
    const c = createChart(box.current, {
      layout: { background: { color: '#020617' }, textColor: '#94a3b8' },
      grid: { vertLines: { color: '#1e293b' }, horzLines: { color: '#1e293b' } },
      timeScale: { timeVisible: true, borderColor: '#334155' },
      rightPriceScale: { borderColor: '#334155' },
      height: 420,
    })
    candles.current = c.addCandlestickSeries({
      upColor: '#10b981',
      downColor: '#f43f5e',
      wickUpColor: '#10b981',
      wickDownColor: '#f43f5e',
      borderVisible: false,
    })
    chart.current = c
    const onResize = () => c.applyOptions({ width: box.current?.clientWidth ?? 600 })
    onResize()
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      c.remove()
      chart.current = null
      candles.current = null
    }
  }, [])

  useEffect(() => {
    if (!candles.current || !data?.bars) return
    candles.current.setData(
      data.bars.map((b) => ({ time: b.ts as never, open: b.o, high: b.h, low: b.l, close: b.c })),
    )
    // Pivot zones drawn as horizontal lines: these are the levels the stop and targets come from.
    for (const z of data.zones ?? []) {
      candles.current.createPriceLine({
        price: z.price,
        color: z.strength >= 5.2 ? '#f59e0b' : '#475569',
        lineWidth: 1,
        lineStyle: 2,
        axisLabelVisible: true,
        title: z.members.slice(0, 2).join('+'),
      })
    }
  }, [data])

  const symbols = useMemo(() => (universe.data ?? []).map((u) => u.symbol), [universe.data])

  return (
    <div className="space-y-4 p-4">
      <ErrorLine error={error ?? universe.error} />
      <div className="flex flex-wrap items-center gap-2">
        <select
          value={active}
          onChange={(e) => setSymbol(e.target.value)}
          className="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-xs"
        >
          {symbols.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        {TFS.map((t) => (
          <button
            key={t}
            onClick={() => setTf(t)}
            className={'rounded px-2 py-1 text-xs ' + (tf === t ? 'bg-slate-700 text-white' : 'bg-slate-900 text-slate-400')}
          >
            {t}
          </button>
        ))}
        {data?.forming && (
          <span className="text-xs text-slate-500">
            forming {data.forming.ist} IST · {fmt.n(data.forming.c)} · vol {fmt.int(data.forming.v)}
          </span>
        )}
      </div>

      <Card title={`${active} ${tf}`} right={`${data?.bars.length ?? 0} bars`}>
        <div ref={box} />
      </Card>

      <Card title="Confluence zones" right="stop and targets are drawn from these">
        <Table head={['Price', 'Strength', 'Wall?', 'Members']} empty="no zones — needs 25+ daily bars">
          {(data?.zones ?? []).map((z) => (
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
