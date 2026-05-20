"""
Cold Math Weather Bot — Test Suite
Comprehensive tests for all components.
"""
import json
import math
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import ColdMathConfig
from core.engine import (
    ColdMathEngine, KellySizer, TradeFilter, TradeLogger,
    MarketCandidate, NWSForecast, TradeSignal, TradeRecord,
    FilterResult, TradeSide, TradeStatus
)
from core.nws import NWSConfidenceScorer, NWSForecastMatcher
from core.scanner import ArchiveDataLoader
from backtest.engine import BacktestEngine, BacktestReporter


class TestKellySizer(unittest.TestCase):
    """Test Kelly criterion position sizing."""
    
    def setUp(self):
        self.cfg = ColdMathConfig()
        self.cfg.starting_bankroll = 100.0
        self.sizer = KellySizer(self.cfg)
    
    def test_basic_kelly(self):
        """Standard Kelly calculation with known values."""
        # 99% win prob, 95¢ entry → b = 0.05/0.95 = 0.0526
        # f* = (0.99*0.0526 - 0.01) / 0.0526 = (0.0521 - 0.01) / 0.0526 = 0.797
        # Quarter Kelly ≈ 0.199
        size, reason = self.sizer.calculate(
            win_prob=0.99, entry_price=0.95, available_liquidity=5000
        )
        self.assertGreater(size, 0)
        self.assertLessEqual(size, self.cfg.max_position_usd)
        self.assertIn("Kelly", reason)
    
    def test_zero_edge(self):
        """No edge → zero position."""
        # If win_prob = 0.50 and entry = 0.95, no edge
        size, reason = self.sizer.calculate(
            win_prob=0.50, entry_price=0.95, available_liquidity=5000
        )
        self.assertEqual(size, 0.0)
        self.assertIn("Negative Kelly", reason)
    
    def test_liquidity_cap(self):
        """Position should be capped by available liquidity."""
        # Very small liquidity should cap position
        size, reason = self.sizer.calculate(
            win_prob=0.99, entry_price=0.95, available_liquidity=2.0
        )
        # Max by liquidity = 2.0 * 0.25 = 0.5
        self.assertLessEqual(size, 0.5)
        self.assertIn("Liquidity", reason)
    
    def test_max_position_cap(self):
        """Position should never exceed max_position_usd."""
        self.sizer._bankroll = 1000000  # Huge bankroll
        size, reason = self.sizer.calculate(
            win_prob=0.99, entry_price=0.95, available_liquidity=1000000
        )
        self.assertLessEqual(size, self.cfg.max_position_usd)
    
    def test_daily_loss_limit(self):
        """Trading should stop after daily loss limit."""
        self.sizer._daily_pnl = -20  # Lost 20% of $100
        size, reason = self.sizer.calculate(
            win_prob=0.99, entry_price=0.95, available_liquidity=5000
        )
        self.assertEqual(size, 0.0)
        self.assertIn("Daily loss", reason)
    
    def test_consecutive_losses_circuit_breaker(self):
        """Circuit breaker after consecutive losses."""
        self.sizer._consecutive_losses = 5
        size, reason = self.sizer.calculate(
            win_prob=0.99, entry_price=0.95, available_liquidity=5000
        )
        self.assertEqual(size, 0.0)
        self.assertIn("Circuit breaker", reason)
    
    def test_bankroll_tracking(self):
        """Bankroll updates after trade results."""
        initial = self.sizer.bankroll
        self.sizer.record_trade_result(5.0)
        self.assertEqual(self.sizer.bankroll, initial + 5.0)
        
        self.sizer.record_trade_result(-2.0)
        self.assertEqual(self.sizer.bankroll, initial + 3.0)
        self.assertEqual(self.sizer._consecutive_losses, 1)  # Loss after win
    
    def test_consecutive_loss_counter(self):
        """Consecutive losses increment and reset correctly."""
        self.sizer.record_trade_result(-1.0)
        self.assertEqual(self.sizer._consecutive_losses, 1)
        self.sizer.record_trade_result(-1.0)
        self.assertEqual(self.sizer._consecutive_losses, 2)
        self.sizer.record_trade_result(0.5)  # Win resets
        self.assertEqual(self.sizer._consecutive_losses, 0)


