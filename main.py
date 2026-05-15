import html
import os
import smtplib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify

app = Flask(__name__)

# ============================================================
#  CONFIGURACOES - edite aqui ou use variaveis de ambiente
# ============================================================
EMAIL_FROM = os.getenv("EMAIL_FROM", "seuemail@gmail.com")
EMAIL_TO = os.getenv("EMAIL_TO", "destino@gmail.com")
GMAIL_PASS = os.getenv("GMAIL_PASS", "sua_app_password")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_FROM = os.getenv("RESEND_FROM", "RSI Monitor <onboarding@resend.dev>")
DISPLAY_TIMEZONE = os.getenv("DISPLAY_TIMEZONE", "America/Sao_Paulo")
DISPLAY_TZ = ZoneInfo(DISPLAY_TIMEZONE)

SYMBOLS = [
    "JTOUSDT",
    "ENAUSDT",
    "IMXUSDT",
    "PENDLEUSDT",
    "BTCUSDT",
]

TIMEFRAMES = {
    "15m": "15m",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
}

RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
RSI_WARNING_LIMIT = float(os.getenv("RSI_WARNING_LIMIT", os.getenv("RSI_LIMIT", "40")))
RSI_EXTREME_LIMIT = float(os.getenv("RSI_EXTREME_LIMIT", "30"))
CHECK_INTERVAL_MIN = int(os.getenv("CHECK_INTERVAL_MIN", "15"))
LEVERAGE = float(os.getenv("LEVERAGE", "10"))
BACKTEST_MIN_TRADES = int(os.getenv("BACKTEST_MIN_TRADES", "12"))
BACKTEST_MAX_SIGNALS = int(os.getenv("BACKTEST_MAX_SIGNALS", "80"))
BACKTEST_VALIDATION_RATIO = float(os.getenv("BACKTEST_VALIDATION_RATIO", "0.35"))
BACKTEST_SIGNAL_COOLDOWN = int(os.getenv("BACKTEST_SIGNAL_COOLDOWN", "0"))
TRAINING_INTERVAL_MIN = int(os.getenv("TRAINING_INTERVAL_MIN", "180"))
TRAINING_CANDLE_LIMIT = int(os.getenv("TRAINING_CANDLE_LIMIT", "3000"))
PLAN_MIN_WIN_RATE = float(os.getenv("PLAN_MIN_WIN_RATE", "60"))
PLAN_MIN_PROFIT_FACTOR = float(os.getenv("PLAN_MIN_PROFIT_FACTOR", "1.25"))
PLAN_MIN_AVG_ROI = float(os.getenv("PLAN_MIN_AVG_ROI", "0"))
PLAN_MIN_SCORE = int(os.getenv("PLAN_MIN_SCORE", "60"))
SEND_ONLY_QUALIFIED_SIGNALS = os.getenv("SEND_ONLY_QUALIFIED_SIGNALS", "true").lower() in {
    "1",
    "true",
    "yes",
}

BINANCE_BASE_URLS = [
    url.strip().rstrip("/")
    for url in os.getenv(
        "BINANCE_BASE_URLS",
        "https://data-api.binance.vision,https://api1.binance.com,https://api.binance.com",
    ).split(",")
    if url.strip()
]
HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", "2.5"))
SCAN_MAX_WORKERS = int(os.getenv("SCAN_MAX_WORKERS", "8"))
CANDLE_LIMIT = max(RSI_PERIOD + 220, 250)

# Controle para nao enviar e-mail duplicado na mesma janela.
_alerted = set()
_scheduler: BackgroundScheduler | None = None
_last_candle_errors = {}
_scan_lock = threading.Lock()
_last_scan = {
    "checked_at_utc": None,
    "values": [],
    "pending_alerts": [],
}
_last_email_results = []
_model_lock = threading.Lock()
_strategy_model = {
    "trained_at_utc": None,
    "running": False,
    "stats": {},
}

ALERT_LEVELS = [
    {
        "key": "warning",
        "limit": RSI_WARNING_LIMIT,
        "subject": "RSI abaixo de {limit:g}",
        "title": "Alerta RSI - Abaixo de {limit:g}",
        "status": "Abaixo de {limit:g}",
        "color": "#e67e22",
    },
    {
        "key": "extreme",
        "limit": RSI_EXTREME_LIMIT,
        "subject": "RSI abaixo de {limit:g} - EXTREMO",
        "title": "Alerta RSI extremo - Abaixo de {limit:g}",
        "status": "Sobrevendido extremo",
        "color": "#c0392b",
    },
]

TIMEFRAME_PRIORITY = {
    "15m": 1,
    "1h": 2,
    "4h": 3,
    "1d": 4,
}

