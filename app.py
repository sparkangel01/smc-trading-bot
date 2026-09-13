from flask import Flask
import requests
from datetime import datetime

app = Flask(__name__)

@app.route("/")
def home():
    try:
        # Yahoo Finance chart API (free, no key needed)
        url = "https://query1.finance.yahoo.com/v8/finance/chart/XAUUSD=X?interval=1d&range=5d"
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=10)
        data = response.json()

        result = data["chart"]["result"][0]
        quote = result["indicators"]["quote"][0]

        # Build clean lists of (high, low, close) tuples, skipping any None values
        clean_candles = []
        for h, l, c in zip(quote["high"], quote["low"], quote["close"]):
            if h is not None and l is not None and c is not None:
                clean_candles.append((h, l, c))

        if len(clean_candles) < 2:
            return "<h1>Not enough data yet. Try refreshing in a minute.</h1>"

        prev_high = clean_candles[-2][0]
        prev_low = clean_candles[-2][1]
        last_close = clean_candles[-1][2]

        if last_close > prev_high:
            signal = "BUY"
            color = "#00e676"
            glow = "rgba(0,230,118,0.4)"
            note = "Bullish Break of Structure"
        elif last_close < prev_low:
            signal = "SELL"
            color = "#ff5252"
            glow = "rgba(255,82,82,0.4)"
            note = "Bearish Break of Structure"
        else:
            signal = "HOLD"
            color = "#9e9e9e"
            glow = "rgba(158,158,158,0.3)"
            note = "No clear break in structure"

        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>SMC Signal Bot</title>
            <style>
                * {{ box-sizing: border-box; margin: 0; padding: 0; }}
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                    background: #0a0e17;
                    color: #e0e0e0;
                    min-height: 100vh;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    padding: 20px;
                }}
                .card {{
                    background: #131824;
                    border: 1px solid #1f2633;
                    border-radius: 20px;
                    padding: 32px 28px;
                    max-width: 420px;
                    width: 100%;
                    box-shadow: 0 20px 60px rgba(0,0,0,0.5);
                }}
                .header {{
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                    margin-bottom: 24px;
                }}
                .title {{
                    font-size: 14px;
                    font-weight: 600;
                    letter-spacing: 2px;
                    color: #6b7280;
                    text-transform: uppercase;
                }}
                .live {{
                    display: flex;
                    align-items: center;
                    gap: 6px;
                    font-size: 11px;
                    color: #00e676;
                    font-weight: 600;
                }}
                .dot {{
                    width: 8px; height: 8px;
                    background: #00e676;
                    border-radius: 50%;
                    animation: pulse 1.5s infinite;
                }}
                @keyframes pulse {{
                    0%, 100% {{ opacity: 1; }}
                    50% {{ opacity: 0.3; }}
                }}
                .asset {{
                    font-size: 13px;
                    color: #6b7280;
                    margin-bottom: 4px;
                }}
                .asset-name {{
                    font-size: 22px;
                    font-weight: 700;
                    color: #ffffff;
                    margin-bottom: 28px;
                }}
                .price-block {{
                    margin-bottom: 28px;
                }}
                .price-label {{
                    font-size: 12px;
                    color: #6b7280;
                    letter-spacing: 1px;
                    margin-bottom: 6px;
                }}
                .price {{
                    font-size: 38px;
                    font-weight: 800;
                    color: #ffffff;
                    letter-spacing: -1px;
                }}
                .levels {{
                    display: flex;
                    gap: 12px;
                    margin-bottom: 28px;
                }}
                .level {{
                    flex: 1;
                    background: #0a0e17;
                    border: 1px solid #1f2633;
                    border-radius: 12px;
                    padding: 12px 14px;
                }}
                .level-label {{
                    font-size: 10px;
                    color: #6b7280;
                    letter-spacing: 1px;
                    margin-bottom: 4px;
                }}
                .level-value {{
                    font-size: 15px;
                    font-weight: 600;
                    color: #e0e0e0;
                }}
                .signal-block {{
                    background: #0a0e17;
                    border: 2px solid {color};
                    border-radius: 16px;
                    padding: 22px;
                    text-align: center;
                    box-shadow: 0 0 30px {glow};
                }}
                .signal-label {{
                    font-size: 11px;
                    color: #6b7280;
                    letter-spacing: 2px;
                    margin-bottom: 8px;
                }}
                .signal {{
                    font-size: 42px;
                    font-weight: 900;
                    color: {color};
                    letter-spacing: 2px;
                }}
                .signal-note {{
                    font-size: 12px;
                    color: #6b7280;
                    margin-top: 8px;
                }}
                .footer {{
                    margin-top: 24px;
                    text-align: center;
                    font-size: 10px;
                    color: #4b5563;
                }}
            </style>
        </head>
        <body>
            <div class="card">
                <div class="header">
                    <div class="title">SMC Signal Bot</div>
                    <div class="live"><span class="dot"></span>LIVE</div>
                </div>

                <div class="asset">Asset</div>
                <div class="asset-name">Gold · XAU/USD</div>

                <div class="price-block">
                    <div class="price-label">CURRENT PRICE</div>
                    <div class="price">${last_close:,.2f}</div>
                </div>

                <div class="levels">
                    <div class="level">
                        <div class="level-label">PREV HIGH</div>
                        <div class="level-value">${prev_high:,.2f}</div>
                    </div>
                    <div class="level">
                        <div class="level-label">PREV LOW</div>
                        <div class="level-value">${prev_low:,.2f}</div>
                    </div>
                </div>

                <div class="signal-block">
                    <div class="signal-label">SIGNAL</div>
                    <div class="signal">{signal}</div>
                    <div class="signal-note">{note}</div>
                </div>

                <div class="footer">Updated {now}</div>
            </div>
        </body>
        </html>
        """

    except Exception as e:
        return f"""
        <body style="background:#0a0e17;color:#ff5252;font-family:sans-serif;padding:40px;">
        <h1>Error</h1><p>{str(e)}</p>
        </body>
        """

if __name__ == "__main__":
    app.run()
