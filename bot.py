import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List

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
API_ENDPOINT = "https://addy-chatgpt-api.vercel.app/"
MEMORY_DIR = Path("json")
MAX_HISTORY = 18
MAX_REPLY_CHARS = 650
CLEAR_INTERVAL_SECONDS = 10

SYSTEM_PROMPT = """
You are a smooth, friendly Telegram chatbot.

STYLE:
- Medium replies only: usually 2 to 5 short paragraphs or a few sentences
- Warm, natural, and slightly playful
- Light flirt is allowed only in a safe, respectful way
- Never explicit, sexual, creepy, manipulative, or obsessive
- Do not overtalk or explain too much
- Use casual human language
- Add emojis sparingly

BEHAVIOR:
- Use the recent chat history to stay consistent
- If the user is serious, respond seriously
- If the user is casual, stay relaxed and friendly
- If the user asks a direct question, answer directly
- Ask at most one short follow-up question when it helps
- Avoid repeating the same phrases

IMPORTANT:
- Keep responses concise and useful
- Sound like a real chat partner
"""

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


def call_api(prompt: str) -> str:
    try:
        response = requests.get(
            API_ENDPOINT,
            params={"text": prompt},
            timeout=60,
        )
        response.raise_for_status()

        try:
            data = response.json()
            if isinstance(data, dict):
                for key in ("response", "result", "message", "text", "answer", "data"):
                    value = data.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
        except Exception:
            pass

        return (response.text or "").strip()

    except Exception as exc:
        logger.error("API call failed: %s", exc)
        return "Hmm, I had a small issue replying. Try again in a moment 🙂"


def trim_reply(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return "Say that again a little differently 😌"

    if len(text) > MAX_REPLY_CHARS:
        cut = text[:MAX_REPLY_CHARS]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        text = cut + "..."
    return text


def is_mentioned(update: Update, bot_username: str | None) -> bool:
    if not bot_username:
        return False

    message = update.effective_message
    if not message or not message.text:
        return False

    text = message.text.lower()
    uname = f"@{bot_username.lower()}"
    if uname in text:
        return True

    for entity in message.entities or []:
        if entity.type == "mention":
            mention_text = message.text[entity.offset : entity.offset + entity.length]
            if mention_text.lower() == uname:
                return True

    return False


async def typing_loop(chat, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await chat.send_action(ChatAction.TYPING)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=3.5)
        except asyncio.TimeoutError:
            continue


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Hey 😌\n"
        "I’m here for smooth chat, replies, and light flirting — kept respectful.\n"
        "Mention me, reply to me, or message me privately."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Use /reset to clear this chat memory.\n"
        "I reply in private chats, when mentioned, or when you reply to my message."
    )


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    path = memory_path(chat_id)
    try:
        path.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning("Reset failed for %s: %s", chat_id, exc)
    await update.message.reply_text("Memory cleared for this chat ✨")


async def chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return

    chat = update.effective_chat
    bot_username = getattr(context.bot, "username", None)

    should_reply = False

    if chat.type == "private":
        should_reply = True
    elif message.reply_to_message and message.reply_to_message.from_user:
        if message.reply_to_message.from_user.id == context.bot.id:
            should_reply = True
    elif is_mentioned(update, bot_username):
        should_reply = True

    if not should_reply:
        return

    chat_id = chat.id
    user_text = message.text.strip()
    if not user_text:
        return

    history = load_history(chat_id)
    prompt = build_prompt(history, user_text)

    stop_event = asyncio.Event()
    typing_task = asyncio.create_task(typing_loop(chat, stop_event))

    add_history(chat_id, "user", user_text)

    try:
        reply = await asyncio.to_thread(call_api, prompt)
        reply = trim_reply(reply)
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
        app.job_queue.run_repeating(auto_clear_job, interval=CLEAR_INTERVAL_SECONDS, first=CLEAR_INTERVAL_SECONDS)
    else:
        logger.warning("JobQueue not available; auto-clear disabled.")

    logger.info("Bot is running.")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
