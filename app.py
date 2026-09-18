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


def fetch(td_symbol, interval, limit):
    r = requests.get(f"{TWELVE_BASE}/time_series", params={
        "symbol": td_symbol, "interval": interval,
        "outputsize": limit, "apikey": TWELVE_KEY,
    }, timeout=20)
    data = r.json()
    if data.get("status") == "error":
        raise ValueError(data.get("message", "API error"))
    values = data.get("values")
    if not values:
        raise ValueError(f"No data for {td_symbol}")
    df = pd.DataFrame(values)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.set_index("datetime").sort_index()
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df[["open", "high", "low", "close"]].dropna()


def get_ohlc(symbol, interval="4h", limit=300):
    if not TWELVE_KEY:
        raise ValueError("Add TWELVE_DATA_API_KEY on Render")
    td = normalize(symbol)
    df = fetch(td, interval, limit)
    try:
        htf = fetch(td, "1day", 100)
    except Exception:
        htf = None
    return df, htf


def compute_atr(df):
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(14).mean().bfill()


def find_swings(df, n=3):
    highs = df["high"].values
    lows = df["low"].values
    swings = []
    for i in range(n, len(df) - n):
        wh = highs[i - n:i + n + 1]
        wl = lows[i - n:i + n + 1]
        if highs[i] == wh.max() and np.sum(wh == highs[i]) == 1:
            swings.append((i, float(highs[i]), "high"))
        if lows[i] == wl.min() and np.sum(wl == lows[i]) == 1:
            swings.append((i, float(lows[i]), "low"))
    return swings


def find_bos(df):
    swings = find_swings(df)
    closes = df["close"].values
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


def find_order_blocks(df):
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    opens = df["open"].values
    atr = compute_atr(df).values
    obs = []
    for i in range(2, len(df) - 2):
        if atr[i] <= 0 or abs(closes[i] - opens[i]) < atr[i] * 0.4:
            continue
        if closes[i] < opens[i]:
            if closes[i + 1] > highs[i] and closes[i + 2] > highs[i]:
                obs.append({"top": float(highs[i]), "bottom": float(lows[i]),
                            "kind": "bullish", "index": i})
        if closes[i] > opens[i]:
            if closes[i + 1] < lows[i] and closes[i + 2] < lows[i]:
                obs.append({"top": float(highs[i]), "bottom": float(lows[i]),
                            "kind": "bearish", "index": i})
    return obs


def detect_liquidity_sweep(df, direction, lookback=10):
    highs = df["high"].values
    lows = df["low"].values
    swings = find_swings(df)
    recent_lows = [s for s in swings if s[2] == "low"][-5:]
    recent_highs = [s for s in swings if s[2] == "high"][-5:]

    if direction == "bullish":
        for i, price, _ in recent_lows:
            for j in range(i + 1, min(i + lookback, len(df))):
                if lows[j] < price and df["close"].iloc[j] > price:
                    return True
    else:
        for i, price, _ in recent_highs:
            for j in range(i + 1, min(i + lookback, len(df))):
                if highs[j] > price and df["close"].iloc[j] < price:
                    return True
    return False


def htf_trend(htf_df):
    if htf_df is None or len(htf_df) < 50:
        return "neutral"
    ema20 = htf_df["close"].ewm(span=20).mean().iloc[-1]
    ema50 = htf_df["close"].ewm(span=50).mean().iloc[-1]
    if ema20 > ema50:
        return "bullish"
    if ema20 < ema50:
        return "bearish"
    return "neutral"


def in_trading_session():
    hour = pd.Timestamp.now(tz="UTC").hour
    return 7 <= hour <= 20