class TestTradeFilter(unittest.TestCase):
    """Test the 3-gate trade filter."""
    
    def setUp(self):
        self.cfg = ColdMathConfig()
        self.cfg.starting_bankroll = 100.0
        self.sizer = KellySizer(self.cfg)
        self.filter = TradeFilter(self.cfg, self.sizer)
    
    def _make_market(self, price=0.95, end_hours=12, liquidity=5000,
                      question="Will NYC high temp exceed 90°F today?"):
        end = (datetime.now(timezone.utc) + timedelta(hours=end_hours)).isoformat()
        return MarketCandidate(
            market_id="test_001", question=question,
            outcome="NO", price=price, volume=10000,
            liquidity=liquidity, end_date=end,
            condition_id="cond_001", token_id="tok_001",
            category="weather"
        )
    
    def _make_nws(self, confidence=0.995):
        return NWSForecast(
            station_id="KNYC", latitude=40.71, longitude=-74.01,
            forecast_text="Sunny, high of 82°F",
            high_temp_f=82, confidence=confidence,
            issued_at=datetime.now(timezone.utc).isoformat(),
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
        )
    
    def test_pass_all_gates(self):
        """Market with high NWS confidence, right price, short resolution → PASS."""
        market = self._make_market(price=0.95)
        nws = self._make_nws(confidence=0.995)
        signal = self.filter.evaluate(market, nws)
        self.assertEqual(signal.filter_result, FilterResult.PASS)
    
    def test_fail_nws_confidence(self):
        """Low NWS confidence → FAIL."""
        market = self._make_market()
        nws = self._make_nws(confidence=0.85)
        signal = self.filter.evaluate(market, nws)
        self.assertEqual(signal.filter_result, FilterResult.FAIL_NWS_CONFIDENCE)
    
    def test_fail_price_too_high(self):
        """Price above max → FAIL."""
        market = self._make_market(price=0.98)
        nws = self._make_nws()
        signal = self.filter.evaluate(market, nws)
        self.assertEqual(signal.filter_result, FilterResult.FAIL_PRICE_TOO_HIGH)
    
    def test_fail_price_too_low(self):
        """Price below min → FAIL (market doubts outcome)."""
        market = self._make_market(price=0.80)
        nws = self._make_nws()
        signal = self.filter.evaluate(market, nws)
        self.assertEqual(signal.filter_result, FilterResult.FAIL_PRICE_TOO_LOW)
    
    def test_fail_resolution_too_far(self):
        """Resolution too far out → FAIL."""
        market = self._make_market(end_hours=72)
        nws = self._make_nws()
        signal = self.filter.evaluate(market, nws)
        self.assertEqual(signal.filter_result, FilterResult.FAIL_RESOLUTION_TOO_FAR)
    
    def test_fail_insufficient_edge(self):
        """No edge between NWS confidence and market price → FAIL."""
        # NWS says 99.5% confident, market at 98¢ → only 1.5¢ edge but price too high
        # Actually: need NWS > min_confidence but edge < min_edge
        # NWS = 0.993, price = 0.97 → NWS passes (0.993 > 0.99) but price fails (>0.96)
        # Use lower min_confidence temporarily
        self.cfg.min_nws_confidence = 0.90  # Lower threshold for this test
        self.cfg.max_entry_price = 0.98     # Allow higher price
        market = self._make_market(price=0.97)
        nws = self._make_nws(confidence=0.91)  # Barely above min_confidence
        signal = self.filter.evaluate(market, nws)
        # With 0.91 NWS confidence vs 0.97 price, edge = 0.91-0.97 = -0.06
        # This should fail insufficient edge or NWS confidence
        self.assertIn(signal.filter_result, [
            FilterResult.FAIL_INSUFFICIENT_EDGE,
            FilterResult.FAIL_NWS_CONFIDENCE
        ])
    
    def test_no_nws_forecast(self):
        """Without NWS, should estimate from price and still evaluate."""
        market = self._make_market()
        signal = self.filter.evaluate(market, None)
        # Should still produce a result (might pass or fail depending on estimate)
        self.assertIsNotNone(signal)
        self.assertIsInstance(signal.filter_result, FilterResult)


