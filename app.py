from flask import Flask, jsonify, render_template_string
import requests
import os
import time
from datetime import datetime

app = Flask(__name__)

TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY")

PAIRS = [
    ("XAU/USD", "Gold"),
    ("EUR/USD", "Euro"),
    ("GBP/USD", "Pound"),
    ("USD/JPY", "Yen"),
    ("BTC/USD", "Bitcoin"),
]

# --- Cache: fetch only once per 60 seconds ---
_cache = {
    "data": None,
    "last_fetch": 0,
}

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
        url = f"https://api.twelvedata.com/time_series?symbol={symbol}&interval=1min&outputsize=5&apikey={TWELVEDATA_API_KEY}"
        response = requests.get(url, timeout=10)
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

        if len(clean) < 2:
            return {"symbol": symbol, "error": "not enough data"}

        prev_high = clean[-2][0]
        prev_low = clean[-2][1]
        last_close = clean[-1][2]

        if last_close > prev_high:
            signal, color = "BUY", "#00e676"
            note = "Bullish BOS"
        elif last_close < prev_low:
            signal, color = "SELL", "#ff5252"
            note = "Bearish BOS"
        else:
            signal, color = "HOLD", "#9e9e9e"
            note = "No break"

        trade = calc_trade(signal, last_close, prev_high, prev_low)

        return {
            "symbol": symbol,
            "price": last_close,
            "prev_high": prev_high,
            "prev_low": prev_low,
            "signal": signal,
            "color": color,
            "note": note,
            "trade": trade,
        }
    except Exception as e:
        return {"symbol": symbol, "error": str(e)}

def fetch_all():
    """Only hits the API if cache is older than 60 seconds."""
    now = time.time()
    if _cache["data"] is not None and (now - _cache["last_fetch"]) < 60:
        return _cache["data"]

    results = []
    for symbol, name in PAIRS:
        r = fetch_one(symbol)
        r["name"] = name
        results.append(r)
        time.sleep(8)  # wait 8 seconds between requests to stay under limit

    fresh = {
        "results": results,
        "updated": datetime.utcnow().strftime("%H:%M:%S UTC"),
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
            align-items: center; margin-bottom: 20px;
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
        .name { font-size: 11px; color: #6b7280; margin-bottom: 8px; }
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
            <div class="title">SMC Signal Bot</div>
            <div class="live"><span class="dot"></span>LIVE</div>
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
        setInterval(refresh, 60000);
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run()
