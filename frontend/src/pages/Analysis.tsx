import { useState, type ReactNode } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { FnoPanel } from '../components/FnoPanel'
import { MicroPanel } from '../components/MicroPanel'
import { Card, ErrorLine, Stat } from '../components/Ui'
import { cls, fmt, ist, istHm } from '../lib/api'
import { usePoll } from '../lib/usePoll'

/* ---------- payload (backend/kotsin_nse/analysis/bundle.py) ---------- */
type Num = number | null | undefined
interface Range { return_pct: number; high: number; low: number; position_pct: number | null }
interface Fib { move: 'UP' | 'DOWN'; swing_high: number; swing_high_day: string; swing_low: number; swing_low_day: string; retraced_pct: number; levels: { ratio: number; price: number }[] }
interface BigPicture {
  last: number; prev_close: number; '1m'?: Range; '3m'?: Range; '1y'?: Range
  sma: Record<string, Num>; trend: string; adr20: number; adr20_pct: number; vol20_ann_pct: number; vol250_ann_pct: Num
  weekly_closes: number[]; monthly_closes: number[]
  last_sessions: { day: string; o: number; h: number; l: number; c: number; v: number }[]
  fibonacci: Fib | null
}
interface PosRow { day: string; fut_close: number; fut_oi: number; fut_oi_chg: number; build_up: string | null; pcr: Num; call_wall: Num; put_wall: Num }
interface Positioning {
  sessions: PosRow[]
  summary: { from: string; to: string; fut_oi_change_pct: Num; fut_price_change_pct: number; pcr_from: Num; pcr_to: Num }
  latest_day: string; expiry: string
  top_calls: { strike: number; oi: number; oi_chg: number; close: number }[]
  top_puts: { strike: number; oi: number; oi_chg: number; close: number }[]
}
interface Zone { low: number; high: number; mid: number; weight: number; members: string[]; kind: string; distance_pct: Num }
interface Levels { last: number; adr: number; clusters: Zone[]; levels: { name: string; price: number; frame: string }[] }
interface Flow15 { ts: number; buy: number; sell: number; delta_pct: Num; ofi: number; queue_imbalance: Num }
interface Today {
  open: number; high: number; low: number; last: number; vwap: Num; gap_pct: Num
  bars_15m: { ts: number; o: number; h: number; l: number; c: number; v: number }[]
  zone_reaction: { zone: string; members: string[]; status: string }[]
  flow_cash: Flow15[]; flow_future?: Flow15[]
}
interface BuildRow { window: string; price_chg_pct: number; oi_chg_pct: number; oi_chg: number; build_up: string }
interface Leg { action: string; type: string; strike: number; ltp: number; delta: number; theta: Num }
interface Setup { name: string; why: string; legs: Leg[]; credit: number; width: number; max_loss: number; credit_lot: number; max_loss_lot: number; breakeven: number | number[]; pop_pct: number; theta_lot: number; reward_risk: Num }
interface Bundle {
  symbol: string; ts: number; sector: string
  big_picture: BigPicture | null; positioning: Positioning | null; buildup?: BuildRow[]; levels: Levels | null
  structure?: { setup: { side: string; z: number; s1: number; r1: Num; r2: Num; rr: Num; state: string } | null; zones: { side: string; low: number; high: number; state: string; bos_kind: string; retests: number }[]; sweeps: { ts: number; level: number; kind: string; side: string }[]; liquidity: { price: number; kind: string }[]; opening_range: number[] | null }
  profile?: { market: Record<string, unknown>; volume: Record<string, unknown> }
  today?: Today
  options: { reading: { bias: string; score: number } | null } | null
  hedge?: { setups: Setup[]; lot: number; note: string } | null
}
interface Summary { text: string; model: string; created_ts: number; seconds: number; cached: boolean; usage: { input: number; output: number; cache_read: number } }

