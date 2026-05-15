import html
import os
import smtplib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

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
CANDLE_LIMIT = max(RSI_PERIOD + 50, 100)

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


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def get_candles(symbol: str, interval: str, limit: int = CANDLE_LIMIT) -> pd.DataFrame | None:
    """Busca candles publicos da Binance Spot."""
    errors = []

    for base_url in BINANCE_BASE_URLS:
        klines_url = f"{base_url}/api/v3/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
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
            df = df.assign(close=pd.to_numeric(df["close"], errors="coerce"))
            df = df.dropna(subset=["close"])
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
    item = {
        "symbol": symbol,
        "tf": tf_label,
        "rsi": round(value, 2),
        "alert": bool(triggered_levels),
        "alert_levels": [level["key"] for level in triggered_levels],
        "already_alerted_this_hour": [
            level["key"]
            for level in triggered_levels
            if f"{symbol}_{tf_label}_{level['key']}_{now_slot}" in _alerted
        ],
    }

    alerts = []
    for level in triggered_levels:
        key = f"{symbol}_{tf_label}_{level['key']}_{now_slot}"
        if key not in _alerted:
            alerts.append(
                {
                    "symbol": symbol,
                    "tf": tf_label,
                    "rsi": value,
                    "key": key,
                    "level": level["key"],
                    "limit": level["limit"],
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


def build_email_html(alerts: list[dict], alert_level: dict) -> str:
    """Monta um e-mail HTML com os alertas."""
    rows = ""
    for alert in alerts:
        value = alert["rsi"]
        color = alert_level["color"]
        status = alert_level["status"].format(limit=alert_level["limit"])
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
          <h2 style="margin:0">{alert_level['title'].format(limit=alert_level['limit'])}</h2>
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
    _scheduler.start()
    print(f"[SCHEDULER] Verificacao agendada a cada {CHECK_INTERVAL_MIN} minutos.")


@app.route("/")
def home():
    return (
        f"<h3>RSI Monitor rodando</h3>"
        f"<p>Proxima verificacao em ate {CHECK_INTERVAL_MIN} minutos.</p>"
        f"<p>Pares: {', '.join(SYMBOLS)}</p>"
        f"<p>Timeframes: {', '.join(TIMEFRAMES.keys())}</p>"
        f"<p>Alertas quando RSI &lt; {RSI_WARNING_LIMIT:g} e RSI &lt; {RSI_EXTREME_LIMIT:g}</p>"
        f"<p>Horario atual: {utc_now().strftime('%d/%m/%Y %H:%M')} UTC</p>"
    ), 200


@app.route("/check")
def force_check():
    """Rota para disparar verificacao manual."""
    alerts = check_rsi()
    if _last_email_results:
        sent = sum(1 for result in _last_email_results if result["sent"])
        failed = sum(1 for result in _last_email_results if not result["sent"])
        return (
            f"Verificacao executada. Alertas encontrados: {len(alerts)}. "
            f"E-mails enviados: {sent}. Falhas de e-mail: {failed}.",
            200,
        )

    return f"Verificacao executada. Alertas encontrados: {len(alerts)}. Nenhum e-mail pendente.", 200


@app.route("/rsi")
def rsi_status():
    """Mostra os RSI que o app calculou por ultimo, sem travar em consultas externas."""
    return jsonify(
        {
            "checked_at_utc": _last_scan["checked_at_utc"],
            "served_at_utc": utc_now().isoformat(),
            "config": {
                "rsi_period": RSI_PERIOD,
                "rsi_warning_limit": RSI_WARNING_LIMIT,
                "rsi_extreme_limit": RSI_EXTREME_LIMIT,
                "check_interval_min": CHECK_INTERVAL_MIN,
                "http_timeout_seconds": HTTP_TIMEOUT_SECONDS,
                "scan_max_workers": SCAN_MAX_WORKERS,
                "binance_base_urls": BINANCE_BASE_URLS,
                "symbols": SYMBOLS,
                "timeframes": list(TIMEFRAMES.keys()),
            },
            "email_configured": bool(EMAIL_FROM and EMAIL_TO and GMAIL_PASS),
            "email_provider": "resend" if RESEND_API_KEY else "gmail_smtp",
            "resend_from": RESEND_FROM if RESEND_API_KEY else None,
            "last_email_results": _last_email_results,
            "scan_running": _scan_lock.locked(),
            "pending_alerts": _last_scan["pending_alerts"],
            "values": _last_scan["values"],
        }
    )


start_scheduler()


if __name__ == "__main__":
    check_rsi()
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
