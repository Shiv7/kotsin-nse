// The review page's filters (src/lib/reviewFilters.ts), run by node's own test runner: `npm test` (node ≥ 22.18 strips the
// types itself; nothing to install). The UI review of 10 Oct found passes, loadFilters, activeCount and the bad-storage
// handling pinned only by its own browser scripts.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  BLANK,
  FIRST,
  STORE,
  activeCount,
  chipOptions,
  lessonLevel,
  loadFilters,
  maxTo,
  passes,
  saveFilters,
  type FilterRow,
  type Filters,
} from '../src/lib/reviewFilters.ts'

const T = 1_791_517_500 // 9 Oct 2026 09:15 IST

function row(over: Partial<FilterRow> = {}): FilterRow {
  return {
    origin: 'self', book: 'FUDKII_RT_Y', label: 'FUDKII-RT-Y', sourceBook: 'FUDKII_RT_Y', variant: 'current', mirror: false, shadow: false,
    sourceShadow: false, status: 'CLOSED', net: -1000, triggerDirection: 'BULLISH', exitReason: 'SL-OP', grade: 'C',
    cause: { key: 'option_stop', label: 'option stop' }, openedTs: T + 600, symbol: 'LTF', contract: 'LTF 27 OCT 2026 CE 270.00',
    directionRead: { label: { tradeRight: false } }, peers: null, ...over,
  }
}

const store = (raw: string | null) => ({ getItem: () => raw, setItem: () => undefined })
const f = (over: Partial<Filters> = {}): Filters => ({ ...BLANK, ...over })

test('a saved filter of the wrong shape never takes the page down', () => {
  for (const raw of [null, 'not json', 'null', '[]', '"x"', '{"books":null}', '{"engines":"mars","better":true,"result":[1]}']) {
    const got = loadFilters(store(raw))
    assert.ok(Array.isArray(got.books) && ['self', 'peer', 'both'].includes(got.engines) && ['', 'any', 'lesson', 'systematic'].includes(got.better))
  }
  assert.deepEqual(loadFilters(store(null)), FIRST, 'a first visit opens on the losses')
  const throwing = { getItem: () => { throw new Error('blocked') }, setItem: () => { throw new Error('blocked') } }
  assert.deepEqual(loadFilters(throwing), FIRST)
  assert.doesNotThrow(() => saveFilters(BLANK, throwing))
  assert.deepEqual(loadFilters(null), FIRST, 'no storage at all')
  const kept: Record<string, string> = {}
  saveFilters(f({ books: ['FUDKII_RT_X'], better: 'lesson' }), { getItem: () => null, setItem: (k, v) => { kept[k] = v } })
  assert.deepEqual(loadFilters(store(kept[STORE])).books, ['FUDKII_RT_X'])
  assert.equal(loadFilters(store(kept[STORE])).better, 'lesson')
})

test('the icon counts what hides cards, not the result the header buttons already show', () => {
  assert.equal(activeCount(FIRST, false), 0, 'a first visit: "Losses" is a header button, not a hidden filter')
  assert.equal(activeCount(f({ result: [] }), false), 0)
  assert.equal(activeCount(f({ result: ['win', 'open'] }), false), 1, 'a mix no button shows is counted')
  assert.equal(activeCount(f({ books: ['X'], better: 'any', artefacts: false }), true), 4)
})

test('a shadow book’s twins are hidden with it', () => {
  const twin = row({ book: 'FUDKII_RT_Y_W1_SE', sourceBook: 'FUDKII_RT_Y_W1', mirror: true, variant: 'E', shadow: false, sourceShadow: true })
  const shadows = f({ mirrors: true, shadows: false })
  assert.equal(passes(twin, shadows), false)
  assert.equal(passes(row({ book: 'FUDKII_RT_Y_W1', shadow: true, sourceShadow: true }), shadows), false)
  assert.equal(passes(row({ mirror: true, variant: 'E', book: 'FUDKII_RT_Y_SE' }), shadows), true, "a trading book's twin stays")
  assert.equal(passes(twin, f({ mirrors: false })), false, 'twins hidden')
})

test('each filter keeps what it names', () => {
  assert.equal(passes(row(), f({ result: ['loss'] })), true)
  assert.equal(passes(row(), f({ result: ['win'] })), false)
  assert.equal(passes(row(), f({ rightDir: ['wrong'] })), true)
  assert.equal(passes(row(), f({ trigger: ['BEARISH'] })), false)
  assert.equal(passes(row(), f({ from: '09:20', to: '09:30' })), true)
  assert.equal(passes(row(), f({ from: '09:30' })), false)
  assert.equal(passes(row({ origin: 'peer' }), f()), false, 'this engine only')
  assert.equal(passes(row({ origin: 'peer' }), f({ engines: 'both' })), true)
  assert.equal(passes(row(), f({ search: '270 ce' })), true)
  const coded = row({ peers: { betterExists: false, scripCode: '113227', better: [] } })
  assert.equal(passes(coded, f({ search: '113227' })), true, 'the scrip code finds the option')
})

test('a better peer, with a lesson or systematic, and the paper artefacts', () => {
  const halt = { meaningful: true, lesson: { key: 'artefact_halt', grade: 'one-off / paper artefact' } }
  const sys = { meaningful: true, lesson: { key: 'early_raised_stop', grade: 'systematic (held across days and both engines)' } }
  const onlyHalt = row({ peers: { betterExists: true, better: [halt] } })
  const both = row({ peers: { betterExists: true, better: [halt, sys] } })
  assert.equal(passes(onlyHalt, f({ better: 'any' })), true)
  assert.equal(passes(onlyHalt, f({ better: 'lesson' })), false, 'a halt is never a lesson')
  assert.equal(passes(both, f({ better: 'systematic' })), true)
  assert.equal(passes(row(), f({ better: 'any' })), false)
  assert.equal(lessonLevel(halt), 'artefact')
  assert.equal(lessonLevel(sys), 'systematic')
  const artefact = row({ net: 1680, peers: { betterExists: false, better: [], selfRefusal: { artefact: true } } })
  assert.equal(passes(artefact, f({ artefacts: false })), false)
  assert.equal(passes(artefact, f()), true)
})

test('a saved value no card has still gets its chip, so it can be cleared', () => {
  const o = chipOptions([row()], f({ exitReasons: ['HALT'], grades: ['A'], causes: ['chased'], books: ['FUDKII_CT_Y'] }))
  assert.ok(o.reasons.includes('HALT') && o.reasons.includes('SL-OP'))
  assert.ok(o.grades.includes('A') && o.causes.some(([k]) => k === 'chased') && o.books.some(([k]) => k === 'FUDKII_CT_Y'))
})

test('a range ends on its tenth session and never after today', () => {
  assert.equal(maxTo('2026-09-28', '2026-12-31'), '2026-10-11', 'Mon 28 Sep + 10 sessions = Fri 9 Oct, and the weekend after it')
  assert.equal(maxTo('2026-10-08', '2026-10-10'), '2026-10-10')
  assert.equal(maxTo('garbage', '2026-10-10'), '2026-10-10')
})
