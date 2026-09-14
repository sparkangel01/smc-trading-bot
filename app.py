import os
import json
from datetime import datetime
import requests as http_requests

from flask import Flask, jsonify, request, render_template
from market_data import MarketData
from smc_analyzer import SMCAnalyzer
from learning import LearningEngine

app = Flask(__name__)
engine = LearningEngine()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
LAST_SIGNALS = {}


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[notifier] Telegram not configured")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = http_requests.post(url, json=payload, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"[notifier] Telegram failed: {e}")
        return False


def format_signal_alert(symbol, timeframe, signal):
    bias = (signal.get("bias") or "neutral").upper()
    conf = signal.get("confidence", 0)
    entry = signal.get("entry")
    sl = signal.get("stop_loss")
    tp1 = signal.get("take_profit_1")
    tp2 = signal.get("take_profit_2")
    reasons = signal.get("reasons", [])

    emoji = "🟢" if bias == "BULLISH" else "🔴" if bias == "BEARISH" else "⚪"

    def fmt(v):
        if v is None:
            return "—"
        n = float(v)
        if n < 10:
            return f"{n:.5f}"
        if n < 1000:
            return f"{n:.2f}"
        return f"{n:,.2f}"

    lines = [
        f"{emoji} <b>{bias} SIGNAL</b>",
        f"<b>{symbol}</b> · {timeframe}",
        f"Confidence: <b>{conf}%</b>",
        "",
        f"Entry: <b>{fmt(entry)}</b>",
        f"Stop Loss: {fmt(sl)}",
        f"TP1: {fmt(tp1)}",
        f"TP2: {fmt(tp2)}",
    ]
    if reasons:
        lines.append("")
        lines.append("<b>Why:</b>")
        for r in reasons[:4]:
            lines.append(f"• {r}")
    return "\n".join(lines)


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
            engine.record_prediction(symbol, timeframe, signal, signal["entry"])

            if notify:
                key = f"{symbol}_{timeframe}"
                last = LAST_SIGNALS.get(key)
                current_bias = signal.get("bias")
                if last != current_bias:
                    send_telegram(format_signal_alert(symbol, timeframe, signal))
                    LAST_SIGNALS[key] = current_bias

        return jsonify({
            "success": True,
            "symbol": symbol,
            "timeframe": timeframe,
            "timestamp": datetime.utcnow().isoformat(),
            **result,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/learning/stats")
def learning_stats():
    return jsonify(engine.get_stats())


@app.route("/api/learning/update")
def learning_update():
    symbols = set(p["symbol"] for p in engine.data["predictions"] if p["outcome"] is None)
    prices = {}
    for sym in symbols:
        try:
            df = MarketData.get_ohlc(sym, "1h", 5)
            prices[sym] = float(df["close"].iloc[-1])
        except Exception:
            continue
    updated = engine.update_outcomes(prices)
    new_weights = engine.optimize_weights()
    return jsonify({"updated": updated, "weights": new_weights})


@app.route("/api/test-notify")
def test_notify():
    ok = send_telegram("✅ <b>SMC Trader</b> notifications are working!")
    return jsonify({"sent": ok, "message": "Check your Telegram"})


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
