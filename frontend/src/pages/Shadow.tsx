import { useState } from 'react'
import { Badge, Card, ErrorLine, Stat, Table } from '../components/Ui'
import { cls, fmt, istHm, pnlColor } from '../lib/api'
import { usePoll } from '../lib/usePoll'

type Wall = { name: string; price: number; bps: number }
type Tags = Record<string, number | boolean | null | undefined>
type Virtual = Record<string, { pnl_bps: number | null; how: string | null; exit: number | null; bars: number }>
interface Signal {
  id: string
  ts: number
  time: string
  symbol: string
  side: 'LONG' | 'SHORT'
  entry: number
  stop: number
  risk_bps: number
  walls: Wall[]
  retest_time: string
  bar?: string
  tags: Tags
  taken: boolean
  why: string
}
interface Position {
  id: string
  symbol: string
  side: 'LONG' | 'SHORT'
  status: 'OPEN' | 'CLOSED'
  qty: number
  entry: number
  initial_stop: number
  stop: number
  target: number | null
  target_name: string
  risk_bps: number
  reward_bps: number
  rule: string
  opened_ts: number
  entry_time: string
  entry_bar?: string
  retest_time: string
  mode: string
  tags: Tags
  bars_held: number
  mfe_bps: number
  mae_bps: number
  last: number
  pnl_bps: number
  closed_ts: number | null
  exit: number | null
  exit_reason: string | null
  gross_inr: number | null
  charges_inr: number | null
  net_inr: number | null
  r: number | null
  virtual: Virtual
}
interface StageRow {
  symbol: string
  side: 'bullish' | 'bearish'
  stage: string
  lvl_os: string
  i_os: number | null
  i_bos: number | null
  i_brk: number | null
  i_rt: number | null
  result: string
  j: number | null
  tol: number
}
interface RuleBlock {
  closed: number
  avg_pnl_bps: number | null
  win_pct: number | null
  exits: Record<string, number>
}
interface View {
  strategy: string
  day: string
  rule: string
  filter: string
  notional_inr: number
  universe: number
  with_levels: number
  orders: number
  errors: number
  summary: {
    signals: number
    taken: number
    skipped: number
    open: number
    closed: number
    wins: number
    losses: number
    avg_pnl_bps: number | null
    sum_gross_inr: number
    sum_net_inr: number
    sum_r: number
    open_pnl_bps: number
    by_rule: Record<string, RuleBlock>
  }
  stages: StageRow[]
  signals: Signal[]
  positions: Position[]
  trades: Position[]
}

const bps = (v: number | null | undefined) => (v == null ? 'DM' : `${v >= 0 ? '+' : ''}${v.toFixed(0)}`)
const B = (v: number | null | undefined) => <span className={pnlColor(v)}>{bps(v)}</span>
const barTime = (i: number | null) => (i == null ? '' : `${String(9 + Math.floor((15 + i * 5) / 60)).padStart(2, '0')}:${String((15 + i * 5) % 60).padStart(2, '0')}`)
const STAGE_TONE: Record<string, 'slate' | 'green' | 'red' | 'amber' | 'violet' | 'blue'> = {
  open_test: 'slate',
  bos: 'blue',
  through: 'violet',
  retest_failed: 'red',
  retest_held: 'amber',
  confirmed: 'green',
}
const TAG_KEYS = ['st60_al', 'dma20_al', 'dma50_al', 'daily_all', 'prev_trend_al', 'nifty_al', 'ib_al', 'xs_top20'] as const

function TagChips({ tags }: { tags: Tags }) {
  return (
    <div className="flex flex-wrap gap-1">
      {TAG_KEYS.filter((k) => k in tags).map((k) => (
        <Badge key={k} tone={tags[k] ? 'green' : 'slate'}>
          {k.replace('_al', '')}
        </Badge>
      ))}
      {tags.move_adr != null && <Badge tone="blue">{`move ${Number(tags.move_adr).toFixed(2)} ADR`}</Badge>}
      {tags.xs_pct != null && <Badge tone="blue">{`rank ${Math.round(Number(tags.xs_pct) * 100)}%`}</Badge>}
    </div>
  )
}

