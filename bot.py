
import asyncio
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

import requests
import cloudinary
import cloudinary.uploader

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME", "").strip()
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY", "").strip()
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET", "").strip()

META_API_VERSION = os.getenv("META_API_VERSION", "v26.0").strip()
META_PAGE_ID = os.getenv("META_PAGE_ID", "").strip()
META_PAGE_ACCESS_TOKEN = os.getenv("META_PAGE_ACCESS_TOKEN", "").strip()
META_IG_USER_ID = os.getenv("META_IG_USER_ID", "").strip()

YOUTUBE_CLIENT_SECRET_FILE = os.getenv(
    "YOUTUBE_CLIENT_SECRET_FILE", "client_secret.json"
).strip()
YOUTUBE_TOKEN_FILE = os.getenv("YOUTUBE_TOKEN_FILE", "youtube_token.json").strip()

# YouTube OAuth scope for uploads.
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

# Temporary files are stored here.
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Per-chat state. This simple bot is intended for personal/admin use.
USER_STATE = {}


# ============================================================
# VALIDATION
# ============================================================

def missing_config() -> list[str]:
    required = {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "CLOUDINARY_CLOUD_NAME": CLOUDINARY_CLOUD_NAME,
        "CLOUDINARY_API_KEY": CLOUDINARY_API_KEY,
        "CLOUDINARY_API_SECRET": CLOUDINARY_API_SECRET,
        "META_PAGE_ID": META_PAGE_ID,
        "META_PAGE_ACCESS_TOKEN": META_PAGE_ACCESS_TOKEN,
    }
    return [name for name, value in required.items() if not value]


def configure_cloudinary() -> None:
    cloudinary.config(
        cloud_name=CLOUDINARY_CLOUD_NAME,
        api_key=CLOUDINARY_API_KEY,
        api_secret=CLOUDINARY_API_SECRET,
        secure=True,
    )


# ============================================================
# HELPERS
# ============================================================

def safe_title(text: str) -> str:
    first_line = text.strip().splitlines()[0] if text.strip() else "New Short"
    first_line = re.sub(r"\s+", " ", first_line).strip()
    return first_line[:100] or "New Short"


def split_caption(text: str) -> tuple[str, str]:
    """
    First line = YouTube title.
    Entire text = description/caption.
    """
    title = safe_title(text)
    description = text.strip()
    return title, description


def graph_url(path: str) -> str:
    return f"https://graph.facebook.com/{META_API_VERSION}/{path.lstrip('/')}"


def raise_for_meta(response: requests.Response, where: str) -> dict:
    try:
        data = response.json()
    except Exception:
        response.raise_for_status()
        raise RuntimeError(f"{where}: unexpected response")

    if response.status_code >= 400 or "error" in data:
        raise RuntimeError(f"{where}: {data}")
    return data


# ============================================================
# CLOUDINARY
# ============================================================

def upload_to_cloudinary(local_path: str) -> str:
    result = cloudinary.uploader.upload(
        local_path,
        resource_type="video",
        folder="telegram-social-poster",
    )
    public_url = result.get("secure_url") or result.get("url")
    if not public_url:
        raise RuntimeError(f"Cloudinary did not return a public URL: {result}")
    return public_url


# ============================================================
# YOUTUBE
# ============================================================

def get_youtube_service():
    creds: Optional[Credentials] = None
    token_path = Path(YOUTUBE_TOKEN_FILE)

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(
            str(token_path),
            YOUTUBE_SCOPES,
        )

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")

    if not creds or not creds.valid:
        secret_path = Path(YOUTUBE_CLIENT_SECRET_FILE)
        if not secret_path.exists():
            raise RuntimeError(
                f"Missing {YOUTUBE_CLIENT_SECRET_FILE}. "
                "Download the Google OAuth Desktop App JSON and put it beside bot.py."
            )

        flow = InstalledAppFlow.from_client_secrets_file(
            str(secret_path),
            YOUTUBE_SCOPES,
        )
        creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return build("youtube", "v3", credentials=creds)


