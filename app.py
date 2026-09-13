import os
import asyncio
from flask import Flask
from metaapi_cloud_sdk import MetaApi

app = Flask(__name__)

# --- CONFIGURATION ---
# These will come from Render's environment variables
METAAPI_TOKEN = os.getenv("METAAPI_TOKEN")
ACCOUNT_ID = os.getenv("METAAPI_ACCOUNT_ID")

# You can change this to any symbol your broker offers
SYMBOL = "EURUSD"

async def get_smc_signal(api, account_id, symbol):
    """
    A simplified SMC signal using MetaAPI.
    Checks if the latest candle broke the previous structure.
    """
    # Connect to the MetaTrader account
    account = await api.metatrader_account_api.get_account(account_id=account_id)
    connection = account.get_rpc_connection()
    await connection.connect()
    await connection.wait_synchronized()

    # Fetch the last 10 candles for the symbol (M15 timeframe)
    candles = await connection.get_historical_candles(
        symbol=symbol,
        timeframe="15m",
        start_time=None,
        limit=10
    )

    if len(candles) < 3:
        return "Not enough data"

    # SMC Rule: Break of Structure (BOS)
    last_candle = candles[-1]
    prev_candle = candles[-2]

    if last_candle['close'] > prev_candle['high']:
        return "BUY (Bullish Break)"
    elif last_candle['close'] < prev_candle['low']:
        return "SELL (Bearish Break)"
    else:
        return "HOLD (No clear break)"

@app.route("/")
def index():
    """A simple web page showing the bot's status and signal."""
    try:
        api = MetaApi(token=METAAPI_TOKEN)
        signal = asyncio.run(get_smc_signal(api, ACCOUNT_ID, SYMBOL))
        
        return f"""
        <h1>MT5 SMC Trading Bot</h1>
        <p><strong>Broker Account:</strong> Connected</p>
        <p><strong>Symbol:</strong> {SYMBOL}</p>
        <p><strong>Signal:</strong> {signal}</p>
        <p><em>Note: This is a signal display. No trades are placed yet.</em></p>
        """
    except Exception as e:
        return f"""
        <h1>MT5 SMC Trading Bot</h1>
        <p style="color: red;"><strong>Error:</strong> {str(e)}</p>
        <p>Check your Render environment variables.</p>
        """

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
