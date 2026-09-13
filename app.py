from flask import Flask
import random

app = Flask(__name__)

@app.route("/")
def home():
    signal = random.choice(["BUY", "SELL", "HOLD"])
    color = "green" if signal == "BUY" else "red" if signal == "SELL" else "gray"
    return f"""
    <h1>SMC Signal Bot</h1>
    <p>Current Signal:</p>
    <h2 style="color:{color};">{signal}</h2>
    <p><em>This is fake. Real data comes next.</em></p>
    """

if __name__ == "__main__":
    app.run()