def upload_youtube(local_path: str, title: str, description: str) -> str:
    youtube = get_youtube_service()

    # Keep the first line as title, rest/full text as description.
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": "22",
        },
        "status": {
            # Google may force uploads from unverified projects to private.
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(
        local_path,
        mimetype="video/mp4",
        resumable=True,
        chunksize=8 * 1024 * 1024,
    )

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        _, response = request.next_chunk()

    video_id = response.get("id")
    if not video_id:
        raise RuntimeError(f"YouTube upload returned no video ID: {response}")

    return f"https://www.youtube.com/watch?v={video_id}"


# ============================================================
# INSTAGRAM REEL
# ============================================================

def publish_instagram_reel(
    public_video_url: str,
    caption: str,
) -> str:
    if not META_IG_USER_ID:
        raise RuntimeError(
            "META_IG_USER_ID is empty. Get the Instagram Business Account ID "
            "connected to your Facebook Page and add it to .env."
        )

    create_url = graph_url(f"{META_IG_USER_ID}/media")
    create_params = {
        "media_type": "REELS",
        "video_url": public_video_url,
        "caption": caption,
        "share_to_feed": "true",
        "access_token": META_PAGE_ACCESS_TOKEN,
    }

    response = requests.post(create_url, params=create_params, timeout=60)
    data = raise_for_meta(response, "Instagram create container")
    container_id = data.get("id")
    if not container_id:
        raise RuntimeError(f"Instagram did not return a container ID: {data}")

    # Meta recommends checking once per minute, for no more than 5 minutes.
    status_url = graph_url(container_id)

    status = None
    for _ in range(5):
        time_left = 60
        while time_left > 0:
            # Sleep in small pieces so Ctrl+C / the bot loop stays responsive.
            await_sleep = min(5, time_left)
            import time
            time.sleep(await_sleep)
            time_left -= await_sleep

        status_response = requests.get(
            status_url,
            params={
                "fields": "status_code,status",
                "access_token": META_PAGE_ACCESS_TOKEN,
            },
            timeout=30,
        )
        status_data = raise_for_meta(
            status_response,
            "Instagram container status",
        )
        status = status_data.get("status_code")
        if status == "FINISHED":
            break
        if status in {"ERROR", "EXPIRED"}:
            raise RuntimeError(
                f"Instagram container failed: {status_data}"
            )

    if status != "FINISHED":
        raise RuntimeError(
            f"Instagram container did not finish in time. Last status: {status}"
        )

    publish_url = graph_url(f"{META_IG_USER_ID}/media_publish")
    publish_response = requests.post(
        publish_url,
        params={
            "creation_id": container_id,
            "access_token": META_PAGE_ACCESS_TOKEN,
        },
        timeout=60,
    )
    publish_data = raise_for_meta(
        publish_response,
        "Instagram publish",
    )

    media_id = publish_data.get("id")
    if not media_id:
        raise RuntimeError(f"Instagram returned no media ID: {publish_data}")

    return media_id


# ============================================================
# FACEBOOK PAGE REEL
# ============================================================

def publish_facebook_reel(
    public_video_url: str,
    title: str,
    description: str,
) -> str:
    # 1) Start upload session
    start_url = graph_url(f"{META_PAGE_ID}/video_reels")
    start_response = requests.post(
        start_url,
        params={
            "upload_phase": "start",
            "access_token": META_PAGE_ACCESS_TOKEN,
        },
        timeout=60,
    )
    start_data = raise_for_meta(
        start_response,
        "Facebook Reel start",
    )

    video_id = start_data.get("video_id")
    upload_url = start_data.get("upload_url")
    if not video_id or not upload_url:
        raise RuntimeError(f"Facebook did not return upload details: {start_data}")

    # 2) Hosted upload from Cloudinary URL
    upload_response = requests.post(
        upload_url,
        headers={
            "Authorization": f"OAuth {META_PAGE_ACCESS_TOKEN}",
            "file_url": public_video_url,
        },
        timeout=120,
    )
    upload_data = raise_for_meta(
        upload_response,
        "Facebook Reel upload",
    )

    if upload_data.get("success") is False:
        raise RuntimeError(
            f"Facebook hosted upload was not successful: {upload_data}"
        )

    # 3) Publish the Reel
    finish_response = requests.post(
        start_url,
        params={
            "upload_phase": "finish",
            "video_id": video_id,
            "video_state": "PUBLISHED",
            "title": title,
            "description": description,
            "access_token": META_PAGE_ACCESS_TOKEN,
        },
        timeout=60,
    )
    finish_data = raise_for_meta(
        finish_response,
        "Facebook Reel publish",
    )

    if finish_data.get("success") is False:
        raise RuntimeError(
            f"Facebook Reel publish failed: {finish_data}"
        )

    return video_id


# ============================================================
# TELEGRAM HANDLERS
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton(
                "🎬 Upload Reel",
                callback_data="upload_reel",
            )
        ]
    ]

    await update.message.reply_text(
        "🚀 Social Auto Poster\n\n"
        "Telegram → YouTube + Instagram + Facebook Page\n\n"
        "Video upload karne ke liye button dabao.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    missing = missing_config()
    if missing:
        await update.message.reply_text(
            "⚠️ Configuration incomplete:\n\n"
            + "\n".join(f"• {x}" for x in missing)
        )
        return

    await update.message.reply_text(
        "✅ Basic configuration is present.\n"
        "YouTube OAuth will be requested automatically on the first YouTube post."
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id

    if query.data == "upload_reel":
        USER_STATE[chat_id] = {
            "waiting_video": True,
            "video_file_id": None,
            "video_name": None,
            "caption": None,
        }

        await query.message.reply_text(
            "📹 Ab apni Reel/Short video bhejo.\n\n"
            "20 MB se chhoti video use karo."
        )

    elif query.data == "post_all":
        state = USER_STATE.get(chat_id)
        if not state or not state.get("video_file_id"):
            await query.message.reply_text(
                "❌ Video nahi mila. /start se dobara shuru karo."
            )
            return

        if not state.get("caption"):
            await query.message.reply_text(
                "❌ Caption/title missing hai."
            )
            return

        # Lock this request so double-clicking does not create duplicates.
        if state.get("posting"):
            await query.message.reply_text("⏳ Posting already in progress...")
            return

        state["posting"] = True
        await query.message.reply_text(
            "🚀 Posting started...\n\n"
            "▶️ YouTube: ⏳\n"
            "📸 Instagram: ⏳\n"
            "📘 Facebook: ⏳"
        )

        try:
            result = await asyncio.to_thread(
                process_and_post_all,
                state,
            )

            await query.message.reply_text(
                "✅ POST ALL COMPLETE!\n\n"
                f"▶️ YouTube: {result['youtube']}\n"
                f"📸 Instagram: {result['instagram']}\n"
                f"📘 Facebook Reel: {result['facebook']}"
            )

        except Exception as exc:
            await query.message.reply_text(
                "❌ Posting failed.\n\n"
                f"{type(exc).__name__}: {exc}\n\n"
                "Check CMD for the full error."
            )
        finally:
            USER_STATE.pop(chat_id, None)


async def media_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = USER_STATE.get(chat_id)

    if not state or not state.get("waiting_video"):
        await update.message.reply_text(
            "Pehle /start dabao aur 🎬 Upload Reel select karo."
        )
        return

    video_file_id = None
    video_name = None

    if update.message.video:
        video_file_id = update.message.video.file_id
        video_name = "reel.mp4"

    elif update.message.document:
        mime = update.message.document.mime_type or ""
        if not mime.startswith("video/"):
            await update.message.reply_text(
                "❌ Ye video file nahi lag rahi. MP4/MOV video bhejo."
            )
            return

        video_file_id = update.message.document.file_id
        video_name = update.message.document.file_name or "reel.mp4"

    if not video_file_id:
        await update.message.reply_text("❌ Video receive nahi hui.")
        return

    state["video_file_id"] = video_file_id
    state["video_name"] = video_name
    state["waiting_video"] = False
    state["waiting_caption"] = True

    await update.message.reply_text(
        "✅ Video received.\n\n"
        "Ab ek message me caption/title bhejo.\n\n"
        "Example:\n"
        "Giant Ice Cube Turns Into Lava 🧊🔥\n\n"
        "#shorts #reels #viral"
    )


async def caption_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = USER_STATE.get(chat_id)

    if not state or not state.get("waiting_caption"):
        return

    caption = (update.message.text or "").strip()

    if not caption:
        await update.message.reply_text("❌ Caption empty hai.")
        return

    state["caption"] = caption
    state["waiting_caption"] = False

    keyboard = [
        [InlineKeyboardButton("🚀 POST ALL", callback_data="post_all")]
    ]

    title, _ = split_caption(caption)

    await update.message.reply_text(
        "✅ Details ready.\n\n"
        f"▶️ YouTube title:\n{title}\n\n"
        f"📱 Caption:\n{caption}\n\n"
        "Sab check karke **POST ALL** dabao.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


def process_and_post_all(state: dict) -> dict:
    missing = missing_config()
    if missing:
        raise RuntimeError(
            "Missing .env settings: " + ", ".join(missing)
        )

    configure_cloudinary()

    # Download Telegram file
    temp_suffix = Path(state.get("video_name") or "reel.mp4").suffix
    if not temp_suffix:
        temp_suffix = ".mp4"

    temp_file = DOWNLOAD_DIR / f"telegram_upload{temp_suffix}"

    # We only have the file_id here. Create a normal Bot API call.
    import time
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile"
    response = requests.get(
        api_url,
        params={"file_id": state["video_file_id"]},
        timeout=60,
    )
    data = response.json()

    if response.status_code >= 400 or not data.get("ok"):
        raise RuntimeError(f"Telegram getFile failed: {data}")

    file_path = data["result"]["file_path"]

    download_url = (
        f"https://api.telegram.org/file/bot"
        f"{TELEGRAM_BOT_TOKEN}/{file_path}"
    )

    with requests.get(download_url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(temp_file, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)

    try:
        # Public URL needed by Meta.
        public_url = upload_to_cloudinary(str(temp_file))

        title, description = split_caption(state["caption"])

        youtube_url = upload_youtube(
            str(temp_file),
            title,
            description,
        )

        instagram_id = publish_instagram_reel(
            public_url,
            description,
        )

        facebook_id = publish_facebook_reel(
            public_url,
            title,
            description,
        )

        return {
            "youtube": youtube_url,
            "instagram": f"Media ID {instagram_id}",
            "facebook": f"Video ID {facebook_id}",
        }

    finally:
        try:
            temp_file.unlink(missing_ok=True)
        except Exception:
            pass


# ============================================================
# MAIN
# ============================================================

def main():
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN is missing in .env")
        return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(
        CallbackQueryHandler(callback_handler)
    )

    application.add_handler(
        MessageHandler(
            filters.VIDEO | filters.Document.VIDEO,
            media_handler,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            caption_handler,
        )
    )

    print("========================================")
    print("Social Auto Poster started")
    print("Telegram -> YouTube + Instagram + Facebook")
    print("Press Ctrl+C to stop.")
    print("========================================")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
