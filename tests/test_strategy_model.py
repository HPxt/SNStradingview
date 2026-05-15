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