const BUILD: Record<string, [string, string]> = {
  LB: ['long build-up', 'bg-emerald-900/60 text-emerald-300'],
  SC: ['short covering', 'bg-emerald-950/60 text-emerald-400'],
  SB: ['short build-up', 'bg-rose-900/60 text-rose-300'],
  LU: ['long unwinding', 'bg-rose-950/60 text-rose-400'],
  FLAT: ['flat OI', 'bg-slate-800 text-slate-400'],
}
const chip = (b: string | null | undefined) =>
  b ? <span className={cls('rounded px-1.5 py-0.5 text-[10px]', BUILD[b]?.[1] ?? 'bg-slate-800')}>{b} · {BUILD[b]?.[0] ?? b}</span> : <span className="text-slate-600">—</span>
const tone = (v: Num) => ((v ?? 0) > 0 ? 'text-emerald-400' : (v ?? 0) < 0 ? 'text-rose-400' : 'text-slate-300')
const big = (v: number) => (Math.abs(v) >= 1e7 ? `${(v / 1e7).toFixed(2)} cr` : Math.abs(v) >= 1e5 ? `${(v / 1e5).toFixed(2)} L` : fmt.int(v))

/** A tiny markdown renderer for the summary: ## headings, - bullets, **bold**, paragraphs. */
function Markdown({ text }: { text: string }) {
  const inline = (s: string) =>
    s.split(/(\*\*[^*]+\*\*)/g).map((p, i) => (p.startsWith('**') && p.endsWith('**') ? <b key={i} className="text-slate-100">{p.slice(2, -2)}</b> : <span key={i}>{p}</span>))
  const out: ReactNode[] = []
  let list: string[] = []
  const flush = () => {
    if (list.length) out.push(<ul key={`u${out.length}`} className="ml-4 list-disc space-y-0.5">{list.map((l, i) => <li key={i}>{inline(l)}</li>)}</ul>)
    list = []
  }
  for (const raw of text.split('\n')) {
    const line = raw.trimEnd()
    if (/^#{1,3} /.test(line)) {
      flush()
      out.push(<h3 key={`h${out.length}`} className="mt-3 text-sm font-semibold text-sky-300">{line.replace(/^#+ /, '')}</h3>)
    } else if (/^\s*[-*] /.test(line)) list.push(line.replace(/^\s*[-*] /, ''))
    else if (line.trim() === '') flush()
    else {
      flush()
      out.push(<p key={`p${out.length}`}>{inline(line)}</p>)
    }
  }
  flush()
  return <div className="space-y-1.5 text-[13px] leading-relaxed text-slate-300">{out}</div>
}

function SummaryCard({ symbol }: { symbol: string }) {
  const [nonce, setNonce] = useState(0)
  const path = `/api/analysis/${encodeURIComponent(symbol)}/summary${nonce ? `?refresh=true&n=${nonce}` : ''}`
  const { data, error, loading } = usePoll<Summary>(path, 600000)
  return (
    <Card
      title="Claude's read"
      right={
        <span className="flex items-center gap-2">
          {data && <span>{data.model} · {istHm(data.created_ts)}{data.cached ? ' · cached' : ` · ${data.seconds}s`}</span>}
          <button onClick={() => setNonce(Date.now())} className="rounded bg-slate-800 px-2 py-0.5 text-slate-200 hover:bg-slate-700" title="a new paid API call">
            regenerate
          </button>
        </span>
      }
    >
      {loading && !data && <div className="text-xs text-slate-500">reading the whole page with Claude… (up to a minute)</div>}
      {error && <div className="text-xs text-amber-400">{error.includes('503') ? 'Summary off: add KN_ANTHROPIC_API_KEY to backend/.env and restart the engine.' : error}</div>}
      {data && <Markdown text={data.text} />}
      {data && (
        <div className="mt-2 text-[10px] text-slate-600">
          tokens in {fmt.int(data.usage.input)} (cached {fmt.int(data.usage.cache_read)}) · out {fmt.int(data.usage.output)} · an LLM's reading of this page's data, not advice
        </div>
      )}
    </Card>
  )
}

function RangeBar({ label, r, last }: { label: string; r?: Range; last: number }) {
  if (!r) return null
  return (
    <div className="font-mono text-[11px]">
      <div className="flex justify-between">
        <span className="text-slate-400">{label}</span>
        <span className={tone(r.return_pct)}>{fmt.pct(r.return_pct)}</span>
      </div>
      <div className="relative mt-1 h-2 rounded bg-slate-800">
        <div className="absolute top-[-2px] h-3 w-1 rounded bg-sky-400" style={{ left: `${Math.min(100, Math.max(0, r.position_pct ?? 0))}%` }} />
      </div>
      <div className="flex justify-between text-[10px] text-slate-500">
        <span>{fmt.n(r.low)}</span>
        <span>at {r.position_pct?.toFixed(0)}% of range · {fmt.n(last)}</span>
        <span>{fmt.n(r.high)}</span>
      </div>
    </div>
  )
}

function BigPictureCard({ b }: { b: BigPicture }) {
  const f = b.fibonacci
  return (
    <Card title="1 · Big picture (daily)" right={b.trend}>
      <div className="grid gap-4 lg:grid-cols-3">
        <div className="space-y-3">
          <RangeBar label="1 month" r={b['1m']} last={b.last} />
          <RangeBar label="3 months" r={b['3m']} last={b.last} />
          <RangeBar label="1 year" r={b['1y']} last={b.last} />
        </div>
        <div className="grid grid-cols-2 gap-2">
          {(['20', '50', '200'] as const).map((n) => (
            <Stat key={n} label={`${n}-day avg`} value={fmt.n(b.sma[n])} sub={b.sma[n] ? `price ${b.last > (b.sma[n] as number) ? 'above' : 'below'} by ${fmt.pct((b.last / (b.sma[n] as number) - 1) * 100)}` : undefined} tone={b.sma[n] && b.last > (b.sma[n] as number) ? 'text-emerald-300' : 'text-rose-300'} />
          ))}
          <Stat label="normal day (20d)" value={`${fmt.n(b.adr20)}`} sub={`${b.adr20_pct}% · vol ${b.vol20_ann_pct}% (1y ${b.vol250_ann_pct ?? '—'}%)`} />
        </div>
        <div className="font-mono text-[11px]">
          {f ? (
            <>
              <div className="text-slate-400">
                Fibonacci of the last 60-session {f.move === 'DOWN' ? 'down' : 'up'}-move
              </div>
              <div className="text-slate-300">
                {f.move === 'DOWN' ? `${fmt.n(f.swing_high)} (${f.swing_high_day}) → ${fmt.n(f.swing_low)} (${f.swing_low_day})` : `${fmt.n(f.swing_low)} (${f.swing_low_day}) → ${fmt.n(f.swing_high)} (${f.swing_high_day})`}
              </div>
              <div className="mt-1 text-sm text-sky-300">{f.retraced_pct.toFixed(1)}% retraced</div>
              <div className="mt-1 space-y-0.5">
                {f.levels.map((l) => (
                  <div key={l.ratio} className={cls('flex justify-between', f.retraced_pct / 100 >= l.ratio ? 'text-slate-500 line-through' : 'text-slate-300')}>
                    <span>{(l.ratio * 100).toFixed(1)}%</span>
                    <span>{fmt.n(l.price)}</span>
                  </div>
                ))}
              </div>
            </>
          ) : (
            <span className="text-slate-600">no swing</span>
          )}
        </div>
      </div>
      <div className="mt-3 grid gap-2 font-mono text-[11px] md:grid-cols-2">
        <div>
          <span className="text-slate-500">weekly closes </span>
          {b.weekly_closes.map((c, i) => <span key={i} className={cls('mr-2', i > 0 && tone(c - b.weekly_closes[i - 1]))}>{fmt.n(c)}</span>)}
          <span className="ml-2 text-slate-500">monthly </span>
          {b.monthly_closes.map((c, i) => <span key={i} className={cls('mr-2', i > 0 && tone(c - b.monthly_closes[i - 1]))}>{fmt.n(c)}</span>)}
        </div>
        <div className="text-slate-500">
          last sessions:{' '}
          {b.last_sessions.map((s) => (
            <span key={s.day} className={cls('mr-2', tone(s.c - s.o))} title={`${s.day} O ${s.o} H ${s.h} L ${s.l} C ${s.c} V ${fmt.int(s.v)}`}>
              {s.day.slice(5)} {fmt.n(s.c)}
            </span>
          ))}
        </div>
      </div>
    </Card>
  )
}

function PositioningCard({ p, build }: { p: Positioning; build?: BuildRow[] }) {
  const s = p.summary
  return (
    <Card title="2 · Positioning (NSE daily F&O) and build-up across timeframes" right={`${s.from} → ${s.to}`}>
      {build && build.length > 0 && (
        <div className="mb-3 flex flex-wrap gap-2 font-mono text-[11px]">
          {build.map((r) => (
            <div key={r.window} className="rounded border border-slate-800 px-2 py-1">
              <div className="text-slate-400">{r.window}</div>
              {chip(r.build_up)}
              <div className="text-slate-500">
                price <span className={tone(r.price_chg_pct)}>{fmt.pct(r.price_chg_pct)}</span> · OI <span className={tone(r.oi_chg_pct)}>{fmt.pct(r.oi_chg_pct)}</span>
              </div>
            </div>
          ))}
        </div>
      )}
      <div className="mb-2 font-mono text-[11px] text-slate-400">
        futures OI {fmt.pct(s.fut_oi_change_pct)} while price {fmt.pct(s.fut_price_change_pct)} · PCR {s.pcr_from ?? '—'} → {s.pcr_to ?? '—'} · nearest expiry {p.expiry}
      </div>
      <div className="grid gap-4 lg:grid-cols-[2fr_1fr]">
        <table className="w-full font-mono text-[11px]">
          <thead className="text-slate-500">
            <tr><th className="text-left">day</th><th className="text-right">future</th><th className="text-right">OI</th><th className="text-right">ΔOI</th><th className="px-2 text-left">read</th><th className="text-right">PCR</th><th className="text-right">call wall</th><th className="text-right">put wall</th></tr>
          </thead>
          <tbody>
            {p.sessions.slice().reverse().map((r) => (
              <tr key={r.day} className="border-t border-slate-900">
                <td>{r.day.slice(5)}</td>
                <td className="text-right">{fmt.n(r.fut_close)}</td>
                <td className="text-right">{big(r.fut_oi)}</td>
                <td className={cls('text-right', tone(r.fut_oi_chg))}>{big(r.fut_oi_chg)}</td>
                <td className="px-2">{chip(r.build_up)}</td>
                <td className="text-right">{r.pcr ?? '—'}</td>
                <td className="text-right text-rose-300">{fmt.n(r.call_wall, 0)}</td>
                <td className="text-right text-emerald-300">{fmt.n(r.put_wall, 0)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="grid grid-cols-2 gap-3 font-mono text-[11px]">
          {(['top_calls', 'top_puts'] as const).map((k) => (
            <div key={k}>
              <div className={k === 'top_calls' ? 'text-rose-300' : 'text-emerald-300'}>{k === 'top_calls' ? 'call OI' : 'put OI'} ({p.latest_day.slice(5)})</div>
              {p[k].map((r) => (
                <div key={r.strike} className="flex justify-between">
                  <span>{fmt.n(r.strike, 0)}</span>
                  <span>{big(r.oi)}</span>
                  <span className={tone(r.oi_chg)}>{big(r.oi_chg)}</span>
                </div>
              ))}
            </div>
          ))}
        </div>
      </div>
    </Card>
  )
}

function LevelsCard({ lv, fib, structure, profile }: { lv: Levels | null; fib: Fib | null | undefined; structure?: Bundle['structure']; profile?: Bundle['profile'] }) {
  const zones = (lv?.clusters ?? []).filter((z) => Math.abs(z.distance_pct ?? 0) < 6).sort((a, b) => b.mid - a.mid)
  const byFrame = (f: string) => (lv?.levels ?? []).filter((l) => l.frame === f).sort((a, b) => b.price - a.price)
  const su = structure?.setup
  const mk = (profile?.market ?? {}) as Record<string, number | string | boolean | null>
  const vp = (profile?.volume ?? {}) as Record<string, number | number[] | null>
  return (
    <Card title="3 · Levels: pivots, zones, Fibonacci, structure (SMC) and profile" right={lv ? `price ${fmt.n(lv.last)} · normal day ${fmt.n(lv.adr)}` : ''}>
      <div className="grid gap-4 lg:grid-cols-3">
        <div className="font-mono text-[11px]">
          <div className="mb-1 text-slate-400">zones where pivots meet (within 6%)</div>
          {zones.map((z) => (
            <div key={`${z.low}`} className={cls('flex justify-between gap-2', z.kind === 'RESISTANCE' ? 'text-rose-300' : z.kind === 'SUPPORT' ? 'text-emerald-300' : 'text-amber-300')}>
              <span>{fmt.n(z.low)}{z.high > z.low ? `–${fmt.n(z.high)}` : ''}</span>
              <span className="text-slate-400">{z.members.join('·')}</span>
              <span>{z.distance_pct != null ? fmt.pct(z.distance_pct) : ''}</span>
            </div>
          ))}
          {fib && (
            <>
              <div className="mb-1 mt-3 text-slate-400">Fibonacci ({fib.retraced_pct.toFixed(1)}% retraced)</div>
              {fib.levels.map((l) => (
                <div key={l.ratio} className="flex justify-between text-sky-300/80">
                  <span>{(l.ratio * 100).toFixed(1)}%</span>
                  <span>{fmt.n(l.price)}</span>
                </div>
              ))}
            </>
          )}
        </div>
        <div className="grid grid-cols-3 gap-2 font-mono text-[10px]">
          {(['D', 'W', 'M'] as const).map((f) => (
            <div key={f}>
              <div className="mb-1 text-slate-400">{f === 'D' ? 'daily' : f === 'W' ? 'weekly' : 'monthly'}</div>
              {byFrame(f).map((l) => (
                <div key={l.name} className={cls('flex justify-between', lv && l.price > lv.last ? 'text-rose-300/80' : 'text-emerald-300/80')}>
                  <span>{l.name}</span>
                  <span>{fmt.n(l.price)}</span>
                </div>
              ))}
            </div>
          ))}
        </div>
        <div className="space-y-2 font-mono text-[11px]">
          <div>
            <div className="text-slate-400">structure (5-minute, SMC)</div>
            {su ? (
              <div className={su.side === 'BULLISH' ? 'text-emerald-300' : 'text-rose-300'}>
                {su.side} order block {su.state.toLowerCase()} · Z {fmt.n(su.z)} · wrong at {fmt.n(su.s1)} · R1 {fmt.n(su.r1)} · R2 {fmt.n(su.r2)} · R:R {su.rr ?? '—'}
              </div>
            ) : (
              <div className="text-slate-600">no live setup</div>
            )}
            {(structure?.zones ?? []).map((z, i) => (
              <div key={i} className="text-slate-400">
                {z.side.toLowerCase()} OB {fmt.n(z.low)}–{fmt.n(z.high)} · {z.state.toLowerCase()} · BOS {z.bos_kind.toLowerCase().replace('_', ' ')}
              </div>
            ))}
            {(structure?.sweeps ?? []).map((w, i) => (
              <div key={`s${i}`} className="text-violet-300">sweep {w.kind.toLowerCase().replace('_', ' ')} {fmt.n(w.level)} at {istHm(w.ts)}</div>
            ))}
          </div>
          <div>
            <div className="text-slate-400">market profile</div>
            <div className="text-slate-300">
              {String(mk.day_type ?? '—').toLowerCase().replace('_', ' ')} · POC {fmt.n(mk.poc as number)} · value {fmt.n(mk.val as number)}–{fmt.n(mk.vah as number)} · first hour {fmt.n(mk.ib_low as number)}–{fmt.n(mk.ib_high as number)}
              {mk.poor_high ? ' · poor high' : ''}
              {mk.poor_low ? ' · poor low' : ''}
            </div>
            <div className="text-slate-400">volume profile</div>
            <div className="text-slate-300">
              POC {fmt.n(vp.poc as number)} · value {fmt.n(vp.val as number)}–{fmt.n(vp.vah as number)} · high-volume nodes {((vp.hvn as number[]) ?? []).map((x) => fmt.n(x)).join(', ')} · low-volume {((vp.lvn as number[]) ?? []).map((x) => fmt.n(x)).join(', ')}
            </div>
          </div>
        </div>
      </div>
    </Card>
  )
}

function TodayCard({ t }: { t: Today }) {
  const fut = new Map((t.flow_future ?? []).map((f) => [f.ts, f]))
  const cash = new Map(t.flow_cash.map((f) => [f.ts, f]))
  return (
    <Card title="4 · Today: how price reacted" right={`gap ${fmt.pct(t.gap_pct)} · VWAP ${fmt.n(t.vwap)} · price ${t.last > (t.vwap ?? 0) ? 'above' : 'below'}`}>
      <div className="grid gap-4 lg:grid-cols-[1fr_2fr]">
        <div className="font-mono text-[11px]">
          <div className="text-slate-400">open {fmt.n(t.open)} · high {fmt.n(t.high)} · low {fmt.n(t.low)} · now {fmt.n(t.last)}</div>
          <div className="mb-1 mt-2 text-slate-400">pivot zones the day reached</div>
          {t.zone_reaction.length === 0 && <div className="text-slate-600">none</div>}
          {t.zone_reaction.map((z) => (
            <div key={z.zone} className="flex justify-between gap-2">
              <span>{z.zone}</span>
              <span className="text-slate-500">{z.members.join('·')}</span>
              <span className={z.status.includes('rejected') ? 'text-rose-300' : z.status.includes('held') ? 'text-emerald-300' : 'text-amber-300'}>{z.status}</span>
            </div>
          ))}
        </div>
        <table className="w-full font-mono text-[11px]">
          <thead className="text-slate-500">
            <tr><th className="text-left">15 min</th><th className="text-right">O</th><th className="text-right">H</th><th className="text-right">L</th><th className="text-right">C</th><th className="text-right">cash buy−sell</th><th className="text-right">future buy−sell</th><th className="text-right">fut OFI</th><th className="text-right">queue imb.</th></tr>
          </thead>
          <tbody>
            {t.bars_15m.map((b) => {
              const c = cash.get(b.ts)
              const f = fut.get(b.ts)
              return (
                <tr key={b.ts} className="border-t border-slate-900">
                  <td>{istHm(b.ts)}</td>
                  <td className="text-right">{fmt.n(b.o)}</td>
                  <td className="text-right">{fmt.n(b.h)}</td>
                  <td className="text-right">{fmt.n(b.l)}</td>
                  <td className={cls('text-right', tone(b.c - b.o))}>{fmt.n(b.c)}</td>
                  <td className={cls('text-right', tone(c?.delta_pct))}>{c?.delta_pct != null ? `${c.delta_pct > 0 ? '+' : ''}${c.delta_pct}%` : '—'}</td>
                  <td className={cls('text-right', tone(f?.delta_pct))}>{f?.delta_pct != null ? `${f.delta_pct > 0 ? '+' : ''}${f.delta_pct}%` : '—'}</td>
                  <td className={cls('text-right', tone(f?.ofi))}>{f ? big(f.ofi) : '—'}</td>
                  <td className={cls('text-right', tone(c?.queue_imbalance))}>{c?.queue_imbalance ?? '—'}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <div className="mt-1 text-[10px] text-slate-600">buy−sell = estimated from snapshots (quote rule) · OFI = order-flow imbalance at the best bid/ask · queue imb. = (bid size − ask size)/both at the touch</div>
    </Card>
  )
}

function HedgeCard({ h, bias }: { h: NonNullable<Bundle['hedge']>; bias: string }) {
  return (
    <Card title="7 · Option selling, hedged: two setups" right={`bias ${bias} · lot ${h.lot}`}>
      <div className="grid gap-4 lg:grid-cols-2">
        {h.setups.length === 0 && <div className="text-xs text-slate-600">no setup fits the strikes subscribed</div>}
        {h.setups.map((s) => (
          <div key={s.name} className="rounded border border-slate-800 p-3 font-mono text-[11px]">
            <div className="text-sm font-semibold text-slate-100">{s.name}</div>
            <div className="mb-2 text-slate-500">{s.why}</div>
            <table className="w-full">
              <tbody>
                {s.legs.map((l, i) => (
                  <tr key={i}>
                    <td className={l.action === 'SELL' ? 'text-rose-300' : 'text-emerald-300'}>{l.action}</td>
                    <td>{fmt.n(l.strike, 0)} {l.type}</td>
                    <td className="text-right">@ {fmt.n(l.ltp)}</td>
                    <td className="text-right text-slate-500">Δ {l.delta.toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-0.5">
              <span className="text-slate-500">credit</span><span className="text-emerald-300">{fmt.n(s.credit)} · ₹{fmt.int(s.credit_lot)}/lot</span>
              <span className="text-slate-500">max loss</span><span className="text-rose-300">{fmt.n(s.max_loss)} · ₹{fmt.int(s.max_loss_lot)}/lot</span>
              <span className="text-slate-500">breakeven</span><span>{Array.isArray(s.breakeven) ? s.breakeven.map((x) => fmt.n(x)).join(' / ') : fmt.n(s.breakeven)}</span>
              <span className="text-slate-500">keeps full credit</span><span>~{s.pop_pct}% (from deltas)</span>
              <span className="text-slate-500">time decay</span><span className="text-emerald-300">+₹{s.theta_lot}/day/lot</span>
              <span className="text-slate-500">reward : risk</span><span>{s.reward_risk ?? '—'}</span>
            </div>
          </div>
        ))}
      </div>
      <div className="mt-2 text-[10px] text-amber-600">{h.note}</div>
    </Card>
  )
}

/** One stock end to end, in reading order: Claude's read, then the evidence behind it. */
export function Analysis() {
  const { symbol: param } = useParams()
  const nav = useNavigate()
  const universe = usePoll<{ symbol: string; kind: string }[]>('/api/universe', 60000)
  const symbol = (param ?? universe.data?.find((u) => u.kind === 'EQUITY')?.symbol ?? '').toUpperCase()
  const { data, error } = usePoll<Bundle>(symbol ? `/api/analysis/${encodeURIComponent(symbol)}` : null, 15000)
  return (
    <div className="space-y-4 p-4">
      <div className="flex flex-wrap items-center gap-3">
        <select value={symbol} onChange={(e) => nav(`/analysis/${e.target.value}`)} className="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm">
          {(universe.data ?? []).map((u) => <option key={u.symbol} value={u.symbol}>{u.symbol}</option>)}
        </select>
        {data && (
          <span className="font-mono text-sm text-slate-300">
            {data.sector} · {fmt.n(data.today?.last ?? data.big_picture?.last)} · updated {ist(data.ts, true).slice(11)} IST
          </span>
        )}
      </div>
      <ErrorLine error={error} />
      {symbol && <SummaryCard symbol={symbol} />}
      {data?.big_picture && <BigPictureCard b={data.big_picture} />}
      {data?.positioning && <PositioningCard p={data.positioning} build={data.buildup} />}
      {data && <LevelsCard lv={data.levels} fib={data.big_picture?.fibonacci} structure={data.structure} profile={data.profile} />}
      {data?.today && <TodayCard t={data.today} />}
      {symbol && (
        <Card title="5 · Microstructure">
          <MicroPanel symbol={symbol} />
        </Card>
      )}
      {symbol && (
        <Card title="6 · Options">
          <FnoPanel symbol={symbol} />
        </Card>
      )}
      {data?.hedge && <HedgeCard h={data.hedge} bias={data.options?.reading?.bias ?? '—'} />}
    </div>
  )
}
