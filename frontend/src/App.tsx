import { BrowserRouter, NavLink, Route, Routes, useLocation } from 'react-router-dom'
import { ErrorBoundary } from './components/ErrorBoundary'
import { ModeBanner } from './components/ModeBanner'
import { usePoll } from './lib/usePoll'
import { AllStrategies } from './pages/AllStrategies'
import { Backtest } from './pages/Backtest'
import { Chart } from './pages/Chart'
import { Committee } from './pages/Committee'
import { HotStocks } from './pages/HotStocks'
import { Options } from './pages/Options'
import { Overview } from './pages/Overview'
import { Risk } from './pages/Risk'
import { Signals } from './pages/Signals'
import { Strategies } from './pages/Strategies'
import { System } from './pages/System'
import { Trades } from './pages/Trades'
import { Universe } from './pages/Universe'
import type { Health } from './types'

const PAGES = [
  ['/', 'Overview'],
  ['/signals', 'Signals'],
  ['/trades', 'Trades'],
  ['/strategies', 'Strategies'],
  ['/chart', 'Chart'],
  ['/options', 'Options'],
  ['/backtest', 'Backtest'],
  ['/committee', 'Committee'],
  ['/hot-stocks', 'Hot Stocks'],
  ['/all-strategies', 'All Strategies'],
  ['/universe', 'Universe'],
  ['/risk', 'Risk'],
  ['/system', 'System'],
] as const

function Pages() {
  const { pathname } = useLocation()
  return (
    <ErrorBoundary page={pathname}>
      <Routes>
        <Route path="/" element={<Overview />} />
        <Route path="/signals" element={<Signals />} />
        <Route path="/trades" element={<Trades />} />
        <Route path="/strategies" element={<Strategies />} />
        <Route path="/chart" element={<Chart />} />
        <Route path="/options" element={<Options />} />
        <Route path="/backtest" element={<Backtest />} />
        <Route path="/committee" element={<Committee />} />
        <Route path="/hot-stocks" element={<HotStocks />} />
        <Route path="/all-strategies" element={<AllStrategies />} />
        <Route path="/universe" element={<Universe />} />
        <Route path="/risk" element={<Risk />} />
        <Route path="/system" element={<System />} />
      </Routes>
    </ErrorBoundary>
  )
}

export default function App() {
  const { data: health, error } = usePoll<Health>('/api/health', 4000)
  return (
    <BrowserRouter>
      <ModeBanner health={health} error={error} />
      <div className="flex min-h-screen">
        <nav className="w-40 shrink-0 space-y-1 border-r border-slate-800 p-3">
          <div className="px-2 pb-3 text-sm font-bold tracking-wide text-slate-200">kotsin-nse</div>
          {PAGES.map(([to, label]) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/'}
              className={({ isActive }) =>
                `block rounded px-2 py-1 text-sm ${isActive ? 'bg-slate-800 text-white' : 'text-slate-400 hover:text-white'}`
              }
            >
              {label}
            </NavLink>
          ))}
          <div className="px-2 pt-4 text-[10px] leading-relaxed text-slate-600">
            FUDKII · FUKAA
            <br />
            NSE + MCX
          </div>
        </nav>
        <main className="min-w-0 flex-1">
          <Pages />
        </main>
      </div>
    </BrowserRouter>
  )
}
