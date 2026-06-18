import os
import threading
from flask import Flask

app = Flask(__name__)


@app.route("/")
def home():
    return "Zero Bot is Running", 200


@app.route("/health")
def health():
    return {"status": "ok", "bot": "Zero"}, 200


def keep_alive():
    port = int(os.getenv("PORT", 5001))
    thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, use_reloader=False),
        daemon=True,
    )
    thread.start()
