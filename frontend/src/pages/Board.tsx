import { useEffect, useMemo, useState } from 'react'
import { StockPopup } from '../components/StockPopup'
import { Card, ErrorLine, Table } from '../components/Ui'
import { cls, fmt, ist } from '../lib/api'
import { usePoll } from '../lib/usePoll'
import type { BoardData, BoardRow } from '../types'

const FIRST = 9 * 60 + 20 // 09:20
const LAST = 15 * 60 + 30 // 15:30
const LIVE_FROM = 9 * 60 + 16 // the first 1m bar closes at 09:16
const EARLY_UNTIL = 9 * 60 + 20
/** IST minute of the day of an epoch-seconds timestamp (IST is UTC+5:30, no DST). */
const minuteOf = (ts: number) => Math.floor((ts + 19800) / 60) % 1440
/** Minutes until the live board's first minute, from the browser's clock. */
const untilBoard = () => Math.max(0, LIVE_FROM - minuteOf(Date.now() / 1000))

const INDICES = new Set(['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'NIFTYNXT50', 'NIFTYFPI'])

interface UniverseRow {
  symbol: string
  kind: string
  ltp: number | null
  prev_close: number | null
}

/**
 * The day's opening gaps, kept on the board all day. Once the board has run, each row's own
 * ``gap_pct`` (the 09:15 open against yesterday's close). Before that, from the live prices: in the
 * pre-open (09:00–09:08) 5paisa moves only the indices; at ~09:08 the auction matches and each
 * stock's price becomes its opening price, so until 09:15 the price IS the gap. An engine without
 * ``gap_pct`` falls back to the live price, and the card then says it is the move so far.
 */
function OpeningGaps({ rows: board, onPick }: { rows?: BoardRow[]; onPick: (r: BoardRow) => void }) {
  const fromBoard = !!board?.some((r) => r.gap_pct != null)
  const { data } = usePoll<UniverseRow[]>(fromBoard ? null : '/api/universe', 5000)
  const preOpen = minuteOf(Date.now() / 1000) < 9 * 60 + 15
  const rows = fromBoard
    ? (board ?? []).filter((r) => r.gap_pct != null).map((r) => ({ symbol: r.symbol, kind: 'EQUITY', ltp: r.last, prev_close: null, gap: r.gap_pct as number }))
    : (data ?? [])
        .filter((r) => r.ltp && r.prev_close)
        .map((r) => ({ ...r, gap: ((r.ltp as number) / (r.prev_close as number) - 1) * 100 }))
  const what = fromBoard ? 'open vs previous close' : preOpen ? 'auction price vs previous close' : 'price now vs previous close (engine has no open yet)'
  // the universe files the indices as EQUITY rows on the cash feed (their 999920xx codes), so by name
  const idx = rows.filter((r) => INDICES.has(r.symbol))
  const eq = rows.filter((r) => r.kind === 'EQUITY' && !INDICES.has(r.symbol))
  const moved = eq.filter((r) => r.gap !== 0)
  const up = [...moved].filter((r) => r.gap > 0).sort((a, b) => b.gap - a.gap)
  const down = [...moved].filter((r) => r.gap < 0).sort((a, b) => a.gap - b.gap)
  const open = (r: (typeof rows)[number]) =>
    onPick(board?.find((x) => x.symbol === r.symbol) ?? ({ symbol: r.symbol, sector: '', last: r.ltp as number, pct: r.gap, score: 0, side: 'NONE' } as BoardRow))
  const list = (title: string, xs: typeof rows, tone: string) => (
    <Card title={title} right={what}>
      <div className="divide-y divide-slate-800 font-mono text-xs">
        {xs.length === 0 && <div className="px-3 py-2 text-slate-600">none yet — stock prices move when the auction matches, ~09:08</div>}
        {xs.length > 0 && <div className="px-3 py-1 text-[10px] text-slate-600">largest 10 of {xs.length}</div>}
        {xs.slice(0, 10).map((r) => (
          <button key={r.symbol} onClick={() => open(r)} className="flex w-full justify-between px-3 py-1 hover:bg-slate-800/60">
            <span className="text-slate-200">{r.symbol}</span>
            <span className="text-slate-400">{fmt.n(r.ltp)}</span>
            <span className={tone}>{fmt.pct(r.gap)}</span>
          </button>
        ))}
      </div>
    </Card>
  )
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-3 font-mono text-xs">
        {idx.map((r) => (
          <span key={r.symbol} className="rounded border border-slate-800 bg-slate-900/60 px-2 py-1">
            <span className="text-slate-400">{r.symbol} </span>
            <span className={r.gap >= 0 ? 'text-emerald-400' : 'text-rose-400'}>{fmt.pct(r.gap)}</span>
          </span>
        ))}
        <span className="px-2 py-1 text-slate-500">
          {moved.length} of {eq.length} stocks priced · {moved.filter((r) => r.gap > 0).length} up / {moved.filter((r) => r.gap < 0).length} down
        </span>
      </div>
      <div className="grid gap-3 md:grid-cols-2">
        {list('Gap up', up, 'text-emerald-400')}
        {list('Gap down', down, 'text-rose-400')}
      </div>
    </div>
  )
}
const hm = (m: number) => `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`

/**
 * The board: every F&O stock scored by how far it has gone one way today in its own normal day,
 * the 20 strongest each side, three picks a side, and the market around them. Live it refreshes
 * each minute; Replay rebuilds any cached session at any minute with the same code.
 */
export function Board() {
  const [mode, setMode] = useState<'live' | 'replay'>('replay')
  const days = usePoll<string[]>('/api/scan/days', 600000)
  const [day, setDay] = useState('')
  const [minute, setMinute] = useState(LAST)
  const [playing, setPlaying] = useState(false)
  const [open, setOpen] = useState<BoardRow | null>(null)
  const activeDay = day || days.data?.[0] || ''

  const path = mode === 'live' ? '/api/scan' : activeDay ? `/api/scan/replay?day=${activeDay}&at=${hm(minute)}` : null
  const { data, error } = usePoll<BoardData>(path, mode === 'live' ? 5000 : 600000)

  useEffect(() => {
    if (!playing || mode !== 'replay') return
    const id = setInterval(() => setMinute((m) => (m >= LAST ? (setPlaying(false), LAST) : m + 1)), 700)
    return () => clearInterval(id)
  }, [playing, mode])

  const bySymbol = useMemo(() => new Map((data?.rows ?? []).map((r) => [r.symbol, r])), [data])
  const bull = (data?.bullish ?? []).map((s) => bySymbol.get(s)).filter(Boolean) as BoardRow[]
  const bear = (data?.bearish ?? []).map((s) => bySymbol.get(s)).filter(Boolean) as BoardRow[]
  const picks = (data?.picks ?? []).map((s) => bySymbol.get(s)).filter(Boolean) as BoardRow[]

  return (
    <div className="space-y-4 p-4">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        {(['replay', 'live'] as const).map((m) => (
          <button
            key={m}
            onClick={() => setMode(m)}
            className={cls('rounded px-3 py-1 font-semibold uppercase', mode === m ? 'bg-sky-800 text-white' : 'bg-slate-900 text-slate-400')}
          >
            {m}
          </button>
        ))}
        {mode === 'replay' && (
          <>
            <select value={activeDay} onChange={(e) => setDay(e.target.value)} className="rounded border border-slate-700 bg-slate-900 px-2 py-1">
              {(days.data ?? []).map((d) => (
                <option key={d} value={d}>
                  {d}
                </option>
              ))}
            </select>
            <button onClick={() => setMinute((m) => Math.max(FIRST, m - 5))} className="rounded bg-slate-900 px-2 py-1 text-slate-300">
              −5m
            </button>
            <button onClick={() => setMinute((m) => Math.max(FIRST, m - 1))} className="rounded bg-slate-900 px-2 py-1 text-slate-300">
              −1m
            </button>
            <input
              type="range"
              min={FIRST}
              max={LAST}
              value={minute}
              onChange={(e) => setMinute(Number(e.target.value))}
              className="w-56 accent-sky-500"
            />
            <span className="w-12 font-mono text-sm text-slate-100">{hm(minute)}</span>
            <button onClick={() => setMinute((m) => Math.min(LAST, m + 1))} className="rounded bg-slate-900 px-2 py-1 text-slate-300">
              +1m
            </button>
            <button onClick={() => setMinute((m) => Math.min(LAST, m + 5))} className="rounded bg-slate-900 px-2 py-1 text-slate-300">
              +5m
            </button>
            <button
              onClick={() => {
                if (minute >= LAST) setMinute(FIRST)
                setPlaying((p) => !p)
              }}
              className={cls('rounded px-3 py-1', playing ? 'bg-amber-800 text-amber-100' : 'bg-emerald-900 text-emerald-200')}
            >
              {playing ? '❚❚ pause' : '▶ play'}
            </button>
          </>
        )}
        {data && <span className="ml-auto text-slate-500">{mode === 'live' ? `live · ${ist(data.ts, true)}` : `${data.symbols ?? 0} stocks · ${data.without_adr?.length ?? 0} without a clean 50-day range`}</span>}
      </div>

      {mode === 'live' && !data && error?.startsWith('404') ? (
        <div className="rounded border border-slate-800 bg-slate-900/60 px-3 py-2 text-xs text-slate-300">
          Live board appears with the first closed minute, 09:16 IST{untilBoard() > 0 ? ` (in ${untilBoard()} min)` : ''}; until 09:20 it is
          flagged as the opening minutes. Until then: the auction&apos;s opening prices below.
        </div>
      ) : (
        <ErrorLine error={error ?? days.error} />
      )}
      {mode === 'live' && <OpeningGaps rows={data?.rows} onPick={setOpen} />}
      {mode === 'replay' && data?.rows?.some((r) => r.gap_pct != null) && <OpeningGaps rows={data.rows} onPick={setOpen} />}
      {mode === 'live' && data && minuteOf(data.ts) < EARLY_UNTIL && (
        <div className="rounded border border-amber-900 bg-amber-950/40 px-3 py-1.5 text-xs text-amber-300">
          opening minutes (before 09:20): the first candles carry the auction&apos;s order imbalance — moves are real but noisy, ranks will shuffle
        </div>
      )}

      {data && (
        <>
          <div className="grid gap-4 xl:grid-cols-[1fr_320px]">
            <Card title="Market picture" right="x = % from previous close · y = strength (× a normal day) · ring = pick · amber = event risk">
              <Strip rows={data.rows} onPick={setOpen} />
              <Breadth b={data.breadth} />
            </Card>
            <Card title="Picks" right="top 3 a side · ≤ 2 per sector · no event risk">
              <div className="space-y-2">
                {picks.length === 0 && <div className="text-xs text-slate-600">no picks yet</div>}
                {picks.map((r) => (
                  <button
                    key={r.symbol}
                    onClick={() => setOpen(r)}
                    className={cls(
                      'flex w-full items-center gap-2 rounded border px-3 py-2 text-left',
                      r.side === 'BULLISH' ? 'border-emerald-800 bg-emerald-950/40' : 'border-rose-800 bg-rose-950/40',
                    )}
                  >
                    <span className={cls('text-[10px] font-bold', r.side === 'BULLISH' ? 'text-emerald-400' : 'text-rose-400')}>
                      {r.side === 'BULLISH' ? '▲' : '▼'} #{r.rank}
                    </span>
                    <span className="font-semibold text-slate-100">{r.symbol}</span>
                    <span className="text-[10px] text-slate-500">{r.sector}</span>
                    <span className="ml-auto font-mono text-xs text-slate-300">{r.score >= 0 ? '+' : ''}{r.score.toFixed(2)}×</span>
                    <span className={cls('font-mono text-xs', (r.pct ?? 0) >= 0 ? 'text-emerald-400' : 'text-rose-400')}>{fmt.pct(r.pct)}</span>
                  </button>
                ))}
              </div>
            </Card>
          </div>

          <div className="grid gap-4 xl:grid-cols-2">
            <Side title="Bullish" rows={bull} tone="green" onOpen={setOpen} />
            <Side title="Bearish" rows={bear} tone="red" onOpen={setOpen} />
          </div>

          <Card title="Sectors" right="ranked by average strength · one cell per stock">
            <div className="space-y-1.5">
              {data.sectors.map((s) => (
                <div key={s.sector} className="flex items-center gap-3 text-xs">
                  <span className="w-56 truncate text-slate-300">{s.sector}</span>
                  <span className={cls('w-16 text-right font-mono', (s.avg_pct ?? 0) >= 0 ? 'text-emerald-400' : 'text-rose-400')}>{fmt.pct(s.avg_pct)}</span>
                  <span className="w-14 text-right text-slate-500">
                    {s.up}↑ {s.down}↓
                  </span>
                  <div className="flex flex-wrap gap-0.5">
                    {s.cells.map((c) => (
                      <span
                        key={c.symbol}
                        title={`${c.symbol} ${fmt.pct(c.pct)}`}
                        onClick={() => {
                          const r = bySymbol.get(c.symbol)
                          if (r) setOpen(r)
                        }}
                        className="h-3 w-3 cursor-pointer rounded-sm"
                        style={{ background: cellColor(c.pct) }}
                      />
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </Card>
        </>
      )}

      {open && <StockPopup row={bySymbol.get(open.symbol) ?? open} day={mode === 'replay' ? activeDay : undefined} at={mode === 'replay' ? hm(minute) : undefined} onClose={() => setOpen(null)} />}
    </div>
  )
}

function cellColor(p: number | null) {
  if (p == null) return '#1e293b'
  const a = Math.min(1, Math.abs(p) / 3)
  return p >= 0 ? `rgba(16,185,129,${0.2 + 0.8 * a})` : `rgba(244,63,94,${0.2 + 0.8 * a})`
}

function Breadth({ b }: { b: BoardData['breadth'] }) {
  const total = Math.max(1, b.bullish + b.bearish + b.neutral)
  return (
    <div className="mt-3 space-y-1 text-xs">
      <div className="flex h-2 overflow-hidden rounded">
        <div className="bg-emerald-500" style={{ width: `${(b.bullish / total) * 100}%` }} />
        <div className="bg-slate-700" style={{ width: `${(b.neutral / total) * 100}%` }} />
        <div className="bg-rose-500" style={{ width: `${(b.bearish / total) * 100}%` }} />
      </div>
      <div className="flex gap-4 text-slate-400">
        <span className="text-emerald-400">▲ {b.bullish} one-sided up</span>
        <span>{b.neutral} neither</span>
        <span className="text-rose-400">▼ {b.bearish} one-sided down</span>
        <span className="ml-auto">
          {b.up} up · {b.down} down from yesterday
        </span>
      </div>
    </div>
  )
}

function Strip({ rows, onPick }: { rows: BoardRow[]; onPick: (r: BoardRow) => void }) {
  const W = 900
  const H = 260
  const pad = 28
  const maxS = Math.max(1, ...rows.map((r) => Math.abs(r.score)))
  const x = (p: number) => pad + ((Math.max(-4, Math.min(4, p)) + 4) / 8) * (W - 2 * pad)
  const y = (s: number) => H / 2 - (s / maxS) * (H / 2 - 16)
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full">
      <line x1={pad} x2={W - pad} y1={H / 2} y2={H / 2} stroke="#334155" />
      <line x1={x(0)} x2={x(0)} y1={8} y2={H - 8} stroke="#1e293b" />
      {[-4, -2, 0, 2, 4].map((p) => (
        <text key={p} x={x(p)} y={H - 2} fontSize="10" textAnchor="middle" fill="#475569">
          {p > 0 ? `+${p}` : p}%
        </text>
      ))}
      {rows.map((r) => {
        const tone = r.event_risk ? '#f59e0b' : r.side === 'BULLISH' ? '#10b981' : r.side === 'BEARISH' ? '#f43f5e' : '#475569'
        const contender = r.rank != null && r.rank <= 20
        return (
          <g key={r.symbol} onClick={() => onPick(r)} className="cursor-pointer">
            <title>{`${r.symbol} ${fmt.pct(r.pct)} · ${r.score.toFixed(2)}×`}</title>
            <circle cx={x(r.pct ?? 0)} cy={y(r.score)} r={contender ? 4 : 2.5} fill={tone} opacity={contender ? 0.95 : 0.45} />
            {r.pick && <circle cx={x(r.pct ?? 0)} cy={y(r.score)} r={8} fill="none" stroke={tone} strokeWidth={1.5} />}
            {r.pick && (
              <text x={x(r.pct ?? 0) + 10} y={y(r.score) + 3} fontSize="10" fill="#e2e8f0">
                {r.symbol}
              </text>
            )}
          </g>
        )
      })}
    </svg>
  )
}

function Side({ title, rows, tone, onOpen }: { title: string; rows: BoardRow[]; tone: 'green' | 'red'; onOpen: (r: BoardRow) => void }) {
  return (
    <Card title={<span className={tone === 'green' ? 'text-emerald-400' : 'text-rose-400'}>{title} — top {rows.length}</span>} right="click a row for its chart">
      <Table head={['#', 'Stock', 'Strength', '%', 'Align', 'Zone (Z · from price)', 'R:R', 'VWAP', 'OI', 'Rule']} empty="none yet">
        {rows.map((r) => (
          <tr key={r.symbol} onClick={() => onOpen(r)} className={cls('cursor-pointer border-b border-slate-900 hover:bg-slate-900', r.pick && 'bg-slate-900/60')}>
            <td className="px-2 py-1 text-slate-500">{r.rank}</td>
            <td className="px-2 py-1 font-semibold text-slate-100">
              {r.symbol}
              {r.event_risk && <span className="ml-1 text-[9px] text-amber-400">event</span>}
            </td>
            <td className="px-2 py-1 font-mono">
              <span className={tone === 'green' ? 'text-emerald-300' : 'text-rose-300'}>{Math.abs(r.score).toFixed(2)}×</span>
              <span className="ml-1 text-[10px] text-slate-600">p{r.percentile}</span>
            </td>
            <td className={cls('px-2 py-1 font-mono', (r.pct ?? 0) >= 0 ? 'text-emerald-400' : 'text-rose-400')}>{fmt.pct(r.pct)}</td>
            <td className={cls('px-2 py-1 font-mono', (r.align ?? 0) >= 4 ? 'text-emerald-300' : (r.align ?? 0) === 3 ? 'text-slate-200' : 'text-slate-600')}>
              {r.align != null ? `${r.align}/4` : '—'}
            </td>
            <td className="px-2 py-1 font-mono text-[11px]">
              {r.setup ? (
                <span>
                  {fmt.n(r.setup.z)} <span className="text-slate-500">· {fmt.pct(r.setup.distance_pct)}</span>{' '}
                  <span className="text-[9px] text-slate-500">{r.setup.state.toLowerCase()}</span>
                </span>
              ) : (
                <span className="text-slate-700">no zone</span>
              )}
            </td>
            <td className={cls('px-2 py-1 font-mono', r.setup?.rr != null && r.setup.rr >= 1.5 ? 'text-emerald-300' : 'text-slate-500')}>
              {r.setup?.rr != null ? `1:${r.setup.rr}` : '—'}
            </td>
            <td className="px-2 py-1">{r.above_vwap == null ? '—' : r.above_vwap ? '▲' : '▼'}</td>
            <td className="px-2 py-1 text-[10px] text-slate-400">{r.oi_build_up ?? '—'}</td>
            <td className="px-2 py-1 text-[10px] text-slate-400">{r.rule}</td>
          </tr>
        ))}
      </Table>
    </Card>
  )
}
