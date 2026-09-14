import os
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import List, Optional

import numpy as np
import pandas as pd
import requests as http_requests
import yfinance as yf
from flask import Flask, jsonify, request, render_template

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
LAST_SIGNALS = {}

# ============================================================
#  MARKET DATA
# ============================================================
class MarketData:
    TIMEFRAME_MAP = {
        "5m": ("5d", "5m"),
        "15m": ("1mo", "15m"),
        "1h": ("3mo", "60m"),
        "4h": ("6mo", "1h"),
        "1d": ("2y", "1d"),
    }

    @staticmethod
    def get_ohlc(symbol, timeframe="1h", limit=500):
        if timeframe not in MarketData.TIMEFRAME_MAP:
            raise ValueError(f"Unsupported timeframe: {timeframe}")
        period, interval = MarketData.TIMEFRAME_MAP[timeframe]
        df = yf.download(symbol, period=period, interval=interval,
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
#  SMC ANALYZER
# ============================================================
@dataclass
class SwingPoint:
    index: int
    price: float
    kind: str
    timestamp: str


@dataclass
class StructureBreak:
    index: int
    price: float
    kind: str
    direction: str
    timestamp: str


@dataclass
class OrderBlock:
    index: int
    top: float
    bottom: float
    kind: str
    mitigated: bool
    timestamp: str


@dataclass
class FVG:
    index: int
    top: float
    bottom: float
    kind: str
    filled: bool
    timestamp: str


@dataclass
class LiquidityZone:
    index: int
    price: float
    kind: str
    swept: bool
    timestamp: str


class SMCAnalyzer:
    def __init__(self, df, swing_lookback=5):
        self.df = df.reset_index()
        time_col = None
        for c in self.df.columns:
            if c.lower() in ("date", "datetime", "index"):
                time_col = c
                break
        self.time_col = time_col
        self.swing_lookback = swing_lookback
        self.swings = []
        self.structure = []
        self.order_blocks = []
        self.fvgs = []
        self.liquidity = []
        self.learned_weights = None

    def _ts(self, idx):
        if self.time_col is None:
            return str(idx)
        return str(self.df.loc[idx, self.time_col])

    def find_swings(self):
        n = self.swing_lookback
        highs = self.df["high"].values
        lows = self.df["low"].values
        for i in range(n, len(self.df) - n):
            wh = highs[i - n:i + n + 1]
            wl = lows[i - n:i + n + 1]
            if highs[i] == wh.max() and np.sum(wh == highs[i]) == 1:
                self.swings.append(SwingPoint(i, float(highs[i]), "high", self._ts(i)))
            if lows[i] == wl.min() and np.sum(wl == lows[i]) == 1:
                self.swings.append(SwingPoint(i, float(lows[i]), "low", self._ts(i)))
        self.swings.sort(key=lambda s: s.index)

    def detect_structure(self):
        if len(self.swings) < 2:
            return
        last_trend = None
        last_high = None
        last_low = None
        for sw in self.swings:
            if sw.kind == "high":
                if last_high is not None:
                    if self._broke(last_high.index, last_high.price, "up"):
                        kind = "CHoCH" if last_trend == "bearish" else "BOS"
                        self.structure.append(StructureBreak(
                            sw.index, last_high.price, kind, "bullish", self._ts(sw.index)))
                        last_trend = "bullish"
                last_high = sw
            else:
                if last_low is not None:
                    if self._broke(last_low.index, last_low.price, "down"):
                        kind = "CHoCH" if last_trend == "bullish" else "BOS"
                        self.structure.append(StructureBreak(
                            sw.index, last_low.price, kind, "bearish", self._ts(sw.index)))
                        last_trend = "bearish"
                last_low = sw

    def _broke(self, from_idx, level, direction):
        seg = self.df.iloc[from_idx + 1:]
        if seg.empty:
            return False
        if direction == "up":
            return bool((seg["close"] > level).any())
        return bool((seg["close"] < level).any())

    def detect_order_blocks(self):
        closes = self.df["close"].values
        opens = self.df["open"].values
        highs = self.df["high"].values
        lows = self.df["low"].values
        atr = self._atr(14)
        for i in range(2, len(self.df) - 3):
            if abs(closes[i] - opens[i]) < atr[i] * 0.5:
                continue
            if closes[i] < opens[i]:
                if (closes[i + 2] - highs[i]) > atr[i] * 1.5 \
                        and closes[i + 1] > highs[i] and closes[i + 2] > highs[i]:
                    mit = bool((self.df["low"].iloc[i + 3:] <= highs[i]).any())
                    self.order_blocks.append(OrderBlock(
                        i, float(highs[i]), float(lows[i]), "bullish", mit, self._ts(i)))
            if closes[i] > opens[i]:
                if (lows[i] - closes[i + 2]) > atr[i] * 1.5 \
                        and closes[i + 1] < lows[i] and closes[i + 2] < lows[i]:
                    mit = bool((self.df["high"].iloc[i + 3:] >= lows[i]).any())
                    self.order_blocks.append(OrderBlock(
                        i, float(highs[i]), float(lows[i]), "bearish", mit, self._ts(i)))

    def detect_fvgs(self):
        highs = self.df["high"].values
        lows = self.df["low"].values
        for i in range(2, len(self.df)):
            if lows[i] > highs[i - 2]:
                top, bot = float(lows[i]), float(highs[i - 2])
                filled = bool((self.df["low"].iloc[i + 1:] <= bot).any())
                self.fvgs.append(FVG(i, top, bot, "bullish", filled, self._ts(i)))
            if highs[i] < lows[i - 2]:
                top, bot = float(lows[i - 2]), float(highs[i])
                filled = bool((self.df["high"].iloc[i + 1:] >= top).any())
                self.fvgs.append(FVG(i, top, bot, "bearish", filled, self._ts(i)))

    def detect_liquidity(self):
        tol = float(self.df["close"].std()) * 0.05
        highs = [s for s in self.swings if s.kind == "high"]
        lows = [s for s in self.swings if s.kind == "low"]
        for i in range(len(highs) - 1):
            for j in range(i + 1, len(highs)):
                if abs(highs[i].price - highs[j].price) < tol:
                    swept = bool((self.df["high"].iloc[highs[j].index + 1:] >
                                  max(highs[i].price, highs[j].price) * 1.0005).any())
                    self.liquidity.append(LiquidityZone(
                        highs[j].index, float(max(highs[i].price, highs[j].price)),
                        "buy_side", swept, self._ts(highs[j].index)))
                    break
        for i in range(len(lows) - 1):
            for j in range(i + 1, len(lows)):
                if abs(lows[i].price - lows[j].price) < tol:
                    swept = bool((self.df["low"].iloc[lows[j].index + 1:] <
                                  min(lows[i].price, lows[j].price) * 0.9995).any())
                    self.liquidity.append(LiquidityZone(
                        lows[j].index, float(min(lows[i].price, lows[j].price)),
                        "sell_side", swept, self._ts(lows[j].index)))
                    break

    def _atr(self, period=14):
        h, l, c = self.df["high"], self.df["low"], self.df["close"]
        tr = pd.concat([
            h - l,
            (h - c.shift()).abs(),
            (l - c.shift()).abs(),
        ], axis=1).max(axis=1)
        return tr.rolling(period).mean().bfill().values

    def generate_signal(self):
        if not self.swings:
            return {"bias": "neutral", "confidence": 0, "reasons": ["Insufficient data"],
                    "entry": None, "stop_loss": None, "take_profit_1": None,
                    "take_profit_2": None, "risk_reward": None, "atr": 0}

        last = float(self.df["close"].iloc[-1])
        atr = float(self._atr(14)[-1])

        recent = self.structure[-3:] if self.structure else []
        bias = recent[-1].direction if recent else "neutral"

        active_obs = [o for o in self.order_blocks if not o.mitigated]
        nearby_obs = [o for o in active_obs
                      if abs(((o.top + o.bottom) / 2) - last) < atr * 3]
        unfilled = [f for f in self.fvgs if not f.filled]
        nearby_fvgs = [f for f in unfilled
                       if abs(((f.top + f.bottom) / 2) - last) < atr * 3]

        w = self.learned_weights or {"structure": 30, "order_block": 25, "fvg": 20}
        score = 0
        reasons = []

        if bias == "bullish":
            score += w.get("structure", 30)
            reasons.append("Recent bullish BOS/CHoCH")
        elif bias == "bearish":
            score += w.get("structure", 30)
            reasons.append("Recent bearish BOS/CHoCH")

        bull_obs = [o for o in nearby_obs if o.kind == "bullish"]
        bear_obs = [o for o in nearby_obs if o.kind == "bearish"]

        if bias == "bullish" and bull_obs:
            score += w.get("order_block", 25)
            reasons.append(f"{len(bull_obs)} bullish OB below")
        if bias == "bearish" and bear_obs:
            score += w.get("order_block", 25)
            reasons.append(f"{len(bear_obs)} bearish OB above")

        if bias == "bullish" and any(f.kind == "bullish" for f in nearby_fvgs):
            score += w.get("fvg", 20)
            reasons.append("Bullish FVG nearby")
        if bias == "bearish" and any(f.kind == "bearish" for f in nearby_fvgs):
            score += w.get("fvg", 20)
            reasons.append("Bearish FVG nearby")

        entry = last
        sl = tp1 = tp2 = None
        min_risk = atr * 0.5

        if bias == "bullish":
            sl = min([o.bottom for o in bull_obs] or [last - atr * 1.5])
            if (entry - sl) < min_risk:
                sl = entry - min_risk
            risk = entry - sl
            tp1, tp2 = entry + risk * 2, entry + risk * 3
        elif bias == "bearish":
            sl = max([o.top for o in bear_obs] or [last + atr * 1.5])
            if (sl - entry) < min_risk:
                sl = entry + min_risk
            risk = sl - entry
            tp1, tp2 = entry - risk * 2, entry - risk * 3

        if bias in ("bullish", "bearish") and sl is not None:
            risk = (entry - sl) if bias == "bullish" else (sl - entry)
            reward = (tp1 - entry) if bias == "bullish" else (entry - tp1)
            if risk <= 0 or reward <= 0 or (reward / risk) < 1.0:
                return {"bias": "neutral", "confidence": 0,
                        "reasons": ["Setup rejected: reward/risk too low"],
                        "entry": entry, "stop_loss": None,
                        "take_profit_1": None, "take_profit_2": None,
                        "risk_reward": None, "atr": atr}

        return {
            "bias": bias, "confidence": min(score, 95), "reasons": reasons,
            "entry": entry, "stop_loss": sl,
            "take_profit_1": tp1, "take_profit_2": tp2,
            "risk_reward": "1:2 / 1:3" if sl else None, "atr": atr,
        }

    def analyze(self):
        self.find_swings()
        self.detect_structure()
        self.detect_order_blocks()
        self.detect_fvgs()
        self.detect_liquidity()
        return {
            "candles": self._candles(),
            "swings": [asdict(s) for s in self.swings],
            "structure": [asdict(s) for s in self.structure],
            "order_blocks": [asdict(o) for o in self.order_blocks],
            "fvgs": [asdict(f) for f in self.fvgs],
            "liquidity": [asdict(l) for l in self.liquidity],
            "signal": self.generate_signal(),
        }

    def _candles(self):
        return [{
            "time": self._ts(i),
            "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"]),
            "volume": float(r["volume"]),
        } for i, r in self.df.iterrows()]


