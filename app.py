import base64
import hashlib
import html
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import cloudinary
import cloudinary.uploader
import requests
from dotenv import load_dotenv
from flask import Flask, request
from pymongo import MongoClient
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# ENV
# ============================================================

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

BUFFER_CLIENT_ID = os.getenv("BUFFER_CLIENT_ID", "").strip()
BUFFER_CLIENT_SECRET = os.getenv("BUFFER_CLIENT_SECRET", "").strip()
BUFFER_REDIRECT_URI = os.getenv("BUFFER_REDIRECT_URI", "").strip()

CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME", "").strip()
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY", "").strip()
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET", "").strip()

MONGO_URI = os.getenv("MONGO_URI", "").strip()

PORT = int(os.getenv("PORT", "10000"))

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Buffer OAuth
BUFFER_AUTH_URL = "https://auth.buffer.com/auth"
BUFFER_TOKEN_URL = "https://auth.buffer.com/token"
BUFFER_API_URL = "https://api.buffer.com"
BUFFER_SCOPES = "account:read posts:read posts:write offline_access"

# Flask web server
web = Flask(__name__)

# Telegram temporary state
memory = {}
memory_lock = threading.Lock()

# ============================================================
# DATABASE (MONGODB)
# ============================================================

if not MONGO_URI:
    raise RuntimeError("MONGO_URI is missing in environment variables.")

mongo_client = MongoClient(MONGO_URI)
db = mongo_client["animax_social_bot"]

# ============================================================
# TELEGRAM MEMORY STATE
# ============================================================

def get_state(chat_id):
    with memory_lock:
        return memory.setdefault(chat_id, {})

def clear_state(chat_id):
    with memory_lock:
        memory.pop(chat_id, None)

# ============================================================
# PKCE
# ============================================================

def make_verifier():
    raw = secrets.token_bytes(32)
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")

def make_challenge(verifier):
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")

# ============================================================
# BUFFER TOKEN STORAGE
# ============================================================

def save_tokens(chat_id, access_token, refresh_token=None, expires_in=None, scope=None):
    expires_at = int(time.time()) + int(expires_in) if expires_in else None
    
    db.tokens.update_one(
        {"chat_id": chat_id},
        {"$set": {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at,
            "scope": scope
        }},
        upsert=True
    )

def get_token_row(chat_id):
    return db.tokens.find_one({"chat_id": chat_id})

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

    if response.status_code >= 400:
        raise RuntimeError(f"Buffer refresh failed [{response.status_code}]: {response.text}")

    data = response.json()

    save_tokens(
        chat_id=chat_id,
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token"),
        expires_in=data.get("expires_in"),
        scope=data.get("scope"),
    )

    return data["access_token"]

def get_access_token(chat_id):
    row = get_token_row(chat_id)

    if not row:
        raise RuntimeError("Buffer is not connected. Send /connect first.")

    access_token = row["access_token"]
    refresh_token = row.get("refresh_token")
    expires_at = row.get("expires_at")

    if expires_at and expires_at <= int(time.time()) + 60:
        if not refresh_token:
            raise RuntimeError("Buffer access token expired. Send /connect again.")
        return refresh_buffer_token(chat_id, refresh_token)

    return access_token

# ============================================================
# BUFFER GRAPHQL
# ============================================================

def buffer_graphql(access_token, query):
    response = requests.post(
        BUFFER_API_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json={"query": query},
        timeout=90,
    )

    if response.status_code >= 400:
        raise RuntimeError(f"Buffer API HTTP {response.status_code}: {response.text}")

    data = response.json()
    if data.get("errors"):
        raise RuntimeError(f"Buffer GraphQL error: {data['errors']}")

    return data

def get_organizations(access_token):
    query = """
    query GetOrganizations {
      account {
        organizations {
          id
          name
        }
      }
    }
    """
    data = buffer_graphql(access_token, query)
    return data["data"]["account"]["organizations"]

def get_channels(access_token, organization_id):
    safe_org_id = organization_id.replace("\\", "\\\\").replace('"', '\\"')
    query = f"""
    query GetChannels {{
      channels(input: {{
        organizationId: "{safe_org_id}"
      }}) {{
        id
        name
        displayName
        service
      }}
    }}
    """
    data = buffer_graphql(access_token, query)
    return data["data"]["channels"]

def sync_channels(chat_id):
    access_token = get_access_token(chat_id)
    found = []
    organizations = get_organizations(access_token)

    for org in organizations:
        found.extend(get_channels(access_token, org["id"]))

    db.channels.delete_many({"chat_id": chat_id})
    
    for channel in found:
        db.channels.insert_one({
            "chat_id": chat_id,
            "channel_id": channel["id"],
            "name": channel.get("displayName") or channel.get("name") or "",
            "service": channel.get("service") or ""
        })

    return found

