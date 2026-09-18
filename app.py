import os
import io
from datetime import datetime

import numpy as np
import pandas as pd
import requests as http_requests
from flask import Flask, jsonify, request, render_template

app = Flask(__name__)


# ============================================================
#  SYMBOL MAPPING  (User input -> Stooq ticker)
# ============================================================
SYMBOL_MAP = {
    # Crypto
    "BTC-USD": "btcusd", "BTCUSD": "btcusd", "BTCUSDT": "btcusd",
    "ETH-USD": "ethusd", "ETHUSD": "ethusd", "ETHUSDT": "ethusd",
    "SOL-USD": "solusd", "SOLUSD": "solusd",
    "XRP-USD": "xrpusd", "XRPUSD": "xrpusd",
    "DOGE-USD": "dogeusd", "DOGEUSD": "dogeusd",
    # Metals
    "XAUUSD": "xauusd", "GOLD": "xauusd",
    "XAGUSD": "xagusd", "SILVER": "xagusd",
    # Forex
    "EURUSD": "eurusd", "GBPUSD": "gbpusd", "USDJPY": "usdjpy",
    "USDCHF": "usdchf", "AUDUSD": "audusd", "NZDUSD": "nzdusd",
    "USDCAD": "usdcad", "EURGBP": "eurgbp", "EURJPY": "eurjpy",
    "GBPJPY": "gbpjpy", "NGNUSD": "ngnusd", "USDNGN": "usdngn",
    # Indices
    "SPX": "^spx", "SP500": "^spx",
    "NDX": "^ndx", "NASDAQ": "^ndx",
    "DJI": "^dji", "DOW": "^dji",
}


def stooq_ticker(symbol):
    s = symbol.upper().strip()
    if s in SYMBOL_MAP:
        return SYMBOL_MAP[s]
    if len(s) == 6 and s.isalpha():
        return s.lower()
    return s.lower().replace("-", ".")


# ============================================================
#  MARKET DATA  (Stooq daily CSV)
# ============================================================
def get_ohlc(symbol, limit=300):
    ticker = stooq_ticker(symbol)
    url = f"https://stooq.com/q/d/l/?s={ticker}&i=d"

    try:
        r = http_requests.get(
            url, timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (TradingBot)"}
        )
    except Exception as e:
        raise ValueError(f"Network error fetching {symbol}: {e}")

    if r.status_code != 200:
        raise ValueError(f"Stooq error {r.status_code} for {symbol}")

    text = r.text.strip()
    if not text or "No data" in text or "Exceeded" in text or len(text) < 50:
        raise ValueError(
            f"No data returned for '{symbol}' (tried '{ticker}'). "
            f"Try: BTC-USD, XAUUSD, EURUSD, AAPL, SPX"
        )

    try:
        df = pd.read_csv(io.StringIO(text))
    except Exception as e:
        raise ValueError(f"Could not parse data: {e}")

    # Normalize column names
    df.columns = [str(c).strip().lower() for c in df.columns]

    # Rename common variations to standard
    rename_map = {
        "data": "date", "date/time": "date", "datetime": "date", "time": "date",
        "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume",
        "adj close": "close", "adj_close": "close", "adjclose": "close",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})

    needed = {"open", "high", "low", "close"}
    if not needed.issubset(df.columns):
        raise ValueError(f"Missing columns. Got: {list(df.columns)}")

    # Find a date column
    date_col = None
    for c in ("date", "index", "unnamed: 0"):
        if c in df.columns:
            date_col = c
            break
    if date_col is None:
        date_col = df.columns[0]

    if "volume" not in df.columns:
        df["volume"] = 0

    df = df[[date_col, "open", "high", "low", "close", "volume"]].copy()
    df.columns = ["date", "open", "high", "low", "close", "volume"]

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])

    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"])

    df = df.set_index("date").sort_index()
    df = df[df["close"] > 0]

    if df.empty:
        raise ValueError(f"Empty data for {symbol}")

    return df.tail(limit)


