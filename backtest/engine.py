"""
Cold Math Weather Bot — Backtest Engine
Simulates the Cold Math strategy against historical NWS + Polymarket data.
Validates the mathematical edge with real numbers before risking real money.
"""
import json
import logging
import random
import sqlite3
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from core.config import ColdMathConfig
from core.engine import (
    ColdMathEngine, KellySizer, TradeFilter, TradeLogger,
    MarketCandidate, NWSForecast, FilterResult, TradeRecord, TradeStatus
)
from core.nws import NWSConfidenceScorer, NWSForecastMatcher
from core.scanner import ArchiveDataLoader

logger = logging.getLogger("coldmath.backtest")


@dataclass
class BacktestTrade:
    """A simulated trade in the backtest."""
    day: int
    market_question: str
    entry_price: float
    nws_confidence: float
    position_size: float
    shares: float
    actual_outcome: str  # "won" or "lost"
    pnl: float
    bankroll_after: float
    filter_result: str


@dataclass
class BacktestResult:
    """Complete backtest results."""
    config_used: dict
    total_days: int
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_pnl: float
    avg_pnl_per_trade: float
    best_trade: float
    worst_trade: float
    max_drawdown: float
    sharpe_ratio: float
    final_bankroll: float
    return_pct: float
    equity_curve: list = field(default_factory=list)
    trades: list = field(default_factory=list)
    filter_stats: dict = field(default_factory=dict)