# ============================================================
#  STRATEGY 1 — SMC/ICT (BOS + OB + Sweep)
# ============================================================
def strategy_smc(df, htf_df):
    if len(df) < 30:
        return None
    last = float(df["close"].iloc[-1])
    atr = float(compute_atr(df).iloc[-1])
    if atr <= 0:
        return None

    ht = htf_trend(htf_df)
    bos = find_bos(df)
    if not bos:
        return None

    direction, level, bos_idx = bos[-1]
    bars_since = len(df) - 1 - bos_idx
    if bars_since > 20:
        return None
    if ht != "neutral" and ht != direction:
        return None
    if not detect_liquidity_sweep(df, direction):
        return None

    obs = find_order_blocks(df)
    matching = [o for o in obs if o["kind"] == direction
                and o["index"] >= bos_idx
                and abs(((o["top"] + o["bottom"]) / 2) - last) < atr * 2.5]
    if not matching:
        return None

    ob = matching[-1]
    if direction == "bullish":
        entry = ob["top"]
        sl = ob["bottom"] - atr * 0.3
        risk = entry - sl
        if risk <= 0:
            return None
        tp1 = entry + risk * 1.5
        tp2 = entry + risk * 3
        action = "BUY"
    else:
        entry = ob["bottom"]
        sl = ob["top"] + atr * 0.3
        risk = sl - entry
        if risk <= 0:
            return None
        tp1 = entry - risk * 1.5
        tp2 = entry - risk * 3
        action = "SELL"

    return {
        "action": action,
        "strategy": "SMC/ICT",
        "entry": round(entry, 5),
        "sl": round(sl, 5),
        "tp1": round(tp1, 5),
        "tp2": round(tp2, 5),
        "reason": [
            f"Bullish BOS · {bars_since} bars ago" if direction == "bullish"
            else f"Bearish BOS · {bars_since} bars ago",
            f"HTF trend aligned: {ht}",
            "Liquidity sweep confirmed",
            f"Entry at {direction} Order Block",
        ],
        "quality": 75 if ht == direction else 55,
    }


# ============================================================
#  STRATEGY 2 — ICT Silver Bullet (time-based FVG entry)
# ============================================================
def strategy_ict_silver_bullet(df, htf_df):
    """
    ICT Silver Bullet:
      - Look for the last FVG that formed within London or NY killzone.
      - Enter when price returns to it.
      - Only trade when HTF trend agrees.
    """
    if len(df) < 30:
        return None
    last = float(df["close"].iloc[-1])
    atr = float(compute_atr(df).iloc[-1])
    if atr <= 0:
        return None

    ht = htf_trend(htf_df)
    if ht == "neutral":
        return None

    highs = df["high"].values
    lows = df["low"].values

    # Find recent unfilled FVGs (in the HTF direction)
    fvgs = []
    for i in range(2, len(df)):
        if ht == "bullish" and lows[i] > highs[i - 2]:
            fvgs.append({"top": float(lows[i]), "bottom": float(highs[i - 2]),
                         "kind": "bullish", "index": i})
        if ht == "bearish" and highs[i] < lows[i - 2]:
            fvgs.append({"top": float(lows[i - 2]), "bottom": float(highs[i]),
                         "kind": "bearish", "index": i})

    if not fvgs:
        return None

    # Last unfilled FVG near current price
    recent = [f for f in fvgs if f["index"] >= len(df) - 20]
    if not recent:
        return None

    fvg = recent[-1]
    mid = (fvg["top"] + fvg["bottom"]) / 2
    if abs(mid - last) > atr * 2:
        return None

    if ht == "bullish":
        entry = mid
        sl = fvg["bottom"] - atr * 0.3
        risk = entry - sl
        if risk <= 0:
            return None
        tp1 = entry + risk * 1.5
        tp2 = entry + risk * 3
        action = "BUY"
    else:
        entry = mid
        sl = fvg["top"] + atr * 0.3
        risk = sl - entry
        if risk <= 0:
            return None
        tp1 = entry - risk * 1.5
        tp2 = entry - risk * 3
        action = "SELL"

    return {
        "action": action,
        "strategy": "ICT Silver Bullet",
        "entry": round(entry, 5),
        "sl": round(sl, 5),
        "tp1": round(tp1, 5),
        "tp2": round(tp2, 5),
        "reason": [
            f"HTF trend: {ht}",
            f"Recent {ht} FVG present",
            "Enter on FVG retest",
        ],
        "quality": 65,
    }


