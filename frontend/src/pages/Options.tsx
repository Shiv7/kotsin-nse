import { useState } from 'react'
import { LiveTradeCards } from '../components/LiveTradeCards'
import { OptionChain } from '../components/OptionChain'
import { cls } from '../lib/api'
import { useEngine } from '../lib/engine'

/**
 * Options: the active trades first (operator, 2026-10-09: "in the dashabpord have a dedicated page for all actove trades
 * (http://127.0.0.1:8501/options and similar for 8500) and in the form of cards"), both engines' on either engine's page;
 * the option chain, as it was, on the second tab.
 */
export function Options() {
  const engine = useEngine()
  const [tab, setTab] = useState<'trades' | 'chain'>('trades')
  return (
    <div>
      <div className="flex items-center gap-1.5 px-4 pt-4">
        {([['trades', 'Active trades'], ['chain', 'Option chain']] as const).map(([k, label]) => (
          <button
            key={k}
            type="button"
            onClick={() => setTab(k)}
            className={cls('rounded px-3 py-1 text-sm', tab === k ? 'bg-slate-700 text-white' : 'bg-slate-900 text-slate-400 hover:text-white')}
          >
            {label}
          </button>
        ))}
      </div>
      {tab === 'trades' ? <LiveTradeCards engine={engine} /> : <OptionChain />}
    </div>
  )
}
