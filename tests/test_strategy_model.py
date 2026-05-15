import os
import unittest

os.environ["DISABLE_SCHEDULER"] = "true"

import pandas as pd

import main


def make_ohlc(rows: int = 180) -> pd.DataFrame:
    closes = []
    price = 100.0
    for i in range(rows):
        if i % 30 < 15:
            price -= 0.8
        else:
            price += 1.0
        closes.append(price)

    df = pd.DataFrame({"close": closes})
    df["open"] = df["close"].shift(1).fillna(df["close"])
    df["high"] = df[["open", "close"]].max(axis=1) + 0.8
    df["low"] = df[["open", "close"]].min(axis=1) - 0.8
    return df[["open", "high", "low", "close"]]


class StrategyModelTests(unittest.TestCase):
    def test_rejects_small_sample(self):
        qualified, reason = main.is_backtest_qualified(
            {
                "sample_size": 1,
                "win_rate_pct": 100,
                "avg_roi_pct": 10,
                "profit_factor": 99,
            }
        )

        self.assertFalse(qualified)
        self.assertIn("amostra insuficiente", reason)

    def test_rejects_overfit_gap(self):
        qualified, reason = main.is_backtest_qualified(
            {
                "sample_size": 20,
                "win_rate_pct": 80,
                "avg_roi_pct": 5,
                "profit_factor": 2,
                "overfit_warning": True,
                "overfit_gap_pct": 30,
                "train_stats": {"sample_size": 20},
            }
        )

        self.assertFalse(qualified)
        self.assertIn("overfitting", reason)

    def test_backtest_returns_validation_metrics(self):
        df = make_ohlc()
        rsi = main.calc_rsi(df["close"])
        stats = main.evaluate_backtest(df, rsi, "15m", "warning")

        self.assertIn("train_stats", stats)
        self.assertIn("all_stats", stats)
        self.assertIn("overfit_warning", stats)
        self.assertGreaterEqual(stats["sample_size"], 0)

    def test_rsi_recovery_accepts_cross_up(self):
        rsi = pd.Series([55, 48, 41, 36, 34, 38, 39, 40.5])
        passed, recovery = main.rsi_recovery_signal(rsi, "warning")

        self.assertTrue(passed)
        self.assertTrue(recovery["crossed_up"])

    def test_rsi_recovery_rejects_falling_rsi(self):
        rsi = pd.Series([55, 48, 41, 39, 38, 36, 34, 33])
        passed, recovery = main.rsi_recovery_signal(rsi, "warning")

        self.assertFalse(passed)
        self.assertIn("sem recuperacao", recovery["reason"])

    def test_split_entry_plan_includes_average_price(self):
        df = make_ohlc()
        stats = {
            "sample_size": main.BACKTEST_MIN_TRADES,
            "win_rate_pct": 70,
            "avg_roi_pct": 5,
            "profit_factor": 2,
            "train_stats": {"sample_size": main.BACKTEST_MIN_TRADES},
            "overfit_warning": False,
        }
        context = {
            "score": main.CONTEXT_MIN_SCORE,
            "passed_count": main.CONTEXT_MIN_FILTERS,
            "total_count": main.CONTEXT_MIN_FILTERS,
            "passed_filters": ["RSI recuperando"],
        }
        plan = main.build_trade_plan(df, 35, "15m", "warning", stats, context=context)

        self.assertTrue(plan["split_entry_enabled"])
        self.assertLess(plan["second_entry_price"], plan["first_entry_price"])
        self.assertLess(plan["average_entry_price"], plan["first_entry_price"])
        self.assertIn("reforco", plan["summary"])

    def test_split_entry_grid_is_wider_for_alts_than_btc(self):
        btc_drops = {
            params["split_second_roi_drop_pct"]
            for params in main.candidate_param_grid("15m", "warning", "BTCUSDT")
        }
        alt_drops = {
            params["split_second_roi_drop_pct"]
            for params in main.candidate_param_grid("15m", "warning", "ENAUSDT")
        }

        self.assertLess(max(btc_drops), max(alt_drops))
        self.assertGreater(len(alt_drops), len(btc_drops))

    def test_selected_split_drop_changes_second_entry_price(self):
        df = make_ohlc()
        stats = {
            "sample_size": main.BACKTEST_MIN_TRADES,
            "win_rate_pct": 70,
            "avg_roi_pct": 5,
            "profit_factor": 2,
            "train_stats": {"sample_size": main.BACKTEST_MIN_TRADES},
            "overfit_warning": False,
        }
        context = {
            "score": main.CONTEXT_MIN_SCORE,
            "passed_count": main.CONTEXT_MIN_FILTERS,
            "total_count": main.CONTEXT_MIN_FILTERS,
            "passed_filters": ["RSI recuperando"],
        }
        plan = main.build_trade_plan(
            df,
            35,
            "15m",
            "warning",
            stats,
            params={
                "tp1_roi_pct": 25,
                "tp2_roi_pct": 45,
                "sl_roi_pct": 10,
                "split_second_roi_drop_pct": 180,
            },
            context=context,
        )

        self.assertEqual(plan["second_entry_roi_drop_pct"], 180)
        self.assertAlmostEqual(
            plan["second_entry_price"],
            plan["first_entry_price"] * (1 - 18 / 100),
        )

    def test_plan_must_be_qualified_to_send(self):
        sendable, reason = main.is_plan_sendable(
            {
                "qualified": False,
                "qualification_reason": "assertividade abaixo do minimo",
                "score": 95,
                "confidence": "alta",
            }
        )

        self.assertFalse(sendable)
        self.assertIn("assertividade", reason)

    def test_plan_must_reach_min_score_to_send(self):
        sendable, reason = main.is_plan_sendable(
            {
                "qualified": True,
                "qualification_reason": "modelo aprovado pelo backtest",
                "score": main.PLAN_MIN_SCORE - 1,
                "confidence": "alta",
            }
        )

        self.assertFalse(sendable)
        self.assertIn("score", reason)

    def test_qualified_plan_can_send(self):
        sendable, reason = main.is_plan_sendable(
            {
                "qualified": True,
                "qualification_reason": "modelo aprovado pelo backtest",
                "score": main.PLAN_MIN_SCORE,
                "confidence": "media",
                "context": {
                    "score": main.CONTEXT_MIN_SCORE,
                    "passed_count": main.CONTEXT_MIN_FILTERS,
                },
            }
        )

        self.assertTrue(sendable)
        self.assertIn("aprovada", reason)


if __name__ == "__main__":
    unittest.main()
