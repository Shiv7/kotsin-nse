import { useEffect } from 'react'
import { usePoll } from './usePoll'

/** Which engine this page belongs to, and its twin (operator, 2026-10-03: phase 34 and phase 35 run
 * side by side — the two dashboards look alike, so each one says which it is). From data/engine.json;
 * blank name and no twin for a single engine. */
export type EngineId = { name: string; peer: { name: string } | null }

export function useEngine(): EngineId | null {
  const { data } = usePoll<EngineId>('/api/engine', 30_000)
  useEffect(() => {
    document.title = data?.name ? `${data.name} · kotsin-nse` : 'kotsin-nse'
  }, [data?.name])
  return data
}
