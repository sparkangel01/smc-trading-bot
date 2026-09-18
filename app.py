import os
import numpy as np
import pandas as pd
import requests
from flask import Flask, jsonify, request, render_template

app = Flask(__name__)

TWELVE_KEY = os.environ.get("TWELVE_DATA_API_KEY", "")
TWELVE_BASE = "https://api.twelvedata.com"


# ============================================================
#  SYMBOL MAP  (user input -> Twelve Data format)
# ============================================================
SYMBOL_MAP = {
    # Forex majors
    "EURUSD": "EUR/USD", "EUR/USD": "EUR/USD",
    "GBPUSD": "GBP/USD", "GBP/USD": "GBP/USD",
    "USDJPY": "USD/JPY", "USD/JPY": "USD/JPY",
    "USDCHF": "USD/CHF", "USD/CHF": "USD/CHF",
    "AUDUSD": "AUD/USD", "AUD/USD": "AUD/USD",
    "NZDUSD": "NZD/USD", "NZD/USD": "NZD/USD",
    "USDCAD": "USD/CAD", "USD/CAD": "USD/CAD",
    "EURGBP": "EUR/GBP", "EUR/GBP": "EUR/GBP",
    "EURJPY": "EUR/JPY", "EUR/JPY": "EUR/JPY",
    "GBPJPY": "GBP/JPY", "GBP/JPY": "GBP/JPY",
    "AUDJPY": "AUD/JPY", "AUD/JPY": "AUD/JPY",
    "EURAUD": "EUR/AUD", "EUR/AUD": "EUR/AUD",
    "EURCHF": "EUR/CHF", "EUR/CHF": "EUR/CHF",
    "EURCAD": "EUR/CAD", "EUR/CAD": "EUR/CAD",
    "GBPCHF": "GBP/CHF", "GBP/CHF": "GBP/CHF",
    "GBPAUD": "GBP/AUD", "GBP/AUD": "GBP/AUD",
    "GBPCAD": "GBP/CAD", "GBP/CAD": "GBP/CAD",
    "CADJPY": "CAD/JPY", "CAD/JPY": "CAD/JPY",
    "CHFJPY": "CHF/JPY", "CHF/JPY": "CHF/JPY",
    "NZDJPY": "NZD/JPY", "NZD/JPY": "NZD/JPY",
    "USDNGN": "USD/NGN", "USD/NGN": "USD/NGN",
    "NGNUSD": "NGN/USD", "NGN/USD": "NGN/USD",
    # Metals
    "XAUUSD": "XAU/USD", "XAU/USD": "XAU/USD",
    "XAGUSD": "XAG/USD", "XAG/USD": "XAG/USD",
    # Crypto
    "BTCUSD": "BTC/USD", "BTC-USD": "BTC/USD",
    "ETHUSD": "ETH/USD", "ETH-USD": "ETH/USD",
}


def normalize(symbol):
    s = symbol.upper().strip()
    if s in SYMBOL_MAP:
        return SYMBOL_MAP[s]
    if len(s) == 6 and s.isalpha():
        return f"{s[:3]}/{s[3:]}"
    return s


# ============================================================
#  MARKET DATA  (Twelve Data)
# ============================================================
def get_ohlc(symbol, interval="4h", limit=200):
    if not TWELVE_KEY:
        raise ValueError("API key missing. Add TWELVE_DATA_API_KEY on Render.")

    td_symbol = normalize(symbol)

    interval_map = {
        "15m": "15min",
        "1h": "1h",
        "4h": "4h",
        "1d": "1day",
    }
    td_interval = interval_map.get(interval, "4h")

    url = f"{TWELVE_BASE}/time_series"
    params = {
        "symbol": td_symbol,
        "interval": td_interval,
        "outputsize": limit,
        "apikey": TWELVE_KEY,
    }

    try:
        r = requests.get(url, params=params, timeout=20)
    except Exception as e:
        raise ValueError(f"Network error: {e}")

    try:
        data = r.json()
    except Exception:
        raise ValueError(f"Bad response from Twelve Data: {r.text[:200]}")

    if isinstance(data, dict) and data.get("status") == "error":
        raise ValueError(f"Twelve Data: {data.get('message', 'error')}")

    values = data.get("values")
    if not values:
        raise ValueError(f"No data for {symbol} ({td_symbol}). "
                         f"Try EURUSD, GBPUSD, XAUUSD.")

    df = pd.DataFrame(values)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.set_index("datetime").sort_index()

    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    if "volume" not in df.columns:
        df["volume"] = 0
    else:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)

    return df[["open", "high", "low", "close", "volume"]].dropna()