# ============================================================
# GRAPHQL STRING HELPERS & SERVICE METADATA
# ============================================================

def escape_graphql_string(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\r", "\\r").replace("\n", "\\n")

def youtube_title(text):
    first_line = (text or "").splitlines()[0].strip()
    if not first_line:
        first_line = "New Short"
    return first_line[:100]

def build_service_metadata(service, text):
    service_name = (service or "").strip().lower()
    yt_title = escape_graphql_string(youtube_title(text))

    if "youtube" in service_name:
        return f"""
          metadata: {{
            youtube: {{
              title: "{yt_title}"
              categoryId: "24"
              privacy: public
              madeForKids: false
              notifySubscribers: true
              embeddable: true
            }}
          }}
        """

    if "instagram" in service_name:
        return """
          metadata: {
            instagram: {
              type: reel
              shouldShareToFeed: true
            }
          }
        """

    if "facebook" in service_name:
        return """
          metadata: {
            facebook: {
              type: reel
            }
          }
        """

    return ""

def create_video_post(access_token, channel_id, text, public_url, service):
    safe_text = escape_graphql_string(text)
    safe_url = escape_graphql_string(public_url)
    service_metadata = build_service_metadata(service, text)

    mutation = f"""
    mutation CreateVideoPost {{
      createPost(
        input: {{
          text: "{safe_text}"
          channelId: "{channel_id}"
          schedulingType: automatic
          mode: shareNow
          assets: [
            {{
              video: {{
                url: "{safe_url}"
                metadata: {{
                  thumbnailOffset: 2000
                }}
              }}
            }}
          ]
          {service_metadata}
        }}
      ) {{
        ... on PostActionSuccess {{
          post {{ id text dueAt assets {{ id mimeType source }} }}
        }}
        ... on MutationError {{
          message
        }}
      }}
    }}
    """

    response = buffer_graphql(access_token, mutation)
    create_result = response["data"]["createPost"]

    if "message" in create_result:
        raise RuntimeError(create_result["message"])

    if "post" not in create_result:
        raise RuntimeError(f"Unexpected Buffer response: {create_result}")

    return create_result["post"]

# ============================================================
# CLOUDINARY
# ============================================================

def configure_cloudinary():
    if not (CLOUDINARY_CLOUD_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET):
        raise RuntimeError("Cloudinary environment variables are missing.")

    cloudinary.config(
        cloud_name=CLOUDINARY_CLOUD_NAME,
        api_key=CLOUDINARY_API_KEY,
        api_secret=CLOUDINARY_API_SECRET,
        secure=True,
    )

def upload_video_to_cloudinary(local_path):
    configure_cloudinary()
    
    # Apply strict Instagram Reel transformations (1080x1920, padded, mp4)
    result = cloudinary.uploader.upload(
        local_path,
        resource_type="video",
        folder="animax-social-poster",
        transformation=[
            {"width": 1080, "height": 1920, "crop": "pad", "background": "black"},
            {"quality": "auto", "fetch_format": "mp4"}
        ]
    )
    
    public_url = result.get("secure_url") or result.get("url")
    if not public_url:
        raise RuntimeError("Cloudinary did not return a public URL.")
    return public_url

# ============================================================
# TELEGRAM COMMANDS
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🔗 Connect Buffer", callback_data="connect")],
        [InlineKeyboardButton("📹 Post Video", callback_data="post")],
    ]
    await update.message.reply_text(
        "🚀 Animax Universal Social Poster\n\n"
        "1. Connect your Buffer account.\n"
        "2. Send a video.\n"
        "3. Send caption.\n"
        "4. Press POST ALL.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def connect_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_buffer_connect_link(update.effective_chat.id, context.bot)

async def post_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = get_state(update.effective_chat.id)
    state.clear()
    state["waiting_video"] = True
    await update.message.reply_text("📹 Video bhejo.")

async def send_buffer_connect_link(chat_id, bot):
    if not BUFFER_CLIENT_ID or not BUFFER_CLIENT_SECRET or not BUFFER_REDIRECT_URI:
        await bot.send_message(chat_id=chat_id, text="❌ Buffer credentials missing on server.")
        return

    verifier = make_verifier()
    code_challenge = make_challenge(verifier)
    state_value = secrets.token_urlsafe(32)

    db.oauth_states.insert_one({
        "state": state_value,
        "chat_id": chat_id,
        "verifier": verifier,
        "created_at": int(time.time())
    })

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

    auth_url = f"{BUFFER_AUTH_URL}?{urlencode(params)}"
    keyboard = [[InlineKeyboardButton("🔗 Connect Buffer", url=auth_url)]]

    await bot.send_message(
        chat_id=chat_id,
        text="Buffer connect karne ke liye button dabao.\n\nBuffer login → Allow → Telegram par wapas.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def callback_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    if query.data == "connect":
        await send_buffer_connect_link(chat_id, context.bot)
        return

    if query.data == "post":
        state = get_state(chat_id)
        state.clear()
        state["waiting_video"] = True
        await query.message.reply_text("📹 Video bhejo.")
        return

    if query.data != "post_all":
        return

    state = get_state(chat_id)
    if not state.get("video_path"):
        await query.message.reply_text("❌ Video missing. /post bhejo.")
        return
    if not state.get("caption"):
        await query.message.reply_text("❌ Caption missing.")
        return

    try:
        await query.message.reply_text("⏳ Video upload ho raha hai...\nUske baad Buffer par publish hoga.")
        import asyncio

        public_url = await asyncio.to_thread(upload_video_to_cloudinary, state["video_path"])
        access_token = await asyncio.to_thread(get_access_token, chat_id)
        channels = await asyncio.to_thread(sync_channels, chat_id)

        if not channels:
            raise RuntimeError("No Buffer channels found for this user.")

        success, failed = [], []
        for channel in channels:
            service = channel.get("service") or "unknown"
            name = channel.get("displayName") or channel.get("name") or channel["id"]
            try:
                await asyncio.to_thread(create_video_post, access_token, channel["id"], state["caption"], public_url, service)
                success.append(f"✅ {service}: {name}")
            except Exception as exc:
                failed.append(f"❌ {service}: {name}\n   {exc}")

        result_lines = ["📊 POST ALL RESULT", ""]
        if success: result_lines.extend(success)
        if failed:
            result_lines.append("")
            result_lines.extend(failed)
        if not success: result_lines.extend(["", "No channel succeeded."])

        await query.message.reply_text("\n".join(result_lines)[:3900])

    except Exception as exc:
        await query.message.reply_text(f"❌ Posting failed:\n\n{type(exc).__name__}: {exc}")
    finally:
        try:
            path = Path(state.get("video_path", ""))
            if str(path): path.unlink(missing_ok=True)
        except Exception:
            pass
        clear_state(chat_id)

async def video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)

    if not state.get("waiting_video"):
        return

    telegram_file, file_name = None, "video.mp4"

    if update.message.video:
        telegram_file = await update.message.video.get_file()
    elif update.message.document and (update.message.document.mime_type or "").startswith("video/"):
        telegram_file = await update.message.document.get_file()
        file_name = update.message.document.file_name or file_name

    if not telegram_file:
        await update.message.reply_text("❌ Please send a video file.")
        return

    local_path = DOWNLOAD_DIR / f"{chat_id}_{int(time.time())}{Path(file_name).suffix or '.mp4'}"
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

    caption = (update.message.text or "").strip()
    if not caption:
        await update.message.reply_text("❌ Caption empty hai.")
        return

    state["caption"] = caption
    state["waiting_caption"] = False

    keyboard = [[InlineKeyboardButton("🚀 POST ALL", callback_data="post_all")]]
    await update.message.reply_text(
        f"✅ Ready!\n\n{caption}\n\nPOST ALL dabao:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

# ============================================================
# BUFFER OAUTH CALLBACK & WEB ROUTES
# ============================================================

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
        return f"<h2>Buffer authorization cancelled</h2><p>{html.escape(error)}</p>", 400
    if not code or not state_value:
        return "Missing authorization code or state.", 400

    row = db.oauth_states.find_one({"state": state_value})
    if not row:
        return "Invalid or expired OAuth state.", 403

    chat_id = row["chat_id"]
    code_verifier = row["verifier"]
    db.oauth_states.delete_one({"state": state_value})

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
        return f"<h2>Buffer token exchange failed</h2><pre>{html.escape(response.text)}</pre>", 400

    data = response.json()
    save_tokens(
        chat_id=chat_id,
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token"),
        expires_in=data.get("expires_in"),
        scope=data.get("scope"),
    )

    try:
        channel_count = len(sync_channels(chat_id))
    except Exception:
        channel_count = 0

    return f"<h2>✅ Buffer connected!</h2><p>Detected {channel_count} channel(s).</p><p>Return to Telegram.</p>"

# ============================================================
# MAIN
# ============================================================

def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing.")

    telegram = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(60)
        .pool_timeout(60)
        .build()
    )

    telegram.add_handler(CommandHandler("start", start_command))
    telegram.add_handler(CommandHandler("connect", connect_command))
    telegram.add_handler(CommandHandler("post", post_command))
    telegram.add_handler(CallbackQueryHandler(callback_button_handler))
    telegram.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, video_handler))
    telegram.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, caption_handler))

    threading.Thread(
        target=lambda: web.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False),
        daemon=True,
    ).start()

    print("==========================================")
    print("Animax Universal Social Poster Bot")
    print("Telegram + Buffer OAuth + Cloudinary + MongoDB + Insta Fix")
    print(f"HTTP port: {PORT}")
    print("==========================================")

    telegram.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