# ============================================================
#  SMC / ICT LOGIC
# ============================================================
def analyze_smc(df):
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    opens = df["open"].values

    last = float(closes[-1])

    # ATR
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr = float(tr.rolling(14).mean().bfill().iloc[-1])
    if atr <= 0:
        atr = last * 0.01

    # ---------- Swing Points ----------
    swings = []
    n = 3
    for i in range(n, len(df) - n):
        wh = highs[i - n:i + n + 1]
        wl = lows[i - n:i + n + 1]
        if highs[i] == wh.max() and np.sum(wh == highs[i]) == 1:
            swings.append({"i": i, "price": float(highs[i]), "type": "high"})
        if lows[i] == wl.min() and np.sum(wl == lows[i]) == 1:
            swings.append({"i": i, "price": float(lows[i]), "type": "low"})

    # ---------- BOS / CHoCH ----------
    bos_signals = []
    last_high = None
    last_low = None
    trend = None
    for sw in swings:
        if sw["type"] == "high":
            if last_high is not None:
                seg = closes[last_high["i"] + 1: sw["i"] + 1]
                if len(seg) and seg.max() > last_high["price"]:
                    kind = "CHoCH" if trend == "bearish" else "BOS"
                    bos_signals.append({
                        "index": sw["i"], "price": last_high["price"],
                        "kind": kind, "direction": "bullish"
                    })
                    trend = "bullish"
            last_high = sw
        else:
            if last_low is not None:
                seg = closes[last_low["i"] + 1: sw["i"] + 1]
                if len(seg) and seg.min() < last_low["price"]:
                    kind = "CHoCH" if trend == "bullish" else "BOS"
                    bos_signals.append({
                        "index": sw["i"], "price": last_low["price"],
                        "kind": kind, "direction": "bearish"
                    })
                    trend = "bearish"
            last_low = sw

    # ---------- Order Blocks ----------
    order_blocks = []
    for i in range(2, len(df) - 2):
        body = abs(closes[i] - opens[i])
        if body < atr * 0.4:
            continue
        if closes[i] < opens[i]:
            if closes[i + 1] > highs[i] and closes[i + 2] > highs[i]:
                order_blocks.append({
                    "top": float(highs[i]), "bottom": float(lows[i]),
                    "kind": "bullish", "index": i
                })
        if closes[i] > opens[i]:
            if closes[i + 1] < lows[i] and closes[i + 2] < lows[i]:
                order_blocks.append({
                    "top": float(highs[i]), "bottom": float(lows[i]),
                    "kind": "bearish", "index": i
                })

    # ---------- FVGs ----------
    fvgs = []
    for i in range(2, len(df)):
        if lows[i] > highs[i - 2]:
            fvgs.append({
                "top": float(lows[i]), "bottom": float(highs[i - 2]),
                "kind": "bullish", "index": i
            })
        if highs[i] < lows[i - 2]:
            fvgs.append({
                "top": float(lows[i - 2]), "bottom": float(highs[i]),
                "kind": "bearish", "index": i
            })

    # ---------- Liquidity Sweeps ----------
    liquidity_sweeps = []
    for i in range(1, len(swings)):
        for j in range(i + 1, len(swings)):
            a, b = swings[i], swings[j]
            if a["type"] != b["type"]:
                continue
            if abs(a["price"] - b["price"]) < atr * 0.3:
                if b["type"] == "high":
                    if any(highs[k] > b["price"] for k in range(b["i"] + 1, len(df))):
                        liquidity_sweeps.append({"kind": "buy_side", "price": b["price"]})
                else:
                    if any(lows[k] < b["price"] for k in range(b["i"] + 1, len(df))):
                        liquidity_sweeps.append({"kind": "sell_side", "price": b["price"]})
                break

    # ---------- Decision ----------
    signal = {
        "action": "WAIT",
        "entry": None, "sl": None, "tp1": None, "tp2": None,
        "reason": [],
    }

    recent_bos = bos_signals[-3:] if bos_signals else []
    last_bos = recent_bos[-1] if recent_bos else None

    near_obs = [o for o in order_blocks
                if abs(((o["top"] + o["bottom"]) / 2) - last) < atr * 3]
    near_fvgs = [f for f in fvgs
                 if abs(((f["top"] + f["bottom"]) / 2) - last) < atr * 3]

    if last_bos and last_bos["direction"] == "bullish":
        signal["action"] = "BUY"
        bull_obs = [o for o in near_obs if o["kind"] == "bullish"]
        if bull_obs:
            entry = bull_obs[-1]["top"]
            sl = bull_obs[-1]["bottom"] - atr * 0.5
        else:
            entry = last
            sl = last - atr * 1.5
        risk = entry - sl
        signal["entry"] = round(entry, 6)
        signal["sl"] = round(sl, 6)
        signal["tp1"] = round(entry + risk * 1.5, 6)
        signal["tp2"] = round(entry + risk * 3, 6)
        signal["reason"].append(f"Bullish {last_bos['kind']} confirmed")
        if bull_obs:
            signal["reason"].append("Entry at bullish Order Block")
        if any(f["kind"] == "bullish" for f in near_fvgs):
            signal["reason"].append("Bullish FVG below price")
        if any(s["kind"] == "sell_side" for s in liquidity_sweeps):
            signal["reason"].append("Sell-side liquidity swept")

    elif last_bos and last_bos["direction"] == "bearish":
        signal["action"] = "SELL"
        bear_obs = [o for o in near_obs if o["kind"] == "bearish"]
        if bear_obs:
            entry = bear_obs[-1]["bottom"]
            sl = bear_obs[-1]["top"] + atr * 0.5
        else:
            entry = last
            sl = last + atr * 1.5
        risk = sl - entry
        signal["entry"] = round(entry, 6)
        signal["sl"] = round(sl, 6)
        signal["tp1"] = round(entry - risk * 1.5, 6)
        signal["tp2"] = round(entry - risk * 3, 6)
        signal["reason"].append(f"Bearish {last_bos['kind']} confirmed")
        if bear_obs:
            signal["reason"].append("Entry at bearish Order Block")
        if any(f["kind"] == "bearish" for f in near_fvgs):
            signal["reason"].append("Bearish FVG above price")
        if any(s["kind"] == "buy_side" for s in liquidity_sweeps):
            signal["reason"].append("Buy-side liquidity swept")

    else:
        signal["reason"].append("No clear structure break on this timeframe")

    signal["order_blocks"] = order_blocks[-8:]
    signal["fvgs"] = fvgs[-8:]
    signal["structure"] = bos_signals[-8:]

    return signal


# ============================================================
#  ROUTES
# ============================================================
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/analyze")
def api_analyze():
    symbol = request.args.get("symbol", "BTC-USD")
    try:
        df = get_ohlc(symbol)
        signal = analyze_smc(df)

        candles = [{
            "time": idx.strftime("%Y-%m-%d"),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
        } for idx, r in df.iterrows()]

        return jsonify({
            "success": True,
            "symbol": symbol,
            "timeframe": "1d",
            "candles": candles,
            "signal": signal,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/debug")
def api_debug():
    """Debug endpoint — shows raw Stooq response for a symbol."""
    symbol = request.args.get("symbol", "BTC-USD")
    ticker = stooq_ticker(symbol)
    url = f"https://stooq.com/q/d/l/?s={ticker}&i=d"
    try:
        r = http_requests.get(url, timeout=15,
                              headers={"User-Agent": "Mozilla/5.0"})
        return jsonify({
            "symbol": symbol,
            "ticker": ticker,
            "url": url,
            "status": r.status_code,
            "first_500_chars": r.text[:500],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