BACKTEST_LOOKAHEAD = {
    "15m": 16,
    "1h": 16,
    "4h": 12,
    "1d": 10,
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def display_now() -> datetime:
    return utc_now().astimezone(DISPLAY_TZ)


def format_display_time() -> str:
    return f"{display_now().strftime('%d/%m/%Y %H:%M')} {DISPLAY_TIMEZONE}"


def get_candles(
    symbol: str,
    interval: str,
    limit: int = CANDLE_LIMIT,
    end_time: int | None = None,
) -> pd.DataFrame | None:
    """Busca candles publicos da Binance Spot."""
    errors = []

    for base_url in BINANCE_BASE_URLS:
        klines_url = f"{base_url}/api/v3/klines"
        params = {"symbol": symbol, "interval": interval, "limit": min(limit, 1000)}
        if end_time is not None:
            params["endTime"] = end_time
        try:
            response = requests.get(klines_url, params=params, timeout=HTTP_TIMEOUT_SECONDS)
            response.raise_for_status()

            df = pd.DataFrame(
                response.json(),
                columns=[
                    "time",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "close_time",
                    "quote_asset_volume",
                    "trades",
                    "taker_buy_base",
                    "taker_buy_quote",
                    "ignore",
                ],
            )
            df = df.assign(
                time=pd.to_numeric(df["time"], errors="coerce"),
                open=pd.to_numeric(df["open"], errors="coerce"),
                high=pd.to_numeric(df["high"], errors="coerce"),
                low=pd.to_numeric(df["low"], errors="coerce"),
                close=pd.to_numeric(df["close"], errors="coerce"),
            )
            df = df.dropna(subset=["time", "open", "high", "low", "close"])
            if not df.empty:
                _last_candle_errors.pop(f"{symbol}_{interval}", None)
                return df
            errors.append(f"{base_url}: empty_response")
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "sem status"
            errors.append(f"{base_url}: HTTP {status}")
        except requests.RequestException as exc:
            errors.append(f"{base_url}: {exc}")
        except Exception as exc:
            errors.append(f"{base_url}: {exc}")

    error_message = "; ".join(errors) if errors else "no_binance_endpoint_configured"
    _last_candle_errors[f"{symbol}_{interval}"] = error_message
    print(f"[ERRO] Candles {symbol} {interval}: {error_message}")
    return None


def get_historical_candles(symbol: str, interval: str, total_limit: int) -> pd.DataFrame | None:
    """Busca mais candles historicos paginando para tras na Binance."""
    chunks = []
    end_time = None

    while sum(len(chunk) for chunk in chunks) < total_limit:
        remaining = total_limit - sum(len(chunk) for chunk in chunks)
        df = get_candles(symbol, interval, limit=min(remaining, 1000), end_time=end_time)
        if df is None or df.empty:
            break

        chunks.append(df)
        earliest_time = int(df["time"].min())
        next_end_time = earliest_time - 1
        if end_time == next_end_time:
            break
        end_time = next_end_time

        if len(df) < min(remaining, 1000):
            break

    if not chunks:
        return None

    combined = pd.concat(chunks, ignore_index=True)
    combined = combined.drop_duplicates(subset=["time"]).sort_values("time").tail(total_limit)
    return combined.reset_index(drop=True)


def calc_rsi(series: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """Calcula RSI usando suavizacao Wilder/RMA, como no TradingView."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain_values = [None] * len(series)
    avg_loss_values = [None] * len(series)

    if len(series) > period:
        gain_values = gain.fillna(0).tolist()
        loss_values = loss.fillna(0).tolist()
        avg_gain_values[period] = sum(gain_values[1 : period + 1]) / period
        avg_loss_values[period] = sum(loss_values[1 : period + 1]) / period

        for i in range(period + 1, len(series)):
            avg_gain_values[i] = ((avg_gain_values[i - 1] * (period - 1)) + gain_values[i]) / period
            avg_loss_values[i] = ((avg_loss_values[i - 1] * (period - 1)) + loss_values[i]) / period

    avg_gain = pd.Series(avg_gain_values, index=series.index, dtype="float64")
    avg_loss = pd.Series(avg_loss_values, index=series.index, dtype="float64")

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.mask(avg_loss == 0, 100)
    rsi = rsi.mask((avg_gain == 0) & (avg_loss == 0), 50)
    return rsi


def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calcula ATR simples para medir volatilidade recente."""
    previous_close = df["close"].shift(1)
    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - previous_close).abs(),
            (df["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(period, min_periods=period).mean()


def price_fmt(value: float) -> str:
    if value >= 100:
        return f"{value:.2f}"
    if value >= 1:
        return f"{value:.4f}"
    return f"{value:.6f}"


def pct_fmt(value: float) -> str:
    return f"{value:.2f}%"


def model_key(symbol: str, tf_label: str, level_key: str) -> str:
    return f"{symbol}:{tf_label}:{level_key}"


def get_strategy_stats(symbol: str, tf_label: str, level_key: str) -> dict | None:
    with _model_lock:
        return _strategy_model["stats"].get(model_key(symbol, tf_label, level_key))


def is_backtest_qualified(backtest_stats: dict | None) -> tuple[bool, str]:
    if not backtest_stats:
        return False, "modelo ainda treinando"

    if backtest_stats["sample_size"] < BACKTEST_MIN_TRADES:
        return False, f"amostra insuficiente ({backtest_stats['sample_size']}/{BACKTEST_MIN_TRADES})"

    train_stats = backtest_stats.get("train_stats")
    if train_stats and train_stats["sample_size"] < BACKTEST_MIN_TRADES:
        return False, f"treino insuficiente ({train_stats['sample_size']}/{BACKTEST_MIN_TRADES})"

    if backtest_stats["win_rate_pct"] < PLAN_MIN_WIN_RATE:
        return False, f"assertividade abaixo do minimo ({pct_fmt(backtest_stats['win_rate_pct'])})"

    if backtest_stats["profit_factor"] < PLAN_MIN_PROFIT_FACTOR:
        return False, f"payoff abaixo do minimo ({backtest_stats['profit_factor']:.2f})"

    if backtest_stats["avg_roi_pct"] < PLAN_MIN_AVG_ROI:
        return False, f"ROI medio abaixo do minimo ({pct_fmt(backtest_stats['avg_roi_pct'])})"

    if backtest_stats.get("overfit_warning"):
        return False, f"risco de overfitting alto (gap {pct_fmt(backtest_stats['overfit_gap_pct'])})"

    return True, "modelo aprovado pelo backtest"


def is_plan_sendable(plan: dict | None) -> tuple[bool, str]:
    if not plan:
        return False, "sem plano tecnico"

    if not plan["qualified"]:
        return False, plan["qualification_reason"]

    if plan["score"] < PLAN_MIN_SCORE:
        return False, f"score abaixo do minimo ({plan['score']}/{PLAN_MIN_SCORE})"

    if plan["confidence"] not in {"media", "alta"}:
        return False, f"confianca insuficiente ({plan['confidence']})"

    return True, "call aprovada para envio"


def build_trade_plan(
    df: pd.DataFrame,
    rsi_value: float,
    tf_label: str,
    level_key: str,
    backtest_stats: dict | None = None,
) -> dict:
    """Gera um plano tecnico educacional, sem automatizar ordem."""
    close = float(df["close"].iloc[-1])
    atr_series = calc_atr(df)
    atr = float(atr_series.dropna().iloc[-1]) if not atr_series.dropna().empty else close * 0.015
    atr_pct = (atr / close) * 100 if close else 0

    recent = df.tail(20)
    swing = df.tail(80)
    recent_low = float(recent["low"].min())
    recent_high = float(recent["high"].max())
    swing_low = float(swing["low"].min())
    swing_high = float(swing["high"].max())
    swing_range = max(swing_high - swing_low, close * 0.001)
    range_position = (close - swing_low) / swing_range

    ma20 = float(df["close"].tail(20).mean())
    ma50 = float(df["close"].tail(50).mean())
    ma200 = float(df["close"].tail(200).mean()) if len(df) >= 200 else ma50
    trend_bias = "alta" if ma20 > ma50 else "baixa"

    priority = TIMEFRAME_PRIORITY.get(tf_label, 1)
    rsi_depth = max(0, RSI_WARNING_LIMIT - rsi_value)
    extreme_bonus = 12 if level_key == "extreme" else 0

    score = 38 + (priority * 8) + min(rsi_depth * 1.4, 18) + extreme_bonus
    if close <= recent_low * 1.015:
        score += 8
    if range_position <= 0.382:
        score += 8
    if ma20 > ma50:
        score += 7
    if close < ma200:
        score -= 5
    score = max(0, min(round(score), 95))

    if score >= 75:
        confidence = "alta"
    elif score >= 58:
        confidence = "media"
    else:
        confidence = "baixa"

    entry_price = close
    entry_pullback_pct = min(max(atr_pct * 0.25, 0.25), 1.2)
    entry_zone_low = entry_price * (1 - entry_pullback_pct / 100)
    entry_zone_high = entry_price * (1 + 0.15 / 100)

    tp1_roi_pct = min(30 + ((priority - 1) * 10) + extreme_bonus, 85)
    tp2_roi_pct = min(50 + ((priority - 1) * 15) + (extreme_bonus * 1.25), 130)
    tp1_price_pct = tp1_roi_pct / LEVERAGE
    tp2_price_pct = tp2_roi_pct / LEVERAGE

    support_sl_pct = max(((entry_price - recent_low) / entry_price) * 100 + (atr_pct * 0.35), 0)
    sl_price_pct = min(max(support_sl_pct, atr_pct * 1.2, 1.0), 6.0)
    sl_roi_pct = sl_price_pct * LEVERAGE

    tp1_price = entry_price * (1 + tp1_price_pct / 100)
    tp2_price = entry_price * (1 + tp2_price_pct / 100)
    sl_price = entry_price * (1 - sl_price_pct / 100)

    fib_note = "correcao profunda perto do fundo recente"
    if range_position > 0.618:
        fib_note = "preco ainda alto no range recente"
    elif range_position > 0.382:
        fib_note = "meio do range, aguardar confirmacao reduz risco"

    notes = [
        f"Prioridade {tf_label}: {priority}/4",
        f"RSI {rsi_value:.2f}",
        f"Tendencia curta: {trend_bias} (MA20 {'>' if ma20 > ma50 else '<='} MA50)",
        f"Leitura tipo Elliott/Fibo: {fib_note}",
        f"ATR aprox.: {pct_fmt(atr_pct)}",
    ]

    qualified, qualification_reason = is_backtest_qualified(backtest_stats)

    if backtest_stats and backtest_stats["sample_size"] >= BACKTEST_MIN_TRADES:
        win_rate = backtest_stats["win_rate_pct"]
        if win_rate >= 65:
            confidence = "alta"
            score = min(score + 8, 95)
        elif win_rate >= 52:
            confidence = "media" if confidence == "baixa" else confidence
            score = min(score + 3, 95)
        else:
            confidence = "baixa"
            score = max(score - 12, 0)

        notes.append(
            f"Backtest local: {pct_fmt(win_rate)} assertividade em {backtest_stats['sample_size']} sinais"
        )
        notes.append(
            f"ROI medio simulado: {pct_fmt(backtest_stats['avg_roi_pct'])}; payoff {backtest_stats['profit_factor']:.2f}"
        )
    else:
        notes.append("Backtest local: amostra insuficiente para calibrar confianca")

    if not qualified:
        confidence = "baixa"
        score = min(score, 55)
        notes.append(f"Plano ocultado: {qualification_reason}")

    return {
        "side": "long educativo",
        "leverage": LEVERAGE,
        "confidence": confidence,
        "score": score,
        "qualified": qualified,
        "qualification_reason": qualification_reason,
        "entry_price": entry_price,
        "entry_zone_low": entry_zone_low,
        "entry_zone_high": entry_zone_high,
        "tp1_price": tp1_price,
        "tp1_price_pct": tp1_price_pct,
        "tp1_roi_pct": tp1_roi_pct,
        "tp2_price": tp2_price,
        "tp2_price_pct": tp2_price_pct,
        "tp2_roi_pct": tp2_roi_pct,
        "sl_price": sl_price,
        "sl_price_pct": sl_price_pct,
        "sl_roi_pct": sl_roi_pct,
        "atr_pct": atr_pct,
        "range_position": round(range_position, 3),
        "notes": notes,
        "backtest": backtest_stats,
        "summary": (
            f"Entrada ref. {price_fmt(entry_price)} | "
            f"TP1 +{pct_fmt(tp1_price_pct)} (~{pct_fmt(tp1_roi_pct)} em {LEVERAGE:g}x) | "
            f"TP2 +{pct_fmt(tp2_price_pct)} (~{pct_fmt(tp2_roi_pct)} em {LEVERAGE:g}x) | "
            f"SL -{pct_fmt(sl_price_pct)} (~-{pct_fmt(sl_roi_pct)} em {LEVERAGE:g}x)"
        ),
    }


def simulate_trade_outcome(df: pd.DataFrame, start_index: int, plan: dict, tf_label: str) -> dict:
    """Simula TP/SL apos um sinal historico. Se TP e SL baterem no mesmo candle, usa SL."""
    horizon = BACKTEST_LOOKAHEAD.get(tf_label, 16)
    future = df.iloc[start_index + 1 : start_index + 1 + horizon]
    if future.empty:
        return {"outcome": "open", "roi_pct": 0.0}

    tp1_price = plan["tp1_price"]
    tp2_price = plan["tp2_price"]
    sl_price = plan["sl_price"]

    for _, candle in future.iterrows():
        if float(candle["low"]) <= sl_price:
            return {"outcome": "sl", "roi_pct": -plan["sl_roi_pct"]}
        if float(candle["high"]) >= tp2_price:
            return {"outcome": "tp2", "roi_pct": plan["tp2_roi_pct"]}
        if float(candle["high"]) >= tp1_price:
            return {"outcome": "tp1", "roi_pct": plan["tp1_roi_pct"]}

    final_close = float(future["close"].iloc[-1])
    roi_pct = ((final_close - plan["entry_price"]) / plan["entry_price"]) * 100 * LEVERAGE
    return {"outcome": "timeout", "roi_pct": roi_pct}


def summarize_trades(trades: list[dict]) -> dict:
    if not trades:
        return {
            "sample_size": 0,
            "win_rate_pct": 0.0,
            "avg_roi_pct": 0.0,
            "profit_factor": 0.0,
            "wins": 0,
            "losses": 0,
            "timeouts": 0,
        }

    wins = [trade for trade in trades if trade["roi_pct"] > 0]
    losses = [trade for trade in trades if trade["roi_pct"] < 0]
    gross_profit = sum(trade["roi_pct"] for trade in wins)
    gross_loss = abs(sum(trade["roi_pct"] for trade in losses))
    profit_factor = gross_profit / gross_loss if gross_loss else gross_profit

    return {
        "sample_size": len(trades),
        "win_rate_pct": round((len(wins) / len(trades)) * 100, 2),
        "avg_roi_pct": round(sum(trade["roi_pct"] for trade in trades) / len(trades), 2),
        "profit_factor": round(profit_factor, 2),
        "wins": len(wins),
        "losses": len(losses),
        "timeouts": len([trade for trade in trades if trade["outcome"] == "timeout"]),
    }


def evaluate_backtest(df: pd.DataFrame, rsi_series: pd.Series, tf_label: str, level_key: str) -> dict:
    """Backtest walk-forward com validacao fora da amostra."""
    limit = RSI_EXTREME_LIMIT if level_key == "extreme" else RSI_WARNING_LIMIT
    start = max(80, RSI_PERIOD + 50)
    horizon = BACKTEST_LOOKAHEAD.get(tf_label, 16)
    cooldown = BACKTEST_SIGNAL_COOLDOWN or horizon
    end = len(df) - horizon - 1

    if end <= start:
        return summarize_trades([])

    signal_indexes = []
    last_signal_index = -cooldown
    for index in range(start, end):
        if index - last_signal_index < cooldown:
            continue
        if pd.notna(rsi_series.iloc[index]) and float(rsi_series.iloc[index]) < limit:
            signal_indexes.append(index)
            last_signal_index = index

    signal_indexes = signal_indexes[-BACKTEST_MAX_SIGNALS:]

    trades = []
    for index in signal_indexes:
        historical_df = df.iloc[: index + 1]
        historical_rsi = float(rsi_series.iloc[index])
        plan = build_trade_plan(historical_df, historical_rsi, tf_label, level_key)
        outcome = simulate_trade_outcome(df, index, plan, tf_label)
        if outcome["outcome"] != "open":
            trades.append(outcome)

    validation_size = max(BACKTEST_MIN_TRADES, round(len(trades) * BACKTEST_VALIDATION_RATIO))
    validation_size = min(validation_size, len(trades))
    train_trades = trades[: len(trades) - validation_size]
    validation_trades = trades[len(trades) - validation_size :]

    train_stats = summarize_trades(train_trades)
    validation_stats = summarize_trades(validation_trades)
    all_stats = summarize_trades(trades)
    overfit_gap = round(train_stats["win_rate_pct"] - validation_stats["win_rate_pct"], 2)
    overfit_warning = (
        train_stats["sample_size"] >= BACKTEST_MIN_TRADES
        and validation_stats["sample_size"] >= BACKTEST_MIN_TRADES
        and overfit_gap > 20
    )

    validation_stats.update(
        {
            "train_stats": train_stats,
            "all_stats": all_stats,
            "validation_ratio": BACKTEST_VALIDATION_RATIO,
            "cooldown_candles": cooldown,
            "overfit_gap_pct": overfit_gap,
            "overfit_warning": overfit_warning,
        }
    )
    return validation_stats


def train_one_strategy(symbol: str, tf_label: str, tf_interval: str) -> dict:
    df = get_historical_candles(symbol, tf_interval, total_limit=TRAINING_CANDLE_LIMIT)
    results = {}
    if df is None:
        for level in ALERT_LEVELS:
            results[model_key(symbol, tf_label, level["key"])] = {
                "sample_size": 0,
                "win_rate_pct": 0.0,
                "avg_roi_pct": 0.0,
                "profit_factor": 0.0,
                "error": "candles_unavailable",
            }
        return results

    rsi_series = calc_rsi(df["close"])
    for level in ALERT_LEVELS:
        stats = evaluate_backtest(df, rsi_series, tf_label, level["key"])
        qualified, reason = is_backtest_qualified(stats)
        stats.update(
            {
                "symbol": symbol,
                "tf": tf_label,
                "level": level["key"],
                "qualified": qualified,
                "qualification_reason": reason,
            }
        )
        results[model_key(symbol, tf_label, level["key"])] = stats

    return results


def train_strategy_model() -> bool:
    """Treina/calibra as estatisticas historicas em background."""
    if not _model_lock.acquire(blocking=False):
        return False

    if _strategy_model["running"]:
        _model_lock.release()
        return False

    _strategy_model["running"] = True
    _model_lock.release()

    print("[TREINO] Iniciando calibracao historica dos planos RSI...")
    jobs = [
        (symbol, tf_label, tf_interval)
        for symbol in SYMBOLS
        for tf_label, tf_interval in TIMEFRAMES.items()
    ]
    stats = {}

    try:
        with ThreadPoolExecutor(max_workers=SCAN_MAX_WORKERS) as executor:
            futures = [
                executor.submit(train_one_strategy, symbol, tf_label, tf_interval)
                for symbol, tf_label, tf_interval in jobs
            ]
            for future in as_completed(futures):
                stats.update(future.result())

        with _model_lock:
            _strategy_model["trained_at_utc"] = utc_now().isoformat()
            _strategy_model["running"] = False
            _strategy_model["stats"] = stats

        qualified_count = len([item for item in stats.values() if item.get("qualified")])
        print(f"[TREINO] Concluido. Estrategias aprovadas: {qualified_count}/{len(stats)}.")
        return True
    except Exception as exc:
        with _model_lock:
            _strategy_model["running"] = False
        print(f"[ERRO] Treino: {exc}")
        return False


def build_model_report() -> dict:
    stats = _strategy_model["stats"]
    by_timeframe = {}
    by_reason = {}

    for item in stats.values():
        tf = item.get("tf", "unknown")
        level = item.get("level", "unknown")
        reason = item.get("qualification_reason", "sem motivo")
        by_timeframe.setdefault(
            tf,
            {
                "total": 0,
                "qualified": 0,
                "levels": {},
                "validation_samples": [],
                "train_samples": [],
            },
        )
        by_timeframe[tf]["total"] += 1
        by_timeframe[tf]["qualified"] += 1 if item.get("qualified") else 0
        by_timeframe[tf]["levels"].setdefault(level, {"total": 0, "qualified": 0})
        by_timeframe[tf]["levels"][level]["total"] += 1
        by_timeframe[tf]["levels"][level]["qualified"] += 1 if item.get("qualified") else 0
        by_timeframe[tf]["validation_samples"].append(item.get("sample_size", 0))
        by_timeframe[tf]["train_samples"].append(item.get("train_stats", {}).get("sample_size", 0))
        by_reason[reason] = by_reason.get(reason, 0) + 1

    for tf, item in by_timeframe.items():
        validation_samples = item.pop("validation_samples")
        train_samples = item.pop("train_samples")
        item["avg_validation_sample"] = (
            round(sum(validation_samples) / len(validation_samples), 2) if validation_samples else 0
        )
        item["min_validation_sample"] = min(validation_samples) if validation_samples else 0
        item["avg_train_sample"] = (
            round(sum(train_samples) / len(train_samples), 2) if train_samples else 0
        )
        item["min_train_sample"] = min(train_samples) if train_samples else 0

    qualified = [item for item in stats.values() if item.get("qualified")]
    return {
        "trained_at_utc": _strategy_model["trained_at_utc"],
        "running": _strategy_model["running"],
        "total": len(stats),
        "qualified": len(qualified),
        "rejected": len(stats) - len(qualified),
        "by_timeframe": by_timeframe,
        "rejection_reasons": by_reason,
        "thresholds": {
            "backtest_min_trades": BACKTEST_MIN_TRADES,
            "backtest_validation_ratio": BACKTEST_VALIDATION_RATIO,
            "backtest_signal_cooldown": BACKTEST_SIGNAL_COOLDOWN,
            "plan_min_win_rate": PLAN_MIN_WIN_RATE,
            "plan_min_profit_factor": PLAN_MIN_PROFIT_FACTOR,
            "plan_min_avg_roi": PLAN_MIN_AVG_ROI,
            "plan_min_score": PLAN_MIN_SCORE,
        },
    }


def scan_one_rsi(symbol: str, tf_label: str, tf_interval: str, now_slot: str) -> tuple[dict, list[dict]]:
    """Calcula o RSI de um par/timeframe."""
    df = get_candles(symbol, tf_interval)
    if df is None:
        return (
            {
                "symbol": symbol,
                "tf": tf_label,
                "rsi": None,
                "alert": False,
                "error": "candles_unavailable",
                "details": _last_candle_errors.get(f"{symbol}_{tf_interval}"),
            },
            [],
        )

    rsi = calc_rsi(df["close"]).dropna()
    if rsi.empty:
        return (
            {
                "symbol": symbol,
                "tf": tf_label,
                "rsi": None,
                "alert": False,
                "error": "insufficient_data",
            },
            [],
        )

    value = float(rsi.iloc[-1])
    triggered_levels = [level for level in ALERT_LEVELS if value < level["limit"]]
    plan_level = "extreme" if any(level["key"] == "extreme" for level in triggered_levels) else "warning"
    backtest_stats = get_strategy_stats(symbol, tf_label, plan_level) if triggered_levels else None
    plan = build_trade_plan(df, value, tf_label, plan_level, backtest_stats)
    item = {
        "symbol": symbol,
        "tf": tf_label,
        "rsi": round(value, 2),
        "price": round(float(df["close"].iloc[-1]), 8),
        "alert": bool(triggered_levels),
        "alert_levels": [level["key"] for level in triggered_levels],
        "already_alerted_this_hour": [
            level["key"]
            for level in triggered_levels
            if f"{symbol}_{tf_label}_{level['key']}_{now_slot}" in _alerted
        ],
        "trade_plan": plan if triggered_levels else None,
    }
    sendable, send_reason = is_plan_sendable(plan) if triggered_levels else (False, "sem alerta")
    item["signal_ready"] = sendable
    item["signal_reason"] = send_reason

    alerts = []
    for level in triggered_levels:
        key = f"{symbol}_{tf_label}_{level['key']}_{now_slot}"
        should_send = sendable or not SEND_ONLY_QUALIFIED_SIGNALS
        if should_send and key not in _alerted:
            alerts.append(
                {
                    "symbol": symbol,
                    "tf": tf_label,
                    "rsi": value,
                    "key": key,
                            "level": level["key"],
                            "limit": level["limit"],
                            "price": float(df["close"].iloc[-1]),
                            "trade_plan": plan,
                        }
                    )

    return item, alerts


def scan_rsi_values() -> tuple[list[dict], list[dict]]:
    """Calcula todos os RSI em paralelo e retorna valores atuais e alertas pendentes."""
    now_slot = utc_now().strftime("%Y-%m-%d %H")
    values = []
    alerts = []
    jobs = [
        (symbol, tf_label, tf_interval)
        for symbol in SYMBOLS
        for tf_label, tf_interval in TIMEFRAMES.items()
    ]

    with ThreadPoolExecutor(max_workers=SCAN_MAX_WORKERS) as executor:
        futures = [
            executor.submit(scan_one_rsi, symbol, tf_label, tf_interval, now_slot)
            for symbol, tf_label, tf_interval in jobs
        ]
        for future in as_completed(futures):
            item, item_alerts = future.result()
            values.append(item)
            alerts.extend(item_alerts)

    values.sort(key=lambda item: (SYMBOLS.index(item["symbol"]), list(TIMEFRAMES).index(item["tf"])))
    return values, alerts


def scan_rsi_values_locked(force: bool = False) -> tuple[list[dict], list[dict], bool]:
    """Executa um scan por vez e guarda o ultimo resultado para diagnostico rapido."""
    acquired = _scan_lock.acquire(timeout=1) if force else _scan_lock.acquire(blocking=False)
    if not acquired:
        return _last_scan["values"], _last_scan["pending_alerts"], False

    try:
        values, alerts = scan_rsi_values()
        _last_scan["checked_at_utc"] = utc_now().isoformat()
        _last_scan["values"] = values
        _last_scan["pending_alerts"] = [
            {
                "symbol": alert["symbol"],
                "tf": alert["tf"],
                "rsi": round(alert["rsi"], 2),
                "level": alert["level"],
                "limit": alert["limit"],
                "price": round(alert["price"], 8),
                "trade_plan": alert["trade_plan"],
            }
            for alert in alerts
        ]
        return values, alerts, True
    finally:
        _scan_lock.release()


def send_email_via_resend(subject: str, body_html: str) -> bool:
    """Envia e-mail pela API HTTPS do Resend."""
    if not RESEND_API_KEY or not EMAIL_TO:
        print("[ERRO] Resend: configure RESEND_API_KEY e EMAIL_TO.")
        return False

    try:
        response = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": RESEND_FROM,
                "to": [EMAIL_TO],
                "subject": subject,
                "html": body_html,
            },
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        if response.status_code >= 400:
            print(f"[ERRO] Resend: HTTP {response.status_code} - {response.text}")
            return False

        print(f"[EMAIL] Enviado via Resend: {subject}")
        return True
    except Exception as exc:
        print(f"[ERRO] Resend: {exc}")
        return False


def send_email_via_gmail(subject: str, body_html: str) -> bool:
    """Envia e-mail via Gmail SMTP."""
    if not EMAIL_FROM or not EMAIL_TO or not GMAIL_PASS:
        print("[ERRO] E-mail: configure EMAIL_FROM, EMAIL_TO e GMAIL_PASS.")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = EMAIL_FROM
        msg["To"] = EMAIL_TO
        msg.attach(MIMEText(body_html, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(EMAIL_FROM, GMAIL_PASS)
            smtp.send_message(msg)

        print(f"[EMAIL] Enviado: {subject}")
        return True
    except Exception as exc:
        print(f"[ERRO] E-mail: {exc}")
        return False


def send_email(subject: str, body_html: str) -> bool:
    """Envia e-mail pelo provedor configurado."""
    if RESEND_API_KEY:
        return send_email_via_resend(subject, body_html)

    return send_email_via_gmail(subject, body_html)


def is_email_configured() -> bool:
    if RESEND_API_KEY:
        return bool(EMAIL_TO)

    return bool(EMAIL_FROM and EMAIL_TO and GMAIL_PASS)


def build_email_html(alerts: list[dict], alert_level: dict) -> str:
    """Monta um e-mail HTML com os alertas."""
    rows = ""
    for alert in alerts:
        value = alert["rsi"]
        color = alert_level["color"]
        status = alert_level["status"].format(limit=alert_level["limit"])
        plan = alert["trade_plan"]
        plan_notes = "<br>".join(html.escape(note) for note in plan["notes"])
        if plan["qualified"]:
            plan_html = f"""
            <strong>Score:</strong> {plan['score']}/95 ({html.escape(plan['confidence'])})<br>
            <strong>Entrada:</strong> {price_fmt(plan['entry_price'])}
            <span style="color:#777">(zona {price_fmt(plan['entry_zone_low'])} - {price_fmt(plan['entry_zone_high'])})</span><br>
            <strong>TP1:</strong> {price_fmt(plan['tp1_price'])} (+{pct_fmt(plan['tp1_price_pct'])}; ~{pct_fmt(plan['tp1_roi_pct'])} em {plan['leverage']:g}x)<br>
            <strong>TP2:</strong> {price_fmt(plan['tp2_price'])} (+{pct_fmt(plan['tp2_price_pct'])}; ~{pct_fmt(plan['tp2_roi_pct'])} em {plan['leverage']:g}x)<br>
            <strong>SL:</strong> {price_fmt(plan['sl_price'])} (-{pct_fmt(plan['sl_price_pct'])}; ~-{pct_fmt(plan['sl_roi_pct'])} em {plan['leverage']:g}x)<br>
            <span style="color:#777">{plan_notes}</span>
            """
        else:
            plan_html = f"""
            <strong>Sem sugestao de entrada.</strong><br>
            <span style="color:#777">
              O alerta de RSI continua valido, mas o plano de TP/SL foi ocultado porque
              {html.escape(plan['qualification_reason'])}.<br>
              {plan_notes}
            </span>
            """
        rows += f"""
        <tr>
          <td style="padding:8px 12px;font-weight:bold">{html.escape(alert['symbol'])}</td>
          <td style="padding:8px 12px">{html.escape(alert['tf'])}</td>
          <td style="padding:8px 12px">{price_fmt(alert['price'])}</td>
          <td style="padding:8px 12px;color:{color};font-weight:bold">{value:.2f}</td>
          <td style="padding:8px 12px">{status}</td>
          <td style="padding:8px 12px;font-size:12px;line-height:1.45">{plan_html}</td>
        </tr>"""

    return f"""
    <html><body style="font-family:Arial,sans-serif;background:#f5f5f5;padding:20px">
      <div style="max-width:640px;margin:auto;background:white;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.1)">
        <div style="background:#1a1a2e;color:white;padding:20px 24px">
          <h2 style="margin:0">{alert_level['title'].format(limit=alert_level['limit'])}</h2>
          <p style="margin:4px 0 0;opacity:.8;font-size:13px">{format_display_time()}</p>
        </div>
        <div style="padding:20px">
          <table style="width:100%;border-collapse:collapse">
            <thead>
              <tr style="background:#f0f0f0">
                <th style="padding:8px 12px;text-align:left">Par</th>
                <th style="padding:8px 12px;text-align:left">Timeframe</th>
                <th style="padding:8px 12px;text-align:left">Preco</th>
                <th style="padding:8px 12px;text-align:left">RSI</th>
                <th style="padding:8px 12px;text-align:left">Status</th>
                <th style="padding:8px 12px;text-align:left">Plano tecnico</th>
              </tr>
            </thead>
            <tbody>{rows}</tbody>
          </table>
        </div>
        <div style="padding:12px 24px;background:#fafafa;font-size:12px;color:#888">
          Alerta automatico. RSI periodo {RSI_PERIOD}. Plano tecnico educacional, nao e recomendacao financeira.
          Em alavancagem, perdas tambem sao multiplicadas; use tamanho de posicao e margem com cautela.
        </div>
      </div>
    </body></html>"""


def check_rsi() -> list[dict]:
    """Verifica o RSI e retorna apenas sinais aprovados para envio."""
    global _alerted, _last_email_results

    now = utc_now()
    now_slot = now.strftime("%Y-%m-%d %H")
    alerts = []

    print(f"\n[{now.strftime('%d/%m %H:%M')}] Verificando RSI...")
    _last_email_results = []

    values, alerts, executed = scan_rsi_values_locked(force=True)
    if not executed:
        print("  Scan ja em andamento; usando ultimo resultado em cache.")
    for item in values:
        if item["rsi"] is None:
            print(f"  {item['symbol']} {item['tf']}: {item['error']}")
        else:
            print(f"  {item['symbol']} {item['tf']}: RSI = {item['rsi']:.2f}")

    # Limpa alertas antigos e mantem apenas o slot atual.
    _alerted = {key for key in _alerted if now_slot in key}

    if alerts:
        for alert_level in ALERT_LEVELS:
            level_alerts = [
                alert for alert in alerts if alert["level"] == alert_level["key"]
            ]
            if not level_alerts:
                continue

            pairs = ", ".join(sorted({alert["symbol"] for alert in level_alerts}))
            subject = f"{alert_level['subject'].format(limit=alert_level['limit'])} - {pairs}"
            sent = send_email(subject, build_email_html(level_alerts, alert_level))
            _last_email_results.append(
                {
                    "level": alert_level["key"],
                    "subject": subject,
                    "alerts": len(level_alerts),
                    "sent": sent,
                }
            )
            if sent:
                _alerted.update(alert["key"] for alert in level_alerts)
            else:
                print(
                    "[ALERTA] E-mail nao enviado; alerta sera tentado novamente na proxima checagem."
                )
    else:
        print("  Nenhum alerta disparado.")

    return alerts


def start_scheduler() -> None:
    """Inicia o agendador tambem quando o app roda via gunicorn."""
    global _scheduler

    if _scheduler is not None and _scheduler.running:
        return

    if os.getenv("DISABLE_SCHEDULER", "").lower() in {"1", "true", "yes"}:
        print("[SCHEDULER] Desativado por DISABLE_SCHEDULER.")
        return

    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        check_rsi,
        "interval",
        minutes=CHECK_INTERVAL_MIN,
        id="check_rsi",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    _scheduler.add_job(
        train_strategy_model,
        "interval",
        minutes=TRAINING_INTERVAL_MIN,
        id="train_strategy_model",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
        next_run_time=utc_now(),
    )
    _scheduler.start()
    print(f"[SCHEDULER] Verificacao agendada a cada {CHECK_INTERVAL_MIN} minutos.")
    print(f"[SCHEDULER] Treino agendado a cada {TRAINING_INTERVAL_MIN} minutos.")


@app.route("/")
def home():
    return (
        f"<h3>RSI Monitor rodando</h3>"
        f"<p>Proxima verificacao em ate {CHECK_INTERVAL_MIN} minutos.</p>"
        f"<p>Pares: {', '.join(SYMBOLS)}</p>"
        f"<p>Timeframes: {', '.join(TIMEFRAMES.keys())}</p>"
        f"<p>Alertas quando RSI &lt; {RSI_WARNING_LIMIT:g} e RSI &lt; {RSI_EXTREME_LIMIT:g}</p>"
        f"<p>E-mail apenas quando call qualificada: {SEND_ONLY_QUALIFIED_SIGNALS}</p>"
        f"<p>Horario atual: {format_display_time()}</p>"
    ), 200


@app.route("/check")
def force_check():
    """Rota para disparar verificacao manual."""
    alerts = check_rsi()
    if _last_email_results:
        sent = sum(1 for result in _last_email_results if result["sent"])
        failed = sum(1 for result in _last_email_results if not result["sent"])
        return (
            f"Verificacao executada. Sinais qualificados: {len(alerts)}. "
            f"E-mails enviados: {sent}. Falhas de e-mail: {failed}.",
            200,
        )

    return (
        f"Verificacao executada. Sinais qualificados: {len(alerts)}. "
        "Nenhum e-mail pendente. Veja /rsi para candidatos recusados.",
        200,
    )


@app.route("/train")
def force_train():
    """Forca treinamento/calibracao historica."""
    started = train_strategy_model()
    if started:
        return "Treinamento executado. Confira /rsi para ver estrategias aprovadas.", 200

    return "Treinamento ja esta em andamento. Confira /rsi em instantes.", 202


@app.route("/model-report")
def model_report():
    """Relatorio do treino por timeframe, nivel e motivo de aprovacao/reprovacao."""
    return jsonify(build_model_report())


@app.route("/rsi")
def rsi_status():
    """Mostra os RSI que o app calculou por ultimo, sem travar em consultas externas."""
    return jsonify(
        {
            "checked_at_utc": _last_scan["checked_at_utc"],
            "served_at_utc": utc_now().isoformat(),
            "served_at_local": display_now().isoformat(),
            "config": {
                "rsi_period": RSI_PERIOD,
                "rsi_warning_limit": RSI_WARNING_LIMIT,
                "rsi_extreme_limit": RSI_EXTREME_LIMIT,
                "check_interval_min": CHECK_INTERVAL_MIN,
                "http_timeout_seconds": HTTP_TIMEOUT_SECONDS,
                "scan_max_workers": SCAN_MAX_WORKERS,
                "leverage": LEVERAGE,
                "backtest_min_trades": BACKTEST_MIN_TRADES,
                "backtest_max_signals": BACKTEST_MAX_SIGNALS,
                "backtest_validation_ratio": BACKTEST_VALIDATION_RATIO,
                "backtest_signal_cooldown": BACKTEST_SIGNAL_COOLDOWN,
                "training_interval_min": TRAINING_INTERVAL_MIN,
                "training_candle_limit": TRAINING_CANDLE_LIMIT,
                "plan_min_win_rate": PLAN_MIN_WIN_RATE,
                "plan_min_profit_factor": PLAN_MIN_PROFIT_FACTOR,
                "plan_min_avg_roi": PLAN_MIN_AVG_ROI,
                "plan_min_score": PLAN_MIN_SCORE,
                "send_only_qualified_signals": SEND_ONLY_QUALIFIED_SIGNALS,
                "display_timezone": DISPLAY_TIMEZONE,
                "binance_base_urls": BINANCE_BASE_URLS,
                "symbols": SYMBOLS,
                "timeframes": list(TIMEFRAMES.keys()),
            },
            "email_configured": is_email_configured(),
            "email_provider": "resend" if RESEND_API_KEY else "gmail_smtp",
            "resend_from": RESEND_FROM if RESEND_API_KEY else None,
            "last_email_results": _last_email_results,
            "scan_running": _scan_lock.locked(),
            "strategy_model": {
                "trained_at_utc": _strategy_model["trained_at_utc"],
                "running": _strategy_model["running"],
                "qualified_count": len(
                    [item for item in _strategy_model["stats"].values() if item.get("qualified")]
                ),
                "total_count": len(_strategy_model["stats"]),
                "report_url": "/model-report",
                "stats": _strategy_model["stats"],
            },
            "pending_alerts": _last_scan["pending_alerts"],
            "values": _last_scan["values"],
        }
    )


start_scheduler()


if __name__ == "__main__":
    check_rsi()
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
