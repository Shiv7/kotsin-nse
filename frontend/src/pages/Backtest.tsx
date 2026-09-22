import { useCallback, useEffect, useMemo, useState } from 'react'
import { CandleChart, type Marker, type PriceLine } from '../components/CandleChart'
import { Gates } from '../components/Gates'
import { Badge, Card, ErrorLine, Stat, Table } from '../components/Ui'
import { cls, fmt, getJson, ist, pnlColor, postJson } from '../lib/api'
import { usePoll } from '../lib/usePoll'
import type { BtDetail, BtSummary, BtTradeRow, TradeView } from '../types'

type Row = BtTradeRow & { idx: number }
type SortKey = 'day' | 'r' | 'net' | 'mfe' | 'mae' | 'held'
type Outcome = 'all' | 'losers' | 'winners' | 'bar1'

function KV({ rows }: { rows: [string, unknown][] }) {
  return (
    <table className="w-full text-[11px]">
      <tbody>
        {rows.map(([k, v]) => (
          <tr key={k} className="border-b border-slate-900/60">
            <td className="py-0.5 pr-3 font-mono text-slate-500">{k}</td>
            <td className="py-0.5 font-mono text-slate-200">{v == null ? 'DM' : typeof v === 'number' ? fmt.n(v, Number.isInteger(v) ? 0 : 3) : String(v)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function Book({ k }: { k: string }) {
  return <Badge tone={k === 'FUKAA' ? 'violet' : 'blue'}>{k}</Badge>
}

const VERDICT_TONE = { TAKEN: 'green', WATCHING: 'amber', REJECTED: 'red' } as const

/**
 * One trade, explained. Every number here was stored by the backtester at the decision (the
 * Signal, FUKAA's verdict) or is computed by the same functions the strategy and the committee
 * use (indicator lines, path). Nothing is re-derived for display.
 */
function TradeDebugger({ runId, index, position, total, onNav }: { runId: string; index: number; position: number; total: number; onNav: (d: number) => void }) {
  const [view, setView] = useState<TradeView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [msg, setMsg] = useState('')
  const [allZones, setAllZones] = useState(false)

  useEffect(() => {
    let alive = true
    setError(null)
    getJson<TradeView>(`/api/backtests/${runId}/trades/${index}`)
      .then((v) => alive && setView(v))
      .catch((e) => alive && setError(e instanceof Error ? e.message : String(e)))
    return () => {
      alive = false
    }
  }, [runId, index])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement | null)?.tagName === 'INPUT') return
      if (e.key === 'ArrowDown' || e.key === 'j') onNav(1)
      if (e.key === 'ArrowUp' || e.key === 'k') onNav(-1)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onNav])

  const { priceLines, markers } = useMemo(() => {
    if (!view) return { priceLines: [] as PriceLine[], markers: [] as Marker[] }
    const L = view.levels
    const t = view.trade
    const bullish = t.direction === 'BULLISH'
    const isLevel = (p: number) => Math.abs(p - L.stop) < 1e-6 || L.targets.some((x) => Math.abs(x - p) < 1e-6)
    const lines: PriceLine[] = [
      { price: L.entry, color: '#e2e8f0', title: 'entry' },
      { price: L.stop, color: '#f43f5e', title: 'stop', width: 2 },
      ...(L.stop_at_exit != null && Math.abs(L.stop_at_exit - L.stop) > 1e-6 ? [{ price: L.stop_at_exit, color: '#fb7185', title: 'stop@exit', style: 2 as const }] : []),
      ...L.targets.map((p, i) => ({ price: p, color: '#10b981', title: `T${i + 1}`, style: (i === 0 ? 0 : 2) as 0 | 2 })),
      { price: L.exit, color: '#f59e0b', title: `exit · ${t.exit_reason}`, width: 2 },
      ...view.zones
        .filter((z) => !isLevel(z.price) && (allZones || z.wall))
        .map((z) => ({ price: z.price, color: z.wall ? 'rgba(245,158,11,0.45)' : 'rgba(71,85,105,0.55)', title: '', style: 3 as const, label: false })),
    ]
    const marks: Marker[] = [
      { ts: L.decision_ts, position: 'inBar', color: '#38bdf8', shape: 'circle', text: 'decision' },
      { ts: L.entry_ts, position: bullish ? 'belowBar' : 'aboveBar', color: bullish ? '#10b981' : '#f43f5e', shape: bullish ? 'arrowUp' : 'arrowDown', text: 'fill' },
      { ts: L.exit_ts, position: bullish ? 'aboveBar' : 'belowBar', color: '#f59e0b', shape: 'square', text: `${t.exit_reason} ${fmt.r(t.r_multiple)}` },
    ]
    return { priceLines: lines, markers: marks }
  }, [view, allZones])

  const ask = async () => {
    setMsg('reviewing… (5 Claude calls)')
    try {
      const r = await postJson<{ failure_mode?: string; confidence?: number; lesson?: string; error?: string | null }>('/api/committee/review/trade', { run_id: runId, index })
      setMsg(r.error ? `error: ${r.error}` : `${r.failure_mode} (${((r.confidence ?? 0) * 100).toFixed(0)}%) — ${r.lesson}`)
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e))
    }
  }

  if (error) return <ErrorLine error={error} />
  if (!view) return <Card title="Trade">{'loading…'}</Card>
  const { trade: t, levels: L, path: P, signal: s, fukaa: f } = view
  const c = s?.context ?? {}
  const indi = c.indicators
  const bullish = t.direction === 'BULLISH'
  const band = indi ? (bullish ? indi.bb_upper : indi.bb_lower) : null
  const stopZoneStrength = view.zones.find((z) => z.members.join(',') === L.stop_zone)?.strength
  const targetZones = new Set(L.target_zones ?? [])
  const pathR = (k: string) => (P[k] == null ? 'DM' : fmt.r(P[k] as number))
  const survives = P['path.max_adverse_before_t1_r'] as number | null
  const missingOi = f?.gates?.some((g) => g.name === 'ref_oi' && g.missing)

  return (
    <div className="space-y-3">
      <Card
        title={
          <span className="flex flex-wrap items-center gap-2">
            <span className="text-base font-semibold">{t.symbol}</span>
            <Book k={t.strategy} />
            <span className={bullish ? 'text-emerald-400' : 'text-rose-400'}>{bullish ? 'LONG' : 'SHORT'}</span>
            <Badge tone="slate">grade {t.grade || '?'}</Badge>
            <span className="text-slate-500">{ist(L.decision_ts)} IST</span>
            <span className="text-slate-400">
              {fmt.n(t.entry)} → {fmt.n(t.exit)} · {t.exit_reason} · {t.bars_held} bars
            </span>
            <span className={cls('font-semibold', pnlColor(t.net))}>
              {fmt.signedInr(t.net)} ({fmt.r(t.r_multiple)})
            </span>
          </span>
        }
        right={
          <span className="flex items-center gap-2 text-[11px]">
            <span className="text-slate-500">
              {position + 1} / {total} · ↑↓ or j/k
            </span>
            <button className="rounded bg-slate-800 px-2 py-0.5 hover:bg-slate-700" onClick={() => onNav(-1)}>
              ← prev
            </button>
            <button className="rounded bg-slate-800 px-2 py-0.5 hover:bg-slate-700" onClick={() => onNav(1)}>
              next →
            </button>
            <button className="rounded bg-violet-900/60 px-2 py-0.5 text-violet-200 hover:bg-violet-800" onClick={() => void ask()}>
              ask the committee why
            </button>
            <label className="ml-1 flex items-center gap-1 text-slate-500">
              <input type="checkbox" checked={allZones} onChange={(e) => setAllZones(e.target.checked)} /> all zones
            </label>
          </span>
        }
      >
        <CandleChart bars={view.bars} priceLines={priceLines} markers={markers} height={380} />
        <div className="mt-1 flex flex-wrap gap-3 text-[10px] text-slate-500">
          <span>● decision bar</span>
          <span className="text-emerald-400">▲/▼ fill (next open + slippage)</span>
          <span className="text-amber-400">■ exit</span>
          <span className="text-rose-400">— stop</span>
          <span className="text-emerald-400">— targets</span>
          <span className="text-sky-400">BB({view.indicator_params.bb_period},{view.indicator_params.bb_mult})</span>
          <span className="text-emerald-500">ST({view.indicator_params.st_atr_period},{view.indicator_params.st_mult})</span>
          <span className="text-amber-500/70">┄ walls</span>
          {msg && <span className="ml-auto text-slate-300">{msg}</span>}
        </div>
      </Card>

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        <Card title="1 · Why it fired" right={s?.score != null ? `score ${s.score}` : ''}>
          {s ? (
            <>
              <div className="mb-2 text-[11px] text-slate-300">{s.reason}</div>
              <Gates gates={s.gates ?? []} />
              {indi && (
                <div className="mt-2">
                  <KV
                    rows={[
                      ['close', s.entry],
                      [bullish ? 'vs upper band' : 'vs lower band', band ? `${fmt.pct((s.entry / band - 1) * 100)}` : 'DM'],
                      ['bb width %', indi.bb_middle ? `${(((indi.bb_upper - indi.bb_lower) / indi.bb_middle) * 100).toFixed(2)}%` : 'DM'],
                      ['supertrend', `${fmt.n(indi.st_value)} (${indi.st_trend > 0 ? 'UP' : 'DOWN'}, ${indi.bars_in_trend} bars)`],
                      ['bars since flip', s.evidence?.bars_since_flip],
                      ['ATR(14)', indi.atr],
                      ['ATR % of price', `${((indi.atr / s.entry) * 100).toFixed(2)}%`],
                      ['volume', s.evidence?.volume],
                    ]}
                  />
                </div>
              )}
            </>
          ) : (
            <div className="text-[11px] text-slate-500">this run predates decision capture — re-run the backtest</div>
          )}
        </Card>

        <Card title="2 · Levels — why this stop, why these targets" right={L.rr != null ? `rr ${fmt.n(L.rr, 2)} · grade ${L.grade}` : ''}>
          <div className="rounded border border-rose-900/50 bg-rose-950/30 p-2 text-[11px]">
            <div className="font-semibold text-rose-300">STOP {fmt.n(L.stop)}</div>
            <div className="text-slate-400">
              {L.stop_dist_pct != null ? `${L.stop_dist_pct}% from entry` : 'DM'}
              {L.stop_dist_atr != null ? ` · ${L.stop_dist_atr} ATR` : ''}
              {L.stop_at_exit != null && Math.abs(L.stop_at_exit - L.stop) > 1e-6 ? ` · moved to ${fmt.n(L.stop_at_exit)} by exit` : ''}
            </div>
            <div className="text-slate-500">{L.stop_zone ? `zone behind: ${L.stop_zone}${stopZoneStrength != null ? ` (strength ${stopZoneStrength})` : ''}` : 'no zone behind — 1 ATR fallback'}</div>
          </div>
          {L.targets.map((p, i) => (
            <div key={i} className="mt-1 rounded border border-emerald-900/50 bg-emerald-950/30 p-2 text-[11px]">
              <div className="font-semibold text-emerald-300">
                T{i + 1} {fmt.n(p)} <span className="font-normal text-slate-500">({((Math.abs(p - L.entry) / L.entry) * 100).toFixed(2)}%{i === 0 && L.t1_dist_atr != null ? ` · ${L.t1_dist_atr} ATR` : ''})</span>
                {i < (t.targets_hit ?? 0) && <span className="ml-2 text-emerald-400">✓ taken</span>}
              </div>
              <div className="text-slate-500">wall ahead: {L.target_zones?.[i] ?? '—'}</div>
            </div>
          ))}
          <div className="mt-2">
            <KV rows={[['fortress (T1 wall)', L.fortress], ['room to next wall (ATR)', L.room_ratio], ['note', L.note || '—']]} />
          </div>
        </Card>

        <Card title="3 · What happened next" right={`${P['path.window_bars'] ?? 0} bars after the decision`}>
          <KV
            rows={[
              ['first touch', P['path.first_touch'] ?? 'DM'],
              ['bars to stop', P['path.bars_to_stop'] ?? '—'],
              ['bars to T1', P['path.bars_to_t1'] ?? '—'],
              ['MFE (window)', `${pathR('path.mfe_r')} @ bar ${P['path.mfe_bar'] ?? '—'}`],
              ['MAE (window)', pathR('path.mae_r')],
              ['max adverse before T1', survives == null ? 'T1 never touched' : `${fmt.r(survives)} → a stop wider than ${fmt.n(Math.abs(survives) * L.risk)} (${L.atr ? (Math.abs(survives * L.risk) / L.atr).toFixed(2) + ' ATR' : ''}) reaches T1`],
              ['close after 1 / 4 / 8 / 16 bars', ['1b', '4b', '8b', '16b'].map((k) => pathR(`path.close_r_${k}`)).join(' / ')],
              ['direction right at window end', P['path.direction_right_at_end'] == null ? 'DM' : P['path.direction_right_at_end'] ? 'yes' : 'no'],
            ]}
          />
        </Card>

        <Card title="4 · Outcome" right={`${t.exit_reason} after ${t.bars_held} bars`}>
          <KV
            rows={[
              ['fill → exit', `${fmt.n(t.entry)} → ${fmt.n(t.exit)}`],
              ['qty', t.qty],
              ['gross', fmt.signedInr(t.gross)],
              ['charges', `${fmt.inr(t.charges)}${t.gross ? ` (${((t.charges / Math.abs(t.gross)) * 100).toFixed(0)}% of |gross|)` : ''}`],
              ['net', fmt.signedInr(t.net)],
              ['R', fmt.r(t.r_multiple)],
              ['MFE / MAE (trade)', `${fmt.r(t.mfe_r)} / ${fmt.r(t.mae_r)}`],
              ['targets taken', `${t.targets_hit ?? 0} / ${L.targets.length}`],
              ['option leg (modelled)', t.opt_r_modelled == null ? 'DM' : `${fmt.r(t.opt_r_modelled)} · ${fmt.signedInr(t.opt_net_modelled)}`],
            ]}
          />
        </Card>

        <Card title="5 · FUKAA on this trigger" right={f ? <Badge tone={VERDICT_TONE[f.verdict]}>{f.verdict}</Badge> : <Badge tone="slate">not evaluated</Badge>}>
          {f ? (
            <>
              <div className="mb-2 text-[11px] text-slate-300">{f.verdict === 'TAKEN' ? f.reason : f.note || (f.binding_gate ? `rejected at ${f.binding_gate}` : '')}</div>
              <Gates gates={f.gates ?? []} />
              {missingOi && <div className="mt-1 text-[11px] text-amber-400">ref_oi is missing: REST history carries no open interest, so FUKAA cannot pass this gate in any backtest (live it reads the front future's OI).</div>}
              <div className="mt-2">
                <KV
                  rows={[
                    ['volume surge T / T-1', `${fmt.n(f.evidence.surge_t, 2)} / ${fmt.n(f.evidence.surge_t1, 2)}`],
                    ['needed (multiplier)', f.evidence.multiplier],
                    ['baseline volume', f.evidence.baseline_volume],
                    ['composite', f.evidence.composite],
                    ['scores vol / oi / mom / rr', [f.evidence.volume_score, f.evidence.oi_score, f.evidence.momentum_score, f.evidence.rr_score].map((x) => (x == null ? 'DM' : fmt.n(x, 0))).join(' / ')],
                  ]}
                />
              </div>
            </>
          ) : (
            <div className="text-[11px] text-slate-500">FUKAA did not evaluate this bar (no base signal reached it, or the run predates decision capture).</div>
          )}
        </Card>

        <Card title="6 · Every zone the engine saw" right={`${view.zones.length} zones · amber = wall`}>
          <div className="max-h-64 overflow-y-auto">
            <Table head={['Price', 'vs entry', 'Str.', 'Wall', 'Role', 'Members']} empty="no zones recorded">
              {view.zones.map((z) => {
                const key = z.members.join(',')
                const role = key === L.stop_zone ? 'STOP' : targetZones.has(key) ? `T${(L.target_zones ?? []).indexOf(key) + 1}` : ''
                return (
                  <tr key={z.price} className={cls('border-b border-slate-900', role === 'STOP' && 'bg-rose-950/30', role.startsWith('T') && 'bg-emerald-950/20')}>
                    <td className="px-2 py-1">{fmt.n(z.price)}</td>
                    <td className={cls('px-2 py-1', z.price > L.entry ? 'text-emerald-500/80' : 'text-rose-500/80')}>{fmt.pct(((z.price - L.entry) / L.entry) * 100)}</td>
                    <td className="px-2 py-1">{fmt.n(z.strength, 1)}</td>
                    <td className="px-2 py-1">{z.wall ? <span className="text-amber-400">wall</span> : '—'}</td>
                    <td className="px-2 py-1 font-semibold">{role}</td>
                    <td className="max-w-[9rem] truncate px-2 py-1 text-[10px] text-slate-500" title={z.members.join(', ')}>{z.members.join(', ')}</td>
                  </tr>
                )
              })}
            </Table>
          </div>
        </Card>
      </div>
    </div>
  )
}

/**
 * Backtests are run from the CLI (`kotsin-nse backtest`) and read here. Pick a run, then any
 * trade: the debugger shows why it fired, why the stop sat where it sat, what the price did next,
 * and what FUKAA said about the same trigger.
 */
export function Backtest() {
  const runs = usePoll<BtSummary[]>('/api/backtests', 15000)
  const [runId, setRunId] = useState<string | null>(null)
  const [detail, setDetail] = useState<BtDetail | null>(null)
  const [busy, setBusy] = useState('')
  const [sel, setSel] = useState<number | null>(null)
  const [book, setBook] = useState('')
  const [dir, setDir] = useState('')
  const [grade, setGrade] = useState('')
  const [exit, setExit] = useState('')
  const [sym, setSym] = useState('')
  const [outcome, setOutcome] = useState<Outcome>('all')
  const [sort, setSort] = useState<SortKey>('day')

  const open = async (id: string) => {
    setBusy(id)
    try {
      const d = await getJson<BtDetail>(`/api/backtests/${id}`)
      setDetail(d)
      setRunId(id)
      setSel(null)
    } finally {
      setBusy('')
    }
  }

  const rows = useMemo<Row[]>(() => {
    const all = (detail?.trades ?? []).map((t, i) => ({ ...t, idx: i }))
    const q = sym.trim().toUpperCase()
    const filtered = all.filter(
      (t) =>
        (!book || t.strategy === book) &&
        (!dir || t.direction === dir) &&
        (!grade || t.grade === grade) &&
        (!exit || t.exit_reason === exit) &&
        (!q || t.symbol.includes(q)) &&
        (outcome === 'all' || (outcome === 'losers' && t.net < 0) || (outcome === 'winners' && t.net > 0) || (outcome === 'bar1' && t.bars_held <= 1 && t.exit_reason.startsWith('SL'))),
    )
    const key: Record<SortKey, (t: Row) => number> = { day: (t) => t.entry_ts, r: (t) => t.r_multiple, net: (t) => t.net, mfe: (t) => -t.mfe_r, mae: (t) => t.mae_r, held: (t) => -t.bars_held }
    return filtered.sort((a, b) => key[sort](a) - key[sort](b))
  }, [detail, book, dir, grade, exit, sym, outcome, sort])

  useEffect(() => {
    if (sel == null || !rows.some((r) => r.idx === sel)) setSel(rows[0]?.idx ?? null)
  }, [rows, sel])

  const position = rows.findIndex((r) => r.idx === sel)
  const nav = useCallback(
    (d: number) => {
      if (!rows.length) return
      const p = rows.findIndex((r) => r.idx === sel)
      const next = Math.min(rows.length - 1, Math.max(0, (p < 0 ? 0 : p) + d))
      setSel(rows[next].idx)
    },
    [rows, sel],
  )

  const s = detail?.summary
  const fk = s?.fukaa_on_triggers
  const grades = useMemo(() => Array.from(new Set((detail?.trades ?? []).map((t) => t.grade).filter(Boolean))).sort(), [detail])
  const exits = useMemo(() => Array.from(new Set((detail?.trades ?? []).map((t) => t.exit_reason))).sort(), [detail])
  const sumNet = rows.reduce((a, t) => a + t.net, 0)
  const avgR = rows.length ? rows.reduce((a, t) => a + t.r_multiple, 0) / rows.length : 0

  return (
    <div className="space-y-4 p-4">
      <ErrorLine error={runs.error} />
      {!runs.data?.length && (
        <Card title="No backtests yet">
          <pre className="whitespace-pre-wrap text-[11px] leading-relaxed text-slate-400">{`cd backend\nuv run kotsin-nse backtest --symbols RELIANCE        # one symbol, a few seconds\nuv run kotsin-nse backtest                            # every cached symbol`}</pre>
        </Card>
      )}

      {!!runs.data?.length && (
        <Card title="Runs" right="newest first · a run stores every trade's decision, so any trade can be opened">
          <div className="max-h-56 overflow-y-auto">
            <Table head={['Run', 'When (IST)', 'Symbols', 'Trades', 'Net', 'Avg R', 't', 'Win %', 'PF', 'Max DD', 'Frame', '']}>
              {runs.data.map((r) => (
                <tr key={r.id} className={cls('border-b border-slate-900', runId === r.id && 'bg-slate-900')}>
                  <td className="px-2 py-1.5 font-mono text-[11px]">{r.id}</td>
                  <td className="px-2 py-1.5 text-slate-500">{ist(r.created_ts)}</td>
                  <td className="px-2 py-1.5">{r.symbols}</td>
                  <td className="px-2 py-1.5">{r.trades}</td>
                  <td className={`px-2 py-1.5 ${pnlColor(r.net)}`}>{fmt.signedInr(r.net)}</td>
                  <td className={`px-2 py-1.5 ${pnlColor(r.avg_r)}`}>{fmt.r(r.avg_r)}</td>
                  <td className="px-2 py-1.5 text-slate-400">{r.avg_r_t?.toFixed(2) ?? 'DM'}</td>
                  <td className="px-2 py-1.5">{r.win_rate ?? 'DM'}</td>
                  <td className="px-2 py-1.5">{r.profit_factor?.toFixed(2) ?? 'DM'}</td>
                  <td className="px-2 py-1.5 text-rose-400/80">{fmt.inr(r.max_drawdown)}</td>
                  <td className="px-2 py-1.5 text-slate-500">{String((r.params as { decision_tf?: string })?.decision_tf ?? '30m')}{(r.params as { holding?: string })?.holding === 'delivery' ? ' · delivery' : ''}</td>
                  <td className="px-2 py-1.5">
                    <button onClick={() => void open(r.id)} className="rounded bg-slate-800 px-2 py-0.5 text-[10px] hover:bg-slate-700">
                      {busy === r.id ? '…' : runId === r.id ? 'opened' : 'open'}
                    </button>
                  </td>
                </tr>
              ))}
            </Table>
          </div>
        </Card>
      )}

      {s && detail && (
        <>
          {s.sample_too_small && (
            <div className="rounded border border-amber-900/60 bg-amber-950/30 px-3 py-2 text-xs text-amber-300">
              {s.trades} trades over {s.n_days} days is too small to conclude anything. The bar is ≥300 out-of-sample trades and a within-day permutation test — docs/LEARNINGS.md R13.
            </div>
          )}
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-8">
            <Stat label="Trades" value={s.trades} sub={`${s.signals} signals · ${fmt.int(s.rejections)} rejected`} />
            <Stat label="Net" value={fmt.signedInr(s.net)} tone={pnlColor(s.net)} sub={`gross ${fmt.signedInr(s.gross)}`} />
            <Stat label="Charges" value={fmt.inr(s.charges)} tone="text-amber-400" sub={s.charges_share_of_gross != null ? `${s.charges_share_of_gross}% of |gross|` : undefined} />
            <Stat label="Avg R" value={fmt.r(s.avg_r)} sub={s.avg_r_stderr != null ? `± ${s.avg_r_stderr} · t ${s.avg_r_t ?? 'DM'}` : undefined} tone={pnlColor(s.avg_r)} />
            <Stat label="Win rate" value={s.win_rate != null ? `${s.win_rate}%` : 'DM'} sub={`PF ${s.profit_factor?.toFixed(2) ?? 'DM'}`} />
            <Stat label="Max DD" value={fmt.inr(s.max_drawdown)} tone="text-rose-400" sub={`${s.n_days} days`} />
            <Stat label="Option leg" value={s.modelled_option_net != null ? fmt.signedInr(s.modelled_option_net) : 'DM'} sub="MODELLED — not measured" tone="text-slate-400" />
            <Stat
              label="FUKAA on triggers"
              value={fk ? `${fk.taken} taken` : 'DM'}
              sub={fk ? `${fk.watching} parked · ${fk.rejected} rejected${Object.keys(fk.by_gate).length ? ` (${Object.entries(fk.by_gate).sort((a, b) => b[1] - a[1]).slice(0, 2).map(([g, n]) => `${g} ${n}`).join(', ')})` : ''}` : 'run predates capture'}
              tone={fk && fk.taken === 0 ? 'text-amber-400' : undefined}
            />
          </div>

          <div className="grid gap-3 lg:grid-cols-3">
            <Card title="By book">
              <Table head={['Book', 'Trades', 'Net', 'Avg R', 'Win %']}>
                {Object.entries(s.by_strategy).map(([k, v]) => (
                  <tr key={k} className="border-b border-slate-900">
                    <td className="px-2 py-1"><Book k={k} /></td>
                    <td className="px-2 py-1">{v.trades}</td>
                    <td className={`px-2 py-1 ${pnlColor(v.net)}`}>{fmt.signedInr(v.net)}</td>
                    <td className={`px-2 py-1 ${pnlColor(v.avg_r)}`}>{fmt.r(v.avg_r)}</td>
                    <td className="px-2 py-1">{v.win_rate}%</td>
                  </tr>
                ))}
              </Table>
            </Card>
            <Card title="By exit reason">
              <Table head={['Reason', 'n', 'Net']}>
                {Object.entries(s.by_exit_reason).map(([k, v]) => (
                  <tr key={k} className="border-b border-slate-900">
                    <td className="px-2 py-1">{k}</td>
                    <td className="px-2 py-1">{v.n}</td>
                    <td className={`px-2 py-1 ${pnlColor(v.net)}`}>{fmt.signedInr(v.net)}</td>
                  </tr>
                ))}
              </Table>
            </Card>
            <Card title="Binding gates" right="why candidates did not become trades">
              <div className="max-h-40 overflow-y-auto">
                <Table head={['Gate', 'Count']}>
                  {Object.entries(s.binding_gates).slice(0, 12).map(([k, v]) => (
                    <tr key={k} className="border-b border-slate-900">
                      <td className="px-2 py-1 font-mono text-[11px]">{k}</td>
                      <td className="px-2 py-1">{fmt.int(v)}</td>
                    </tr>
                  ))}
                </Table>
              </div>
            </Card>
          </div>

          <Card
            title="Trades"
            right={
              <div className="flex flex-wrap items-center gap-1 text-[11px]">
                <select value={book} onChange={(e) => setBook(e.target.value)} className="rounded bg-slate-900 px-1 py-0.5"><option value="">both books</option><option>FUDKII</option><option>FUKAA</option></select>
                <select value={dir} onChange={(e) => setDir(e.target.value)} className="rounded bg-slate-900 px-1 py-0.5"><option value="">long+short</option><option value="BULLISH">long</option><option value="BEARISH">short</option></select>
                <select value={grade} onChange={(e) => setGrade(e.target.value)} className="rounded bg-slate-900 px-1 py-0.5"><option value="">any grade</option>{grades.map((g) => <option key={g}>{g}</option>)}</select>
                <select value={exit} onChange={(e) => setExit(e.target.value)} className="rounded bg-slate-900 px-1 py-0.5"><option value="">any exit</option>{exits.map((g) => <option key={g}>{g}</option>)}</select>
                <select value={outcome} onChange={(e) => setOutcome(e.target.value as Outcome)} className="rounded bg-slate-900 px-1 py-0.5"><option value="all">all outcomes</option><option value="losers">losers</option><option value="winners">winners</option><option value="bar1">stopped on bar 1</option></select>
                <input value={sym} onChange={(e) => setSym(e.target.value)} placeholder="symbol" className="w-24 rounded bg-slate-900 px-1 py-0.5" />
                <select value={sort} onChange={(e) => setSort(e.target.value as SortKey)} className="rounded bg-slate-900 px-1 py-0.5"><option value="day">by time</option><option value="r">worst R first</option><option value="net">worst net first</option><option value="mfe">largest MFE first</option><option value="mae">worst MAE first</option><option value="held">longest held first</option></select>
                <span className="text-slate-500">
                  {rows.length} shown · net {fmt.signedInr(sumNet)} · avg {fmt.r(avgR)}
                </span>
              </div>
            }
          >
            <div className="grid gap-3 xl:grid-cols-12">
              <div className="max-h-[900px] overflow-y-auto xl:col-span-4">
                <table className="w-full text-xs">
                  <thead className="sticky top-0 bg-slate-950">
                    <tr className="border-b border-slate-800 text-slate-500">
                      {['Time', 'Book', 'Symbol', 'Dir', 'R', 'Net', 'MFE', 'Exit', 'Held', 'Gr'].map((h) => (
                        <th key={h} className="whitespace-nowrap px-1.5 py-1 text-left font-medium">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((t) => (
                      <tr key={t.idx} onClick={() => setSel(t.idx)} className={cls('cursor-pointer border-b border-slate-900 hover:bg-slate-900/60', sel === t.idx && 'bg-slate-800/70')}>
                        <td className="whitespace-nowrap px-1.5 py-1 text-slate-500">{`${ist(t.entry_ts).slice(0, 5)} ${ist(t.entry_ts).slice(11, 16)}`}</td>
                        <td className="px-1.5 py-1"><Book k={t.strategy} /></td>
                        <td className="px-1.5 py-1 font-medium">{t.symbol}</td>
                        <td className={cls('px-1.5 py-1', t.direction === 'BULLISH' ? 'text-emerald-400' : 'text-rose-400')}>{t.direction === 'BULLISH' ? 'L' : 'S'}</td>
                        <td className={cls('px-1.5 py-1 font-semibold', pnlColor(t.r_multiple))}>{fmt.r(t.r_multiple)}</td>
                        <td className={cls('px-1.5 py-1', pnlColor(t.net))}>{fmt.signedInr(t.net)}</td>
                        <td className="px-1.5 py-1 text-emerald-500/70">{fmt.r(t.mfe_r)}</td>
                        <td className="px-1.5 py-1 text-slate-400">{t.exit_reason}</td>
                        <td className="px-1.5 py-1 text-slate-500">{t.bars_held}</td>
                        <td className="px-1.5 py-1 text-slate-500">{t.grade}</td>
                      </tr>
                    ))}
                    {!rows.length && (
                      <tr>
                        <td colSpan={10} className="px-2 py-6 text-center text-slate-600">no trades match</td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
              <div className="xl:col-span-8">
                {runId && sel != null ? <TradeDebugger runId={runId} index={sel} position={position} total={rows.length} onNav={nav} /> : <div className="text-[11px] text-slate-500">pick a trade</div>}
              </div>
            </div>
          </Card>
        </>
      )}
    </div>
  )
}
