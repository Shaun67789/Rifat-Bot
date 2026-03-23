from flask import Flask, render_template, request, jsonify
import requests

app = Flask(__name__)
API = "https://addy-chatgpt-api.vercel.app/"

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/chat", methods=["POST"])
def chat():
    user = (request.json or {}).get("message", "").strip()
    if not user:
        return jsonify({"reply": "Say something first 😌"})

    try:
        r = requests.get(API, params={"text": user}, timeout=60)
        try:
            data = r.json()
            reply = data.get("response") or data.get("message") or data.get("text") or r.text
        except Exception:
            reply = r.text
    except Exception:
        reply = "Error 😅"

    return jsonify({"reply": reply})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