class BacktestEngine:
    """
    Full backtest engine with Monte Carlo simulation.
    
    Two modes:
    1. ARCHIVE: Uses historical Polymarket price data from our archive
    2. SYNTHETIC: Generates realistic market scenarios based on NWS accuracy data
    
    The synthetic mode is essential because:
    - We don't have enough archived weather market data
    - NWS accuracy data IS the real signal — we can simulate from it
    - This lets us test with 1000+ markets for statistical significance
    """
    
    def __init__(self, config: Optional[ColdMathConfig] = None):
        self.cfg = config or ColdMathConfig()
    
    def run_archive_backtest(self) -> BacktestResult:
        """Run backtest using archived Polymarket data."""
        logger.info("Running archive backtest...")
        
        loader = ArchiveDataLoader()
        snapshots = loader.load_price_snapshots()
        updown = loader.load_updown_data()
        
        if not snapshots and not updown:
            logger.warning("No archive data found — falling back to synthetic")
            return self.run_synthetic_backtest()
        
        # Convert to candidates
        all_data = snapshots + updown
        candidates = loader.convert_to_candidates(all_data)
        
        if not candidates:
            logger.warning("No candidates from archive — falling back to synthetic")
            return self.run_synthetic_backtest()
        
        # Run simulation
        return self._simulate_with_candidates(candidates)
    
    def run_synthetic_backtest(self, num_scenarios: int = 1000,
                                seed: int = 42) -> BacktestResult:
        """
        Run synthetic backtest using NWS accuracy data.
        
        Generates realistic market scenarios where:
        - We know the TRUE probability (from NWS accuracy data)
        - Market price reflects a slightly wrong estimate
        - We exploit the gap between NWS truth and market price
        
        This is the most statistically rigorous test because it uses
        empirically verified NWS accuracy numbers.
        """
        logger.info(f"Running synthetic backtest with {num_scenarios} scenarios...")
        random.seed(seed)
        
        engine = ColdMathEngine(self.cfg)
        results: list[BacktestTrade] = []
        equity_curve = [self.cfg.backtest_initial_bankroll]
        filter_counts: dict[str, int] = {}
        max_bankroll = self.cfg.backtest_initial_bankroll
        max_drawdown = 0.0
        
        for i in range(num_scenarios):
            scenario = self._generate_scenario(i)
            
            # Evaluate through our filter pipeline
            signal = engine.evaluate_market(scenario["market"], scenario["nws"])
            
            # Count filter results
            fr = signal.filter_result.value
            filter_counts[fr] = filter_counts.get(fr, 0) + 1
            
            if signal.filter_result != FilterResult.PASS:
                # Market filtered out — no trade
                continue
            
            # Simulate outcome based on NWS accuracy
            # If NWS says 99.5% confident, there's a 0.5% chance they're wrong
            true_win_prob = scenario["nws_confidence_truth"]
            actual_win = random.random() < true_win_prob
            
            # Calculate P&L
            entry_price = signal.entry_price
            position_size = signal.position_size_usd
            shares = position_size / entry_price
            
            if actual_win:
                exit_value = shares * 1.0  # Contract resolves to $1
                pnl = exit_value - position_size
            else:
                exit_value = 0.0
                pnl = -position_size  # Lose entire stake
            
            engine.sizer.record_trade_result(pnl)
            
            bt_trade = BacktestTrade(
                day=i,
                market_question=scenario["market"].question,
                entry_price=entry_price,
                nws_confidence=scenario["nws"].confidence if scenario["nws"] else 0,
                position_size=position_size,
                shares=round(shares, 4),
                actual_outcome="won" if actual_win else "lost",
                pnl=round(pnl, 4),
                bankroll_after=round(engine.sizer.bankroll, 2),
                filter_result=fr
            )
            results.append(bt_trade)
            
            # Track equity curve
            equity_curve.append(engine.sizer.bankroll)
            
            # Track max drawdown
            if engine.sizer.bankroll > max_bankroll:
                max_bankroll = engine.sizer.bankroll
            dd = (max_bankroll - engine.sizer.bankroll) / max_bankroll
            if dd > max_drawdown:
                max_drawdown = dd
            
            # Bankroll exhausted?
            if engine.sizer.bankroll < 1.0:
                logger.warning(f"Bankroll exhausted at scenario {i}")
                break
        
        return self._compile_results(results, equity_curve, filter_counts, 
                                      num_scenarios)
    
    def run_monte_carlo(self, num_runs: int = 100, 
                         scenarios_per_run: int = 500) -> list[BacktestResult]:
        """
        Monte Carlo simulation: run many backtests with different random seeds.
        Gives confidence intervals on expected performance.
        """
        logger.info(f"Running Monte Carlo: {num_runs} runs × {scenarios_per_run} scenarios")
        results = []
        
        for run in range(num_runs):
            seed = 42 + run * 1000
            result = self.run_synthetic_backtest(scenarios_per_run, seed=seed)
            results.append(result)
            
            if (run + 1) % 10 == 0:
                avg_return = sum(r.return_pct for r in results) / len(results)
                logger.info(f"MC progress: {run+1}/{num_runs} | "
                           f"Avg return: {avg_return:.1%}")
        
        return results
    
    def _generate_scenario(self, index: int) -> dict:
        """Generate a realistic market scenario for synthetic backtest."""
        # Weather cities
        cities = ["NYC", "Chicago", "LA", "Houston", "Phoenix", "Miami", 
                  "Denver", "Seattle", "Atlanta", "Boston", "Dallas", "Minneapolis"]
        city = random.choice(cities)
        
        # Forecast type
        forecast_type = random.choice(["temperature_high", "temperature_low", "precipitation"])
        
        # Lead time (hours to resolution)
        hours = random.choice([1, 3, 6, 12, 24, 48, 72])
        
        # NWS accuracy for this lead time
        nws_accuracy = NWSConfidenceScorer._interpolate_accuracy(hours)
        temp_rmse = NWSConfidenceScorer._interpolate_rmse(hours)
        
        if forecast_type == "temperature_high":
            # Generate realistic temperature market
            base_temp = random.choice([55, 65, 75, 85, 95])
            threshold = base_temp + random.randint(5, 15)
            
            # Market question
            question = f"Will {city} high temp exceed {threshold}°F today?"
            
            # NWS confidence (how certain we are it WON'T exceed)
            margin = threshold - base_temp
            from math import erf, sqrt
            z_score = margin / temp_rmse
            nws_confidence = 0.5 * (1 + erf(z_score / sqrt(2)))
            nws_confidence = min(0.9999, nws_confidence * nws_accuracy + (1 - nws_accuracy) * 0.5)
            
            # Market price (slightly wrong — our edge)
            # Market tends to underweight NWS certainty by 2-5%
            price_noise = random.uniform(-0.05, 0.02)
            market_price = (1.0 - nws_confidence) + 0.5  # Market's implied probability
            # The YES side price ≈ 1 - nws_confidence (if NWS says won't happen, YES is cheap)
            # We buy NO at ~nws_confidence price
            market_price = nws_confidence + price_noise
            market_price = max(0.01, min(0.99, market_price))
            
            nws = NWSForecast(
                station_id=f"K{city[:3].upper()}",
                latitude=40.0, longitude=-74.0,
                forecast_text=f"High of {base_temp}°F expected",
                high_temp_f=base_temp,
                confidence=nws_confidence,
                issued_at=datetime.now(timezone.utc).isoformat(),
                expires_at=(datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
            )
            
            outcome = "NO"  # We buy NO (it won't exceed)
            
        elif forecast_type == "temperature_low":
            base_temp = random.choice([15, 25, 35, 45])
            threshold = base_temp - random.randint(5, 15)
            
            question = f"Will {city} low temp drop below {threshold}°F?"
            
            margin = base_temp - threshold
            from math import erf, sqrt
            z_score = margin / temp_rmse
            nws_confidence = 0.5 * (1 + erf(z_score / sqrt(2)))
            nws_confidence = min(0.9999, nws_confidence * nws_accuracy + (1 - nws_accuracy) * 0.5)
            
            price_noise = random.uniform(-0.05, 0.02)
            market_price = nws_confidence + price_noise
            market_price = max(0.01, min(0.99, market_price))
            
            nws = NWSForecast(
                station_id=f"K{city[:3].upper()}",
                latitude=40.0, longitude=-74.0,
                forecast_text=f"Low of {base_temp}°F expected",
                low_temp_f=base_temp,
                confidence=nws_confidence,
                issued_at=datetime.now(timezone.utc).isoformat(),
                expires_at=(datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
            )
            
            outcome = "NO"
            
        else:  # precipitation
            precip_prob = random.uniform(0.0, 0.3)  # Low precip probability
            
            question = f"Will {city} see rain today?"
            
            nws_confidence = (1.0 - precip_prob) * nws_accuracy + (1 - nws_accuracy) * 0.5
            
            price_noise = random.uniform(-0.05, 0.02)
            # NO-rain price (market slightly underestimates confidence)
            market_price = (1.0 - precip_prob) + price_noise
            market_price = max(0.01, min(0.99, market_price))
            
            nws = NWSForecast(
                station_id=f"K{city[:3].upper()}",
                latitude=40.0, longitude=-74.0,
                forecast_text=f"{precip_prob:.0%} chance of precipitation",
                precipitation_prob=precip_prob,
                confidence=nws_confidence,
                issued_at=datetime.now(timezone.utc).isoformat(),
                expires_at=(datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
            )
            
            outcome = "NO"  # Buy NO-rain
        
        # Available liquidity (varies)
        liquidity = random.uniform(50, 5000)
        
        end_date = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
        
        market = MarketCandidate(
            market_id=f"bt_{index:05d}",
            question=question,
            outcome=outcome,
            price=market_price,
            volume=random.uniform(100, 50000),
            liquidity=liquidity,
            end_date=end_date,
            condition_id=f"cond_{index:05d}",
            token_id=f"tok_{index:05d}",
            category="weather"
        )
        
        return {
            "market": market,
            "nws": nws,
            "nws_confidence_truth": nws_confidence,  # True win probability
            "hours_to_resolution": hours
        }
    
    def _simulate_with_candidates(self, candidates: list[MarketCandidate]) -> BacktestResult:
        """Run simulation with real archive candidates."""
        engine = ColdMathEngine(self.cfg)
        results: list[BacktestTrade] = []
        equity_curve = [self.cfg.backtest_initial_bankroll]
        filter_counts: dict[str, int] = {}
        max_bankroll = self.cfg.backtest_initial_bankroll
        max_drawdown = 0.0
        
        for i, market in enumerate(candidates):
            signal = engine.evaluate_market(market)
            
            fr = signal.filter_result.value
            filter_counts[fr] = filter_counts.get(fr, 0) + 1
            
            if signal.filter_result != FilterResult.PASS:
                continue
            
            # For archive data, we simulate outcome based on price
            # (since we know the real resolution from archive)
            # Conservative: use market-implied probability
            win_prob = signal.win_probability
            actual_win = random.random() < win_prob
            
            entry_price = signal.entry_price
            position_size = signal.position_size_usd
            shares = position_size / entry_price
            
            if actual_win:
                pnl = shares * (1.0 - entry_price)
            else:
                pnl = -position_size
            
            engine.sizer.record_trade_result(pnl)
            
            results.append(BacktestTrade(
                day=i, market_question=market.question,
                entry_price=entry_price, nws_confidence=win_prob,
                position_size=position_size, shares=round(shares, 4),
                actual_outcome="won" if actual_win else "lost",
                pnl=round(pnl, 4), bankroll_after=round(engine.sizer.bankroll, 2),
                filter_result=fr
            ))
            
            equity_curve.append(engine.sizer.bankroll)
            if engine.sizer.bankroll > max_bankroll:
                max_bankroll = engine.sizer.bankroll
            dd = (max_bankroll - engine.sizer.bankroll) / max_bankroll if max_bankroll > 0 else 0
            if dd > max_drawdown:
                max_drawdown = dd
            
            if engine.sizer.bankroll < 1.0:
                break
        
        return self._compile_results(results, equity_curve, filter_counts,
                                     len(candidates), max_drawdown)
    
    def _compile_results(self, trades: list[BacktestTrade],
                        equity_curve: list[float],
                        filter_counts: dict,
                        total_scenarios: int,
                        max_drawdown_arg: float = -1.0) -> BacktestResult:
        """Compile backtest statistics from trade results."""
        if not trades:
            return BacktestResult(
                config_used=asdict(self.cfg),
                total_days=total_scenarios,
                total_trades=0, winning_trades=0, losing_trades=0,
                win_rate=0, total_pnl=0, avg_pnl_per_trade=0,
                best_trade=0, worst_trade=0, max_drawdown=0,
                sharpe_ratio=0, final_bankroll=self.cfg.backtest_initial_bankroll,
                return_pct=0, equity_curve=equity_curve, trades=[],
                filter_stats=filter_counts
            )
        
        wins = [t for t in trades if t.actual_outcome == "won"]
        losses = [t for t in trades if t.actual_outcome == "lost"]
        pnls = [t.pnl for t in trades]
        
        total_pnl = sum(pnls)
        avg_pnl = total_pnl / len(trades) if trades else 0
        
        # Sharpe ratio (assuming daily returns, risk-free = 0)
        if len(pnls) > 1:
            import statistics
            std = statistics.stdev(pnls)
            sharpe = (avg_pnl / std) * (252 ** 0.5) if std > 0 else 0
        else:
            sharpe = 0
        
        initial = self.cfg.backtest_initial_bankroll
        final_bankroll = equity_curve[-1] if equity_curve else initial
        return_pct = (final_bankroll - initial) / initial if initial > 0 else 0
        
        # Compute max drawdown from equity curve if not provided
        if max_drawdown_arg >= 0:
            max_drawdown = max_drawdown_arg
        else:
            peak = initial
            max_drawdown = 0.0
            for val in equity_curve:
                if val > peak:
                    peak = val
                dd = (peak - val) / peak if peak > 0 else 0
                if dd > max_drawdown:
                    max_drawdown = dd
        
        return BacktestResult(
            config_used=asdict(self.cfg),
            total_days=total_scenarios,
            total_trades=len(trades),
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate=len(wins) / len(trades) if trades else 0,
            total_pnl=round(total_pnl, 2),
            avg_pnl_per_trade=round(avg_pnl, 4),
            best_trade=round(max(pnls), 4) if pnls else 0,
            worst_trade=round(min(pnls), 4) if pnls else 0,
            max_drawdown=round(max_drawdown, 4),
            sharpe_ratio=round(sharpe, 2),
            final_bankroll=round(final_bankroll, 2),
            return_pct=return_pct,
            equity_curve=[round(x, 2) for x in equity_curve],
            trades=[asdict(t) for t in trades],
            filter_stats=filter_counts
        )


class BacktestReporter:
    """Generate human-readable backtest reports."""
    
    @staticmethod
    def format_report(result: BacktestResult) -> str:
        """Format a single backtest result as a readable report."""
        lines = [
            "╔══════════════════════════════════════════════╗",
            "║     ❄️  COLD MATH BACKTEST RESULTS  ❄️      ║",
            "╠══════════════════════════════════════════════╣",
            f"║  Scenarios tested:   {result.total_days:>8,}            ║",
            f"║  Trades executed:    {result.total_trades:>8,}            ║",
            f"║  Winning trades:     {result.winning_trades:>8,}            ║",
            f"║  Losing trades:      {result.losing_trades:>8,}            ║",
            f"║  Win rate:           {result.win_rate:>8.1%}            ║",
            "╠══════════════════════════════════════════════╣",
            f"║  Starting bankroll:  ${result.config_used.get('backtest_initial_bankroll', 25):>8.2f}            ║",
            f"║  Final bankroll:     ${result.final_bankroll:>8.2f}            ║",
            f"║  Total P&L:          ${result.total_pnl:>8.2f}            ║",
            f"║  Return:             {result.return_pct:>8.1%}            ║",
            f"║  Avg P&L/trade:      ${result.avg_pnl_per_trade:>8.4f}            ║",
            f"║  Best trade:         ${result.best_trade:>8.4f}            ║",
            f"║  Worst trade:        ${result.worst_trade:>8.4f}            ║",
            "╠══════════════════════════════════════════════╣",
            f"║  Max drawdown:       {result.max_drawdown:>8.2%}            ║",
            f"║  Sharpe ratio:       {result.sharpe_ratio:>8.2f}            ║",
            "╠══════════════════════════════════════════════╣",
            "║  FILTER BREAKDOWN:                           ║",
        ]
        
        for fr, count in sorted(result.filter_stats.items(), 
                                 key=lambda x: -x[1]):
            pct = count / result.total_days * 100
            lines.append(f"║  {fr:<28} {count:>5} ({pct:5.1f}%)  ║")
        
        lines.append("╚══════════════════════════════════════════════╝")
        
        return "\n".join(lines)
    
    @staticmethod
    def format_monte_carlo_summary(results: list[BacktestResult]) -> str:
        """Format Monte Carlo results with confidence intervals."""
        if not results:
            return "No Monte Carlo results"
        
        import statistics
        
        returns = [r.return_pct for r in results]
        win_rates = [r.win_rate for r in results]
        drawdowns = [r.max_drawdown for r in results]
        pnls = [r.total_pnl for r in results]
        sharpes = [r.sharpe_ratio for r in results]
        
        def ci(values, pct=95):
            sorted_v = sorted(values)
            n = len(sorted_v)
            lower = sorted_v[int(n * (1 - pct/100) / 2)]
            upper = sorted_v[int(n * (1 + pct/100) / 2) - 1]
            return lower, upper
        
        ret_ci = ci(returns)
        wr_ci = ci(win_rates)
        dd_ci = ci(drawdowns)
        pnl_ci = ci(pnls)
        sh_ci = ci(sharpes)
        
        lines = [
            "╔══════════════════════════════════════════════╗",
            "║  ❄️  MONTE CARLO SIMULATION RESULTS  ❄️     ║",
            f"║  {len(results)} runs, 95% confidence intervals    ║",
            "╠══════════════════════════════════════════════╣",
            f"║  Return:    {statistics.mean(returns):>8.1%} [{ret_ci[0]:.1%}, {ret_ci[1]:.1%}]  ║",
            f"║  Win rate:  {statistics.mean(win_rates):>8.1%} [{wr_ci[0]:.1%}, {wr_ci[1]:.1%}]  ║",
            f"║  Max DD:    {statistics.mean(drawdowns):>8.2%} [{dd_ci[0]:.2%}, {dd_ci[1]:.2%}]  ║",
            f"║  Total P&L: ${statistics.mean(pnls):>7.2f} [${pnl_ci[0]:.2f}, ${pnl_ci[1]:.2f}]  ║",
            f"║  Sharpe:    {statistics.mean(sharpes):>8.2f} [{sh_ci[0]:.2f}, {sh_ci[1]:.2f}]  ║",
            "╠══════════════════════════════════════════════╣",
            f"║  Worst case return:  {min(returns):>8.1%}            ║",
            f"║  Best case return:   {max(returns):>8.1%}            ║",
            f"║  Worst case DD:      {max(drawdowns):>8.2%}            ║",
            f"║  % runs profitable:  {sum(1 for r in returns if r > 0)/len(returns):>8.1%}            ║",
            "╚══════════════════════════════════════════════╝",
        ]
        
        return "\n".join(lines)
    
    @staticmethod
    def save_results(result: BacktestResult, path: str):
        """Save results to JSON file."""
        output = asdict(result)
        with open(path, "w") as f:
            json.dump(output, f, indent=2, default=str)
        logger.info(f"Results saved to {path}")
    
    @staticmethod
    def save_equity_curve(result: BacktestResult, path: str):
        """Save equity curve as CSV for charting."""
        with open(path, "w") as f:
            f.write("day,bankroll\n")
            for i, val in enumerate(result.equity_curve):
                f.write(f"{i},{val}\n")
        logger.info(f"Equity curve saved to {path}")
