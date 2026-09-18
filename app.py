import os
import smtplib
from email.mime.text import MIMEText
from datetime import datetime

import numpy as np
import pandas as pd
import requests
from flask import Flask, jsonify, request, render_template

app = Flask(__name__)

TWELVE_KEY = os.environ.get("TWELVE_DATA_API_KEY", "")
TWELVE_BASE = "https://api.twelvedata.com"

GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD", "")
ALERT_TO   = os.environ.get("ALERT_EMAIL_TO", GMAIL_USER)
ALERTS_ON  = os.environ.get("ALERTS_ENABLED", "false").lower() == "true"

LAST_SIGNALS = {}

SYMBOL_MAP = {
    "EURUSD": "EUR/USD", "GBPUSD": "GBP/USD", "USDJPY": "USD/JPY",
    "USDCHF": "USD/CHF", "AUDUSD": "AUD/USD", "NZDUSD": "NZD/USD",
    "USDCAD": "USD/CAD", "EURGBP": "EUR/GBP", "EURJPY": "EUR/JPY",
    "GBPJPY": "GBP/JPY", "AUDJPY": "AUD/JPY", "CADJPY": "CAD/JPY",
    "CHFJPY": "CHF/JPY", "NZDJPY": "NZD/JPY",
    "XAUUSD": "XAU/USD", "XAGUSD": "XAG/USD",
    "BTCUSD": "BTC/USD", "ETHUSD": "ETH/USD",
}


def normalize(symbol):
    s = symbol.upper().strip()
    if s in SYMBOL_MAP:
        return SYMBOL_MAP[s]
    if len(s) == 6 and s.isalpha():
        return f"{s[:3]}/{s[3:]}"
    return s


def get_ohlc(symbol, interval="4h", limit=200):
    if not TWELVE_KEY:
        raise ValueError("Add TWELVE_DATA_API_KEY on Render")

    td_symbol = normalize(symbol)
    td_interval = {"15m": "15min", "1h": "1h", "4h": "4h", "1d": "1day"}.get(interval, "4h")

    r = requests.get(f"{TWELVE_BASE}/time_series", params={
        "symbol": td_symbol,
        "interval": td_interval,
        "outputsize": limit,
        "apikey": TWELVE_KEY,
    }, timeout=20)

    data = r.json()
    if data.get("status") == "error":
        raise ValueError(data.get("message", "API error"))

    values = data.get("values")
    if not values:
        raise ValueError(f"No data for {symbol}")

    df = pd.DataFrame(values)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.set_index("datetime").sort_index()
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df[["open", "high", "low", "close"]].dropna()


def analyze(df):
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    last = float(closes[-1])

    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr = float(tr.rolling(14).mean().bfill().iloc[-1])

    swings = []
    n = 3
    for i in range(n, len(df) - n):
        wh = highs[i - n:i + n + 1]
        wl = lows[i - n:i + n + 1]
        if highs[i] == wh.max() and np.sum(wh == highs[i]) == 1:
            swings.append((i, float(highs[i]), "high"))
        if lows[i] == wl.min() and np.sum(wl == lows[i]) == 1:
            swings.append((i, float(lows[i]), "low"))

    bos = []
    last_high = last_low = None
    for i, price, kind in swings:
        if kind == "high":
            if last_high is not None:
                if closes[last_high[0] + 1:i + 1].max() > last_high[1]:
                    bos.append(("bullish", last_high[1]))
            last_high = (i, price)
        else:
            if last_low is not None:
                if closes[last_low[0] + 1:i + 1].min() < last_low[1]:
                    bos.append(("bearish", last_low[1]))
            last_low = (i, price)

    signal = {"action": "WAIT", "entry": None, "sl": None,
              "tp1": None, "tp2": None, "reason": []}

    if not bos:
        signal["reason"].append("No clear structure break")
        return signal

    direction = bos[-1][0]

    if direction == "bullish":
        signal["action"] = "BUY"
        entry = last
        sl = entry - atr * 1.5
        risk = entry - sl
        signal["entry"] = round(entry, 5)
        signal["sl"] = round(sl, 5)
        signal["tp1"] = round(entry + risk * 1.5, 5)
        signal["tp2"] = round(entry + risk * 3, 5)
        signal["reason"].append("Bullish BOS confirmed")
    else:
        signal["action"] = "SELL"
        entry = last
        sl = entry + atr * 1.5
        risk = sl - entry
        signal["entry"] = round(entry, 5)
        signal["sl"] = round(sl, 5)
        signal["tp1"] = round(entry - risk * 1.5, 5)
        signal["tp2"] = round(entry - risk * 3, 5)
        signal["reason"].append("Bearish BOS confirmed")

    return signal


def send_email(subject, body):
    if not ALERTS_ON:
        return False, "Alerts disabled"
    if not (GMAIL_USER and GMAIL_PASS and ALERT_TO):
        return False, "Email not configured"

    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = GMAIL_USER
        msg["To"] = ALERT_TO

        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(GMAIL_USER, GMAIL_PASS)
            server.send_message(msg)
        return True, "Sent"
    except Exception as e:
        return False, str(e)


def format_email(symbol, interval, signal):
    lines = [
        f"{signal['action']} — {symbol} ({interval})",
        "",
        f"Entry:  {signal['entry']}",
        f"SL:     {signal['sl']}",
        f"TP1:    {signal['tp1']}",
        f"TP2:    {signal['tp2']}",
        "",
        "Reason: " + ", ".join(signal["reason"]),
        "",
        f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
    ]
    return "\n".join(lines)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/analyze")
def api_analyze():
    symbol = request.args.get("symbol", "EURUSD")
    interval = request.args.get("interval", "4h")
    notify = request.args.get("notify", "1") == "1"
    try:
        df = get_ohlc(symbol, interval)
        signal = analyze(df)

        # Send email only when action changes
        if notify and signal["action"] in ("BUY", "SELL"):
            key = f"{symbol}_{interval}"
            if LAST_SIGNALS.get(key) != signal["action"]:
                subject = f"[SMC] {signal['action']} {symbol} @ {signal['entry']}"
                send_email(subject, format_email(symbol, interval, signal))
                LAST_SIGNALS[key] = signal["action"]

        candles = [{
            "time": idx.strftime("%Y-%m-%d %H:%M"),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
        } for idx, r in df.iterrows()]

        return jsonify({"success": True, "candles": candles, "signal": signal})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/test-email")
def test_email():
    ok, msg = send_email("[SMC] Test", "Email alerts are working.")
    return jsonify({"sent": ok, "message": msg})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
