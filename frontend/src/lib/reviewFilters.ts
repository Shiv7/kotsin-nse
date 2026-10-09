import { contractName } from './api.ts'
import type { StopRule } from '../types.ts'

// The review page's filters, pure (operator, 2026-10-09: "add strategy and other filters on http://127.0.0.1:8501/review").
// Kept from the last visit through storage that can be missing or refuse, applied to the cards, counted on the filter icon.
// Nothing here touches React or the page, so node's own test runner checks it as it is (tests/reviewFilters.test.ts):
// the review of 10 Oct found these pinned only by its own scripts.

export type Side = 'self' | 'peer'
/** "a better peer exists": any, one whose lesson is not a refusal or a halt, or one graded systematic */
export type BetterLevel = '' | 'any' | 'lesson' | 'systematic'
export type Filters = {
  engines: 'both' | Side
  books: string[]
  variants: StopRule[]
  mirrors: boolean
  shadows: boolean
  result: string[]
  trigger: string[]
  rightDir: string[]
  exitReasons: string[]
  grades: string[]
  causes: string[]
  search: string
  from: string
  to: string
  better: BetterLevel
  /** false: hide the cards whose own result rests on a refused paper exit */
  artefacts: boolean
}

/** What a filter reads of a card: the review's own fields, and the engine it came from. */
export type FilterRow = {
  origin: Side
  book: string
  label?: string
  sourceBook?: string
  sourceLabel?: string
  variant?: StopRule
  mirror?: boolean
  shadow?: boolean
  sourceShadow?: boolean
  status: 'OPEN' | 'CLOSED'
  net: number | null
  triggerDirection?: string | null
  exitReason: string | null
  grade: string | null
  cause: { key: string; label: string }
  openedTs: number
  symbol: string
  contract: string | null
  directionRead?: { label: { tradeRight: boolean | null } }
  peers?: {
    betterExists: boolean
    scripCode?: string
    selfRefusal?: { artefact: boolean } | null
    better: { meaningful: boolean; lesson: { key: string; grade: string | null } }[]
  } | null
}

export const STORE = 'kn.review.filters.v1'
export const BLANK: Filters = {
  engines: 'self', books: [], variants: [], mirrors: false, shadows: true, result: [], trigger: [], rightDir: [], exitReasons: [], grades: [],
  causes: [], search: '', from: '', to: '', better: '', artefacts: true,
}
/** The page opens on the losses, as it always has. */
export const FIRST: Filters = { ...BLANK, result: ['loss'] }
/** The result buttons on the page's header: a filter shown there is not counted again on the icon. */
export const QUICK = [
  ['losses', 'Losses', ['loss']],
  ['all', 'All', []],
  ['open', 'Open', ['open']],
  ['wins', 'Wins', ['win']],
] as const
export const REVIEW_MAX_SESSIONS = 10
const BETTER_LEVELS: BetterLevel[] = ['', 'any', 'lesson', 'systematic']

type StorageLike = { getItem: (k: string) => string | null; setItem: (k: string, v: string) => void }

/** The filters kept from the last visit. Storage can be missing or refuse (a private window): the page works without it. A
 *  saved field is kept only with the type it should have (the Options page's rule: a hand-edited {"books": null} must never
 *  take the page down). ``storage`` is for the tests; the page reads the browser's. */
export function loadFilters(storage?: StorageLike | null): Filters {
  try {
    const st = storage === undefined ? window.localStorage : storage
    const raw = st ? st.getItem(STORE) : null
    const saved: unknown = raw ? JSON.parse(raw) : null
    if (!saved || typeof saved !== 'object' || Array.isArray(saved)) return FIRST
    const s = saved as Record<string, unknown>
    const out: Record<string, unknown> = { ...BLANK }
    for (const [k, want] of Object.entries(BLANK)) {
      const v = s[k]
      if (Array.isArray(want) ? Array.isArray(v) && v.every((x) => typeof x === 'string') : typeof v === typeof want) out[k] = v
    }
    if (!['both', 'self', 'peer'].includes(out.engines as string)) out.engines = 'self'
    if (!BETTER_LEVELS.includes(out.better as BetterLevel)) out.better = ''
    return out as Filters
  } catch {
    return FIRST
  }
}

export function saveFilters(f: Filters, storage?: StorageLike | null) {
  try {
    const st = storage === undefined ? window.localStorage : storage
    st?.setItem(STORE, JSON.stringify(f))
  } catch {
    /* not kept: the filters still apply on this page */
  }
}

/** The result preset the header's buttons show for these filters, if any. */
export function quickOf(f: Filters): (typeof QUICK)[number][0] | undefined {
  return QUICK.find(([, , r]) => r.length === f.result.length && r.every((x) => f.result.includes(x)))?.[0]
}

/** How many filters hide cards beyond the page's own view (this engine, the twins on their trade's row, these days, the
 *  result its header buttons show). A first visit shows none: "Losses" is on the header, not in the count. */
export function activeCount(f: Filters, ranged: boolean): number {
  const lists = [f.books, f.variants, f.trigger, f.rightDir, f.exitReasons, f.grades, f.causes].filter((x) => x.length).length
  return lists + (f.result.length && !quickOf(f) ? 1 : 0) + (f.engines !== 'self' ? 1 : 0) + (f.mirrors ? 1 : 0) + (!f.shadows ? 1 : 0)
    + (f.from || f.to ? 1 : 0) + (f.search.trim() ? 1 : 0) + (f.better ? 1 : 0) + (!f.artefacts ? 1 : 0) + (ranged ? 1 : 0)
}