# ============================================================
#  LEARNING ENGINE
# ============================================================
class LearningEngine:
    def __init__(self):
        self.data = {
            "predictions": [],
            "weights": {"structure": 30, "order_block": 25, "fvg": 20},
            "stats": {"total": 0, "correct": 0, "wrong": 0},
        }

    def record(self, symbol, timeframe, signal, entry):
        self.data["predictions"].append({
            "symbol": symbol, "timeframe": timeframe,
            "bias": signal.get("bias"), "confidence": signal.get("confidence"),
            "entry": entry, "stop_loss": signal.get("stop_loss"),
            "take_profit_1": signal.get("take_profit_1"),
            "timestamp": datetime.utcnow().isoformat(), "outcome": None,
        })

    def update_outcomes(self, prices):
        updated = 0
        for p in self.data["predictions"]:
            if p["outcome"] is not None or p["symbol"] not in prices:
                continue
            price = prices[p["symbol"]]
            bias, tp, sl = p["bias"], p["take_profit_1"], p["stop_loss"]
            if not tp or not sl:
                p["outcome"] = "invalid"
                continue
            if bias == "bullish":
                if price >= tp: p["outcome"] = "win"
                elif price <= sl: p["outcome"] = "loss"
            elif bias == "bearish":
                if price <= tp: p["outcome"] = "win"
                elif price >= sl: p["outcome"] = "loss"
            if p["outcome"]:
                self.data["stats"]["total"] += 1
                if p["outcome"] == "win":
                    self.data["stats"]["correct"] += 1
                else:
                    self.data["stats"]["wrong"] += 1
                updated += 1
        return updated

    def optimize(self):
        resolved = [p for p in self.data["predictions"] if p["outcome"] in ("win", "loss")]
        if len(resolved) < 10:
            return self.data["weights"]
        recent = resolved[-100:]
        w = self.data["weights"].copy()
        for key in w:
            hi = [p for p in recent if p["confidence"] and p["confidence"] > 60]
            if len(hi) < 5:
                continue
            wr = sum(1 for p in hi if p["outcome"] == "win") / len(hi)
            w[key] = round(w[key] * 0.9 + (20 + wr * 20) * 0.1, 2)
        total = sum(w.values())
        if total > 0:
            w = {k: round(v / total * 90, 2) for k, v in w.items()}
        self.data["weights"] = w
        return w

    def stats(self):
        return self.data