class TestNWSConfidenceScorer(unittest.TestCase):
    """Test NWS confidence scoring logic."""
    
    def test_high_confidence_near_term(self):
        """1-hour forecast should have very high confidence."""
        confidence = NWSConfidenceScorer.score_temperature_market(
            forecast_high=82, forecast_low=None,
            threshold_temp=95,
            comparison="above", hours_to_resolution=1,
            current_temp=None
        )
        self.assertGreater(confidence, 0.99)
    
    def test_low_confidence_far_out(self):
        """7-day forecast should have much lower confidence."""
        confidence = NWSConfidenceScorer.score_temperature_market(
            forecast_high=82, forecast_low=None,
            threshold_temp=85,
            comparison="above", hours_to_resolution=168
        )
        self.assertLess(confidence, 0.95)
    
    def test_large_margin_high_confidence(self):
        """Big margin between forecast and threshold = high confidence."""
        confidence = NWSConfidenceScorer.score_temperature_market(
            forecast_high=72, forecast_low=None,
            threshold_temp=90,
            comparison="above", hours_to_resolution=6
        )
        self.assertGreater(confidence, 0.99)
    
    def test_small_margin_lower_confidence(self):
        """Small margin = lower confidence."""
        confidence = NWSConfidenceScorer.score_temperature_market(
            forecast_high=89, forecast_low=None,
            threshold_temp=90,
            comparison="above", hours_to_resolution=6
        )
        # This should be much less certain
        self.assertLess(confidence, 0.90)
    
    def test_precipitation_scoring(self):
        """Precipitation scoring with known probability."""
        # 10% chance of rain → 90% confidence it won't rain
        confidence = NWSConfidenceScorer.score_precipitation_market(
            precip_probability=0.10, threshold="no_rain",
            hours_to_resolution=12
        )
        self.assertGreater(confidence, 0.85)
    
    def test_accuracy_interpolation(self):
        """Accuracy interpolation from lead time table."""
        self.assertAlmostEqual(
            NWSConfidenceScorer._interpolate_accuracy(1), 0.999, places=3
        )
        self.assertAlmostEqual(
            NWSConfidenceScorer._interpolate_accuracy(24), 0.97, places=2
        )


class TestNWSForecastMatcher(unittest.TestCase):
    """Test market question parsing."""
    
    def test_parse_temperature_above(self):
        """Parse 'Will NYC high temp exceed 90°F?'"""
        result = NWSForecastMatcher.parse_market_question(
            "Will NYC high temp exceed 90°F today?"
        )
        self.assertIsNotNone(result)
        if result:
            self.assertEqual(result["metric"], "temperature")
            self.assertEqual(result["comparison"], "above")
            self.assertEqual(result["threshold_temp"], 90)
            self.assertTrue(result["found_coords"])
    
    def test_parse_temperature_below(self):
        """Parse 'Will Chicago low temp drop below 20°F?'"""
        result = NWSForecastMatcher.parse_market_question(
            "Will Chicago low temp drop below 20°F?"
        )
        self.assertIsNotNone(result)
        if result:
            self.assertEqual(result["comparison"], "below")
            self.assertEqual(result["threshold_temp"], 20)
    
    def test_parse_precipitation(self):
        """Parse 'Will Miami see rain today?'"""
        result = NWSForecastMatcher.parse_market_question(
            "Will Miami see rain today?"
        )
        self.assertIsNotNone(result)
        if result:
            self.assertEqual(result["metric"], "precipitation")
    
    def test_parse_unknown_question(self):
        """Non-weather questions should return None."""
        result = NWSForecastMatcher.parse_market_question(
            "Will the Fed raise rates?"
        )
        self.assertIsNone(result)
    
    def test_parse_unknown_city(self):
        """City not in database should have found_coords=False."""
        result = NWSForecastMatcher.parse_market_question(
            "Will Timbuktu high temp exceed 100°F?"
        )
        if result:
            self.assertFalse(result.get("found_coords", True))


