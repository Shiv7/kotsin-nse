// The trade map: one rail for the equity, the future and the option, as the alerts page draws it on every trigger card
// (components/BookCards.tsx). Kept here so the active-trades cards (pages/Options.tsx; operator, 2026-10-09: "targets
// options, fut, equity (we have this in live alerts page fudkii-rt-y tab on a progress bar, hence reuse it)") draw the
// very same rail. Moved out of BookCards unchanged: the alerts page renders as it did.

type Level = { label: string; price: number }

/** What the rail reads off a card: the alert card's own fields (BookCards' ``Card`` carries them all); the active-trades
 *  route (/api/positions/live, ``map``) serves them under the same names. */
export type TradeMapCard = {
  direction: 'BULLISH' | 'BEARISH'
  /** the trigger close, the stock's stop and targets */
  entry: number
  stop: number
  targets: number[] | null
  rr: number | null
  confluence: { stop_zone?: string; target_zones?: string[] }
  futLevels: { close: number; behind: Level | null; ahead: Level[] } | null
  position: { entry: number; option_sl: number; initial_option_sl?: number; option_targets: number[]; targets_hit: number; note: string } | null
  plan: { ok: boolean; premium?: number; optionSl?: number; ladder?: number[]; delta?: number } | null
  live: { line: number; optionSl: number; ladder: number[]; targetsHit: number; underlying: number | null } | null
  rtCard?: { stop?: { delta: number } } | null
  spark: [number, number][]
}

const f = (v: number | null | undefined, d = 2) => (v === null || v === undefined || Number.isNaN(v) ? '—' : v.toFixed(d))
/** What the option stop is (operator, 2026-10-06: "the dashboard says equity projected stop at 19.21 … entry was 18.4"): a stop
 *  above the premium paid is the trail locking a gain, a raised one the rising line, else the stock's stop through δ. */
export const optStopNote = (c: TradeMapCard, optSl: number, optEntry: number | null | undefined) => {
  if (optEntry && optSl > optEntry + 1e-6) return `trail — locks +${(((optSl - optEntry) / optEntry) * 100).toFixed(1)}%`
  if (c.live && c.live.line > c.live.optionSl) return 'rising line'
  const first = c.position?.initial_option_sl
  if (first && optSl > first + 1e-6) return `raised from ${f(first)}`
  return 'equity stop via δ'
}

/** The trade on ONE rail (operator, 2026-09-30: "one rail, colour-coded"): every level of the equity (●),
 *  the future (◆) and the option (▲) placed where the STOCK would be when that level is reached — a
 *  future level by its own % move, an option level through the option's delta, as the engine projects
 *  it — so the three ladders read on one axis. Risk to the left of the entry, reward to the right,
 *  whichever way the trade runs. A level the stock has passed turns green with a ✔ (a live trade's
 *  option rungs: the rungs actually taken); the next level ahead on each ladder is named underneath. */
type RailMark = { key: string; inst: 'equity' | 'future' | 'option'; code: string; kind: 'stop' | 'target'; price: number; move: number; zone?: string | null; crossed: boolean }

