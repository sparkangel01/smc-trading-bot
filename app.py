from flask import Flask
import requests

app = Flask(__name__)

@app.route("/")
def home():
    try:
        # Yahoo Finance chart API (free, no key needed)
        url = "https://query1.finance.yahoo.com/v8/finance/chart/XAUUSD=X?interval=1d&range=5d"
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=10)
        data = response.json()

        # Get the list of highs, lows, and closes
        result = data["chart"]["result"][0]
        highs = result["indicators"]["quote"][0]["high"]
        lows = result["indicators"]["quote"][0]["low"]
        closes = result["indicators"]["quote"][0]["close"]

        # Clean out any None values (Yahoo sometimes returns these)
        highs = [h for h in highs if h is not None]
        lows = [l for l in lows if l is not None]
        closes = [c for c in closes if c is not None]

        if len(closes) < 2:
            return "<h1>Not enough data yet</h1>"

        prev_high = highs[-2]
        prev_low = lows[-2]
        last_close = closes[-1]

        # SMC Break of Structure logic
        if last_close > prev_high:
            signal = "BUY (Bullish BOS)"
            color = "green"
        elif last_close < prev_low:
            signal = "SELL (Bearish BOS)"
            color = "red"
        else:
            signal = "HOLD (No break)"
            color = "gray"

        return f"""
        <h1>SMC Signal Bot - XAUUSD</h1>
        <p>Asset: <strong>Gold (XAU/USD)</strong></p>
        <p>Current Price: <strong>${last_close:,.2f}</strong></p>
        <p>Previous High: ${prev_high:,.2f}</p>
        <p>Previous Low: ${prev_low:,.2f}</p>
        <hr>
        <p>Signal:</p>
        <h2 style="color:{color};">{signal}</h2>
        <p><em>Real XAUUSD data. No trades placed yet.</em></p>
        """

    except Exception as e:
        return f"<h1>Error</h1><p>{str(e)}</p>"

if __name__ == "__main__":
    app.run()
