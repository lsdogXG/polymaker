import { useState, useEffect, useRef, useCallback } from 'react'

// ========================================
// WEBSOCKET HOOK
// ========================================
function useWebSocket(url) {
  const [data, setData] = useState(null)
  const [status, setStatus] = useState('disconnected')
  const wsRef = useRef(null)
  const reconnectTimeoutRef = useRef(null)

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return

    setStatus('connecting')
    const ws = new WebSocket(url)

    ws.onopen = () => {
      setStatus('connected')
      console.log('[WS] Connected')
    }

    ws.onmessage = (event) => {
      try {
        const parsed = JSON.parse(event.data)
        if (parsed.type !== 'ping') {
          setData(parsed)
        }
      } catch (e) {
        console.warn('[WS] Parse error:', e)
      }
    }

    ws.onclose = () => {
      setStatus('disconnected')
      console.log('[WS] Disconnected, reconnecting in 1s...')
      reconnectTimeoutRef.current = setTimeout(connect, 1000)
    }

    ws.onerror = (err) => {
      console.error('[WS] Error:', err)
      ws.close()
    }

    wsRef.current = ws
  }, [url])

  useEffect(() => {
    connect()
    return () => {
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current)
      }
      if (wsRef.current) {
        wsRef.current.close()
      }
    }
  }, [connect])

  return { data, status }
}

// ========================================
// UTILITY FUNCTIONS
// ========================================
function formatNumber(num, decimals = 2) {
  if (num === null || num === undefined) return '—'
  return Number(num).toFixed(decimals)
}

function formatPrice(num) {
  if (num === null || num === undefined) return '—'
  return '$' + Number(num).toFixed(4)
}

function formatHash(hash) {
  if (!hash || hash.length < 12) return hash || '—'
  return `${hash.slice(0, 6)}...${hash.slice(-4)}`
}

function formatUptime(seconds) {
  if (!seconds) return '00:00:00'
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = seconds % 60
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

// ========================================
// HUD DECORATION COMPONENT
// ========================================
function HudDecoration({ count = 8 }) {
  return (
    <div className="hud-decoration">
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} className={`hud-tick ${i % 3 === 0 ? 'active' : ''}`} />
      ))}
    </div>
  )
}

// ========================================
// POSITIONS PANEL
// ========================================
function PositionsPanel({ positions }) {
  const up = positions?.UP || { qty: 0, avg_price: 0, cost: 0, pnl: 0 }
  const down = positions?.DOWN || { qty: 0, avg_price: 0, cost: 0, pnl: 0 }

  const maxQty = Math.max(up.qty, down.qty, 1)
  const upWidth = (up.qty / maxQty) * 100
  const downWidth = (down.qty / maxQty) * 100

  return (
    <div className="terminal-panel relative">
      <div className="panel-header">POSITIONS</div>
      <div className="p-4 pr-8 space-y-4">
        {/* UP Position */}
        <div className="space-y-2">
          <div className="flex justify-between items-center text-xs">
            <span className="text-green font-semibold tracking-wide">▲ UP</span>
            <div className="flex gap-6 text-right tabular">
              <span className="text-muted">QTY: <span className="text-primary">{formatNumber(up.qty, 0)}</span></span>
              <span className="text-muted">AVG: <span className="text-primary">{formatPrice(up.avg_price)}</span></span>
              <span className="text-muted">COST: <span className="text-primary">${formatNumber(up.cost)}</span></span>
              <span className="text-muted">PNL: <span className={up.pnl >= 0 ? 'text-green glow-green' : 'text-red glow-red'}>${formatNumber(up.pnl)}</span></span>
            </div>
          </div>
          <div className="position-bar">
            <div className="position-bar-fill up" style={{ width: `${upWidth}%` }} />
          </div>
        </div>

        {/* DOWN Position */}
        <div className="space-y-2">
          <div className="flex justify-between items-center text-xs">
            <span className="text-red font-semibold tracking-wide">▼ DOWN</span>
            <div className="flex gap-6 text-right tabular">
              <span className="text-muted">QTY: <span className="text-primary">{formatNumber(down.qty, 0)}</span></span>
              <span className="text-muted">AVG: <span className="text-primary">{formatPrice(down.avg_price)}</span></span>
              <span className="text-muted">COST: <span className="text-primary">${formatNumber(down.cost)}</span></span>
              <span className="text-muted">PNL: <span className={down.pnl >= 0 ? 'text-green glow-green' : 'text-red glow-red'}>${formatNumber(down.pnl)}</span></span>
            </div>
          </div>
          <div className="position-bar">
            <div className="position-bar-fill down" style={{ width: `${downWidth}%` }} />
          </div>
        </div>
      </div>
      <HudDecoration />
    </div>
  )
}