function TradeRows({ rows, rule }: { rows: Position[]; rule: string }) {
  const others = ['wall_then_trail', 'fixed_wall', 'fixed_1to2'].filter((r) => r !== rule)
  return (
    <>
      {rows.map((p) => (
        <tr key={p.id} className="border-b border-slate-900 align-top">
          <td className="whitespace-nowrap px-2 py-1 text-slate-400" title={`bars labelled by their open; the entry fires at ${p.entry_time}`}>
            {p.retest_time} → {p.entry_bar ?? p.entry_time}
          </td>
          <td className="px-2 py-1 font-semibold">{p.symbol}</td>
          <td className={cls('px-2 py-1', p.side === 'LONG' ? 'text-emerald-400' : 'text-rose-400')}>{p.side}</td>
          <td className="px-2 py-1 text-right">{fmt.n(p.entry)}</td>
          <td className="px-2 py-1 text-right">
            {fmt.n(p.stop)}
            {Math.abs(p.stop - p.initial_stop) > 1e-9 && <span className="text-slate-500"> ({fmt.n(p.initial_stop)})</span>}
          </td>
          <td className="px-2 py-1 text-right">
            {fmt.n(p.target)} <span className="text-slate-500">{p.target_name}</span>
          </td>
          <td className="px-2 py-1 text-right">{p.risk_bps.toFixed(0)}</td>
          <td className="px-2 py-1 text-right">{p.reward_bps.toFixed(0)}</td>
          <td className="px-2 py-1 text-right">{p.bars_held}</td>
          <td className="px-2 py-1 text-right">{B(p.mfe_bps)}</td>
          <td className="px-2 py-1 text-right">{B(p.mae_bps)}</td>
          <td className="px-2 py-1 text-right font-semibold">{B(p.pnl_bps)}</td>
          <td className={cls('px-2 py-1 text-right', pnlColor(p.net_inr))}>{p.net_inr == null ? fmt.n(p.last) : fmt.signedInr(p.net_inr)}</td>
          <td className="px-2 py-1 text-right">{fmt.r(p.r)}</td>
          <td className="px-2 py-1">
            {p.status === 'OPEN' ? (
              <Badge tone="blue">open</Badge>
            ) : (
              <Badge tone={p.exit_reason === 'stop' ? 'red' : p.exit_reason === 'target' ? 'green' : p.exit_reason === 'trail' ? 'amber' : 'slate'}>
                {p.exit_reason} {fmt.n(p.exit)}
              </Badge>
            )}
          </td>
          <td className="whitespace-nowrap px-2 py-1 text-slate-400">
            {others.map((r) => (
              <span key={r} className="mr-2">
                {r.replace('_', ' ')}: {p.virtual?.[r] ? <>{B(p.virtual[r].pnl_bps)} <span className="text-slate-600">{p.virtual[r].how}</span></> : 'running'}
              </span>
            ))}
          </td>
          <td className="px-2 py-1">
            <TagChips tags={p.tags} />
          </td>
        </tr>
      ))}
    </>
  )
}

const TRADE_HEAD = ['Retest → entry bar', 'Symbol', 'Side', 'Entry', 'Stop (initial)', 'Target', 'Risk bps', 'Reward bps', 'Bars', 'MFE', 'MAE', 'P&L bps', 'Net ₹ / last', 'R', 'Exit', 'Other rules', 'Context']

/** The shadow book of the one strategy: the owner's CPR retest sequence, mechanical, every confirmed
 * retest taken at the bar's close, stop under the retest bar, target the next wall then trail. Numbers here
 * are what the research measured on 3,101 instances (0 to +4 bps gross); the day is for looking, not for size. */