export const IST_S = 19800 // the clock reads UTC; shift so it shows IST
export const hm = (ts: number | null | undefined) => (ts ? new Date((ts + IST_S) * 1000).toISOString().slice(11, 16) : '—')
export const resultOf = (t: FilterRow) => (t.status === 'OPEN' ? 'open' : (t.net ?? 0) >= 0 ? 'win' : 'loss')
export const rightOf = (t: FilterRow) => {
  const r = t.directionRead?.label.tradeRight
  return r === true ? 'right' : r === false ? 'wrong' : 'neither'
}

/** The better peer the card's chip names: the first that made meaningfully more (the server lists it first). */
export function bestBetter<T extends FilterRow>(t: T): NonNullable<T['peers']>['better'][number] | undefined {
  return t.peers?.betterExists ? t.peers.better.find((g) => g.meaningful) : undefined
}

/** A better peer's lesson as the chip and the filter read it: a refusal or a halt is never a lesson. */
export function lessonLevel(g: { lesson: { key: string; grade: string | null } } | undefined): 'artefact' | 'systematic' | 'suggestive' | 'one-off' | null {
  if (!g) return null
  if (g.lesson.key.startsWith('artefact')) return 'artefact'
  const grade = g.lesson.grade ?? ''
  return grade.startsWith('systematic') ? 'systematic' : grade === 'suggestive' ? 'suggestive' : 'one-off'
}

function betterPasses(t: FilterRow, level: BetterLevel): boolean {
  if (!level) return true
  const groups = (t.peers?.better ?? []).filter((g) => g.meaningful)
  if (!t.peers?.betterExists || !groups.length) return false
  if (level === 'any') return true
  const levels = groups.map(lessonLevel)
  return level === 'lesson' ? levels.some((x) => x !== 'artefact') : levels.includes('systematic')
}

export function passes(t: FilterRow, f: Filters, opts: { skipResult?: boolean } = {}): boolean {
  if (f.engines !== 'both' && t.origin !== f.engines) return false
  if (f.books.length && !f.books.includes(t.sourceBook || t.book)) return false
  if (f.variants.length && !f.variants.includes(t.variant ?? 'current')) return false
  if (!f.mirrors && t.mirror) return false
  // a twin goes with its book: hidden with the shadows when the book it copies is one (the Options page's rule)
  if (!f.shadows && (t.mirror ? (t.sourceShadow ?? t.shadow) : t.shadow)) return false
  if (!opts.skipResult && f.result.length && !f.result.includes(resultOf(t))) return false
  if (f.trigger.length && !f.trigger.includes(t.triggerDirection ?? '')) return false
  if (f.rightDir.length && !f.rightDir.includes(rightOf(t))) return false
  if (f.exitReasons.length && !f.exitReasons.includes(t.exitReason ?? 'open')) return false
  if (f.grades.length && !f.grades.includes(t.grade ?? '—')) return false
  if (f.causes.length && !f.causes.includes(t.cause.key)) return false
  if (!betterPasses(t, f.better)) return false
  if (!f.artefacts && t.peers?.selfRefusal?.artefact) return false
  const at = hm(t.openedTs)
  if (f.from && at < f.from) return false
  if (f.to && at > f.to) return false
  // every word, in any order: "270 CE" finds "LTF 27 OCT 2026 CE 270"; the option's scrip code finds it too
  const words = f.search.trim().toLowerCase().split(/[\s,]+/).filter(Boolean)
  const hay = `${t.symbol} ${t.contract ?? ''} ${contractName(t.contract) ?? ''} ${t.peers?.scripCode ?? ''}`.toLowerCase()
  if (words.length && !words.every((w) => hay.includes(w))) return false
  return true
}

/** The chips each list offers: what the cards hold, and every value a saved filter holds even when no card of these days
 *  has it — a saved HALT on a day without one would otherwise hide every card with no chip left to clear it. */
export function chipOptions(rows: FilterRow[], f: Filters) {
  const books = new Map<string, string>()
  const reasons = new Set<string>()
  const grades = new Set<string>()
  const causes = new Map<string, string>()
  for (const r of rows) {
    books.set(r.sourceBook || r.book, r.sourceLabel || r.label || r.book)
    reasons.add(r.exitReason ?? 'open')
    grades.add(r.grade ?? '—')
    causes.set(r.cause.key, r.cause.label)
  }
  for (const b of f.books) if (!books.has(b)) books.set(b, b)
  for (const x of f.exitReasons) reasons.add(x)
  for (const x of f.grades) grades.add(x)
  for (const x of f.causes) if (!causes.has(x)) causes.set(x, x)
  return {
    books: [...books.entries()].sort((a, b) => a[1].localeCompare(b[1])), reasons: [...reasons].sort(), grades: [...grades].sort(),
    causes: [...causes.entries()].sort((a, b) => a[1].localeCompare(b[1])),
  }
}

/** The last day a range from ``day`` may end on: its tenth session (weekdays, as the server counts them), and never after
 *  ``today``. Both YYYY-MM-DD. */
export function maxTo(day: string, today: string): string {
  const d = new Date(`${day}T00:00:00Z`)
  if (Number.isNaN(d.getTime())) return today
  let sessions = 0
  let last = day
  for (let i = 0; i < 3 * REVIEW_MAX_SESSIONS && sessions < REVIEW_MAX_SESSIONS; i++) {
    const cur = new Date(d.getTime() + i * 86_400_000)
    const wd = cur.getUTCDay()
    if (wd === 0 || wd === 6) continue
    sessions += 1
    last = cur.toISOString().slice(0, 10)
  }
  // the weekend after the tenth session adds no session: the range may run on through it
  const end = new Date(`${last}T00:00:00Z`)
  if (end.getUTCDay() === 5) last = new Date(end.getTime() + 2 * 86_400_000).toISOString().slice(0, 10)
  return last < today ? last : today
}
