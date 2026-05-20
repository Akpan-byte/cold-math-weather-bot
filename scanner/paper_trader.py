#!/usr/bin/env python3
"""
Cold Math — Paper Trade Scanner v2
BIDIRECTIONAL: buys NO when forecast < threshold, buys YES when forecast > threshold.
Works with enhanced_markets.json from nws_fetcher.py.
"""
import json, math, os, sys
from datetime import datetime, timezone
from pathlib import Path

# ─── Configuration ───
CONFIG = {
    "min_nws_confidence": 0.97,
    "no_price_lo": 0.90,     # Buy NO in this price range
    "no_price_hi": 0.97,
    "yes_price_lo": 0.03,    # Buy YES in this price range (when forecast > threshold)
    "yes_price_hi": 0.10,
    "max_position": 5.0,
    "kelly_fraction": 0.25,
    "starting_bankroll": 25.0,
    "circuit_breaker_consec_losses": 2,
    "circuit_breaker_daily_pct": 0.10,
    "margin_scale": True,
    "data_dir": "/config/coldmath/data",
    "log_dir": "/config/coldmath/logs",
    "nws_rms": 0.8,
}

def nws_confidence(margin_c, rmse=None):
    rmse = rmse or CONFIG["nws_rms"]
    z = abs(margin_c) / rmse
    return min(0.9999, 0.5 * (1 + math.erf(z / math.sqrt(2))))

def size_position(confidence, entry_price, margin_c, bankroll):
    """Kelly-based position sizing with margin scaling."""
    b = (1 - entry_price) / entry_price
    if b <= 0: return 0, "invalid_price"
    
    fk = (confidence * b - (1 - confidence)) / b
    if fk <= 0: return 0, "negative_edge"
    
    raw = bankroll * fk * CONFIG["kelly_fraction"]
    
    if CONFIG["margin_scale"]:
        if abs(margin_c) < 2.5: raw *= 0.3
        elif abs(margin_c) < 4.0: raw *= 0.6
        elif abs(margin_c) < 6.0: raw *= 0.8
    
    pos = max(0.50, raw)
    pos = min(pos, CONFIG["max_position"])
    return round(pos, 2), "ok"