# ============================================================
#  SMC / ICT SIGNAL
# ============================================================
def analyze_smc(df):
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    opens = df["open"].values
    last = float(closes[-1])

    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr = float(tr.rolling(14).mean().bfill().iloc[-1])
    if atr <= 0:
        atr = last * 0.001

    # Swing Points
    swings = []
    n = 3
    for i in range(n, len(df) - n):
        wh = highs[i - n:i + n + 1]
        wl = lows[i - n:i + n + 1]
        if highs[i] == wh.max() and np.sum(wh == highs[i]) == 1:
            swings.append({"i": i, "price": float(highs[i]), "type": "high"})
        if lows[i] == wl.min() and np.sum(wl == lows[i]) == 1:
            swings.append({"i": i, "price": float(lows[i]), "type": "low"})

    # BOS / CHoCH
    bos = []
    last_high = None
    last_low = None
    trend = None
    for sw in swings:
        if sw["type"] == "high":
            if last_high is not None:
                seg = closes[last_high["i"] + 1: sw["i"] + 1]
                if len(seg) and seg.max() > last_high["price"]:
                    kind = "CHoCH" if trend == "bearish" else "BOS"
                    bos.append({"i": sw["i"], "price": last_high["price"],
                                "kind": kind, "dir": "bullish"})
                    trend = "bullish"
            last_high = sw
        else:
            if last_low is not None:
                seg = closes[last_low["i"] + 1: sw["i"] + 1]
                if len(seg) and seg.min() < last_low["price"]:
                    kind = "CHoCH" if trend == "bullish" else "BOS"
                    bos.append({"i": sw["i"], "price": last_low["price"],
                                "kind": kind, "dir": "bearish"})
                    trend = "bearish"
            last_low = sw

    # Order Blocks
    obs = []
    for i in range(2, len(df) - 2):
        body = abs(closes[i] - opens[i])
        if body < atr * 0.4:
            continue
        if closes[i] < opens[i]:
            if closes[i + 1] > highs[i] and closes[i + 2] > highs[i]:
                obs.append({"top": float(highs[i]), "bottom": float(lows[i]),
                            "kind": "bullish"})
        if closes[i] > opens[i]:
            if closes[i + 1] < lows[i] and closes[i + 2] < lows[i]:
                obs.append({"top": float(highs[i]), "bottom": float(lows[i]),
                            "kind": "bearish"})

    # FVGs
    fvgs = []
    for i in range(2, len(df)):
        if lows[i] > highs[i - 2]:
            fvgs.append({"top": float(lows[i]), "bottom": float(highs[i - 2]),
                         "kind": "bullish"})
        if highs[i] < lows[i - 2]:
            fvgs.append({"top": float(lows[i - 2]), "bottom": float(highs[i]),
                         "kind": "bearish"})

    # Decision
    signal = {"action": "WAIT", "entry": None, "sl": None,
              "tp1": None, "tp2": None, "reason": []}
    last_bos = bos[-1] if bos else None

    if last_bos and last_bos["dir"] == "bullish":
        signal["action"] = "BUY"
        bull_obs = [o for o in obs if o["kind"] == "bullish"
                    and abs(((o["top"] + o["bottom"]) / 2) - last) < atr * 3]
        if bull_obs:
            entry = bull_obs[-1]["top"]
            sl = bull_obs[-1]["bottom"] - atr * 0.5
            signal["reason"].append("Entry at bullish Order Block")
        else:
            entry = last
            sl = last - atr * 1.5
        risk = entry - sl
        signal["entry"] = round(entry, 6)
        signal["sl"] = round(sl, 6)
        signal["tp1"] = round(entry + risk * 1.5, 6)
        signal["tp2"] = round(entry + risk * 3, 6)
        signal["reason"].insert(0, f"Bullish {last_bos['kind']} confirmed")

    elif last_bos and last_bos["dir"] == "bearish":
        signal["action"] = "SELL"
        bear_obs = [o for o in obs if o["kind"] == "bearish"
                    and abs(((o["top"] + o["bottom"]) / 2) - last) < atr * 3]
        if bear_obs:
            entry = bear_obs[-1]["bottom"]
            sl = bear_obs[-1]["top"] + atr * 0.5
            signal["reason"].append("Entry at bearish Order Block")
        else:
            entry = last
            sl = last + atr * 1.5
        risk = sl - entry
        signal["entry"] = round(entry, 6)
        signal["sl"] = round(sl, 6)
        signal["tp1"] = round(entry - risk * 1.5, 6)
        signal["tp2"] = round(entry - risk * 3, 6)
        signal["reason"].insert(0, f"Bearish {last_bos['kind']} confirmed")

    else:
        signal["reason"].append("No clear BOS/CHoCH — waiting")

    return signal


# ============================================================
#  ROUTES
# ============================================================
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/analyze")
def api_analyze():
    symbol = request.args.get("symbol", "EURUSD")
    interval = request.args.get("interval", "4h")
    try:
        df = get_ohlc(symbol, interval)
        signal = analyze_smc(df)
        candles = [{
            "time": idx.strftime("%Y-%m-%d %H:%M"),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
        } for idx, r in df.iterrows()]
        return jsonify({
            "success": True, "symbol": symbol, "interval": interval,
            "candles": candles, "signal": signal,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "key_set": bool(TWELVE_KEY)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
