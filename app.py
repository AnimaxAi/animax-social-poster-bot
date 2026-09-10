import base64
import hashlib
import html
import os
import secrets
import sqlite3
import time
import threading
from pathlib import Path
from urllib.parse import urlencode

import cloudinary
import cloudinary.uploader
import requests
from dotenv import load_dotenv
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
BUFFER_CLIENT_ID = os.getenv("BUFFER_CLIENT_ID", "").strip()
BUFFER_CLIENT_SECRET = os.getenv("BUFFER_CLIENT_SECRET", "").strip()
BUFFER_REDIRECT_URI = os.getenv("BUFFER_REDIRECT_URI", "").strip()

CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME", "").strip()
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY", "").strip()
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET", "").strip()

PORT = int(os.getenv("PORT", "10000"))
DB_PATH = Path(os.getenv("DB_PATH", "bot.db"))
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

BUFFER_AUTH_URL = "https://auth.buffer.com/auth"
BUFFER_TOKEN_URL = "https://auth.buffer.com/token"
BUFFER_API_URL = "https://api.buffer.com"
BUFFER_SCOPES = "account:read posts:read posts:write offline_access"

web = Flask(__name__)
memory = {}
memory_lock = threading.Lock()


def db_init():
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS oauth_states (state TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, verifier TEXT NOT NULL, created_at INTEGER NOT NULL)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS tokens (chat_id INTEGER PRIMARY KEY, access_token TEXT NOT NULL, refresh_token TEXT, expires_at INTEGER, scope TEXT)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS channels (chat_id INTEGER NOT NULL, channel_id TEXT NOT NULL, name TEXT, service TEXT, PRIMARY KEY(chat_id, channel_id))"
        )


def db(sql, params=(), fetch=False):
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(sql, params)
        rows = cur.fetchall() if fetch else None
        con.commit()
        return rows


def get_state(chat_id):
    with memory_lock:
        return memory.setdefault(chat_id, {})


def clear_state(chat_id):
    with memory_lock:
        memory.pop(chat_id, None)


def make_verifier():
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def make_challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def save_tokens(chat_id, access_token, refresh_token, expires_in, scope):
    expires_at = int(time.time()) + int(expires_in) if expires_in else None
    db(
        "INSERT INTO tokens(chat_id,access_token,refresh_token,expires_at,scope) VALUES(?,?,?,?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET access_token=excluded.access_token, refresh_token=excluded.refresh_token, "
        "expires_at=excluded.expires_at, scope=excluded.scope",
        (chat_id, access_token, refresh_token, expires_at, scope),
    )


def get_token_row(chat_id):
    rows = db(
        "SELECT access_token,refresh_token,expires_at,scope FROM tokens WHERE chat_id=?",
        (chat_id,),
        fetch=True,
    )
    return rows[0] if rows else None


