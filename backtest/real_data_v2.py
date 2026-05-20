"""
Cold Math Weather Bot — Real Data Backtest Engine (v2)
Direct historical backtest: tests our NWS confidence model against
actual Polymarket weather market resolutions.

No live filter bypass needed — this computes everything directly.
"""
import json
import logging
import math
import statistics
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("coldmath.backtest.real")


@dataclass
class RealBacktestTrade:
    day: int
    market_question: str
    margin_c: float
    nws_confidence: float
    entry_price: float
    position_size: float
    pnl: float
    bankroll_after: float
    won: bool


@dataclass
class RealBacktestResult:
    total_markets: int
    confidence_threshold: float
    qualifying_markets: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_pnl: float
    return_pct: float
    max_drawdown: float
    sharpe_ratio: float
    final_bankroll: float
    equity_curve: list
    trades: list
    avg_margin_c: float
    min_margin_c: float
    false_positives: int  # Markets where NWS said high confidence but we lost
    false_negatives: int  # Markets where NWS said low confidence but would have won


class RealDataBacktesterV2:
    """
    Direct historical backtest using verified Polymarket weather markets.
    
    For each market, we:
    1. Compute NWS confidence from temperature margin vs NWS RMSE
    2. Apply our confidence threshold filter
    3. Kelly-size the position
    4. Check actual resolution (did temp exceed threshold?)
    5. Calculate P&L
    
    No time-gate needed — these are historical markets with known outcomes.
    """
    
    NWS_RMSE_C = 1.5  # Empirical NWS RMSE for 24h temperature forecasts
    
    def __init__(self, starting_bankroll: float = 25.0,
                 kelly_fraction: float = 0.25,
                 rmse_c: float = 1.5):
        self.starting_bankroll = starting_bankroll
        self.kelly_fraction = kelly_fraction
        self.rmse_c = rmse_c
    
    def compute_nws_confidence(self, margin_c: float) -> float:
        """Gaussian CDF: P(temp stays on correct side of threshold)."""
        z = abs(margin_c) / self.rmse_c
        return min(0.9999, 0.5 * (1 + math.erf(z / math.sqrt(2))))
    
    def kelly_size(self, bankroll: float, win_prob: float, 
                   entry_price: float) -> float:
        """Kelly criterion position sizing."""
        b = (1 - entry_price) / entry_price  # Net odds
        p = win_prob
        q = 1 - p
        f = (b * p - q) / b if b > 0 else 0
        f = max(0, f)
        # Quarter-Kelly for safety
        return bankroll * f * self.kelly_fraction
    
    def run(self, data_path: str, confidence_threshold: float = 0.99,
            min_entry_price: float = 0.05, max_entry_price: float = 0.96) -> RealBacktestResult:
        """Run backtest at a given confidence threshold."""
        
        with open(data_path) as f:
            markets = json.load(f)
        
        # Dedup
        seen = set()
        unique = []
        for m in markets:
            q = m.get("question", "")
            if q and q not in seen:
                seen.add(q)
                unique.append(m)
        markets = unique
        
        bankroll = self.starting_bankroll
        max_bankroll = bankroll
        max_drawdown = 0.0
        equity_curve = [bankroll]
        trades = []
        
        false_positives = 0  # High conf, lost
        false_negatives = 0  # Low conf, would have won
        
        for i, m in enumerate(markets):
            margin_c = abs(m.get("margin_c", 0))
            nws_conf = self.compute_nws_confidence(margin_c)
            
            # Determine actual outcome
            # actual_won_yes=True → YES won → temp exceeded → our NO bet LOSES
            actual_yes = m.get("actual_won_yes", m.get("actual_yes", None))
            
            # If we can't determine outcome, skip
            if actual_yes is None:
                # Try inference from margin: if margin > 0 and question says "X°C or more",
                # and margin is negative, then actual exceeded
                # For now, skip unknowns
                if nws_conf < confidence_threshold:
                    false_negatives += 1
                continue
            
            our_side_won = not actual_yes  # We buy NO
            
            # Check confidence filter
            if nws_conf < confidence_threshold:
                # Would have been filtered out
                if our_side_won:
                    false_negatives += 1
                continue
            
            # Entry price: NO contract price ≈ market implied probability of NO
            # Which ≈ NWS confidence, but market is slightly less confident (our edge)
            edge = nws_conf - 0.50  # How much better than coin flip
            entry_price = nws_conf - (edge * 0.1)  # Market underestimates by 10% of edge
            entry_price = max(min_entry_price, min(max_entry_price, entry_price))
            
            # Position size (quarter-Kelly)
            position_size = self.kelly_size(bankroll, nws_conf, entry_price)
            position_size = max(0.10, min(bankroll * 0.25, position_size))  # Cap at 25% of bankroll
            
            shares = position_size / entry_price
            
            # Calculate P&L
            if our_side_won:
                pnl = shares * (1.0 - entry_price)
            else:
                pnl = -position_size
                false_positives += 1
            
            bankroll += pnl
            bankroll = max(0.01, bankroll)  # Floor at 1 cent
            
            if bankroll > max_bankroll:
                max_bankroll = bankroll
            dd = (max_bankroll - bankroll) / max_bankroll if max_bankroll > 0 else 0
            max_drawdown = max(max_drawdown, dd)
            
            trades.append(RealBacktestTrade(
                day=i,
                market_question=m.get("question", "")[:80],
                margin_c=round(margin_c, 2),
                nws_confidence=round(nws_conf, 4),
                entry_price=round(entry_price, 4),
                position_size=round(position_size, 4),
                pnl=round(pnl, 4),
                bankroll_after=round(bankroll, 2),
                won=our_side_won
            ))
            
            equity_curve.append(round(bankroll, 2))
            
            if bankroll < 0.01:
                logger.warning(f"Bankroll exhausted at market {i}")
                break
        
        # Compile results
        wins = [t for t in trades if t.won]
        losses = [t for t in trades if not t.won]
        pnls = [t.pnl for t in trades]
        
        avg_margin = statistics.mean([t.margin_c for t in trades]) if trades else 0
        min_margin = min([t.margin_c for t in trades]) if trades else 0
        
        if len(pnls) > 1:
            std = statistics.stdev(pnls)
            sharpe = (statistics.mean(pnls) / std) * (252 ** 0.5) if std > 0 else 0
        elif len(pnls) == 1:
            sharpe = 0
        else:
            sharpe = 0
        
        return RealBacktestResult(
            total_markets=len(markets),
            confidence_threshold=confidence_threshold,
            qualifying_markets=len(trades),
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate=len(wins) / len(trades) if trades else 0,
            total_pnl=round(sum(pnls), 2),
            return_pct=round((bankroll - self.starting_bankroll) / self.starting_bankroll, 4) if self.starting_bankroll > 0 else 0,
            max_drawdown=round(max_drawdown, 4),
            sharpe_ratio=round(sharpe, 2),
            final_bankroll=round(bankroll, 2),
            equity_curve=equity_curve,
            trades=[asdict(t) for t in trades],
            avg_margin_c=round(avg_margin, 2),
            min_margin_c=round(min_margin, 2),
            false_positives=false_positives,
            false_negatives=false_negatives
        )
    
    def run_sensitivity(self, data_path: str) -> dict:
        """Run across multiple confidence thresholds."""
        results = {}
        for threshold in [0.80, 0.85, 0.90, 0.95, 0.97, 0.99, 0.995, 0.999]:
            r = self.run(data_path, confidence_threshold=threshold)
            results[f"conf_{threshold:.3f}"] = {
                "threshold": threshold,
                "qualifying": r.qualifying_markets,
                "wins": r.winning_trades,
                "losses": r.losing_trades,
                "win_rate": round(r.win_rate, 4),
                "total_pnl": r.total_pnl,
                "return_pct": r.return_pct,
                "max_drawdown": r.max_drawdown,
                "sharpe": r.sharpe_ratio,
                "final_bankroll": r.final_bankroll,
                "avg_margin": r.avg_margin_c,
                "min_margin": r.min_margin_c,
                "false_positives": r.false_positives,
                "false_negatives": r.false_negatives,
            }
        return results
    
    def run_monte_carlo(self, data_path: str, num_runs: int = 100,
                        confidence_threshold: float = 0.99) -> list[RealBacktestResult]:
        """Monte Carlo: bootstrap resample markets with replacement."""
        with open(data_path) as f:
            markets = json.load(f)
        
        results = []
        for run_idx in range(num_runs):
            # Bootstrap: resample with replacement
            import random
            resampled = random.choices(markets, k=len(markets))
            
            # Save temp file
            tmp_path = f"/tmp/coldmath_mc_run_{run_idx}.json"
            with open(tmp_path, "w") as f:
                json.dump(resampled, f)
            
            result = self.run(tmp_path, confidence_threshold)
            results.append(result)
        
        return results