class TestColdMathEngine(unittest.TestCase):
    """Test the main engine pipeline."""
    
    def setUp(self):
        self.cfg = ColdMathConfig()
        self.cfg.starting_bankroll = 100.0
        self.cfg.db_path = tempfile.mktemp(suffix=".db")
        self.cfg.trade_log_path = tempfile.mktemp(suffix=".jsonl")
        self.engine = ColdMathEngine(self.cfg)
    
    def tearDown(self):
        # Clean up temp files
        for path in [self.cfg.db_path, self.cfg.trade_log_path]:
            try:
                os.unlink(path)
            except OSError:
                pass
    
    def test_evaluate_and_execute(self):
        """Full pipeline: evaluate market → execute signal → resolve trade."""
        end = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
        market = MarketCandidate(
            market_id="test_001",
            question="Will NYC high temp exceed 90°F today?",
            outcome="NO", price=0.94, volume=10000,
            liquidity=5000, end_date=end,
            condition_id="cond_001", token_id="tok_001",
            category="weather"
        )
        nws = NWSForecast(
            station_id="KNYC", latitude=40.71, longitude=-74.01,
            forecast_text="High of 82°F", high_temp_f=82,
            confidence=0.997,
            issued_at=datetime.now(timezone.utc).isoformat(),
            expires_at=end
        )
        
        signal = self.engine.evaluate_market(market, nws)
        self.assertEqual(signal.filter_result, FilterResult.PASS)
        
        trade = self.engine.execute_signal(signal)
        self.assertIsNotNone(trade)
        if trade:
            self.assertEqual(trade.status, TradeStatus.ENTERED)
        
        # Resolve as won
        if trade:
            pnl = self.engine.resolve_trade(trade.trade_id, won=True)
            self.assertGreater(pnl, 0)
        
        status = self.engine.get_status()
        self.assertEqual(status["total_trades"], 1)
    
    def test_status_reporting(self):
        """Engine status should reflect current state."""
        status = self.engine.get_status()
        self.assertEqual(status["mode"], "paper")
        self.assertEqual(status["bankroll"], 100.0)


class TestTradeLogger(unittest.TestCase):
    """Test SQLite + JSONL persistence."""
    
    def setUp(self):
        self.cfg = ColdMathConfig()
        self.cfg.db_path = tempfile.mktemp(suffix=".db")
        self.cfg.trade_log_path = tempfile.mktemp(suffix=".jsonl")
        self.logger = TradeLogger(self.cfg)
    
    def tearDown(self):
        for path in [self.cfg.db_path, self.cfg.trade_log_path]:
            try:
                os.unlink(path)
            except OSError:
                pass
    
    def test_log_and_retrieve(self):
        """Trade should be logged and retrievable."""
        trade = TradeRecord(
            trade_id="test_001",
            timestamp=datetime.now(timezone.utc).isoformat(),
            market_id="mkt_001", question="Test market",
            side="NO", entry_price=0.94,
            position_size_usd=5.0, shares=5.32,
            status=TradeStatus.ENTERED,
            notes="Test trade"
        )
        self.logger.log_trade(trade)
        
        stats = self.logger.get_stats()
        self.assertEqual(stats["total_trades"], 0)  # No resolved trades yet
    
    def test_update_resolution(self):
        """Trade resolution should update P&L."""
        trade = TradeRecord(
            trade_id="test_002",
            timestamp=datetime.now(timezone.utc).isoformat(),
            market_id="mkt_002", question="Test market 2",
            side="NO", entry_price=0.94,
            position_size_usd=5.0, shares=5.32,
            status=TradeStatus.ENTERED
        )
        self.logger.log_trade(trade)
        
        self.logger.update_trade_result("test_002", 1.0, 0.32, "won")
        
        stats = self.logger.get_stats()
        self.assertEqual(stats["total_trades"], 1)
        self.assertEqual(stats["wins"], 1)
    
    def test_jsonl_audit_trail(self):
        """JSONL file should contain all trades."""
        trade = TradeRecord(
            trade_id="test_003",
            timestamp=datetime.now(timezone.utc).isoformat(),
            market_id="mkt_003", question="Audit test",
            side="YES", entry_price=0.90,
            position_size_usd=10.0, shares=11.11,
            status=TradeStatus.ENTERED
        )
        self.logger.log_trade(trade)
        
        with open(self.cfg.trade_log_path) as f:
            lines = f.readlines()
        
        self.assertEqual(len(lines), 1)
        data = json.loads(lines[0])
        self.assertEqual(data["trade_id"], "test_003")


