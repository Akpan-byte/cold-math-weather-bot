"""
Cold Math Weather Bot — Real-Time Dashboard
Self-contained HTML dashboard served via Python HTTP server.
Shows: equity curve, trade history, NWS confidence, engine status, circuit breaker state.
"""
import json
import logging
import sqlite3
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional

from core.config import ColdMathConfig
from core.engine import ColdMathEngine

logger = logging.getLogger("coldmath.dashboard")


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>❄️ Cold Math Weather Bot</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { 
    background: #0a0e17; color: #c8d6e5; font-family: 'JetBrains Mono', 'Fira Code', monospace;
    min-height: 100vh; overflow-x: hidden;
}
.container { max-width: 1400px; margin: 0 auto; padding: 20px; }
header { 
    text-align: center; padding: 30px 0 20px; border-bottom: 1px solid #1a2332;
    background: linear-gradient(180deg, #0d1321 0%, transparent 100%);
}
header h1 { 
    font-size: 2.2em; color: #4fc3f7; letter-spacing: 4px;
    text-shadow: 0 0 20px rgba(79,195,247,0.3);
}
header .subtitle { color: #546e7a; font-size: 0.85em; margin-top: 8px; letter-spacing: 2px; }
.stats-grid { 
    display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); 
    gap: 12px; margin: 20px 0; 
}
.stat-card {
    background: linear-gradient(135deg, #111827 0%, #1a2332 100%);
    border: 1px solid #1e3a5f; border-radius: 8px; padding: 16px;
    position: relative; overflow: hidden;
}
.stat-card::before {
    content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
    background: linear-gradient(90deg, #4fc3f7, #00b0ff, #4fc3f7);
}
.stat-label { color: #546e7a; font-size: 0.7em; text-transform: uppercase; letter-spacing: 1px; }
.stat-value { color: #e1f5fe; font-size: 1.5em; margin-top: 4px; font-weight: bold; }
.stat-value.positive { color: #4caf50; }
.stat-value.negative { color: #f44336; }
.stat-value.ice { color: #4fc3f7; }
.section { 
    background: #111827; border: 1px solid #1a2332; border-radius: 8px; 
    padding: 20px; margin: 16px 0; 
}
.section-title { 
    color: #4fc3f7; font-size: 1.1em; margin-bottom: 16px; 
    display: flex; align-items: center; gap: 8px;
}
.section-title::before { content: '❄️'; }
#equity-canvas { width: 100%; height: 300px; background: #0a0e17; border-radius: 4px; }
.trade-table { width: 100%; border-collapse: collapse; font-size: 0.8em; }
.trade-table th { 
    color: #546e7a; text-align: left; padding: 8px; border-bottom: 1px solid #1a2332;
    text-transform: uppercase; letter-spacing: 1px; font-size: 0.85em;
}
.trade-table td { padding: 8px; border-bottom: 1px solid #111827; }
.trade-table tr:hover { background: #1a2332; }
.trade-table .won { color: #4caf50; }
.trade-table .lost { color: #f44336; }
.trade-table .pending { color: #ffb74d; }
.status-indicator { 
    display: inline-block; width: 10px; height: 10px; border-radius: 50%; 
    margin-right: 6px; animation: pulse 2s infinite;
}
.status-indicator.active { background: #4caf50; box-shadow: 0 0 8px #4caf50; }
.status-indicator.paused { background: #ffb74d; box-shadow: 0 0 8px #ffb74d; }
.status-indicator.stopped { background: #f44336; box-shadow: 0 0 8px #f44336; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
.filter-bar { 
    display: flex; gap: 8px; flex-wrap: wrap; margin: 12px 0; 
}
.filter-tag { 
    background: #1a2332; border: 1px solid #2a3a4f; border-radius: 4px; 
    padding: 4px 10px; font-size: 0.75em; color: #78909c;
}
.filter-tag .count { color: #4fc3f7; font-weight: bold; }
.nws-bar { 
    height: 6px; background: #1a2332; border-radius: 3px; margin-top: 4px;
    overflow: hidden;
}
.nws-bar-fill { height: 100%; border-radius: 3px; transition: width 0.5s ease; }
.nws-bar-fill.high { background: linear-gradient(90deg, #4caf50, #66bb6a); }
.nws-bar-fill.medium { background: linear-gradient(90deg, #ffb74d, #ffa726); }
.nws-bar-fill.low { background: linear-gradient(90deg, #f44336, #ef5350); }
.circuit-breaker {
    background: linear-gradient(135deg, #1a0000, #330000);
    border: 1px solid #5c0000; border-radius: 8px;
    padding: 16px; margin: 12px 0; text-align: center;
    color: #f44336; font-size: 1.1em; font-weight: bold;
}
.circuit-breaker.inactive { display: none; }
.footer { text-align: center; padding: 30px 0; color: #37474f; font-size: 0.75em; }
</style>
</head>
<body>
<div class="container">
    <header>
        <h1>❄️ COLD MATH</h1>
        <div class="subtitle">WEATHER BOT — PREDICTION MARKET ARBITRAGE</div>
    </header>
    
    <div class="stats-grid" id="stats-grid">
        <!-- Populated by JS -->
    </div>
    
    <div class="circuit-breaker inactive" id="circuit-breaker">
        🚨 CIRCUIT BREAKER ACTIVE — TRADING PAUSED
    </div>
    
    <div class="section">
        <div class="section-title">Equity Curve</div>
        <canvas id="equity-canvas"></canvas>
    </div>
    
    <div class="section">
        <div class="section-title">Filter Breakdown</div>
        <div class="filter-bar" id="filter-bar"></div>
    </div>
    
    <div class="section">
        <div class="section-title">Recent Trades</div>
        <table class="trade-table">
            <thead>
                <tr>
                    <th>Time</th>
                    <th>Market</th>
                    <th>Side</th>
                    <th>Entry</th>
                    <th>Size</th>
                    <th>NWS</th>
                    <th>Confidence</th>
                    <th>Status</th>
                    <th>P&L</th>
                </tr>
            </thead>
            <tbody id="trades-body"></tbody>
        </table>
    </div>
    
    <div class="footer">
        Cold Math Weather Bot v1.0 — NWS-powered prediction market edge | 
        <span id="last-update">Never updated</span>
    </div>
</div>

<script>
const STATS_GRID = document.getElementById('stats-grid');
const EQUITY_CANVAS = document.getElementById('equity-canvas');
const FILTER_BAR = document.getElementById('filter-bar');
const TRADES_BODY = document.getElementById('trades-body');
const CIRCUIT_BREAKER = document.getElementById('circuit-breaker');
const LAST_UPDATE = document.getElementById('last-update');

function formatMoney(v) {
    if (v === null || v === undefined) return '$0.00';
    const n = parseFloat(v);
    if (n >= 0) return '+$' + n.toFixed(2);
    return '-$' + Math.abs(n).toFixed(2);
}

function formatPct(v) { return (parseFloat(v) * 100).toFixed(1) + '%'; }

function nwsBarHTML(confidence) {
    const pct = (parseFloat(confidence) * 100).toFixed(0);
    const cls = pct >= 99 ? 'high' : pct >= 95 ? 'medium' : 'low';
    return `<div class="nws-bar"><div class="nws-bar-fill ${cls}" style="width:${pct}%"></div></div>`;
}

async function fetchData() {
    try {
        const resp = await fetch('/api/status');
        return await resp.json();
    } catch (e) {
        console.error('Fetch error:', e);
        return null;
    }
}

async function fetchEquity() {
    try {
        const resp = await fetch('/api/equity');
        return await resp.json();
    } catch (e) {
        return [];
    }
}

async function fetchTrades() {
    try {
        const resp = await fetch('/api/trades');
        return await resp.json();
    } catch (e) {
        return [];
    }
}

function renderStats(data) {
    if (!data) return;
    const bankroll = data.bankroll || 0;
    const pnl = data.total_pnl || 0;
    const cards = [
        { label: 'Bankroll', value: '$' + bankroll.toFixed(2), cls: 'ice' },
        { label: 'Total P&L', value: formatMoney(pnl), cls: pnl >= 0 ? 'positive' : 'negative' },
        { label: 'Win Rate', value: formatPct(data.win_rate || 0), cls: 'ice' },
        { label: 'Total Trades', value: (data.total_trades || 0).toString(), cls: '' },
        { label: 'Consecutive Losses', value: (data.consecutive_losses || 0).toString(), 
          cls: (data.consecutive_losses || 0) >= 3 ? 'negative' : '' },
        { label: 'Mode', value: (data.mode || 'paper').toUpperCase(), cls: 'ice' },
    ];
    STATS_GRID.innerHTML = cards.map(c => 
        `<div class="stat-card">
            <div class="stat-label">${c.label}</div>
            <div class="stat-value ${c.cls}">${c.value}</div>
        </div>`
    ).join('');
    
    // Circuit breaker
    if ((data.consecutive_losses || 0) >= 5) {
        CIRCUIT_BREAKER.classList.remove('inactive');
    } else {
        CIRCUIT_BREAKER.classList.add('inactive');
    }
}

function renderEquity(data) {
    if (!data || !data.length) return;
    const canvas = EQUITY_CANVAS;
    const ctx = canvas.getContext('2d');
    const rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * window.devicePixelRatio;
    canvas.height = rect.height * window.devicePixelRatio;
    ctx.scale(window.devicePixelRatio, window.devicePixelRatio);
    
    const w = rect.width, h = rect.height;
    const pad = { top: 20, right: 20, bottom: 30, left: 60 };
    const plotW = w - pad.left - pad.right;
    const plotH = h - pad.top - pad.bottom;
    
    const values = data.map(d => d.bankroll);
    const minV = Math.min(...values);
    const maxV = Math.max(...values);
    const rangeV = maxV - minV || 1;
    
    // Background
    ctx.fillStyle = '#0a0e17';
    ctx.fillRect(0, 0, w, h);
    
    // Grid
    ctx.strokeStyle = '#1a2332';
    ctx.lineWidth = 0.5;
    for (let i = 0; i <= 5; i++) {
        const y = pad.top + (plotH * i / 5);
        ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(pad.left + plotW, y); ctx.stroke();
        ctx.fillStyle = '#37474f';
        ctx.font = '10px monospace';
        ctx.textAlign = 'right';
        ctx.fillText('$' + (maxV - (rangeV * i / 5)).toFixed(0), pad.left - 8, y + 3);
    }
    
    // Equity line
    ctx.beginPath();
    ctx.strokeStyle = '#4fc3f7';
    ctx.lineWidth = 2;
    ctx.shadowColor = '#4fc3f7';
    ctx.shadowBlur = 8;
    
    values.forEach((v, i) => {
        const x = pad.left + (i / (values.length - 1)) * plotW;
        const y = pad.top + plotH - ((v - minV) / rangeV) * plotH;
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
    
    // Fill area
    ctx.shadowBlur = 0;
    ctx.lineTo(pad.left + plotW, pad.top + plotH);
    ctx.lineTo(pad.left, pad.top + plotH);
    ctx.closePath();
    const grad = ctx.createLinearGradient(0, pad.top, 0, pad.top + plotH);
    grad.addColorStop(0, 'rgba(79,195,247,0.15)');
    grad.addColorStop(1, 'rgba(79,195,247,0.01)');
    ctx.fillStyle = grad;
    ctx.fill();
}

function renderTrades(trades) {
    if (!trades || !trades.length) {
        TRADES_BODY.innerHTML = '<tr><td colspan="9" style="text-align:center;color:#37474f">No trades yet</td></tr>';
        return;
    }
    
    TRADES_BODY.innerHTML = trades.slice(0, 50).reverse().map(t => {
        const statusCls = t.status === 'won' ? 'won' : t.status === 'lost' ? 'lost' : 'pending';
        const pnl = t.pnl !== null ? formatMoney(t.pnl) : '—';
        return `<tr>
            <td>${(t.timestamp || '').slice(11, 19)}</td>
            <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" 
                title="${t.question || ''}">${(t.question || '').slice(0, 40)}...</td>
            <td>${t.side || ''}</td>
            <td>${parseFloat(t.entry_price || 0).toFixed(4)}</td>
            <td>$${parseFloat(t.position_size_usd || 0).toFixed(2)}</td>
            <td>${nwsBarHTML(t.nws_confidence || 0)}</td>
            <td>${formatPct(t.nws_confidence || 0)}</td>
            <td class="${statusCls}">${(t.status || '').toUpperCase()}</td>
            <td class="${statusCls}">${pnl}</td>
        </tr>`;
    }).join('');
}

async function refresh() {
    const [status, equity, trades] = await Promise.all([
        fetchData(), fetchEquity(), fetchTrades()
    ]);
    renderStats(status);
    renderEquity(equity);
    renderTrades(trades);
    LAST_UPDATE.textContent = 'Updated: ' + new Date().toLocaleTimeString();
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the dashboard."""
    
    engine: Optional[ColdMathEngine] = None  # Set by DashboardServer
    
    def do_GET(self):
        if self.path == '/' or self.path == '/index.html':
            self._send_html(DASHBOARD_HTML)
        elif self.path == '/api/status':
            self._send_json(self._get_status())
        elif self.path == '/api/equity':
            self._send_json(self._get_equity())
        elif self.path == '/api/trades':
            self._send_json(self._get_trades())
        else:
            self.send_error(404)
    
    def _send_html(self, content: str):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(content.encode())
    
    def _send_json(self, data):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str).encode())
    
    def _get_status(self) -> dict:
        if self.engine:
            return self.engine.get_status()
        return {"error": "Engine not connected"}
    
    def _get_equity(self) -> list:
        """Get equity curve from bankroll snapshots."""
        try:
            db_path = DashboardHandler.engine.cfg.db_path if DashboardHandler.engine else "/config/coldmath/data/coldmath.db"
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    "SELECT timestamp, bankroll FROM bankroll_snapshots ORDER BY id"
                )
                return [dict(row) for row in cur.fetchall()]
        except Exception:
            return []
    
    def _get_trades(self) -> list:
        """Get recent trades."""
        try:
            db_path = DashboardHandler.engine.cfg.db_path if DashboardHandler.engine else "/config/coldmath/data/coldmath.db"
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    "SELECT * FROM trades ORDER BY timestamp DESC LIMIT 50"
                )
                return [dict(row) for row in cur.fetchall()]
        except Exception:
            return []
    
    def log_message(self, format, *args):
        logger.debug(f"Dashboard: {format % args}")


class DashboardServer:
    """
    Runs the Cold Math dashboard on a local port.
    Thread-based HTTP server with graceful shutdown.
    """
    
    def __init__(self, engine: ColdMathEngine, port: int = 8199):
        self.engine = engine
        self.port = port
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
    
    def start(self):
        """Start the dashboard server in a background thread."""
        DashboardHandler.engine = self.engine
        
        self._server = HTTPServer(('0.0.0.0', self.port), DashboardHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, 
                                         daemon=True)
        self._thread.start()
        logger.info(f"❄️ Dashboard running at http://localhost:{self.port}")
    
    def stop(self):
        """Stop the dashboard server."""
        if self._server:
            self._server.shutdown()
            logger.info("Dashboard stopped")
