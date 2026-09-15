"""
SMC/ICT Trading Bot — Dashboard only (no Telegram)
"""
import logging
from datetime import datetime

from flask import Flask, render_template, jsonify
import pandas as pd
import yfinance as yf
from ta.momentum import RSIIndicator
from ta.trend import MACD, EMAIndicator
from smartmoneyconcepts import smc
from apscheduler.schedulers.background import BackgroundScheduler

# ==========================================================
# ⚙️  CONFIG
# ==========================================================
SYMBOLS               = ["BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD", "XRP-USD"]
SCAN_INTERVAL_MINUTES = 15
RSI_OVERSOLD          = 30
RSI_OVERBOUGHT        = 70
PORT                  = 5000

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
SIGNAL_HISTORY = []
MAX_HISTORY = 200


# ==========================================================
# 📊 DATA + INDICATORS
# ==========================================================
def fetch_data(symbol: str, period="3mo", interval="1h") -> pd.DataFrame:
    try:
        df = yf.download(symbol, period=period, interval=interval,
                         progress=False, auto_adjust=True)
        if df.empty:
            return pd.DataFrame()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except Exception as e:
        log.error(f"fetch_data({symbol}) failed: {e}")
        return pd.DataFrame()


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or len(df) < 50:
        return df
    close = df["Close"]
    df["RSI"]       = RSIIndicator(close, window=14).rsi()
    df["MACD_DIFF"] = MACD(close).macd_diff()
    df["EMA_20"]    = EMAIndicator(close, window=20).ema_indicator()
    df["EMA_50"]    = EMAIndicator(close, window=50).ema_indicator()
    return df


# ==========================================================
# 🧠 SMC / ICT ENGINE
# ==========================================================
class SMCEngine:
    def __init__(self, swing_length: int = 50):
        self.swing_length = swing_length

    def _prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        d = df.copy()
        d.columns = [c.lower() for c in d.columns]
        if "volume" not in d.columns:
            d["volume"] = 0
        return d

    def analyze(self, df: pd.DataFrame) -> dict:
        data = self._prepare(df)
        result = {"fvg": None, "bos": None, "choch": None,
                  "order_block": None, "liquidity": None, "bias": "NEUTRAL"}
        try:
            swing_hl  = smc.swing_highs_lows(data, swing_length=self.swing_length)
            fvg       = smc.fvg(data, join_consecutive=False)
            bos_choch = smc.bos_choch(data, swing_hl, close_break=True)
            ob        = smc.ob(data, swing_hl, close_mitigation=False)
            liquidity = smc.liquidity(data, swing_hl, range_percent=0.01)
        except Exception as e:
            log.error(f"SMC compute failed: {e}")
            return result

        price = float(data["close"].iloc[-1])

        # FVG
        if fvg is not None and not fvg.empty:
            for _, row in fvg.dropna(subset=["FVG"]).tail(5).iterrows():
                if pd.notna(row.get("MitigatedIndex")):
                    continue
                top, bottom = float(row["Top"]), float(row["Bottom"])
                direction = "BULLISH" if row["FVG"] == 1 else "BEARISH"
                if bottom <= price <= top:
                    result["fvg"] = {"direction": direction, "top": top,
                                     "bottom": bottom, "strength": "INSIDE"}
                    break
                edge = top if direction == "BEARISH" else bottom
                if abs(price - edge) / price < 0.005:
                    result["fvg"] = {"direction": direction, "top": top,
                                     "bottom": bottom, "strength": "NEAR"}
                    break

        # BOS / CHoCH
        if bos_choch is not None and not bos_choch.empty:
            for _, row in bos_choch.dropna(subset=["BOS", "CHOCH"]).tail(3).iterrows():
                if pd.notna(row.get("BOS")):
                    result["bos"] = {
                        "direction": "BULLISH" if row["BOS"] == 1 else "BEARISH",
                        "level": float(row["Level"]) if pd.notna(row["Level"]) else None,
                    }
                if pd.notna(row.get("CHOCH")):
                    result["choch"] = {
                        "direction": "BULLISH" if row["CHOCH"] == 1 else "BEARISH",
                        "level": float(row["Level"]) if pd.notna(row["Level"]) else None,
                    }

        # Order Block
        if ob is not None and not ob.empty:
            for _, row in ob.dropna(subset=["OB"]).tail(5).iterrows():
                if pd.notna(row.get("MitigatedIndex")):
                    continue
                top, bottom = float(row["Top"]), float(row["Bottom"])
                if bottom <= price <= top:
                    result["order_block"] = {
                        "direction": "BULLISH" if row["OB"] == 1 else "BEARISH",
                        "top": top, "bottom": bottom,
                    }
                    break

        # Liquidity
        if liquidity is not None and not liquidity.empty:
            for _, row in liquidity.dropna(subset=["Liquidity"]).tail(3).iterrows():
                result["liquidity"] = {
                    "direction": "BULLISH" if row["Liquidity"] == 1 else "BEARISH",
                    "level": float(row["Level"]) if pd.notna(row["Level"]) else None,
                }

        # Bias
        bull = bear = 0
        if result["fvg"]:
            if result["fvg"]["direction"] == "BULLISH": bull += 2
            else: bear += 2
        if result["bos"]:
            if result["bos"]["direction"] == "BULLISH": bull += 2
            else: bear += 2
        if result["choch"]:
            if result["choch"]["direction"] == "BULLISH": bull += 1
            else: bear += 1
        if result["order_block"]:
            if result["order_block"]["direction"] == "BULLISH": bull += 2
            else: bear += 2
        result["bias"] = "BULLISH" if bull > bear else "BEARISH" if bear > bull else "NEUTRAL"
        return result