// ========================================
// MARKET ANALYSIS PANEL
// ========================================
function MarketAnalysisPanel({ market }) {
  const m = market || {
    up_price: 0,
    down_price: 0,
    combined: 0,
    spread: 0,
    pairs: 0,
    delta: 0,
    total_pnl: 0,
  }

  // Heat meter based on spread (positive = opportunity)
  const spreadPercent = Math.min(Math.max((m.spread + 0.1) * 5, 0), 1) * 100
  const segments = 20
  const activeSegments = Math.floor((spreadPercent / 100) * segments)

  return (
    <div className="terminal-panel relative">
      <div className="panel-header">MARKET ANALYSIS</div>
      <div className="p-4 pr-8">
        <div className="grid grid-cols-4 gap-4 mb-4">
          {/* UP Price */}
          <div className="data-cell">
            <div className="data-label">UP Price</div>
            <div className="data-value text-green text-lg">{formatPrice(m.up_price)}</div>
          </div>

          {/* DOWN Price */}
          <div className="data-cell">
            <div className="data-label">DOWN Price</div>
            <div className="data-value text-red text-lg">{formatPrice(m.down_price)}</div>
          </div>

          {/* Combined */}
          <div className="data-cell">
            <div className="data-label">Combined</div>
            <div className="data-value text-cyan text-lg">{formatPrice(m.combined)}</div>
          </div>

          {/* Spread */}
          <div className="data-cell">
            <div className="data-label">Spread</div>
            <div className={`data-value text-lg ${m.spread >= 0 ? 'positive' : 'negative'}`}>
              {m.spread >= 0 ? '+' : ''}{formatNumber(m.spread * 100, 2)}%
            </div>
          </div>
        </div>

        {/* Heat Meter */}
        <div className="mb-4">
          <div className="flex justify-between text-xs text-muted mb-1">
            <span>ARB OPPORTUNITY</span>
            <span>{formatNumber(m.spread * 100, 3)}%</span>
          </div>
          <div className="heat-meter">
            {Array.from({ length: segments }).map((_, i) => (
              <div
                key={i}
                className={`heat-segment ${i < activeSegments ? (m.spread >= 0 ? 'hot' : 'cold') : ''}`}
              />
            ))}
          </div>
        </div>

        <div className="grid grid-cols-3 gap-4">
          {/* Pairs */}
          <div className="data-cell">
            <div className="data-label">Pairs</div>
            <div className="data-value">{m.pairs}</div>
          </div>

          {/* Delta */}
          <div className="data-cell">
            <div className="data-label">Delta</div>
            <div className={`data-value ${m.delta >= 0 ? 'text-green' : 'text-red'}`}>
              {m.delta >= 0 ? '+' : ''}{formatNumber(m.delta, 4)}
            </div>
          </div>

          {/* Total PnL */}
          <div className="data-cell">
            <div className="data-label">Total PnL</div>
            <div className={`data-value ${m.total_pnl >= 0 ? 'positive' : 'negative'}`}>
              ${formatNumber(m.total_pnl)}
            </div>
          </div>
        </div>
      </div>
      <HudDecoration count={12} />
    </div>
  )
}