export function Shadow() {
  const [day, setDay] = useState('')
  const { data, error } = usePoll<View>(`/api/strategy${day ? `?day=${day}` : ''}`, 5000)
  const s = data?.summary
  const rule = data?.rule ?? 'wall_then_trail'
  return (
    <div className="space-y-4 p-4">
      <ErrorLine error={error} />
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-sm font-semibold text-slate-200">Shadow · CPR retest</h1>
        <input type="date" value={day} onChange={(e) => setDay(e.target.value)} className="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-xs text-slate-300" />
        {day && (
          <button onClick={() => setDay('')} className="text-xs text-slate-400 hover:text-white">
            today
          </button>
        )}
        <span className="text-xs text-slate-500">
          {data ? `${data.day} · rule ${data.rule} · filter ${data.filter} · ₹${fmt.int(data.notional_inr)} a trade · ${data.with_levels}/${data.universe} stocks with levels · ${data.orders} shadow orders` : ''}
          {data && data.errors > 0 && <span className="text-amber-400"> · {data.errors} bar errors</span>}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-7">
        <Stat label="Signals" value={s?.signals ?? 'DM'} sub={`${s?.taken ?? 0} taken · ${s?.skipped ?? 0} skipped`} />
        <Stat label="Open" value={s?.open ?? 'DM'} sub={<span className={pnlColor(s?.open_pnl_bps)}>{bps(s?.open_pnl_bps)} bps</span>} />
        <Stat label="Closed" value={s?.closed ?? 'DM'} sub={`${s?.wins ?? 0} wins · ${s?.losses ?? 0} losses`} />
        <Stat label="Avg P&L" value={`${bps(s?.avg_pnl_bps)} bps`} tone={pnlColor(s?.avg_pnl_bps)} sub="per closed trade, gross" />
        <Stat label="Net ₹" value={fmt.signedInr(s?.sum_net_inr)} tone={pnlColor(s?.sum_net_inr)} sub={`gross ${fmt.signedInr(s?.sum_gross_inr)}`} />
        <Stat label="Sum R" value={fmt.r(s?.sum_r)} tone={pnlColor(s?.sum_r)} />
        <Stat
          label="Other rules"
          value={
            <span className="text-xs">
              {Object.entries(s?.by_rule ?? {}).map(([r, b]) => (
                <span key={r} className="mr-2 whitespace-nowrap">
                  {r.replace('_', ' ')} {B(b.avg_pnl_bps)}
                </span>
              ))}
            </span>
          }
          sub="avg bps per closed trade"
        />
      </div>

      <Card title="Trades" right="entry = the confirmation bar's close · stop under the retest bar less a tolerance · stop before target when a bar touches both">
        <Table head={TRADE_HEAD} empty="no trade yet — the sequence needs an open test by 09:45, a break of structure within the hour, a close through the CPR, a held retest and a close beyond it">
          <TradeRows rows={[...(data?.positions ?? []), ...(data?.trades ?? [])]} rule={rule} />
        </Table>
      </Card>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card title="Signals" right="every confirmed retest, taken or not">
          <Table head={['Retest → entry bar', 'Symbol', 'Side', 'Entry', 'Stop', 'Risk bps', 'Walls', 'Taken', 'Context']} empty="none yet">
            {(data?.signals ?? []).map((g) => (
              <tr key={g.id} className="border-b border-slate-900 align-top">
                <td className="whitespace-nowrap px-2 py-1 text-slate-400" title={`bars labelled by their open; the signal fires at ${g.time}`}>
                  {g.retest_time} → {g.bar ?? g.time}
                </td>
                <td className="px-2 py-1 font-semibold">{g.symbol}</td>
                <td className={cls('px-2 py-1', g.side === 'LONG' ? 'text-emerald-400' : 'text-rose-400')}>{g.side}</td>
                <td className="px-2 py-1 text-right">{fmt.n(g.entry)}</td>
                <td className="px-2 py-1 text-right">{fmt.n(g.stop)}</td>
                <td className="px-2 py-1 text-right">{g.risk_bps.toFixed(0)}</td>
                <td className="whitespace-nowrap px-2 py-1 text-slate-400">{g.walls.slice(0, 3).map((w) => `${w.name} ${w.price.toFixed(2)} (+${w.bps.toFixed(0)})`).join(' · ')}</td>
                <td className="px-2 py-1">{g.taken ? <Badge tone="green">taken</Badge> : <span className="text-xs text-amber-400">{g.why}</span>}</td>
                <td className="px-2 py-1">
                  <TagChips tags={g.tags} />
                </td>
              </tr>
            ))}
          </Table>
        </Card>
        <Card title="Where each stock stands" right="the sequence on the closed 5m bars so far; bar times are the bucket starts">
          <Table head={['Symbol', 'Side', 'Stage', 'Open test', 'BOS', 'Through CPR', 'Retest', 'Confirmed', 'Tol']} empty="nothing has tested a level yet">
            {(data?.stages ?? []).map((r) => (
              <tr key={`${r.symbol}-${r.side}`} className="border-b border-slate-900">
                <td className="px-2 py-1 font-semibold">{r.symbol}</td>
                <td className={cls('px-2 py-1', r.side === 'bullish' ? 'text-emerald-400' : 'text-rose-400')}>{r.side}</td>
                <td className="px-2 py-1">
                  <Badge tone={STAGE_TONE[r.stage] ?? 'slate'}>{r.stage.replace('_', ' ')}</Badge>
                </td>
                <td className="px-2 py-1 text-slate-400">
                  {barTime(r.i_os)} {r.lvl_os}
                </td>
                <td className="px-2 py-1 text-slate-400">{barTime(r.i_bos)}</td>
                <td className="px-2 py-1 text-slate-400">{barTime(r.i_brk)}</td>
                <td className="px-2 py-1 text-slate-400">
                  {barTime(r.i_rt)} {r.result}
                </td>
                <td className="px-2 py-1 text-slate-400">{barTime(r.j)}</td>
                <td className="px-2 py-1 text-right text-slate-500">{r.tol.toFixed(2)}</td>
              </tr>
            ))}
          </Table>
        </Card>
      </div>
      <div className="text-[11px] text-slate-500">
        times are IST; a 5m bar closes five minutes after the time shown · first bar of the session {istHm(0) ? '' : ''}09:15
      </div>
    </div>
  )
}