smc_engine = SMCEngine(swing_length=50)


# ==========================================================
# 🎯 SIGNAL GENERATION
# ==========================================================
def generate_signal(symbol: str) -> dict:
    df = fetch_data(symbol)
    if df.empty or len(df) < 50:
        return {"symbol": symbol, "signal": "NO_DATA", "score": 0,
                "reasons": [], "timestamp": datetime.utcnow().isoformat()+"Z"}

    df = compute_indicators(df)
    last, prev = df.iloc[-1], df.iloc[-2]

    price     = float(last["Close"])
    rsi       = float(last["RSI"]) if not pd.isna(last["RSI"]) else 50
    macd_diff = float(last["MACD_DIFF"]) if not pd.isna(last["MACD_DIFF"]) else 0
    ema20     = float(last["EMA_20"]) if not pd.isna(last["EMA_20"]) else price
    ema50     = float(last["EMA_50"]) if not pd.isna(last["EMA_50"]) else price

    smc_result = smc_engine.analyze(df)
    score, reasons = 0, []

    # Traditional
    if rsi < RSI_OVERSOLD:     score += 2; reasons.append(f"RSI oversold ({rsi:.1f})")
    elif rsi > RSI_OVERBOUGHT: score -= 2; reasons.append(f"RSI overbought ({rsi:.1f})")

    if macd_diff > 0 and float(prev["MACD_DIFF"]) <= 0:
        score += 2; reasons.append("MACD bullish crossover")
    elif macd_diff < 0 and float(prev["MACD_DIFF"]) >= 0:
        score -= 2; reasons.append("MACD bearish crossover")

    if ema20 > ema50: score += 1; reasons.append("EMA20 > EMA50 (uptrend)")
    else:             score -= 1; reasons.append("EMA20 < EMA50 (downtrend)")

    # SMC (higher weight)
    if smc_result["fvg"]:
        if smc_result["fvg"]["direction"] == "BULLISH":
            score += 3; reasons.append(f"Bullish FVG @ {smc_result['fvg']['top']:.4f}")
        else:
            score -= 3; reasons.append(f"Bearish FVG @ {smc_result['fvg']['bottom']:.4f}")

    if smc_result["bos"]:
        if smc_result["bos"]["direction"] == "BULLISH":
            score += 3; reasons.append("BOS Bullish (trend continuation)")
        else:
            score -= 3; reasons.append("BOS Bearish (trend continuation)")

    if smc_result["choch"]:
        if smc_result["choch"]["direction"] == "BULLISH":
            score += 4; reasons.append("CHoCH Bullish (reversal)")
        else:
            score -= 4; reasons.append("CHoCH Bearish (reversal)")

    if smc_result["order_block"]:
        if smc_result["order_block"]["direction"] == "BULLISH":
            score += 3; reasons.append("Inside Bullish Order Block")
        else:
            score -= 3; reasons.append("Inside Bearish Order Block")

    if   score >= 8:  signal = "STRONG_BUY"
    elif score >= 4:  signal = "BUY"
    elif score <= -8: signal = "STRONG_SELL"
    elif score <= -4: signal = "SELL"
    else:             signal = "HOLD"

    return {
        "symbol": symbol,
        "price": round(price, 4),
        "rsi": round(rsi, 2),
        "macd_diff": round(macd_diff, 4),
        "smc_bias": smc_result["bias"],
        "signal": signal,
        "score": score,
        "reasons": reasons,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }


def scan_market():
    log.info("Running market scan...")
    for sym in SYMBOLS:
        sig = generate_signal(sym)
        SIGNAL_HISTORY.append(sig)
        if len(SIGNAL_HISTORY) > MAX_HISTORY:
            SIGNAL_HISTORY.pop(0)
        log.info(f"{sig['symbol']}: {sig['signal']} (score={sig['score']}, bias={sig.get('smc_bias')})")


# ==========================================================
# 🌐 ROUTES
# ==========================================================
@app.route("/")
def index():
    return render_template("index.html", symbols=SYMBOLS)


@app.route("/api/signal/<path:symbol>")
def api_signal(symbol):
    return jsonify(generate_signal(symbol))


@app.route("/api/signals")
def api_signals():
    return jsonify(list(reversed(SIGNAL_HISTORY[-100:])))


@app.route("/api/scan", methods=["POST"])
def api_scan():
    scan_market()
    return jsonify({"status": "ok", "count": len(SIGNAL_HISTORY)})


# ==========================================================
# ⏰ SCHEDULER
# ==========================================================
scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(scan_market, "interval",
                  minutes=SCAN_INTERVAL_MINUTES, id="market_scan",
                  next_run_time=datetime.now())


def start_scheduler():
    if not scheduler.running:
        scheduler.start()


# ==========================================================
# ▶️ ENTRYPOINT
# ==========================================================
if __name__ == "__main__":
    start_scheduler()
    app.run(host="0.0.0.0", port=PORT, debug=False)