def refresh_buffer_token(chat_id, refresh_token):
    response = requests.post(
        BUFFER_TOKEN_URL,
        data={
            "client_id": BUFFER_CLIENT_ID,
            "client_secret": BUFFER_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    save_tokens(chat_id, data["access_token"], data.get("refresh_token"), data.get("expires_in"), data.get("scope"))
    return data["access_token"]


def get_access_token(chat_id):
    row = get_token_row(chat_id)
    if not row:
        raise RuntimeError("Buffer is not connected. Use /connect first.")
    access_token, refresh_token, expires_at, _scope = row
    if expires_at and expires_at <= int(time.time()) + 60:
        if not refresh_token:
            raise RuntimeError("Buffer token expired. Use /connect again.")
        return refresh_buffer_token(chat_id, refresh_token)
    return access_token


def buffer_graphql(access_token, query):
    response = requests.post(
        BUFFER_API_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json={"query": query},
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("errors"):
        raise RuntimeError(str(data["errors"]))
    return data


def get_organizations(access_token):
    return buffer_graphql(access_token, "query { organizations { id name } }")["data"]["organizations"]


def get_channels(access_token, organization_id):
    query = (
        "query { channels(input: { organizationId: \""
        + organization_id
        + "\" }) { id name displayName service } }"
    )
    return buffer_graphql(access_token, query)["data"]["channels"]


def sync_channels(chat_id):
    token = get_access_token(chat_id)
    found = []
    for organization in get_organizations(token):
        found.extend(get_channels(token, organization["id"]))

    db("DELETE FROM channels WHERE chat_id=?", (chat_id,))
    for channel in found:
        db(
            "INSERT OR REPLACE INTO channels(chat_id,channel_id,name,service) VALUES(?,?,?,?)",
            (
                chat_id,
                channel["id"],
                channel.get("displayName") or channel.get("name") or "",
                channel.get("service") or "",
            ),
        )
    return found


def create_video_post(access_token, channel_id, text, public_url):
    text = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    public_url = public_url.replace("\\", "\\\\").replace('"', '\\"')
    mutation = (
        "mutation { createPost(input: { "
        f'text: "{text}" '
        f'channelId: "{channel_id}" '
        "schedulingType: automatic mode: shareNow "
        f'assets: [{{ video: {{ url: "{public_url}" }} }}] '
        "}) { ... on PostActionSuccess { post { id text dueAt } } ... on MutationError { message } } }"
    )
    result = buffer_graphql(access_token, mutation)["data"]["createPost"]
    if "message" in result:
        raise RuntimeError(result["message"])
    return result["post"]


def cloudinary_url(local_path):
    cloudinary.config(
        cloud_name=CLOUDINARY_CLOUD_NAME,
        api_key=CLOUDINARY_API_KEY,
        api_secret=CLOUDINARY_API_SECRET,
        secure=True,
    )
    result = cloudinary.uploader.upload(
        local_path,
        resource_type="video",
        folder="animax-social-poster",
    )
    url = result.get("secure_url") or result.get("url")
    if not url:
        raise RuntimeError("Cloudinary did not return a public URL.")
    return url


async def send_connect_link(chat_id, bot):
    if not BUFFER_CLIENT_ID or not BUFFER_REDIRECT_URI:
        await bot.send_message(chat_id=chat_id, text="❌ BUFFER_CLIENT_ID / BUFFER_REDIRECT_URI missing.")
        return

    code_verifier = make_verifier()
    code_challenge = make_challenge(code_verifier)
    state_value = secrets.token_urlsafe(24)
    db(
        "INSERT INTO oauth_states(state,chat_id,verifier,created_at) VALUES(?,?,?,?)",
        (state_value, chat_id, code_verifier, int(time.time())),
    )

    params = {
        "client_id": BUFFER_CLIENT_ID,
        "redirect_uri": BUFFER_REDIRECT_URI,
        "response_type": "code",
        "scope": BUFFER_SCOPES,
        "state": state_value,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": "consent",
    }
    url = f"{BUFFER_AUTH_URL}?{urlencode(params)}"
    await bot.send_message(
        chat_id=chat_id,
        text="🔗 Buffer connect karne ke liye button dabao.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Connect Buffer", url=url)]]),
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 Animax Universal Social Poster\n\n"
        "/connect — Buffer connect karo\n"
        "/post — video bhejo aur POST ALL karo"
    )


async def connect_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_connect_link(update.effective_chat.id, context.bot)


async def post_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = get_state(update.effective_chat.id)
    state.clear()
    state["waiting_video"] = True
    await update.message.reply_text("📹 Video bhejo.")


async def media_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    if not state.get("waiting_video"):
        return

    telegram_file = None
    file_name = "video.mp4"
    if update.message.video:
        telegram_file = await update.message.video.get_file()
    elif update.message.document and (update.message.document.mime_type or "").startswith("video/"):
        telegram_file = await update.message.document.get_file()
        file_name = update.message.document.file_name or file_name
    if not telegram_file:
        return

    suffix = Path(file_name).suffix or ".mp4"
    local_path = DOWNLOAD_DIR / f"{chat_id}_{int(time.time())}{suffix}"
    await telegram_file.download_to_drive(str(local_path))
    state["video_path"] = str(local_path)
    state["waiting_video"] = False
    state["waiting_caption"] = True
    await update.message.reply_text("✅ Video received.\n\nAb caption/title bhejo.")