engine = LearningEngine()


# ============================================================
#  TELEGRAM
# ============================================================
def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[notifier] Telegram not configured")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = http_requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID, "text": message,
            "parse_mode": "HTML", "disable_web_page_preview": True,
        }, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"[notifier] failed: {e}")
        return False


def format_alert(symbol, timeframe, signal):
    bias = (signal.get("bias") or "neutral").upper()
    emoji = "🟢" if bias == "BULLISH" else "🔴" if bias == "BEARISH" else "⚪"

    def fmt(v):
        if v is None: return "—"
        n = float(v)
        if n < 10: return f"{n:.5f}"
        if n < 1000: return f"{n:.2f}"
        return f"{n:,.2f}"

    lines = [
        f"{emoji} <b>{bias} SIGNAL</b>",
        f"<b>{symbol}</b> · {timeframe}",
        f"Confidence: <b>{signal.get('confidence', 0)}%</b>", "",
        f"Entry: <b>{fmt(signal.get('entry'))}</b>",
        f"SL: {fmt(signal.get('stop_loss'))}",
        f"TP1: {fmt(signal.get('take_profit_1'))}",
        f"TP2: {fmt(signal.get('take_profit_2'))}",
    ]
    for r in (signal.get("reasons") or [])[:4]:
        lines.append(f"• {r}")
    return "\n".join(lines)


