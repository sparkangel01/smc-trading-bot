from flask import Flask, jsonify, render_template_string
import requests
import os
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)

TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY")

PAIRS = [
    ("XAU/USD", "Gold"),
    ("EUR/USD", "Euro"),
    ("BTC/USD", "Bitcoin"),
]

_cache = {"data": None, "last_fetch": 0}

def in_killzone():
    hour = datetime.now(timezone.utc).hour
    london = 7 <= hour < 10
    newyork = 13 <= hour < 16
    return london or newyork

def killzone_name():
    hour = datetime.now(timezone.utc).hour
    if 7 <= hour < 10:
        return "London"
    if 13 <= hour < 16:
        return "New York"
    return "Closed"

def get_htf_bias(symbol):
    try:
        url = f"https://api.twelvedata.com/time_series?symbol={symbol}&interval=4h&outputsize=20&apikey={TWELVEDATA_API_KEY}"
        r = requests.get(url, timeout=15)
        data = r.json()
        if "values" not in data:
            return None

        candles = list(reversed(data["values"]))
        highs, lows, closes = [], [], []
        for c in candles:
            try:
                highs.append(float(c["high"]))
                lows.append(float(c["low"]))
                closes.append(float(c["close"]))
            except (KeyError, ValueError):
                continue

        if len(closes) < 5:
            return None

        recent_high = max(highs[-5:])
        earlier_high = max(highs[-10:-5]) if len(highs) >= 10 else max(highs[:-5])
        recent_low = min(lows[-5:])
        earlier_low = min(lows[-10:-5]) if len(lows) >= 10 else min(lows[:-5])

        if recent_high > earlier_high and recent_low > earlier_low:
            return "BULLISH"
        elif recent_high < earlier_high and recent_low < earlier_low:
            return "BEARISH"
        else:
            return "RANGING"
    except Exception:
        return None

def calc_trade(signal, entry, prev_high, prev_low):
    if signal == "BUY":
        sl = prev_low
        tp = entry + ((entry - sl) * 2)
        return {"entry": entry, "sl": sl, "tp": tp}
    elif signal == "SELL":
        sl = prev_high
        tp = entry - ((sl - entry) * 2)
        return {"entry": entry, "sl": sl, "tp": tp}
    return None

