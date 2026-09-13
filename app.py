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
    ("XAU/USD", "Gold", "twelve"),
    ("EUR/USD", "Euro", "twelve"),
    ("BTC/USDT", "Bitcoin", "binance"),
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

# ---------- DATA FETCHERS ----------

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

def fetch_binance(symbol, interval, size):
    binance_symbol = symbol.replace("/", "")
    url = f"https://api.binance.com/api/v3/klines?symbol={binance_symbol}&interval={interval}&limit={size}"
    try:
        r = requests.get(url, timeout=15)
        data = r.json()
        if not isinstance(data, list):
            return None
        out = []
        for c in data:
            try:
                out.append({
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                    "volume": float(c[5]),
                })
            except (KeyError, ValueError, IndexError):
                continue
        return out
    except Exception:
        return None

def get_candles(symbol, source, interval, size):
    if source == "binance":
        return fetch_binance(symbol, interval, size)
    return fetch_twelve(symbol, interval, size)

# ---------- INDICATORS ----------

def atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i-1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    return sum(trs[-period:]) / period

def volatility_ok(candles):
    if len(candles) < 100:
        return True
    current = atr(candles, 14)
    historic = atr(candles[-100:], 14)
    if current is None or historic is None or historic == 0:
        return True
    return current < historic * 2

def volume_poc(candles, bins=20):
    if not candles or all(c["volume"] == 0 for c in candles):
        return None
    lo = min(c["low"] for c in candles)
    hi = max(c["high"] for c in candles)
    if hi == lo:
        return None
    bin_size = (hi - lo) / bins
    buckets = [0] * bins
    for c in candles:
        span = max(c["high"] - c["low"], 1e-9)
        for b in range(bins):
            bucket_lo = lo + b * bin_size
            bucket_hi = bucket_lo + bin_size
            overlap = min(c["high"], bucket_hi) - max(c["low"], bucket_lo)
            if overlap > 0:
                buckets[b] += c["volume"] * (overlap / span)
    max_idx = buckets.index(max(buckets))
    return lo + (max_idx + 0.5) * bin_size

def cvd_binance(symbol):
    binance_symbol = symbol.replace("/", "")
    url = f"https://api.binance.com/api/v3/aggTrades?symbol={binance_symbol}&limit=1000"
    try:
        r = requests.get(url, timeout=15)
        trades = r.json()
        if not isinstance(trades, list):
            return None
        cvd = 0
        for t in trades:
            try:
                qty = float(t["q"])
                if t["m"]:
                    cvd -= qty
                else:
                    cvd += qty
            except (KeyError, ValueError):
                continue
        return cvd
    except Exception:
        return None

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

def fetch_one(symbol, source):
    try:
        candles = get_candles(symbol, source, "1h", 120)
        if not candles or len(candles) < 20:
            return {"symbol": symbol, "error": "no data"}

        htf_candles = get_candles(symbol, source, "4h", 20)

        prev_high = candles[-2]["high"]
        prev_low = candles[-2]["low"]
        last_close = candles[-1]["close"]

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

        vol_ok = volatility_ok(candles)
        poc = volume_poc(candles[-50:])
        cvd = cvd_binance(symbol) if source == "binance" else None

        poc_ok = True
        if poc is not None:
            if raw_signal == "BUY" and prev_high < poc:
                poc_ok = False
            if raw_signal == "SELL" and prev_low > poc:
                poc_ok = False

        cvd_ok = True
        cvd_note = ""
        if cvd is not None:
            if raw_signal == "BUY" and cvd < 0:
                cvd_ok = False
            if raw_signal == "SELL" and cvd > 0:
                cvd_ok = False
            cvd_note = f"CVD {'+' if cvd > 0 else ''}{cvd:.1f}"

        if raw_signal == "HOLD":
            signal, color, note = "HOLD", "#9e9e9e", "No break"
        elif htf == "BEARISH" and raw_signal == "BUY":
            signal, color, note = "HOLD", "#9e9e9e", "Blocked: 4H bearish"
        elif htf == "BULLISH" and raw_signal == "SELL":
            signal, color, note = "HOLD", "#9e9e9e", "Blocked: 4H bullish"
        elif not vol_ok:
            signal, color, note = "HOLD", "#9e9e9e", "Blocked: high volatility"
        elif fvg is None or fvg["type"] != ("bullish" if raw_signal == "BUY" else "bearish"):
            signal, color, note = "HOLD", "#9e9e9e", "No FVG aligned"
        elif ob is None:
            signal, color, note = "HOLD", "#9e9e9e", "No order block"
        elif not poc_ok:
            signal, color, note = "HOLD", "#9e9e9e", "Blocked: POC disagrees"
        elif not cvd_ok:
            signal, color, note = "HOLD", "#9e9e9e", "Blocked: CVD disagrees"
        else:
            signal = raw_signal
            color = "#00e676" if signal == "BUY" else "#ff5252"
            extras = []
            if poc is not None: extras.append(f"POC {poc:.2f}")
            if cvd_note: extras.append(cvd_note)
            note = f"{signal} · " + " · ".join(extras) if extras else f"{signal} confirmed"

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
            "poc": poc,
            "cvd": cvd,
        }
    except Exception as e:
        return {"symbol": symbol, "error": str(e)}

def fetch_all():
    now = time.time()
    if _cache["data"] is not None and (now - _cache["last_fetch"]) < 300:
        return _cache["data"]

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda p: fetch_one(p[0], p[2]), PAIRS))

    for r, (symbol, name, _) in zip(results, PAIRS):
        r["name"] = name

        sig = r.get("signal")
        prev_sig = _cache["last_signals"].get(symbol)
        if sig in ("BUY", "SELL") and sig != prev_sig and r.get("trade"):
            t = r["trade"]
            emoji = "🟢" if sig == "BUY" else "🔴"
            msg = (
                f"{emoji} *{sig} SIGNAL: {symbol}*\n"
                f"Entry: `{t['entry']:.2f}`\n"
                f"SL: `{t['sl']:.2f}`\n"
                f"TP: `{t['tp']:.2f}`\n"
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
            <span class="mode-live">24/7 ACTIVE</span>
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
                        const fvgOn = r.fvg ? 'tag-on' : 'tag-off';
                        const volOn = r.vol_ok ? 'tag-on' : 'tag-off';
                        const cvdOn = r.cvd === null ? 'tag-off' : (r.cvd > 0 ? 'tag-on' : 'tag-off');
                        html += '<div class="tags">';
                        html += '<span class="tag ' + fvgOn + '">FVG ' + (r.fvg ? '✓' : '✗') + '</span>';
                        html += '<span class="tag ' + (r.trade ? 'tag-on' : 'tag-off') + '">OB ' + (r.trade ? '✓' : '✗') + '</span>';
                        html += '<span class="tag ' + volOn + '">ATR ' + (r.vol_ok ? '✓' : '✗') + '</span>';
                        html += '<span class="tag ' + cvdOn + '">CVD ' + (r.cvd === null ? '—' : (r.cvd > 0 ? '↑' : '↓')) + '</span>';
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
