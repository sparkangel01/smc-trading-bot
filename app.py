import os
import numpy as np
import pandas as pd
import yfinance as yf
from flask import Flask, jsonify, request, render_template

app = Flask(__name__)


# ============================================================
#  MARKET DATA
# ============================================================
def get_ohlc(symbol, timeframe="1h", limit=300):
    tf_map = {
        "15m": ("1mo", "15m"),
        "1h":  ("3mo", "60m"),
        "4h":  ("6mo", "1h"),
        "1d":  ("2y", "1d"),
    }
    if timeframe not in tf_map:
        raise ValueError("Unsupported timeframe")

    s = symbol.upper().strip()
    # Symbol normalization
    if s in ("XAUUSD", "GOLD"):
        s = "GC=F"
    elif s in ("XAGUSD", "SILVER"):
        s = "SI=F"
    elif len(s) == 6 and s.isalpha() and not s.endswith("=X"):
        s = s + "=X"

    period, interval = tf_map[timeframe]
    df = yf.download(s, period=period, interval=interval,
                     progress=False, auto_adjust=False)
    if df.empty:
        raise ValueError(f"No data for {symbol}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)
    df = df[["open", "high", "low", "close", "volume"]].dropna()
    if timeframe == "4h":
        df = df.resample("4H").agg({
            "open": "first", "high": "max",
            "low": "min", "close": "last", "volume": "sum"
        }).dropna()
    return df.tail(limit)


# ============================================================
#  SMC / ICT LOGIC — Simple, Direct
# ============================================================
def analyze_smc(df):
    """
    Detect:
      - Swing highs/lows (structural points)
      - BOS / CHoCH (break of structure / change of character)
      - Order Blocks (last opposing candle before displacement)
      - Fair Value Gaps (3-candle imbalance)
      - Liquidity sweeps (equal highs/lows taken out)
    Then output a BUY or SELL with Entry / SL / TP.
    """
    highs = df["high"].values
    lows  = df["low"].values
    closes = df["close"].values
    opens  = df["open"].values

    last = float(closes[-1])

    # ATR for stop distance
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"]  - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr = float(tr.rolling(14).mean().bfill().iloc[-1])

    # ---------- Swing Points ----------
    swings = []
    n = 3
    for i in range(n, len(df) - n):
        window_h = highs[i - n:i + n + 1]
        window_l = lows[i - n:i + n + 1]
        if highs[i] == window_h.max() and np.sum(window_h == highs[i]) == 1:
            swings.append({"i": i, "price": float(highs[i]), "type": "high"})
        if lows[i] == window_l.min() and np.sum(window_l == lows[i]) == 1:
            swings.append({"i": i, "price": float(lows[i]), "type": "low"})

    # ---------- BOS / CHoCH ----------
    # BOS: price breaks the most recent swing in the trend direction
    # CHoCH: first break against the prior trend
    bos_signals = []
    last_high = None
    last_low = None
    trend = None
    for sw in swings:
        if sw["type"] == "high":
            if last_high is not None:
                # Look for a close above this high after its index
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
    # Bullish OB: last down candle before a strong bullish move
    # Bearish OB: last up candle before a strong bearish move
    order_blocks = []
    for i in range(2, len(df) - 2):
        body = abs(closes[i] - opens[i])
        if body < atr * 0.4:
            continue
        # Bullish OB
        if closes[i] < opens[i]:
            if closes[i + 1] > highs[i] and closes[i + 2] > highs[i]:
                order_blocks.append({
                    "top": float(highs[i]), "bottom": float(lows[i]),
                    "kind": "bullish", "index": i
                })
        # Bearish OB
        if closes[i] > opens[i]:
            if closes[i + 1] < lows[i] and closes[i + 2] < lows[i]:
                order_blocks.append({
                    "top": float(highs[i]), "bottom": float(lows[i]),
                    "kind": "bearish", "index": i
                })

    # ---------- Fair Value Gaps ----------
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
                # Equal highs/lows — check if swept
                if b["type"] == "high":
                    if any(highs[k] > b["price"] for k in range(b["i"] + 1, len(df))):
                        liquidity_sweeps.append({"kind": "buy_side", "price": b["price"]})
                else:
                    if any(lows[k] < b["price"] for k in range(b["i"] + 1, len(df))):
                        liquidity_sweeps.append({"kind": "sell_side", "price": b["price"]})
                break

    # ============================================================
    #  DECISION — Simple rules for BUY / SELL
    # ============================================================
    signal = {
        "action": "WAIT",
        "entry": None, "sl": None, "tp1": None, "tp2": None,
        "reason": [],
        "bos": None, "ob": None, "fvg": None,
    }

    # Last BOS/CHoCH
    recent_bos = bos_signals[-3:] if bos_signals else []
    last_bos = recent_bos[-1] if recent_bos else None

    # Nearest unmitigated-ish OB near price
    near_obs = [o for o in order_blocks
                if abs(((o["top"] + o["bottom"]) / 2) - last) < atr * 3]

    # Nearest FVG near price
    near_fvgs = [f for f in fvgs
                 if abs(((f["top"] + f["bottom"]) / 2) - last) < atr * 3]

    # ---------- BUY ----------
    if last_bos and last_bos["direction"] == "bullish":
        signal["action"] = "BUY"
        signal["bos"] = last_bos["kind"]

        # Entry: at nearest bullish OB or current price
        bull_obs = [o for o in near_obs if o["kind"] == "bullish"]
        if bull_obs:
            entry = bull_obs[-1]["top"]  # top of OB
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

    # ---------- SELL ----------
    elif last_bos and last_bos["direction"] == "bearish":
        signal["action"] = "SELL"
        signal["bos"] = last_bos["kind"]

        bear_obs = [o for o in near_obs if o["kind"] == "bearish"]
        if bear_obs:
            entry = bear_obs[-1]["bottom"]  # bottom of OB
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
        signal["action"] = "WAIT"
        signal["reason"].append("No clear structure break")

    # Attach recent context for the UI
    signal["order_blocks"] = order_blocks[-6:]
    signal["fvgs"] = fvgs[-6:]
    signal["structure"] = bos_signals[-6:]

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
    timeframe = request.args.get("timeframe", "1h")
    try:
        df = get_ohlc(symbol, timeframe)
        signal = analyze_smc(df)
        candles = [{
            "time": str(idx),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
        } for idx, r in df.iterrows()]

        return jsonify({
            "success": True,
            "symbol": symbol,
            "timeframe": timeframe,
            "candles": candles,
            "signal": signal,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