class PaperLedger:
    def __init__(self, data_dir=None):
        self.data_dir = Path(data_dir or CONFIG["data_dir"])
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.ledger_path = self.data_dir / "paper_trades.jsonl"
        self.state_path = self.data_dir / "paper_state.json"
        self.load_state()
    
    def load_state(self):
        if self.state_path.exists():
            with open(self.state_path) as f:
                self.state = json.load(f)
        else:
            self.state = {
                "bankroll": CONFIG["starting_bankroll"],
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "total_pnl": 0,
                "open_positions": [],
                "daily_pnl": 0,
                "consec_losses": 0,
                "no_trades": 0,
                "yes_trades": 0,
                "created": datetime.now(timezone.utc).isoformat(),
                "last_updated": None,
            }
    
    def save_state(self):
        self.state["last_updated"] = datetime.now(timezone.utc).isoformat()
        with open(self.state_path, "w") as f:
            json.dump(self.state, f, indent=2)
    
    def record_signal(self, signal):
        if signal["action"] not in ("BUY_NO", "BUY_YES"):
            return
        entry = {"type": "ENTRY", **signal, "trade_id": f"P{self.state['total_trades']+len(self.state['open_positions'])+1:04d}"}
        self.state["open_positions"].append(entry)
        self._append_ledger(entry)
        self.save_state()
    
    def record_result(self, trade_id, won, actual_temp=None):
        pos = None
        for p in self.state["open_positions"]:
            if p.get("trade_id") == trade_id:
                pos = p; break
        if not pos: return None
        
        entry_price = pos["entry_price"]
        pos_size = pos["position_size"]
        shares = pos_size / entry_price
        
        # For both BUY_NO and BUY_YES: win = payout $1/share, lose = lose stake
        if won:
            pnl = shares * (1.0 - entry_price)
        else:
            pnl = -pos_size
        
        self.state["bankroll"] += pnl
        self.state["total_pnl"] += pnl
        self.state["total_trades"] += 1
        
        if pos["action"] == "BUY_NO":
            self.state["no_trades"] = self.state.get("no_trades", 0) + 1
        else:
            self.state["yes_trades"] = self.state.get("yes_trades", 0) + 1
        
        if won:
            self.state["wins"] += 1
            self.state["consec_losses"] = 0
        else:
            self.state["losses"] += 1
            self.state["consec_losses"] += 1
        
        self.state["daily_pnl"] += pnl
        self.state["open_positions"] = [p for p in self.state["open_positions"] if p.get("trade_id") != trade_id]
        
        result = {
            "type": "RESULT", "trade_id": trade_id, "won": won,
            "pnl": round(pnl, 4), "actual_temp": actual_temp,
            "bankroll_after": round(self.state["bankroll"], 2),
            "win_rate": round(self.state["wins"] / self.state["total_trades"], 4),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._append_ledger(result)
        self.save_state()
        return result
    
    def _append_ledger(self, entry):
        with open(self.ledger_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    
    def get_status(self):
        s = self.state
        wr = s["wins"] / s["total_trades"] if s["total_trades"] > 0 else 0
        return {
            "bankroll": round(s["bankroll"], 2),
            "total_trades": s["total_trades"],
            "wins": s["wins"], "losses": s["losses"],
            "win_rate": f"{wr:.1%}",
            "total_pnl": round(s["total_pnl"], 2),
            "open_positions": len(s["open_positions"]),
            "consec_losses": s["consec_losses"],
            "return_pct": f"{(s['bankroll'] - CONFIG['starting_bankroll']) / CONFIG['starting_bankroll']:.1%}",
            "no_trades": s.get("no_trades", 0),
            "yes_trades": s.get("yes_trades", 0),
            "circuit_breaker": s["consec_losses"] >= CONFIG["circuit_breaker_consec_losses"],
        }

def scan_enhanced_markets():
    """Scan enhanced markets (with NWS forecasts) for trade signals."""
    ledger = PaperLedger()
    status = ledger.get_status()
    
    print(f"\u2744\ufe0f  Cold Math Paper Trader v2 (Bidirectional)")
    print(f"{'='*45}")
    print(f"  Bankroll: ${status['bankroll']:.2f} ({status['return_pct']})")
    print(f"  Record: {status['wins']}W / {status['losses']}L ({status['win_rate']})")
    print(f"  NO trades: {status['no_trades']} | YES trades: {status['yes_trades']}")
    print(f"  Open: {status['open_positions']}")
    
    if status["circuit_breaker"]:
        print(f"  \u26d4 CIRCUIT BREAKER — {status['consec_losses']} consecutive losses")
        return []
    
    # Load enhanced markets
    em_path = Path(CONFIG["data_dir"]) / "enhanced_markets.json"
    if not em_path.exists():
        print(f"  \u2139\ufe0f  No enhanced market data. Run nws_fetcher.py first.")
        return []
    
    with open(em_path) as f:
        markets = json.load(f)
    
    signals = []
    for m in markets:
        margin = m.get("margin_c", 0)
        conf = nws_confidence(margin)
        yes_price = m.get("yes_price", 0.5)
        no_price = 1 - yes_price
        
        if conf < CONFIG["min_nws_confidence"]:
            continue
        
        if margin > 0:
            # Forecast < threshold → buy NO
            if no_price < CONFIG["no_price_lo"] or no_price > CONFIG["no_price_hi"]:
                continue
            pos_size, reason = size_position(conf, no_price, margin, ledger.state["bankroll"])
            if pos_size <= 0: continue
            
            signal = {
                "action": "BUY_NO", "paper": True,
                "market": m.get("question", ""), "city": m.get("city", ""),
                "date": m.get("date_raw", ""),
                "nws_confidence": round(conf, 4),
                "entry_price": round(no_price, 4),
                "position_size": pos_size,
                "margin_c": round(margin, 1),
                "edge_cents": round((conf - no_price) * 100, 2),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        else:
            # Forecast > threshold → buy YES
            if yes_price < CONFIG["yes_price_lo"] or yes_price > CONFIG["yes_price_hi"]:
                continue
            pos_size, reason = size_position(conf, yes_price, margin, ledger.state["bankroll"])
            if pos_size <= 0: continue
            
            signal = {
                "action": "BUY_YES", "paper": True,
                "market": m.get("question", ""), "city": m.get("city", ""),
                "date": m.get("date_raw", ""),
                "nws_confidence": round(conf, 4),
                "entry_price": round(yes_price, 4),
                "position_size": pos_size,
                "margin_c": round(margin, 1),
                "edge_cents": round((conf - (1 - yes_price)) * 100, 2),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        
        signals.append(signal)
        ledger.record_signal(signal)
    
    print(f"\n  Signals: {len(signals)}")
    for s in signals:
        icon = "\ud83d\udcca" if s["action"] == "BUY_NO" else "\ud83d\udcc8"
        print(f"    {icon} {s['action']:7s} | {s['city']:15s} | conf={s['nws_confidence']:.1%} | entry={s['entry_price']:.3f} | ${s['position_size']:.2f}")
    
    return signals

if __name__ == "__main__":
    scan_enhanced_markets()
