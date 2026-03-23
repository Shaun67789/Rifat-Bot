import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_ENDPOINT = "https://addy-chatgpt-api.vercel.app/?text="
MEMORY_DIR = Path("json")
MAX_HISTORY = 12
MAX_REPLY_CHARS = 420
CLEAR_INTERVAL_SECONDS = 10

SYSTEM_PROMPT = """
You are a smooth Telegram chatbot.

Style:
- Short, natural, and friendly
- Slightly flirty sometimes, but always respectful
- Keep replies brief: 1 to 3 short paragraphs max
- Sound human, not robotic
- Use emojis lightly, not too much

Behavior:
- Reply based on recent chat history
- If the user is serious, answer seriously
- If the user is casual, be chill and playful
- If the user asks a direct question, answer directly
- Ask at most one short follow-up question when needed

Rules:
- No explicit, sexual, creepy, manipulative, or obsessive content
- No long explanations unless the user asks
- Avoid repeating the same phrases
- Keep it concise and engaging
""".strip()

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("telegram-chatbot")


def ensure_memory_dir() -> None:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)


def memory_path(chat_id: int) -> Path:
    ensure_memory_dir()
    path = MEMORY_DIR / f"{chat_id}.json"
    if not path.exists():
        path.write_text("[]", encoding="utf-8")
    return path


def load_history(chat_id: int) -> List[Dict[str, Any]]:
    path = memory_path(chat_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as exc:
        logger.warning("Failed to load history for %s: %s", chat_id, exc)
        return []


def save_history(chat_id: int, history: List[Dict[str, Any]]) -> None:
    path = memory_path(chat_id)
    try:
        compact = history[-MAX_HISTORY:]
        path.write_text(
            json.dumps(compact, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("Failed to save history for %s: %s", chat_id, exc)


def add_history(chat_id: int, role: str, text: str) -> None:
    history = load_history(chat_id)
    history.append(
        {
            "role": role,
            "text": text,
            "time": time.time(),
        }
    )
    save_history(chat_id, history)


def clear_all_memory_files() -> None:
    ensure_memory_dir()
    for file_path in MEMORY_DIR.glob("*.json"):
        try:
            file_path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Could not delete %s: %s", file_path, exc)


def build_prompt(history: List[Dict[str, Any]], user_text: str) -> str:
    lines = [SYSTEM_PROMPT, "", "Conversation history:"]
    if history:
        for item in history[-MAX_HISTORY:]:
            role = item.get("role", "user")
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            label = "User" if role == "user" else "Assistant"
            lines.append(f"{label}: {text}")
    else:
        lines.append("No previous history.")

    lines.extend(
        [
            "",
            f"User: {user_text}",
            "Assistant:",
        ]
    )
    return "\n".join(lines)


def recursive_find_text(obj: Any) -> Optional[str]:
    if isinstance(obj, str):
        stripped = obj.strip()
        return stripped if stripped else None

    if isinstance(obj, dict):
        priority_keys = (
            "response",
            "result",
            "message",
            "text",
            "answer",
            "reply",
            "output",
            "content",
            "data",
            "choices",
        )

        for key in priority_keys:
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


def clean_reply(text: str) -> str:
    text = (text or "").strip()

    if not text:
        return "Hmm, say that a little differently 😌"

    # Remove obvious JSON-looking wrappers if they sneak through
    if text.startswith("{") and text.endswith("}"):
        try:
            parsed = json.loads(text)
            extracted = recursive_find_text(parsed)
            if extracted:
                text = extracted
        except Exception:
            pass

    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1].strip()

    if len(text) > MAX_REPLY_CHARS:
        cut = text[:MAX_REPLY_CHARS]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        text = cut + "..."

    return text


def call_api(prompt: str) -> str:
    """
    Tries hard to avoid leaking raw JSON.
    If the endpoint returns a structured payload, we search for usable text.
    If it returns nothing useful, we fall back to a short safe reply.
    """
    try:
        response = requests.get(
            API_ENDPOINT,
            params={"text": prompt},
            timeout=60,
        )
        response.raise_for_status()

        raw_text = (response.text or "").strip()

        # First try JSON parsing
        try:
            data = response.json()
            extracted = recursive_find_text(data)
            if extracted and extracted != prompt:
                return extracted
        except Exception:
            pass

        # If plain text and not a JSON echo, use it
        if raw_text:
            if raw_text.startswith("{") and raw_text.endswith("}"):
                try:
                    parsed = json.loads(raw_text)
                    extracted = recursive_find_text(parsed)
                    if extracted and extracted != prompt:
                        return extracted
                except Exception:
                    pass
            if raw_text != prompt:
                return raw_text

        return "I’m here 😌 say that another way and I’ll answer properly."

    except Exception as exc:
        logger.error("API call failed: %s", exc)
        return "Hmm, I hit a small issue. Try again in a sec 🙂"


def is_mentioned(message_text: str, bot_username: Optional[str]) -> bool:
    if not message_text or not bot_username:
        return False
    return f"@{bot_username.lower()}" in message_text.lower()


async def typing_loop(chat, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await chat.send_action(ChatAction.TYPING)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=3)
        except asyncio.TimeoutError:
            continue


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Hey 😌\n"
        "Talk to me in private, mention me, or reply to one of my messages."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Commands:\n"
        "/start - intro\n"
        "/reset - clear chat memory\n\n"
        "I reply in private chats, when mentioned, or when you reply to my message."
    )


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    path = memory_path(chat_id)
    try:
        path.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning("Reset failed for %s: %s", chat_id, exc)
    await update.message.reply_text("Memory cleared ✨")


async def chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return

    chat = update.effective_chat
    bot_username = getattr(context.bot, "username", None)
    bot_id = context.bot.id

    should_reply = False

    if chat.type == "private":
        should_reply = True
    elif message.reply_to_message and message.reply_to_message.from_user:
        if message.reply_to_message.from_user.id == bot_id:
            should_reply = True
    elif is_mentioned(message.text, bot_username):
        should_reply = True

    if not should_reply:
        return

    user_text = message.text.strip()
    if not user_text:
        return

    chat_id = chat.id
    history = load_history(chat_id)
    prompt = build_prompt(history, user_text)

    stop_event = asyncio.Event()
    typing_task = asyncio.create_task(typing_loop(chat, stop_event))

    add_history(chat_id, "user", user_text)

    try:
        reply = await asyncio.to_thread(call_api, prompt)
        reply = clean_reply(reply)
        add_history(chat_id, "assistant", reply)
        await message.reply_text(reply)
    finally:
        stop_event.set()
        try:
            await typing_task
        except Exception:
            pass


async def auto_clear_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_all_memory_files()
    logger.info("All JSON memory files cleared.")


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Set it as an environment variable.")

    ensure_memory_dir()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_handler))

    if app.job_queue is not None:
        app.job_queue.run_repeating(
            auto_clear_job,
            interval=CLEAR_INTERVAL_SECONDS,
            first=CLEAR_INTERVAL_SECONDS,
        )
    else:
        logger.warning("JobQueue not available; auto-clear disabled.")

    logger.info("Bot is running.")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
