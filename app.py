from flask import Flask, jsonify, render_template_string
import requests
import os
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)

TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

PAIRS = [
    ("XAU/USD", "Gold"),
    ("EUR/USD", "Euro"),
    ("BTC/USD", "Bitcoin"),
]

_cache = {"data": None, "last_fetch": 0, "last_signals": {}}

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=10)
    except Exception:
        pass

# ---------- DATA ----------

def fetch_twelve(symbol, interval, size):
    url = f"https://api.twelvedata.com/time_series?symbol={symbol}&interval={interval}&outputsize={size}&apikey={TWELVEDATA_API_KEY}"
    try:
        r = requests.get(url, timeout=15)
        data = r.json()
        if "values" not in data:
            return None
        raw = list(reversed(data["values"]))
        out = []
        for c in raw:
            try:
                out.append({
                    "high": float(c["high"]),
                    "low": float(c["low"]),
                    "close": float(c["close"]),
                    "open": float(c["open"]),
                    "volume": float(c.get("volume", 0) or 0),
                })
            except (KeyError, ValueError):
                continue
        return out
    except Exception:
        return None

# ---------- INDICATORS ----------

def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema_val = sum(values[:period]) / period
    for v in values[period:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val

def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(values)):
        diff = values[i] - values[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period

def bollinger(values, period=20, mult=2):
    if len(values) < period:
        return None, None, None
    recent = values[-period:]
    mid = sum(recent) / period
    variance = sum((x - mid) ** 2 for x in recent) / period
    std = variance ** 0.5
    return mid + mult * std, mid, mid - mult * std

def momentum(values, period=10):
    if len(values) < period + 1:
        return None
    return ((values[-1] - values[-period-1]) / values[-period-1]) * 100

def find_swing_levels(candles, lookback=50):
    """Find recent swing highs and lows."""
    highs = [c["high"] for c in candles[-lookback:]]
    lows = [c["low"] for c in candles[-lookback:]]
    return max(highs), min(lows)

def volatility_ok(candles):
    if len(candles) < 100:
        return True
    current = atr(candles, 14)
    historic = atr(candles[-100:], 14)
    if current is None or historic is None or historic == 0:
        return True
    return current < historic * 2

# ---------- SMC PATTERNS ----------

def get_htf_bias(candles):
    if not candles or len(candles) < 10:
        return None
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    rh = max(highs[-5:]); eh = max(highs[-10:-5])
    rl = min(lows[-5:]);  el = min(lows[-10:-5])
    if rh > eh and rl > el: return "BULLISH"
    if rh < eh and rl < el: return "BEARISH"
    return "RANGING"

def find_fvg(candles):
    if len(candles) < 3:
        return None
    for i in range(len(candles) - 1, max(len(candles) - 6, 1), -1):
        c1 = candles[i - 2]
        c3 = candles[i]
        if c1["high"] < c3["low"]:
            return {"type": "bullish", "top": c3["low"], "bottom": c1["high"]}
        if c1["low"] > c3["high"]:
            return {"type": "bearish", "top": c1["low"], "bottom": c3["high"]}
    return None

def find_order_block(candles, direction):
    if len(candles) < 5:
        return None
    for i in range(len(candles) - 2, max(len(candles) - 10, 0), -1):
        c = candles[i]
        nxt = candles[i + 1]
        body = abs(c["close"] - c["open"])
        next_body = abs(nxt["close"] - nxt["open"])
        if next_body < body * 1.5:
            continue
        if direction == "bullish" and c["close"] < c["open"]:
            return {"top": c["high"], "bottom": c["low"]}
        if direction == "bearish" and c["close"] > c["open"]:
            return {"top": c["high"], "bottom": c["low"]}
    return None

def calc_trade(signal, entry, prev_high, prev_low, ob):
    if signal == "BUY":
        sl = ob["bottom"] if ob else prev_low
        risk = entry - sl
        if risk <= 0: return None
        return {"entry": entry, "sl": sl, "tp": entry + risk * 2}
    elif signal == "SELL":
        sl = ob["top"] if ob else prev_high
        risk = sl - entry
        if risk <= 0: return None
        return {"entry": entry, "sl": sl, "tp": entry - risk * 2}
    return None

# ---------- MAIN LOGIC ----------

def fetch_one(symbol):
    try:
        candles = fetch_twelve(symbol, "1h", 220)
        if not candles or len(candles) < 50:
            return {"symbol": symbol, "error": "no data"}

        htf_candles = fetch_twelve(symbol, "4h", 20)
        closes = [c["close"] for c in candles]

        prev_high = candles[-2]["high"]
        prev_low = candles[-2]["low"]
        last_close = candles[-1]["close"]

        # --- SMC base ---
        htf = get_htf_bias(htf_candles) if htf_candles else None
        if last_close > prev_high:
            raw_signal = "BUY"
        elif last_close < prev_low:
            raw_signal = "SELL"
        else:
            raw_signal = "HOLD"

        fvg = find_fvg(candles)
        ob = None
        if raw_signal == "BUY":
            ob = find_order_block(candles, "bullish")
        elif raw_signal == "SELL":
            ob = find_order_block(candles, "bearish")

        # --- Confirmations from other strategies ---
        confirmations = []
        confirmations_failed = []

        # 1. EMA trend
        ema50 = ema(closes, 50)
        ema200 = ema(closes, 200)
        ema_ok = False
        if ema50 and ema200:
            if raw_signal == "BUY" and ema50 > ema200:
                ema_ok = True; confirmations.append("EMA")
            elif raw_signal == "SELL" and ema50 < ema200:
                ema_ok = True; confirmations.append("EMA")
            else:
                confirmations_failed.append("EMA")

        # 2. RSI
        rsi_val = rsi(closes, 14)
        rsi_ok = False
        if rsi_val is not None:
            if raw_signal == "BUY" and rsi_val > 50:
                rsi_ok = True; confirmations.append("RSI")
            elif raw_signal == "SELL" and rsi_val < 50:
                rsi_ok = True; confirmations.append("RSI")
            else:
                confirmations_failed.append("RSI")

        # 3. Bollinger Bands
        bb_up, bb_mid, bb_lo = bollinger(closes, 20, 2)
        bb_ok = False
        if bb_up is not None:
            if raw_signal == "BUY" and last_close > bb_mid:
                bb_ok = True; confirmations.append("BB")
            elif raw_signal == "SELL" and last_close < bb_mid:
                bb_ok = True; confirmations.append("BB")
            else:
                confirmations_failed.append("BB")

        # 4. Momentum
        mom = momentum(closes, 10)
        mom_ok = False
        if mom is not None:
            if raw_signal == "BUY" and mom > 0:
                mom_ok = True; confirmations.append("MOM")
            elif raw_signal == "SELL" and mom < 0:
                mom_ok = True; confirmations.append("MOM")
            else:
                confirmations_failed.append("MOM")

        # 5. Support / Resistance
        res, sup = find_swing_levels(candles, 50)
        sr_ok = False
        if raw_signal == "BUY" and last_close > res * 0.995:
            sr_ok = True; confirmations.append("S/R")
        elif raw_signal == "SELL" and last_close < sup * 1.005:
            sr_ok = True; confirmations.append("S/R")
        else:
            confirmations_failed.append("S/R")

        # Volatility filter
        vol_ok = volatility_ok(candles)

        # --- Final decision ---
        # Rule: SMC setup must exist, ATR must be safe, and at least 2 confirmations
        smc_ready = (
            raw_signal in ("BUY", "SELL") and
            not (htf == "BEARISH" and raw_signal == "BUY") and
            not (htf == "BULLISH" and raw_signal == "SELL") and
            fvg is not None and fvg["type"] == ("bullish" if raw_signal == "BUY" else "bearish") and
            ob is not None
        )

        if raw_signal == "HOLD":
            signal, color, note = "HOLD", "#9e9e9e", "No break"
        elif not smc_ready:
            signal, color, note = "HOLD", "#9e9e9e", "SMC setup incomplete"
        elif not vol_ok:
            signal, color, note = "HOLD", "#9e9e9e", "Blocked: high volatility"
        elif len(confirmations) < 2:
            signal, color, note = "HOLD", "#9e9e9e", f"Only {len(confirmations)} confirmation(s)"
        else:
            signal = raw_signal
            color = "#00e676" if signal == "BUY" else "#ff5252"
            note = f"{signal} · {'+'.join(confirmations)} ({len(confirmations)}/5)"

        trade = calc_trade(signal, last_close, prev_high, prev_low, ob) if signal in ("BUY", "SELL") else None

        return {
            "symbol": symbol,
            "price": last_close,
            "prev_high": prev_high,
            "prev_low": prev_low,
            "signal": signal,
            "color": color,
            "note": note,
            "htf": htf or "UNKNOWN",
            "fvg": fvg,
            "trade": trade,
            "vol_ok": vol_ok,
            "confirmations": confirmations,
            "confirmations_failed": confirmations_failed,
        }
    except Exception as e:
        return {"symbol": symbol, "error": str(e)}

def fetch_all():
    now = time.time()
    if _cache["data"] is not None and (now - _cache["last_fetch"]) < 300:
        return _cache["data"]

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda p: fetch_one(p[0]), PAIRS))

    for r, (symbol, name) in zip(results, PAIRS):
        r["name"] = name

        sig = r.get("signal")
        prev_sig = _cache["last_signals"].get(symbol)
        if sig in ("BUY", "SELL") and sig != prev_sig and r.get("trade"):
            t = r["trade"]
            emoji = "🟢" if sig == "BUY" else "🔴"
            confs = "+".join(r.get("confirmations", []))
            msg = (
                f"{emoji} *{sig} SIGNAL: {symbol}*\n"
                f"Entry: `{t['entry']:.2f}`\n"
                f"SL: `{t['sl']:.2f}`\n"
                f"TP: `{t['tp']:.2f}`\n"
                f"Confirmations: {confs}\n"
                f"4H Bias: {r['htf']}"
            )
            send_telegram(msg)
        _cache["last_signals"][symbol] = sig

    fresh = {
        "results": results,
        "updated": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
    }
    _cache["data"] = fresh
    _cache["last_fetch"] = now
    return fresh

