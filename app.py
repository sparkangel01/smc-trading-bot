from flask import Flask
import requests

app = Flask(__name__)

@app.route("/")
def home():
    try:
        # Fetch XAUUSD daily candles from TradingView (free, no key)
        # Symbol format on TradingView for Gold is OANDA:XAUUSD
        url = "https://scanner.tradingview.com/forex/scan"
        payload = {
            "symbols": {"tickers": ["OANDA:XAUUSD"], "query": {"types": []}},
            "columns": ["close", "high", "low", "open", "change"]
        }
        headers = {"Content-Type": "application/json"}
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        data = response.json()

        # Extract the data
        d = data["data"][0]["d"]
        last_close = d[0]  # close
        prev_high = d[1]  # high
        prev_low = d[2]   # low
        last_open = d[3]  # open

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