export function TradeMap({ c }: { c: TradeMapCard }) {
  const bull = c.direction === 'BULLISH'
  const sg = bull ? 1 : -1
  const p = c.position, plan = c.plan, fut = c.futLevels
  const optEntry = p ? p.entry : plan?.ok ? plan.premium ?? null : null
  const optSl = p ? (c.live ? Math.max(c.live.line, c.live.optionSl) : p.option_sl) : plan?.ok ? plan.optionSl ?? null : null
  const optLadder = p ? (c.live?.ladder?.length ? c.live.ladder : p.option_targets) : plan?.ok ? plan.ladder ?? [] : []
  // the option's delta: the live preview's, the card's stop projection's, else the one the position was opened on
  const noteDelta = Number(/delta≈\s*([\d.]+)/.exec(p?.note ?? '')?.[1] ?? 0)
  const delta = Math.abs(plan?.ok ? plan.delta ?? 0 : 0) || Math.abs(c.rtCard?.stop?.delta ?? 0) || noteDelta || null
  const rungsTaken = p ? (c.live?.targetsHit ?? p.targets_hit) : 0
  const now = c.live?.underlying ?? (c.spark?.length ? c.spark[c.spark.length - 1][1] : null)
  // favourable stock move, in % of the trigger close: + is toward the targets
  const fav = (px: number) => (sg * (px - c.entry) / c.entry) * 100
  const nowRaw = now !== null ? fav(now) : null
  const nowAtEntry = nowRaw !== null && Math.abs(nowRaw) < 0.05  // on the entry itself: one label, not two on top of each other
  const nowMove = nowAtEntry ? null : nowRaw
  const crossedAt = (move: number) => nowMove !== null && move > 0 && nowMove >= move
  const marks: RailMark[] = []
  marks.push({ key: 'e-sl', inst: 'equity', code: 'SL', kind: 'stop', price: c.stop, move: fav(c.stop), zone: c.confluence.stop_zone, crossed: false })
  ;(c.targets ?? []).slice(0, 4).forEach((t, i) => {
    const m = fav(t)
    marks.push({ key: `e${i}`, inst: 'equity', code: `E${i + 1}`, kind: 'target', price: t, move: m, zone: c.confluence.target_zones?.[i], crossed: crossedAt(m) })
  })
  if (fut && fut.close) {
    const fmove = (px: number) => (sg * (px - fut.close) / fut.close) * 100
    if (fut.behind) marks.push({ key: 'f-sl', inst: 'future', code: 'SL', kind: 'stop', price: fut.behind.price, move: fmove(fut.behind.price), zone: fut.behind.label, crossed: false })
    fut.ahead.slice(0, 4).forEach((l, i) => {
      const m = fmove(l.price)
      marks.push({ key: `f${i}`, inst: 'future', code: `F${i + 1}`, kind: 'target', price: l.price, move: m, zone: l.label, crossed: crossedAt(m) })
    })
  }
  if (optEntry && delta) {
    // the stock move that takes the premium to a level: (level − entry premium) ÷ δ, in % of the close
    const omove = (px: number) => ((px - optEntry) / delta / c.entry) * 100
    if (optSl) marks.push({ key: 'o-sl', inst: 'option', code: 'SL', kind: 'stop', price: optSl, move: omove(optSl), zone: optStopNote(c, optSl, optEntry), crossed: false })
    optLadder.slice(0, 4).forEach((l, i) => {
      const m = omove(l)
      marks.push({ key: `o${i}`, inst: 'option', code: `O${i + 1}`, kind: 'target', price: l, move: m, zone: i === optLadder.length - 1 ? 'the rest' : '1 lot', crossed: p ? i < rungsTaken : crossedAt(m) })
    })
  }
  // the scale: every stop, the nearer targets and "now"; a far option rung is pinned to the edge (›)
  const tgMoves = marks.filter((m) => m.kind === 'target').map((m) => m.move).sort((a, b) => a - b)
  const lo = Math.min(0, ...marks.filter((m) => m.kind === 'stop').map((m) => m.move), nowMove ?? 0)
  const eqLast = Math.max(0, ...marks.filter((m) => m.inst !== 'option' && m.kind === 'target').map((m) => m.move))
  const hiRaw = Math.max(eqLast, nowMove ?? 0, tgMoves.length ? tgMoves[Math.min(tgMoves.length - 1, 5)] : 0.5)
  const hi = hiRaw <= 0 ? 0.5 : hiRaw
  // two pieces: the risk side (lo … 0) gets at least 12 % of the rail, so the stops and the entry
  // stay apart when the targets run far; the header, the stops line and the hover keep the exact %
  const hiP = hi * 1.04, loP = lo < 0 ? lo * 1.08 : -0.05
  const riskW = Math.max(12, (Math.abs(loP) / (hiP - loP)) * 96)
  const x = (m: number) => Math.max(1.5, Math.min(98.5, m <= 0 ? 2 + riskW * (1 - m / loP) : 2 + riskW + ((96 - riskW) * m) / hiP))
  const off = (m: number) => m > hi * 1.03
  // codes stagger into three rows where two sit closer than 4.5 % of the rail
  const rowOf: Record<string, number> = {}
  const lastInRow = [-100, -100, -100]
  for (const m of [...marks].sort((a, b) => x(a.move) - x(b.move))) {
    const px = x(m.move)
    let r = lastInRow.findIndex((last) => px - last >= 4.5)
    if (r < 0) r = 2
    rowOf[m.key] = r
    lastInRow[r] = px
  }
  const col = { equity: 'text-sky-300', future: 'text-violet-300', option: 'text-amber-300' }
  const bg = { equity: 'bg-sky-300', future: 'bg-violet-300', option: 'bg-amber-300' }
  const shape = (m: RailMark) => {
    const fill = m.kind === 'stop' ? 'bg-rose-400' : m.crossed ? 'bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.9)]' : bg[m.inst]
    if (m.inst === 'future') return <span className={`block h-[11px] w-[11px] rotate-45 ${fill}`} />
    if (m.inst === 'option') {
      const tri = m.kind === 'stop' ? 'border-b-rose-400' : m.crossed ? 'border-b-emerald-400' : 'border-b-amber-300'
      return <span className={`block h-0 w-0 border-x-[7px] border-b-[12px] border-x-transparent ${tri}`} />
    }
    return <span className={`block h-3 w-3 rounded-full ${fill}`} />
  }
  const pctTxt = (v: number) => `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`
  const ticks = (() => {
    const range = hi - lo, step = range > 6 ? 2 : range > 3 ? 1 : range > 1.2 ? 0.5 : 0.25
    const out: number[] = []
    for (let t = Math.ceil(lo / step) * step; t <= hi + 1e-9; t += step) out.push(Math.round(t * 100) / 100)
    return out
  })()
  const next = (inst: RailMark['inst']) => marks.find((m) => m.inst === inst && m.kind === 'target' && !m.crossed)
  const anyOf = (inst: RailMark['inst']) => marks.some((m) => m.inst === inst && m.kind === 'target')
  const stopOf = (inst: RailMark['inst']) => marks.find((m) => m.inst === inst && m.kind === 'stop')
  const risk = Math.abs(fav(c.stop)), reward = c.targets?.length ? fav(c.targets[0]) : null
  const label = { equity: 'Equity', future: 'Future', option: 'Option' }
  const nextTxt = (inst: RailMark['inst']) => {
    if (!anyOf(inst)) return null
    const n = next(inst)
    if (!n) return <span key={inst} className={col[inst]}>{label[inst]}: all crossed ✓</span>
    const more = nowMove !== null ? n.move - nowMove : n.move
    return <span key={inst}><span className={col[inst]}>{label[inst]} {n.code.replace(/^[EFO]/, 'T')} {f(n.price)}</span> <span className="text-slate-400">({inst === 'option' ? `stock ${pctTxt(n.move)}` : `${pctTxt(more)} more`})</span></span>
  }
  return (
    <div className="rounded-xl bg-slate-950/50 px-4 pb-3 pt-3">
      <div className="mb-1 flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-[12px] font-semibold uppercase tracking-wider text-slate-400">trade map · stock move from the trigger close {f(c.entry)}{bull ? '' : ' (a fall is progress)'}</span>
        <span className="text-[13px] text-slate-400">risk <b className="text-rose-300">{risk.toFixed(2)}%</b>{reward !== null ? <> · to T1 <b className="text-emerald-300">{reward.toFixed(2)}%</b></> : <> · <span className="text-slate-500">no target</span></>} · RR <b className="text-slate-100">{f(c.rr, 1)}</b></span>
      </div>
      <div className="relative h-[118px]">
        {/* the rail, the stretch from the entry to now, the entry */}
        <div className="absolute left-[2%] right-[2%] top-[58px] h-[6px] rounded-full bg-slate-800" />
        <div className="absolute top-[58px] h-[6px] rounded-l-full bg-rose-500/40" style={{ left: `${x(lo)}%`, width: `${x(0) - x(lo)}%` }} />
        {nowMove !== null && (
          <div className={`absolute top-[58px] h-[6px] ${nowMove >= 0 ? 'bg-emerald-500/60' : 'bg-rose-500/70'}`} style={{ left: `${Math.min(x(0), x(nowMove))}%`, width: `${Math.abs(x(nowMove) - x(0))}%` }} />
        )}
        <div className="absolute top-[53px] -translate-x-1/2" style={{ left: `${x(0)}%` }} title={`entry — trigger close ${f(c.entry)}${optEntry ? ` · option ${f(optEntry)}` : ''}${fut ? ` · future ${f(fut.close)}` : ''}`}>
          <span className="block h-4 w-4 rounded-full bg-amber-300 ring-4 ring-slate-950" />
        </div>
        <span className="absolute top-[74px] -translate-x-1/2 whitespace-nowrap text-[12px] font-semibold uppercase tracking-wide text-amber-300" style={{ left: `${x(0)}%` }}>{nowAtEntry ? 'entry · now' : 'entry'}</span>
        {marks.map((m) => (
          <div key={m.key} className="absolute top-0 h-full -translate-x-1/2" style={{ left: `${x(m.move)}%` }}
            title={`${label[m.inst]} ${m.kind === 'stop' ? 'stop' : m.code.replace(/^[EFO]/, 'T')} ${f(m.price)}${m.zone ? ` · ${m.zone}` : ''} · stock ${pctTxt(m.move)}${m.inst === 'option' ? ' (via δ)' : ''}${m.crossed ? ' · crossed ✓' : ''}${off(m.move) ? ' · beyond the scale' : ''}`}>
            <span className={`absolute left-1/2 -translate-x-1/2 whitespace-nowrap text-[12.5px] font-bold ${m.kind === 'stop' ? 'text-rose-300' : m.crossed ? 'text-emerald-300' : col[m.inst]}`} style={{ top: `${4 + rowOf[m.key] * 15}px` }}>
              {m.crossed ? '✔ ' : ''}{m.code}{off(m.move) ? ' ›' : ''}
            </span>
            <div className="absolute left-1/2 top-[55px] flex -translate-x-1/2 items-center justify-center">{shape(m)}</div>
          </div>
        ))}
        {nowMove !== null && (
          <div className="absolute top-[70px] -translate-x-1/2 text-center" style={{ left: `${x(nowMove)}%` }} title={`stock now ${f(now)}`}>
            <div className="mx-auto h-0 w-0 border-x-[7px] border-b-[10px] border-x-transparent border-b-sky-300" />
            <div className="whitespace-nowrap text-[12px] font-semibold text-sky-300">now {pctTxt(nowMove)}</div>
          </div>
        )}
        <div className="absolute bottom-0 left-0 right-0 h-[16px]">
          {ticks.map((t) => <span key={t} className="absolute -translate-x-1/2 text-[11.5px] text-slate-500" style={{ left: `${x(t)}%` }}>{t === 0 ? '0' : pctTxt(t).replace('.00', '')}</span>)}
        </div>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[12.5px] text-slate-400">
        <span className="inline-flex items-center gap-1.5"><span className="block h-2.5 w-2.5 rounded-full bg-sky-300" />equity</span>
        <span className="inline-flex items-center gap-1.5"><span className="block h-2.5 w-2.5 rotate-45 bg-violet-300" />future</span>
        <span className="inline-flex items-center gap-1.5"><span className="block h-0 w-0 border-x-[5px] border-b-[9px] border-x-transparent border-b-amber-300" />option{marks.some((m) => m.inst === 'option') ? ` (via δ ${delta!.toFixed(2)})` : !optEntry ? ' — no contract' : ' — no delta, see Details'}</span>
        <span className="inline-flex items-center gap-1.5"><span className="text-emerald-300">✔</span> crossed</span>
        <span className="inline-flex items-center gap-1.5"><span className="block h-2.5 w-2.5 rounded-full bg-rose-400" />stops</span>
      </div>
      <div className="mt-1.5 flex flex-wrap gap-x-5 gap-y-1 text-[13.5px]">
        <span className="text-[12px] font-semibold uppercase tracking-wide text-slate-500">next</span>
        {(['equity', 'future', 'option'] as const).map((i) => nextTxt(i))}
      </div>
      <div className="mt-1 flex flex-wrap gap-x-5 gap-y-1 text-[13.5px]">
        <span className="text-[12px] font-semibold uppercase tracking-wide text-slate-500">stops</span>
        {(['equity', 'future', 'option'] as const).map((i) => {
          const s = stopOf(i)
          return s ? <span key={i}><span className={col[i]}>{label[i]}</span> <span className="text-rose-300">{f(s.price)}</span> <span className="text-slate-500">({pctTxt(s.move)})</span></span> : null
        })}
      </div>
    </div>
  )
}
