#!/usr/bin/env python3
"""
❄️ Cold Math Weather Bot — Main Entry Point
Orchestrates: Scanner → NWS → Engine → Executor → Alerts → Dashboard

Usage:
    python run.py backtest          # Run synthetic backtest
    python run.py backtest --archive  # Run archive backtest
    python run.py monte-carlo       # Monte Carlo simulation
    python run.py paper             # Paper trading mode
    python run.py live              # Live trading (requires credentials)
    python run.py dashboard         # Start dashboard only
    python run.py status            # Show current status
"""
import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from core.config import ColdMathConfig
from core.engine import ColdMathEngine, MarketCandidate, NWSForecast, FilterResult
from backtest.engine import BacktestEngine, BacktestReporter
from paper.trader import PaperTrader, PaperTraderSync
from live.executor import LiveExecutor, LiveSafetyGuard
from alerts.telegram import TelegramAlerter, AlertManager
from dashboard.server import DashboardServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("coldmath")


def cmd_backtest(args):
    """Run backtest."""
    cfg = ColdMathConfig()
    engine = BacktestEngine(cfg)
    reporter = BacktestReporter()
    
    if args.archive:
        result = engine.run_archive_backtest()
    else:
        result = engine.run_synthetic_backtest(
            num_scenarios=args.scenarios or 1000,
            seed=args.seed or 42
        )
    
    # Print report
    print(reporter.format_report(result))
    
    # Save results
    output_dir = Path(cfg.data_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    reporter.save_results(result, str(output_dir / "backtest_results.json"))
    reporter.save_equity_curve(result, str(output_dir / "equity_curve.csv"))
    
    logger.info(f"Results saved to {output_dir}/")
    
    # Key metrics
    if result.total_trades > 0:
        logger.info(f"Win rate: {result.win_rate:.1%}")
        logger.info(f"Total P&L: ${result.total_pnl:.2f}")
        logger.info(f"Return: {result.return_pct:.1%}")
        logger.info(f"Max drawdown: {result.max_drawdown:.2%}")
        logger.info(f"Sharpe: {result.sharpe_ratio:.2f}")


def cmd_monte_carlo(args):
    """Run Monte Carlo simulation."""
    cfg = ColdMathConfig()
    engine = BacktestEngine(cfg)
    reporter = BacktestReporter()
    
    num_runs = args.runs or 100
    scenarios = args.scenarios or 500
    
    results = engine.run_monte_carlo(num_runs, scenarios)
    
    # Print summary
    print(reporter.format_monte_carlo_summary(results))
    
    # Save
    output_dir = Path(cfg.data_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    for i, result in enumerate(results):
        reporter.save_results(result, str(output_dir / f"mc_run_{i:03d}.json"))
    
    logger.info(f"Saved {num_runs} Monte Carlo runs to {output_dir}/")


def cmd_paper(args):
    """Start paper trading loop."""
    cfg = ColdMathConfig()
    cfg.mode = "paper"
    cfg.dry_run = True
    
    engine = ColdMathEngine(cfg)
    trader = PaperTraderSync(cfg)
    alerter = AlertManager(cfg)
    dashboard = DashboardServer(engine, port=args.port or 8199)
    dashboard.start()
    
    logger.info("❄️ Cold Math Weather Bot — PAPER TRADING MODE")
    logger.info(f"Starting bankroll: ${cfg.starting_bankroll:.2f}")
    logger.info(f"Dashboard: http://localhost:{args.port or 8199}")
    
    cycle = 0
    try:
        while True:
            cycle += 1
            logger.info(f"═══ Scan Cycle #{cycle} ═══")
            
            # Scan and evaluate
            try:
                trades = trader.run_scan_cycle()
                if trades:
                    logger.info(f"📋 {len(trades)} paper trades entered")
                    # Send alerts
                    for t_data in trades:
                        # Convert back to TradeRecord for alerting
                        pass  # Alerts would go here in production
                else:
                    logger.info("No qualifying markets found this cycle")
            except Exception as e:
                logger.error(f"Scan cycle error: {e}")
            
            # Check resolutions
            try:
                resolved = trader.check_resolutions()
                if resolved:
                    for r in resolved:
                        logger.info(f"📊 Resolved: {r['trade_id']} — "
                                   f"{'WON' if r['won'] else 'LOST'} — "
                                   f"P&L: ${r['pnl']:.4f}")
            except Exception as e:
                logger.error(f"Resolution check error: {e}")
            
            # Snapshot bankroll
            status = trader.get_status()
            engine.logger.snapshot_bankroll(
                status["bankroll"],
                status.get("daily_pnl", 0),
                status.get("total_trades", 0),
                status.get("wins", 0),
                status.get("losses", 0)
            )
            
            logger.info(f"💰 Bankroll: ${status['bankroll']:.2f} | "
                       f"Open: {status.get('open_positions', 0)}")
            
            # Wait for next cycle
            interval = args.interval or 300
            logger.info(f"Next scan in {interval}s...")
            time.sleep(interval)
            
    except KeyboardInterrupt:
        logger.info("Shutting down paper trader...")
        dashboard.stop()


def cmd_live(args):
    """Start live trading (requires confirmation)."""
    cfg = ColdMathConfig()
    
    # Safety: require explicit confirmation
    if not args.confirm:
        print("⚠️  LIVE TRADING MODE")
        print("This will use REAL MONEY on Polymarket.")
        print("Run with --confirm to proceed.")
        return
    
    cfg.mode = "live"
    cfg.dry_run = False
    
    if not cfg.polymarket_private_key:
        print("❌ Missing POLYMARKET_PRIVATE_KEY environment variable")
        return
    
    engine = ColdMathEngine(cfg)
    executor = LiveExecutor(cfg, engine)
    alerter = AlertManager(cfg)
    dashboard = DashboardServer(engine, port=args.port or 8199)
    dashboard.start()
    
    logger.info("❄️ Cold Math Weather Bot — LIVE TRADING MODE")
    logger.warning("⚠️  REAL MONEY — Safety guards active")
    
    try:
        while True:
            # Live trading loop (similar to paper but with real execution)
            logger.info("Live scan cycle...")
            time.sleep(cfg.scan_interval_seconds)
    except KeyboardInterrupt:
        executor.cancel_all_orders()
        executor.safety.activate_kill_switch()
        dashboard.stop()


def cmd_dashboard(args):
    """Start dashboard only."""
    cfg = ColdMathConfig()
    engine = ColdMathEngine(cfg)
    dashboard = DashboardServer(engine, port=args.port or 8199)
    dashboard.start()
    
    logger.info(f"❄️ Dashboard running at http://localhost:{args.port or 8199}")
    logger.info("Press Ctrl+C to stop")
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        dashboard.stop()


def cmd_status(args):
    """Show current status."""
    cfg = ColdMathConfig()
    engine = ColdMathEngine(cfg)
    status = engine.get_status()
    
    print("╔══════════════════════════════════════════════╗")
    print("║     ❄️  COLD MATH STATUS  ❄️                ║")
    print("╠══════════════════════════════════════════════╣")
    print(f"║  Mode:           {status.get('mode', 'unknown'):>10}                ║")
    print(f"║  Bankroll:       ${status.get('bankroll', 0):>9.2f}                ║")
    print(f"║  Daily P&L:      ${status.get('daily_pnl', 0):>9.4f}                ║")
    print(f"║  Total Trades:   {status.get('total_trades', 0):>10}                ║")
    print(f"║  Win Rate:       {status.get('win_rate', 0):>9.1%}                ║")
    print(f"║  Cons. Losses:   {status.get('consecutive_losses', 0):>10}                ║")
    print(f"║  Kelly Fraction: {status.get('kelly_fraction', 0):>10.4f}                ║")
    print("╚══════════════════════════════════════════════╝")


def main():
    parser = argparse.ArgumentParser(
        description="❄️ Cold Math Weather Bot — Prediction Market Arbitrage"
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")
    
    # Backtest
    bt = subparsers.add_parser("backtest", help="Run backtest")
    bt.add_argument("--archive", action="store_true", help="Use archive data")
    bt.add_argument("--scenarios", type=int, help="Number of scenarios")
    bt.add_argument("--seed", type=int, help="Random seed")
    
    # Monte Carlo
    mc = subparsers.add_parser("monte-carlo", help="Monte Carlo simulation")
    mc.add_argument("--runs", type=int, help="Number of MC runs")
    mc.add_argument("--scenarios", type=int, help="Scenarios per run")
    
    # Paper trading
    pt = subparsers.add_parser("paper", help="Paper trading")
    pt.add_argument("--port", type=int, help="Dashboard port")
    pt.add_argument("--interval", type=int, help="Scan interval in seconds")
    
    # Live trading
    lt = subparsers.add_parser("live", help="Live trading (real money)")
    lt.add_argument("--confirm", action="store_true", help="Confirm live trading")
    lt.add_argument("--port", type=int, help="Dashboard port")
    
    # Dashboard
    db = subparsers.add_parser("dashboard", help="Dashboard only")
    db.add_argument("--port", type=int, help="Dashboard port")
    
    # Status
    subparsers.add_parser("status", help="Show status")
    
    args = parser.parse_args()
    
    commands = {
        "backtest": cmd_backtest,
        "monte-carlo": cmd_monte_carlo,
        "paper": cmd_paper,
        "live": cmd_live,
        "dashboard": cmd_dashboard,
        "status": cmd_status,
    }
    
    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
