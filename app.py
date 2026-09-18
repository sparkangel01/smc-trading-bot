import os
import numpy as np
import pandas as pd
import requests
from flask import Flask, jsonify, request, render_template

app = Flask(__name__)

TWELVE_KEY = os.environ.get("TWELVE_DATA_API_KEY", "")
TWELVE_BASE = "https://api.twelvedata.com"

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


def get_ohlc(symbol, interval="4h", limit=300):
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


def compute_atr_series(df):
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(14).mean().bfill()


def find_bos(df):
    """Return list of (direction, level, break_index)."""
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values

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
    last_high = None
    last_low = None
    for i, price, kind in swings:
        if kind == "high":
            if last_high is not None:
                seg = closes[last_high[0] + 1:i + 1]
                if len(seg) and seg.max() > last_high[1]:
                    bos.append(("bullish", last_high[1], i))
            last_high = (i, price)
        else:
            if last_low is not None:
                seg = closes[last_low[0] + 1:i + 1]
                if len(seg) and seg.min() < last_low[1]:
                    bos.append(("bearish", last_low[1], i))
            last_low = (i, price)

    return bos


def evaluate_history(df, bos_list):
    """For each past BOS, check outcome."""
    atr_series = compute_atr_series(df)
    results = []

    for direction, level, idx in bos_list[-10:]:
        atr = float(atr_series.iloc[idx])
        entry = float(df["close"].iloc[idx])

        if direction == "bullish":
            sl = entry - atr * 1.5
            risk = entry - sl
            tp1 = entry + risk * 1.5
            tp2 = entry + risk * 3
        else:
            sl = entry + atr * 1.5
            risk = sl - entry
            tp1 = entry - risk * 1.5
            tp2 = entry - risk * 3

        outcome = "ACTIVE"
        after = df.iloc[idx + 1:]

        for _, c in after.iterrows():
            if direction == "bullish":
                if c["low"] <= sl:
                    outcome = "STOPPED"
                    break
                if c["high"] >= tp2:
                    outcome = "TP2_HIT"
                    break
                if c["high"] >= tp1 and outcome == "ACTIVE":
                    outcome = "TP1_HIT"
            else:
                if c["high"] >= sl:
                    outcome = "STOPPED"
                    break
                if c["low"] <= tp2:
                    outcome = "TP2_HIT"
                    break
                if c["low"] <= tp1 and outcome == "ACTIVE":
                    outcome = "TP1_HIT"

        results.append({
            "time": df.index[idx].strftime("%m-%d %H:%M"),
            "direction": direction.upper(),
            "entry": round(entry, 5),
            "sl": round(sl, 5),
            "tp1": round(tp1, 5),
            "tp2": round(tp2, 5),
            "outcome": outcome,
        })

    return results


def analyze(df):
    closes = df["close"].values
    last = float(closes[-1])
    atr = float(compute_atr_series(df).iloc[-1])

    bos_list = find_bos(df)

    signal = {"action": "WAIT", "entry": None, "sl": None,
              "tp1": None, "tp2": None, "reason": [],
              "bars_since_bos": None}

    if not bos_list:
        signal["reason"].append("No clear structure break")
        history = []
        return signal, history

    direction, level, bos_idx = bos_list[-1]
    bars_since = len(df) - 1 - bos_idx
    signal["bars_since_bos"] = bars_since

    if direction == "bullish":
        signal["action"] = "BUY"
        entry = last
        sl = entry - atr * 1.5
        risk = entry - sl
        signal["entry"] = round(entry, 5)
        signal["sl"] = round(sl, 5)
        signal["tp1"] = round(entry + risk * 1.5, 5)
        signal["tp2"] = round(entry + risk * 3, 5)
        signal["reason"].append(f"Bullish BOS · {bars_since} bars ago")
    else:
        signal["action"] = "SELL"
        entry = last
        sl = entry + atr * 1.5
        risk = sl - entry
        signal["entry"] = round(entry, 5)
        signal["sl"] = round(sl, 5)
        signal["tp1"] = round(entry - risk * 1.5, 5)
        signal["tp2"] = round(entry - risk * 3, 5)
        signal["reason"].append(f"Bearish BOS · {bars_since} bars ago")

    history = evaluate_history(df, bos_list)
    return signal, history


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/analyze")
def api_analyze():
    symbol = request.args.get("symbol", "EURUSD")
    interval = request.args.get("interval", "4h")
    try:
        df = get_ohlc(symbol, interval)
        signal, history = analyze(df)
        candles = [{
            "time": idx.strftime("%Y-%m-%d %H:%M"),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
        } for idx, r in df.iterrows()]
        return jsonify({
            "success": True,
            "candles": candles,
            "signal": signal,
            "history": history,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
