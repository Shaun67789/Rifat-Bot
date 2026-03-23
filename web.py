from flask import Flask, render_template, request, jsonify
import requests
import json

app = Flask(__name__)
API_ENDPOINT = "https://addy-chatgpt-api.vercel.app/?text="


def recursive_find_text(obj):
    if isinstance(obj, str):
        text = obj.strip()
        return text if text else None

    if isinstance(obj, dict):
        for key in ("response", "result", "message", "text", "answer", "reply", "output", "content", "data"):
            if key in obj:
                found = recursive_find_text(obj[key])
                if found:
                    return found
        for value in obj.values():
            found = recursive_find_text(value)
            if found:
                return found

    if isinstance(obj, list):
        for item in obj:
            found = recursive_find_text(item)
            if found:
                return found

    return None


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/chat", methods=["POST"])
def chat():
    payload = request.get_json(silent=True) or {}
    user = str(payload.get("message", "")).strip()

    if not user:
        return jsonify({"reply": "Say something first 😌"})

    try:
        r = requests.get(API_ENDPOINT, params={"text": user}, timeout=60)
        r.raise_for_status()

        try:
            data = r.json()
            reply = recursive_find_text(data)
            if not reply:
                reply = r.text.strip()
        except Exception:
            reply = r.text.strip()

        if reply.startswith("{") and reply.endswith("}"):
            try:
                parsed = json.loads(reply)
                extracted = recursive_find_text(parsed)
                if extracted:
                    reply = extracted
            except Exception:
                pass

        if not reply:
            reply = "I’m here 😌"

    except Exception:
        reply = "Error 😅"

    return jsonify({"reply": reply})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