def format_real_report(result: RealBacktestResult) -> str:
    """Format real-data backtest results."""
    lines = [
        "╔══════════════════════════════════════════════╗",
        "║ ❄️ COLD MATH REAL-DATA BACKTEST ❄️          ║",
        "╠══════════════════════════════════════════════╣",
        f"║ Total markets:       {result.total_markets:>10}           ║",
        f"║ Confidence ≥:        {result.confidence_threshold:>9.1%}           ║",
        f"║ Qualifying markets:  {result.qualifying_markets:>10}           ║",
        "╠══════════════════════════════════════════════╣",
        f"║ Winning trades:      {result.winning_trades:>10}           ║",
        f"║ Losing trades:       {result.losing_trades:>10}           ║",
        f"║ Win rate:            {result.win_rate:>9.1%}           ║",
        "╠══════════════════════════════════════════════╣",
        f"║ Starting bankroll:   ${25.00:>9.2f}           ║",
        f"║ Final bankroll:      ${result.final_bankroll:>9.2f}           ║",
        f"║ Total P&L:           ${result.total_pnl:>9.2f}           ║",
        f"║ Return:              {result.return_pct:>9.1%}           ║",
        f"║ Max drawdown:        {result.max_drawdown:>9.2%}           ║",
        f"║ Sharpe ratio:        {result.sharpe_ratio:>10.2f}          ║",
        "╠══════════════════════════════════════════════╣",
        f"║ Avg margin:          {result.avg_margin_c:>7.2f}°C          ║",
        f"║ Min margin:          {result.min_margin_c:>7.2f}°C          ║",
        f"║ False positives:     {result.false_positives:>10}  (high conf, lost) ║",
        f"║ False negatives:     {result.false_negatives:>10}  (low conf, won)  ║",
        "╚══════════════════════════════════════════════╝",
    ]
    return "\n".join(lines)