def fetch_one(symbol):
    try:
        url = f"https://api.twelvedata.com/time_series?symbol={symbol}&interval=1h&outputsize=10&apikey={TWELVEDATA_API_KEY}"
        response = requests.get(url, timeout=15)
        data = response.json()

        if "values" not in data:
            return {"symbol": symbol, "error": data.get("message", "no data")}

        candles = list(reversed(data["values"]))
        clean = []
        for c in candles:
            try:
                clean.append((float(c["high"]), float(c["low"]), float(c["close"])))
            except (KeyError, ValueError):
                continue

        if len(clean) < 3:
            return {"symbol": symbol, "error": "not enough data"}

        prev_high = clean[-2][0]
        prev_low = clean[-2][1]
        last_close = clean[-1][2]

        htf = get_htf_bias(symbol)

        if last_close > prev_high:
            raw_signal, color = "BUY", "#00e676"
            note = "Bullish BOS"
        elif last_close < prev_low:
            raw_signal, color = "SELL", "#ff5252"
            note = "Bearish BOS"
        else:
            raw_signal, color = "HOLD", "#9e9e9e"
            note = "No break"

        if raw_signal == "BUY" and htf == "BEARISH":
            signal = "HOLD"
            color = "#9e9e9e"
            note = "BUY blocked (4H bearish)"
        elif raw_signal == "SELL" and htf == "BULLISH":
            signal = "HOLD"
            color = "#9e9e9e"
            note = "SELL blocked (4H bullish)"
        elif raw_signal in ("BUY", "SELL") and not in_killzone():
            signal = "HOLD"
            color = "#9e9e9e"
            note = "Signal ignored (outside killzone)"
        else:
            signal = raw_signal

        trade = calc_trade(signal, last_close, prev_high, prev_low) if signal in ("BUY", "SELL") else None

        return {
            "symbol": symbol,
            "price": last_close,
            "prev_high": prev_high,
            "prev_low": prev_low,
            "signal": signal,
            "color": color,
            "note": note,
            "htf": htf or "UNKNOWN",
            "trade": trade,
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

    fresh = {
        "results": results,
        "updated": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
        "killzone": killzone_name(),
        "in_killzone": in_killzone(),
    }
    _cache["data"] = fresh
    _cache["last_fetch"] = now
    return fresh

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

PAGE = """
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SMC Signal Bot</title>
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
            align-items: center; margin-bottom: 12px;
            padding: 0 4px;
        }
        .title {
            font-size: 14px; font-weight: 600; letter-spacing: 2px;
            color: #6b7280; text-transform: uppercase;
        }
        .live {
            display: flex; align-items: center; gap: 6px;
            font-size: 11px; color: #00e676; font-weight: 600;
        }
        .dot {
            width: 8px; height: 8px; background: #00e676;
            border-radius: 50%; animation: pulse 1.5s infinite;
        }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
        .kz-bar {
            background: #131824; border: 1px solid #1f2633;
            border-radius: 12px; padding: 10px 14px;
            margin-bottom: 16px; font-size: 11px;
            display: flex; justify-content: space-between;
            align-items: center;
        }
        .kz-open { color: #00e676; font-weight: 700; }
        .kz-closed { color: #6b7280; font-weight: 700; }
        .card {
            background: #131824; border: 1px solid #1f2633;
            border-radius: 16px; padding: 18px 20px;
            margin-bottom: 12px;
        }
        .row1 {
            display: flex; justify-content: space-between;
            align-items: flex-start; margin-bottom: 14px;
        }
        .pair { font-size: 16px; font-weight: 700; color: #fff; margin-bottom: 2px; }
        .name { font-size: 11px; color: #6b7280; margin-bottom: 4px; }
        .htf {
            font-size: 10px; font-weight: 600;
            letter-spacing: 1px; margin-bottom: 8px;
        }
        .htf-bull { color: #00e676; }
        .htf-bear { color: #ff5252; }
        .htf-range { color: #6b7280; }
        .price { font-size: 20px; font-weight: 700; color: #e0e0e0; letter-spacing: -0.5px; }
        .signal-badge {
            display: inline-block;
            font-size: 13px; font-weight: 800; letter-spacing: 1px;
            padding: 6px 12px; border-radius: 8px;
            border: 1.5px solid;
        }
        .trade-grid {
            display: grid; grid-template-columns: 1fr 1fr 1fr;
            gap: 8px; margin-top: 12px;
            padding-top: 12px; border-top: 1px solid #1f2633;
        }
        .trade-cell {
            text-align: center; padding: 8px 4px;
            background: #0a0e17; border-radius: 8px;
        }
        .trade-label {
            font-size: 9px; color: #6b7280;
            letter-spacing: 1px; margin-bottom: 4px;
        }
        .trade-value {
            font-size: 12px; font-weight: 700; color: #e0e0e0;
        }
        .err { color: #ff5252; font-size: 12px; }
        .footer {
            text-align: center; font-size: 10px; color: #4b5563;
            margin-top: 20px; padding: 0 4px;
        }
        .loading {
            text-align: center; color: #6b7280;
            padding: 40px 20px; font-size: 13px;
        }
    </style>
</head>
<body>
    <div class="wrap">
        <div class="header">
            <div class="title">SMC Signal Bot · 1H</div>
            <div class="live"><span class="dot"></span>LIVE</div>
        </div>
        <div class="kz-bar">
            <span>Killzone:</span>
            <span id="kz-status" class="kz-closed">Loading...</span>
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

                const kzEl = document.getElementById('kz-status');
                if (d.in_killzone) {
                    kzEl.textContent = d.killzone + ' OPEN';
                    kzEl.className = 'kz-open';
                } else {
                    kzEl.textContent = 'Closed (no signals)';
                    kzEl.className = 'kz-closed';
                }

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