# ============================================================
#  ROUTES
# ============================================================
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/analyze")
def analyze():
    symbol = request.args.get("symbol", "BTC-USD")
    timeframe = request.args.get("timeframe", "1h")
    limit = int(request.args.get("limit", 500))
    notify = request.args.get("notify", "1") == "1"
    try:
        df = MarketData.get_ohlc(symbol, timeframe, limit)
        analyzer = SMCAnalyzer(df)
        analyzer.learned_weights = engine.data.get("weights", {})
        result = analyzer.analyze()
        signal = result["signal"]
        if signal.get("bias") != "neutral":
            engine.record(symbol, timeframe, signal, signal["entry"])
            if notify:
                key = f"{symbol}_{timeframe}"
                if LAST_SIGNALS.get(key) != signal.get("bias"):
                    send_telegram(format_alert(symbol, timeframe, signal))
                    LAST_SIGNALS[key] = signal.get("bias")
        return jsonify({"success": True, "symbol": symbol,
                        "timeframe": timeframe, **result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/learning/stats")
def learning_stats():
    return jsonify(engine.stats())


@app.route("/api/learning/update")
def learning_update():
    symbols = set(p["symbol"] for p in engine.data["predictions"]
                  if p["outcome"] is None)
    prices = {}
    for sym in symbols:
        try:
            df = MarketData.get_ohlc(sym, "1h", 5)
            prices[sym] = float(df["close"].iloc[-1])
        except Exception:
            continue
    updated = engine.update_outcomes(prices)
    new_w = engine.optimize()
    return jsonify({"updated": updated, "weights": new_w})


@app.route("/api/test-notify")
def test_notify():
    ok = send_telegram("✅ <b>SMC Trader</b> notifications working!")
    return jsonify({"sent": ok})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
