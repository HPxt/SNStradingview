import html
import os
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pandas as pd
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask

app = Flask(__name__)

# ============================================================
#  CONFIGURACOES - edite aqui ou use variaveis de ambiente
# ============================================================
EMAIL_FROM = os.getenv("EMAIL_FROM", "seuemail@gmail.com")
EMAIL_TO = os.getenv("EMAIL_TO", "destino@gmail.com")
GMAIL_PASS = os.getenv("GMAIL_PASS", "sua_app_password")

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
RSI_LIMIT = float(os.getenv("RSI_LIMIT", "40"))
CHECK_INTERVAL_MIN = int(os.getenv("CHECK_INTERVAL_MIN", "15"))

BINANCE_BASE_URL = os.getenv("BINANCE_BASE_URL", "https://api.binance.com")
BINANCE_KLINES_URL = f"{BINANCE_BASE_URL.rstrip('/')}/api/v3/klines"
HTTP_TIMEOUT_SECONDS = 10
CANDLE_LIMIT = max(RSI_PERIOD + 50, 100)

# Controle para nao enviar e-mail duplicado na mesma janela.
_alerted = set()
_scheduler: BackgroundScheduler | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def get_candles(symbol: str, interval: str, limit: int = CANDLE_LIMIT) -> pd.DataFrame | None:
    """Busca candles publicos da Binance Spot."""
    try:
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        response = requests.get(BINANCE_KLINES_URL, params=params, timeout=HTTP_TIMEOUT_SECONDS)
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
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df = df.dropna(subset=["close"])
        return df if not df.empty else None
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "sem status"
        print(f"[ERRO] Binance {symbol} {interval}: HTTP {status} - {exc}")
        return None
    except requests.RequestException as exc:
        print(f"[ERRO] Binance {symbol} {interval}: {exc}")
        return None
    except Exception as exc:
        print(f"[ERRO] Candles {symbol} {interval}: {exc}")
        return None


def calc_rsi(series: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """Calcula RSI usando media exponencial suavizada, mais proximo do padrao Wilder."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.mask(avg_loss == 0, 100)
    rsi = rsi.mask((avg_gain == 0) & (avg_loss == 0), 50)
    return rsi


def send_email(subject: str, body_html: str) -> bool:
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


def build_email_html(alerts: list[dict]) -> str:
    """Monta um e-mail HTML com os alertas."""
    rows = ""
    for alert in alerts:
        value = alert["rsi"]
        color = "#c0392b" if value < 30 else "#e67e22"
        status = "Sobrevendido extremo" if value < 30 else f"Abaixo de {RSI_LIMIT:g}"
        rows += f"""
        <tr>
          <td style="padding:8px 12px;font-weight:bold">{html.escape(alert['symbol'])}</td>
          <td style="padding:8px 12px">{html.escape(alert['tf'])}</td>
          <td style="padding:8px 12px;color:{color};font-weight:bold">{value:.2f}</td>
          <td style="padding:8px 12px">{status}</td>
        </tr>"""

    return f"""
    <html><body style="font-family:Arial,sans-serif;background:#f5f5f5;padding:20px">
      <div style="max-width:640px;margin:auto;background:white;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.1)">
        <div style="background:#1a1a2e;color:white;padding:20px 24px">
          <h2 style="margin:0">Alerta RSI - Abaixo de {RSI_LIMIT:g}</h2>
          <p style="margin:4px 0 0;opacity:.8;font-size:13px">{utc_now().strftime('%d/%m/%Y %H:%M')} UTC</p>
        </div>
        <div style="padding:20px">
          <table style="width:100%;border-collapse:collapse">
            <thead>
              <tr style="background:#f0f0f0">
                <th style="padding:8px 12px;text-align:left">Par</th>
                <th style="padding:8px 12px;text-align:left">Timeframe</th>
                <th style="padding:8px 12px;text-align:left">RSI</th>
                <th style="padding:8px 12px;text-align:left">Status</th>
              </tr>
            </thead>
            <tbody>{rows}</tbody>
          </table>
        </div>
        <div style="padding:12px 24px;background:#fafafa;font-size:12px;color:#888">
          Alerta automatico. RSI periodo {RSI_PERIOD}.
        </div>
      </div>
    </body></html>"""


def check_rsi() -> list[dict]:
    """Verifica o RSI de todos os pares e timeframes."""
    global _alerted

    now = utc_now()
    now_slot = now.strftime("%Y-%m-%d %H")
    alerts = []

    print(f"\n[{now.strftime('%d/%m %H:%M')}] Verificando RSI...")

    for symbol in SYMBOLS:
        for tf_label, tf_interval in TIMEFRAMES.items():
            df = get_candles(symbol, tf_interval)
            if df is None:
                continue

            rsi = calc_rsi(df["close"]).dropna()
            if rsi.empty:
                print(f"  {symbol} {tf_label}: dados insuficientes")
                continue

            value = float(rsi.iloc[-1])
            print(f"  {symbol} {tf_label}: RSI = {value:.2f}")

            key = f"{symbol}_{tf_label}_{now_slot}"
            if value < RSI_LIMIT and key not in _alerted:
                alerts.append(
                    {
                        "symbol": symbol,
                        "tf": tf_label,
                        "rsi": value,
                    }
                )
                _alerted.add(key)

    # Limpa alertas antigos e mantem apenas o slot atual.
    _alerted = {key for key in _alerted if now_slot in key}

    if alerts:
        pairs = ", ".join(sorted({alert["symbol"] for alert in alerts}))
        subject = f"RSI abaixo de {RSI_LIMIT:g} - {pairs}"
        send_email(subject, build_email_html(alerts))
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
    _scheduler.start()
    print(f"[SCHEDULER] Verificacao agendada a cada {CHECK_INTERVAL_MIN} minutos.")


@app.route("/")
def home():
    return (
        f"<h3>RSI Monitor rodando</h3>"
        f"<p>Proxima verificacao em ate {CHECK_INTERVAL_MIN} minutos.</p>"
        f"<p>Pares: {', '.join(SYMBOLS)}</p>"
        f"<p>Timeframes: {', '.join(TIMEFRAMES.keys())}</p>"
        f"<p>Alerta quando RSI &lt; {RSI_LIMIT:g}</p>"
        f"<p>Horario atual: {utc_now().strftime('%d/%m/%Y %H:%M')} UTC</p>"
    ), 200


@app.route("/check")
def force_check():
    """Rota para disparar verificacao manual."""
    alerts = check_rsi()
    return f"Verificacao executada. Alertas encontrados: {len(alerts)}.", 200


start_scheduler()


if __name__ == "__main__":
    check_rsi()
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