// ========================================
// TRANSACTIONS PANEL
// ========================================
function TransactionsPanel({ transactions }) {
  const txList = transactions || []

  return (
    <div className="terminal-panel relative">
      <div className="panel-header">RECENT TRANSACTIONS</div>
      <div className="p-4 pr-8 max-h-64 overflow-y-auto">
        {txList.length === 0 ? (
          <div className="text-center text-muted py-8">
            <span className="blink">_</span> AWAITING TRANSACTIONS...
          </div>
        ) : (
          <table className="tx-table">
            <thead>
              <tr>
                <th>TIME</th>
                <th>SIDE</th>
                <th>PRICE</th>
                <th>SIZE</th>
                <th>TX HASH</th>
              </tr>
            </thead>
            <tbody>
              {txList.slice().reverse().map((tx, i) => (
                <tr key={i}>
                  <td className="text-muted">{tx.time}</td>
                  <td className={tx.side === 'UP' ? 'side-up' : 'side-down'}>
                    {tx.side}
                  </td>
                  <td className="tabular">{formatPrice(tx.price)}</td>
                  <td className="tabular">{formatNumber(tx.size, 0)}</td>
                  <td className="tx-hash">{formatHash(tx.tx_hash)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <HudDecoration count={6} />
    </div>
  )
}

// ========================================
// STATS FOOTER
// ========================================
function StatsFooter({ stats, status }) {
  const s = stats || {}

  return (
    <div className="terminal-panel">
      <div className="stats-bar">
        <div className="stat-item">
          <div className={`status-dot ${status === 'connected' ? '' : 'bg-red-500'}`} />
          <span className="stat-label">STATUS:</span>
          <span className="stat-value uppercase">{status}</span>
        </div>
        <div className="stat-item">
          <span className="stat-label">UPTIME:</span>
          <span className="stat-value">{formatUptime(s.uptime)}</span>
        </div>
        <div className="stat-item">
          <span className="stat-label">TRADES:</span>
          <span className="stat-value">{s.trades || 0}</span>
        </div>
        <div className="stat-item">
          <span className="stat-label">VOLUME:</span>
          <span className="stat-value">${formatNumber(s.volume)}</span>
        </div>
        <div className="stat-item">
          <span className="stat-label">BOOKS:</span>
          <span className="stat-value">{s.book_updates || 0}</span>
        </div>
        <div className="stat-item">
          <span className="stat-label">INTENTS:</span>
          <span className="stat-value">{(s.intents_fullset || 0) + (s.intents_single_leg || 0)}</span>
        </div>
        <div className="stat-item">
          <span className="stat-label">CYCLES:</span>
          <span className="stat-value">{s.active_cycles || 0}</span>
        </div>
        <div className="stat-item">
          <span className="stat-label">WALLET:</span>
          <span className="stat-value text-cyan">{formatHash(s.wallet)}</span>
        </div>
      </div>
    </div>
  )
}

// ========================================
// HEADER
// ========================================
function Header({ timestamp }) {
  return (
    <div className="flex justify-between items-center mb-4 px-2">
      <div className="flex items-center gap-3">
        <div className="text-cyan text-xl font-bold tracking-widest glow-cyan">
          POLYMARKET ARB TERMINAL
        </div>
        <div className="text-muted text-xs">v1.0.0</div>
      </div>
      <div className="flex items-center gap-4 text-xs">
        <span className="text-muted">BTC 15-MIN ROUNDS</span>
        <span className="text-cyan tabular">{timestamp || '—'}</span>
      </div>
    </div>
  )
}

// ========================================
// MAIN APP
// ========================================
export default function App() {
  const wsUrl = `ws://${window.location.hostname}:8080/ws`
  const { data, status } = useWebSocket(wsUrl)

  return (
    <div className="scanlines noise vignette min-h-screen bg-terminal-bg p-4">
      <div className="max-w-7xl mx-auto space-y-4">
        <Header timestamp={data?.timestamp} />

        {/* Positions Panel */}
        <PositionsPanel positions={data?.positions} />

        {/* Market Analysis Panel */}
        <MarketAnalysisPanel market={data?.market} />

        {/* Transactions Panel */}
        <TransactionsPanel transactions={data?.transactions} />

        {/* Footer Stats */}
        <StatsFooter stats={data?.stats} status={status} />
      </div>
    </div>
  )
}