# ---------- ROUTES ----------

@app.route("/data")
def data_route():
    return jsonify(fetch_all())

@app.route("/")
def home():
    initial = fetch_all()
    return render_template_string(PAGE, initial=initial)

@app.route("/health")
def health():
    return "ok"

@app.route("/test")
def test():
    send_telegram("✅ Test message from your SMC bot!")
    return "sent"

PAGE = """
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SMC Signal Bot · 24/7</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #0a0e17; color: #e0e0e0;
            min-height: 100vh; padding: 20px;
            display: flex; justify-content: center;
        }
        .wrap { width: 100%; max-width: 480px; }
        .header {
            display: flex; justify-content: space-between;
            align-items: center; margin-bottom: 12px; padding: 0 4px;
        }
        .title { font-size: 14px; font-weight: 600; letter-spacing: 2px; color: #6b7280; text-transform: uppercase; }
        .live { display: flex; align-items: center; gap: 6px; font-size: 11px; color: #00e676; font-weight: 600; }
        .dot { width: 8px; height: 8px; background: #00e676; border-radius: 50%; animation: pulse 1.5s infinite; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
        .mode-bar {
            background: #131824; border: 1px solid #1f2633;
            border-radius: 12px; padding: 10px 14px;
            margin-bottom: 16px; font-size: 11px;
            display: flex; justify-content: space-between; align-items: center;
        }
        .mode-live { color: #00e676; font-weight: 700; }
        .card {
            background: #131824; border: 1px solid #1f2633;
            border-radius: 16px; padding: 18px 20px; margin-bottom: 12px;
        }
        .row1 { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 14px; }
        .pair { font-size: 16px; font-weight: 700; color: #fff; margin-bottom: 2px; }
        .name { font-size: 11px; color: #6b7280; margin-bottom: 4px; }
        .htf { font-size: 10px; font-weight: 600; letter-spacing: 1px; margin-bottom: 8px; }
        .htf-bull { color: #00e676; }
        .htf-bear { color: #ff5252; }
        .htf-range { color: #6b7280; }
        .price { font-size: 20px; font-weight: 700; color: #e0e0e0; letter-spacing: -0.5px; }
        .signal-badge {
            display: inline-block;
            font-size: 13px; font-weight: 800; letter-spacing: 1px;
            padding: 6px 12px; border-radius: 8px; border: 1.5px solid;
        }
        .tags { margin-top: 10px; display: flex; gap: 6px; flex-wrap: wrap; }
        .tag {
            font-size: 9px; font-weight: 700; letter-spacing: 1px;
            padding: 3px 8px; border-radius: 5px;
            background: #0a0e17; border: 1px solid #1f2633;
        }
        .tag-on { color: #00e676; border-color: #00e676; }
        .tag-off { color: #4b5563; }
        .note-line { font-size: 10px; color: #6b7280; margin-top: 8px; }
        .trade-grid {
            display: grid; grid-template-columns: 1fr 1fr 1fr;
            gap: 8px; margin-top: 12px;
            padding-top: 12px; border-top: 1px solid #1f2633;
        }
        .trade-cell {
            text-align: center; padding: 8px 4px;
            background: #0a0e17; border-radius: 8px;
        }
        .trade-label { font-size: 9px; color: #6b7280; letter-spacing: 1px; margin-bottom: 4px; }
        .trade-value { font-size: 12px; font-weight: 700; color: #e0e0e0; }
        .err { color: #ff5252; font-size: 12px; }
        .footer { text-align: center; font-size: 10px; color: #4b5563; margin-top: 20px; padding: 0 4px; }
        .loading { text-align: center; color: #6b7280; padding: 40px 20px; font-size: 13px; }
    </style>
</head>
<body>
    <div class="wrap">
        <div class="header">
            <div class="title">SMC Signal Bot · 1H</div>
            <div class="live"><span class="dot"></span>LIVE</div>
        </div>
        <div class="mode-bar">
            <span>Mode:</span>
            <span class="mode-live">24/7 · MULTI-STRATEGY</span>
        </div>
        <div id="cards"><div class="loading">Loading signals...</div></div>
        <div class="footer">Updated <span id="updated">—</span></div>
    </div>

    <script>
        let rendered = false;

        async function refresh() {
            try {
                const res = await fetch('/data');
                const d = await res.json();
                const container = document.getElementById('cards');
                let html = '';

                for (const r of d.results) {
                    html += '<div class="card">';
                    html += '<div class="row1"><div>';
                    html += '<div class="pair">' + r.symbol + '</div>';
                    html += '<div class="name">' + r.name + '</div>';

                    let htfClass = 'htf-range';
                    if (r.htf === 'BULLISH') htfClass = 'htf-bull';
                    if (r.htf === 'BEARISH') htfClass = 'htf-bear';
                    html += '<div class="htf ' + htfClass + '">4H: ' + (r.htf || '—') + '</div>';

                    if (r.error) {
                        html += '<div class="err">' + r.error + '</div>';
                    } else {
                        html += '<div class="price">$' + r.price.toFixed(2) + '</div>';
                    }
                    html += '</div>';

                    if (!r.error) {
                        html += '<div style="text-align:right;">';
                        html += '<div class="signal-badge" style="color:' + r.color + ';border-color:' + r.color + ';">' + r.signal + '</div>';
                        html += '</div>';
                    }
                    html += '</div>';

                    if (!r.error) {
                        const confs = r.confirmations || [];
                        const fvgOn = r.fvg ? 'tag-on' : 'tag-off';
                        const volOn = r.vol_ok ? 'tag-on' : 'tag-off';
                        html += '<div class="tags">';
                        html += '<span class="tag ' + fvgOn + '">FVG ' + (r.fvg ? '✓' : '✗') + '</span>';
                        html += '<span class="tag ' + (r.trade ? 'tag-on' : 'tag-off') + '">OB ' + (r.trade ? '✓' : '✗') + '</span>';
                        html += '<span class="tag ' + volOn + '">ATR ' + (r.vol_ok ? '✓' : '✗') + '</span>';
                        html += '<span class="tag ' + (confs.includes('EMA') ? 'tag-on' : 'tag-off') + '">EMA</span>';
                        html += '<span class="tag ' + (confs.includes('RSI') ? 'tag-on' : 'tag-off') + '">RSI</span>';
                        html += '<span class="tag ' + (confs.includes('BB') ? 'tag-on' : 'tag-off') + '">BB</span>';
                        html += '<span class="tag ' + (confs.includes('MOM') ? 'tag-on' : 'tag-off') + '">MOM</span>';
                        html += '<span class="tag ' + (confs.includes('S/R') ? 'tag-on' : 'tag-off') + '">S/R</span>';
                        html += '</div>';
                        html += '<div class="note-line">' + r.note + '</div>';
                    }

                    if (!r.error && r.trade) {
                        html += '<div class="trade-grid">';
                        html += '<div class="trade-cell"><div class="trade-label">ENTRY</div><div class="trade-value">' + r.trade.entry.toFixed(2) + '</div></div>';
                        html += '<div class="trade-cell"><div class="trade-label">STOP LOSS</div><div class="trade-value" style="color:#ff5252;">' + r.trade.sl.toFixed(2) + '</div></div>';
                        html += '<div class="trade-cell"><div class="trade-label">TAKE PROFIT</div><div class="trade-value" style="color:#00e676;">' + r.trade.tp.toFixed(2) + '</div></div>';
                        html += '</div>';
                    }

                    html += '</div>';
                }

                container.innerHTML = html;
                document.getElementById('updated').textContent = d.updated;
                rendered = true;
            } catch (e) {
                console.log('Refresh failed:', e);
                if (!rendered) {
                    document.getElementById('cards').innerHTML = '<div class="loading">Slow connection. Retrying...</div>';
                }
            }
        }

        refresh();
        setInterval(refresh, 300000);
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run()