# ============================================================
#  STRATEGY 3 — EMA Pullback (simple trend continuation)
# ============================================================
def strategy_ema_pullback(df, htf_df):
    """
    Simple but effective:
      - Trend = EMA20 above EMA50 (bullish) or below (bearish)
      - Entry when price pulls back to EMA20
      - SL below EMA50, TP at 1.5R and 3R
    """
    if len(df) < 60:
        return None

    closes = df["close"]
    last = float(closes.iloc[-1])
    ema20 = closes.ewm(span=20).mean()
    ema50 = closes.ewm(span=50).mean()

    atr = float(compute_atr(df).iloc[-1])
    if atr <= 0:
        return None

    e20 = float(ema20.iloc[-1])
    e50 = float(ema50.iloc[-1])

    # Trend direction
    if e20 > e50:
        direction = "bullish"
        action = "BUY"
    elif e20 < e50:
        direction = "bearish"
        action = "SELL"
    else:
        return None

    # HTF agreement
    ht = htf_trend(htf_df)
    if ht != "neutral" and ht != direction:
        return None

    # Price must be near EMA20 (within 0.5 ATR)
    distance = abs(last - e20)
    if distance > atr * 0.7:
        return None

    # Previous candle must be in trend direction (confirmation)
    prev_close = float(closes.iloc[-2])
    prev_open = float(df["open"].iloc[-2])
    if direction == "bullish" and prev_close <= prev_open:
        return None
    if direction == "bearish" and prev_close >= prev_open:
        return None

    # Entry / SL / TP
    if direction == "bullish":
        entry = last
        sl = min(e50, last - atr * 1.2)
        risk = entry - sl
        if risk <= 0:
            return None
        tp1 = entry + risk * 1.5
        tp2 = entry + risk * 3
    else:
        entry = last
        sl = max(e50, last + atr * 1.2)
        risk = sl - entry
        if risk <= 0:
            return None
        tp1 = entry - risk * 1.5
        tp2 = entry - risk * 3

    return {
        "action": action,
        "strategy": "EMA Pullback",
        "entry": round(entry, 5),
        "sl": round(sl, 5),
        "tp1": round(tp1, 5),
        "tp2": round(tp2, 5),
        "reason": [
            f"{direction.title()} trend (EMA20/50)",
            "Price pulled back to EMA20",
            "Rejection candle confirms",
            f"HTF: {ht}",
        ],
        "quality": 60,
    }


# ============================================================
#  COMBINE STRATEGIES
# ============================================================
def build_signal(df, htf_df):
    if not in_trading_session():
        return _no_signal("Outside London/NY session")

    last = float(df["close"].iloc[-1])
    atr = float(compute_atr(df).iloc[-1])
    if atr <= 0 or (atr / last) < 0.0015:
        return _no_signal("Market too quiet")

    results = []
    for strat in (strategy_smc, strategy_ict_silver_bullet, strategy_ema_pullback):
        try:
            r = strat(df, htf_df)
            if r:
                results.append(r)
        except Exception:
            continue

    if not results:
        return _no_signal("No strategy found an edge")

    # Pick highest quality
    results.sort(key=lambda r: r["quality"], reverse=True)
    return results[0]


def _no_signal(reason):
    return {"action": "WAIT", "strategy": None, "entry": None, "sl": None,
            "tp1": None, "tp2": None, "reason": [reason], "quality": 0}


# ============================================================
#  BACKTEST HISTORY
# ============================================================
def evaluate_history(df, htf_df):
    atr_series = compute_atr(df)
    bos = find_bos(df)
    obs = find_order_blocks(df)
    results = []

    for direction, level, idx in bos[-20:]:
        try:
            atr = float(atr_series.iloc[idx])
            if atr <= 0:
                continue

            if htf_df is not None:
                hist = htf_df.loc[:df.index[idx]]
                ht = htf_trend(hist)
                if ht != "neutral" and ht != direction:
                    continue

            matching_ob = None
            for o in obs:
                if o["kind"] == direction and idx - 5 <= o["index"] <= idx:
                    matching_ob = o
                    break
            if matching_ob is None:
                continue

            if direction == "bullish":
                entry = matching_ob["top"]
                sl = matching_ob["bottom"] - atr * 0.3
                risk = entry - sl
                if risk <= 0:
                    continue
                tp1 = entry + risk * 1.5
                tp2 = entry + risk * 3
            else:
                entry = matching_ob["bottom"]
                sl = matching_ob["top"] + atr * 0.3
                risk = sl - entry
                if risk <= 0:
                    continue
                tp1 = entry - risk * 1.5
                tp2 = entry - risk * 3

            outcome = "ACTIVE"
            for _, c in df.iloc[idx + 1:].iterrows():
                if direction == "bullish":
                    if c["low"] <= sl:
                        outcome = "STOPPED"; break
                    if c["high"] >= tp2:
                        outcome = "TP2_HIT"; break
                    if c["high"] >= tp1 and outcome == "ACTIVE":
                        outcome = "TP1_HIT"
                else:
                    if c["high"] >= sl:
                        outcome = "STOPPED"; break
                    if c["low"] <= tp2:
                        outcome = "TP2_HIT"; break
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
        except Exception:
            continue
    return results


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/analyze")
def api_analyze():
    symbol = request.args.get("symbol", "EURUSD")
    interval = request.args.get("interval", "4h")
    try:
        df, htf = get_ohlc(symbol, interval)
        signal = build_signal(df, htf)
        history = evaluate_history(df, htf)
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