async def caption_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    if not state.get("waiting_caption"):
        return

    text = (update.message.text or "").strip()
    if not text:
        return
    state["caption"] = text
    state["waiting_caption"] = False
    await update.message.reply_text(
        f"✅ Ready!\n\n{text}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 POST ALL", callback_data="post_all")]]),
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    if query.data == "connect":
        await send_connect_link(chat_id, context.bot)
        return

    if query.data != "post_all":
        return

    state = get_state(chat_id)
    if not state.get("video_path") or not state.get("caption"):
        await query.message.reply_text("❌ Video/caption missing.")
        return

    try:
        await query.message.reply_text("⏳ Uploading video and publishing...")
        public_url = await __import__("asyncio").to_thread(cloudinary_url, state["video_path"])
        token = await __import__("asyncio").to_thread(get_access_token, chat_id)
        channels = await __import__("asyncio").to_thread(sync_channels, chat_id)

        success = []
        failed = []
        for channel in channels:
            name = channel.get("displayName") or channel.get("name") or channel["id"]
            service = channel.get("service") or "unknown"
            try:
                await __import__("asyncio").to_thread(
                    create_video_post,
                    token,
                    channel["id"],
                    state["caption"],
                    public_url,
                )
                success.append(f"✅ {service}: {name}")
            except Exception as exc:
                failed.append(f"❌ {service}: {name} — {exc}")

        message = "📊 POST ALL RESULT\n\n" + "\n".join(success)
        if failed:
            message += "\n\n" + "\n".join(failed)
        if not success:
            message += "\n\nNo channel succeeded."
        await query.message.reply_text(message[:3900])

    except Exception as exc:
        await query.message.reply_text(f"❌ Posting failed:\n\n{type(exc).__name__}: {exc}")
    finally:
        try:
            Path(state.get("video_path", "")).unlink(missing_ok=True)
        except Exception:
            pass
        clear_state(chat_id)


@web.get("/")
def home():
    return "Animax Universal Social Poster is running."


@web.get("/health")
def health():
    return {"status": "ok"}


@web.get("/buffer/callback")
def buffer_callback():
    code = request.args.get("code")
    state_value = request.args.get("state")
    error = request.args.get("error")

    if error:
        return f"<h2>Buffer connection cancelled</h2><p>{html.escape(error)}</p>", 400

    rows = db("SELECT chat_id,verifier FROM oauth_states WHERE state=?", (state_value,), fetch=True)
    if not rows or not code:
        return "Invalid or expired OAuth state.", 403

    chat_id, code_verifier = rows[0]
    db("DELETE FROM oauth_states WHERE state=?", (state_value,))

    response = requests.post(
        BUFFER_TOKEN_URL,
        data={
            "client_id": BUFFER_CLIENT_ID,
            "client_secret": BUFFER_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": BUFFER_REDIRECT_URI,
            "code_verifier": code_verifier,
        },
        timeout=60,
    )

    if response.status_code >= 400:
        return f"<h2>Token exchange failed</h2><pre>{html.escape(response.text)}</pre>", 400

    data = response.json()
    save_tokens(chat_id, data["access_token"], data.get("refresh_token"), data.get("expires_in"), data.get("scope"))
    try:
        count = len(sync_channels(chat_id))
    except Exception:
        count = 0
    return f"<h2>✅ Buffer connected!</h2><p>Detected {count} channel(s). Return to Telegram.</p>"


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
    db_init()

    bot = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    bot.add_handler(CommandHandler("start", start))
    bot.add_handler(CommandHandler("connect", connect_command))
    bot.add_handler(CommandHandler("post", post_command))
    bot.add_handler(CallbackQueryHandler(callback_handler))
    bot.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, media_handler))
    bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, caption_handler))

    threading.Thread(
        target=lambda: web.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False),
        daemon=True,
    ).start()

    print("Animax Universal Buffer Telegram Bot running")
    print(f"HTTP port: {PORT}")
    bot.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