class TestBacktestEngine(unittest.TestCase):
    """Test backtest simulation."""
    
    def test_synthetic_backtest_runs(self):
        """Synthetic backtest should complete without errors."""
        cfg = ColdMathConfig()
        cfg.backtest_initial_bankroll = 25.0
        engine = BacktestEngine(cfg)
        result = engine.run_synthetic_backtest(num_scenarios=100, seed=42)
        
        self.assertGreater(result.total_days, 0)
        self.assertIsNotNone(result.equity_curve)
        self.assertEqual(len(result.equity_curve), result.total_trades + 1)
    
    def test_backtest_results_format(self):
        """Backtest results should have all expected fields."""
        cfg = ColdMathConfig()
        engine = BacktestEngine(cfg)
        result = engine.run_synthetic_backtest(num_scenarios=50, seed=123)
        
        self.assertGreaterEqual(result.total_trades, 0)
        self.assertGreaterEqual(result.winning_trades, 0)
        self.assertGreaterEqual(result.losing_trades, 0)
        if result.total_trades > 0:
            self.assertAlmostEqual(
                result.win_rate,
                result.winning_trades / result.total_trades,
                places=2
            )
    
    def test_reporter_format(self):
        """Reporter should produce readable output."""
        cfg = ColdMathConfig()
        engine = BacktestEngine(cfg)
        result = engine.run_synthetic_backtest(num_scenarios=50, seed=42)
        report = BacktestReporter.format_report(result)
        
        self.assertIn("COLD MATH", report)
        self.assertIn("Win rate", report)
    
    def test_monte_carlo(self):
        """Monte Carlo should produce multiple results."""
        cfg = ColdMathConfig()
        engine = BacktestEngine(cfg)
        results = engine.run_monte_carlo(num_runs=5, scenarios_per_run=20)
        
        self.assertEqual(len(results), 5)
    
    def test_save_results(self):
        """Results should save to JSON correctly."""
        cfg = ColdMathConfig()
        engine = BacktestEngine(cfg)
        result = engine.run_synthetic_backtest(num_scenarios=20, seed=42)
        
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        
        try:
            BacktestReporter.save_results(result, path)
            with open(path) as f:
                data = json.load(f)
            self.assertIn("total_trades", data)
            self.assertIn("equity_curve", data)
        finally:
            os.unlink(path)


class TestConfig(unittest.TestCase):
    """Test configuration."""
    
    def test_default_config(self):
        """Default config should have sensible values."""
        cfg = ColdMathConfig()
        self.assertEqual(cfg.min_nws_confidence, 0.99)
        self.assertEqual(cfg.max_entry_price, 0.96)
        self.assertEqual(cfg.kelly_fraction, 0.1425)
        self.assertEqual(cfg.mode, "paper")
        self.assertTrue(cfg.dry_run)
    
    def test_config_from_env(self):
        """Config should load from environment variables."""
        os.environ["COLDMATH_MODE"] = "live"
        os.environ["COLDMATH_BANKROLL"] = "500"
        try:
            cfg = ColdMathConfig.from_env()
            self.assertEqual(cfg.mode, "live")
            self.assertEqual(cfg.starting_bankroll, 500.0)
        finally:
            del os.environ["COLDMATH_MODE"]
            del os.environ["COLDMATH_BANKROLL"]


if __name__ == "__main__":
    unittest.main(verbosity=2)
