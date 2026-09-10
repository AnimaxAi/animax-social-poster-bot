import base64
import hashlib
import html
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import cloudinary
import cloudinary.uploader
import requests
from dotenv import load_dotenv
from flask import Flask, request
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

PORT = int(os.getenv("PORT", "10000"))
DB_PATH = Path(os.getenv("DB_PATH", "bot.db"))

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Buffer OAuth
BUFFER_AUTH_URL = "https://auth.buffer.com/auth"
BUFFER_TOKEN_URL = "https://auth.buffer.com/token"
BUFFER_API_URL = "https://api.buffer.com"

# OAuth scopes configured for your Buffer App Client.
BUFFER_SCOPES = "account:read posts:read posts:write offline_access"

# Flask web server
web = Flask(__name__)

# Telegram temporary state
memory = {}
memory_lock = threading.Lock()


# ============================================================
# DATABASE
# ============================================================

def db_init():
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS oauth_states (
                state TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                verifier TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS tokens (
                chat_id INTEGER PRIMARY KEY,
                access_token TEXT NOT NULL,
                refresh_token TEXT,
                expires_at INTEGER,
                scope TEXT
            )
            """
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS channels (
                chat_id INTEGER NOT NULL,
                channel_id TEXT NOT NULL,
                name TEXT,
                service TEXT,
                PRIMARY KEY (chat_id, channel_id)
            )
            """
        )


def db(sql, params=(), fetch=False):
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(sql, params)
        rows = cur.fetchall() if fetch else None
        con.commit()
        return rows


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

def save_tokens(
    chat_id,
    access_token,
    refresh_token=None,
    expires_in=None,
    scope=None,
):
    expires_at = (
        int(time.time()) + int(expires_in)
        if expires_in
        else None
    )

    db(
        """
        INSERT INTO tokens
            (chat_id, access_token, refresh_token, expires_at, scope)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            access_token = excluded.access_token,
            refresh_token = excluded.refresh_token,
            expires_at = excluded.expires_at,
            scope = excluded.scope
        """,
        (
            chat_id,
            access_token,
            refresh_token,
            expires_at,
            scope,
        ),
    )


def get_token_row(chat_id):
    rows = db(
        """
        SELECT access_token, refresh_token, expires_at, scope
        FROM tokens
        WHERE chat_id=?
        """,
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

    if response.status_code >= 400:
        raise RuntimeError(
            f"Buffer refresh failed "
            f"[{response.status_code}]: {response.text}"
        )

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
        raise RuntimeError(
            "Buffer is not connected. Send /connect first."
        )

    access_token, refresh_token, expires_at, _scope = row

    # Refresh shortly before expiry.
    if expires_at and expires_at <= int(time.time()) + 60:
        if not refresh_token:
            raise RuntimeError(
                "Buffer access token expired and no refresh token "
                "is available. Send /connect again."
            )

        return refresh_buffer_token(
            chat_id,
            refresh_token,
        )

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
        json={
            "query": query,
        },
        timeout=90,
    )

    if response.status_code >= 400:
        raise RuntimeError(
            f"Buffer API HTTP {response.status_code}: "
            f"{response.text}"
        )

    data = response.json()

    if data.get("errors"):
        raise RuntimeError(
            f"Buffer GraphQL error: {data['errors']}"
        )

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

    data = buffer_graphql(
        access_token,
        query,
    )

    return data["data"]["account"]["organizations"]


def get_channels(access_token, organization_id):
    safe_org_id = (
        organization_id
        .replace("\\", "\\\\")
        .replace('"', '\\"')
    )

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

    data = buffer_graphql(
        access_token,
        query,
    )

    return data["data"]["channels"]


def sync_channels(chat_id):
    access_token = get_access_token(chat_id)

    found = []

    organizations = get_organizations(
        access_token
    )

    for organization in organizations:
        found.extend(
            get_channels(
                access_token,
                organization["id"],
            )
        )

    db(
        "DELETE FROM channels WHERE chat_id=?",
        (chat_id,),
    )

    for channel in found:
        db(
            """
            INSERT OR REPLACE INTO channels
                (chat_id, channel_id, name, service)
            VALUES (?, ?, ?, ?)
            """,
            (
                chat_id,
                channel["id"],
                channel.get("displayName")
                or channel.get("name")
                or "",
                channel.get("service")
                or "",
            ),
        )

    return found


# ============================================================
# GRAPHQL STRING HELPERS
# ============================================================

def escape_graphql_string(value):
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def youtube_title(text):
    # Use first line as YouTube title.
    # Buffer requires a title when creating a YouTube post.
    first_line = (
        (text or "")
        .splitlines()[0]
        .strip()
    )

    if not first_line:
        first_line = "New Short"

    # Keep under YouTube's 100-character title limit.
    return first_line[:100]


# ============================================================
# SERVICE METADATA
# ============================================================

def build_service_metadata(service, text):
    service_name = (
        (service or "")
        .strip()
        .lower()
    )

    yt_title = escape_graphql_string(
        youtube_title(text)
    )

    # YouTube Shorts
    if "youtube" in service_name:
        return f"""
          metadata: {{
            youtube: {{
              type: short
              title: "{yt_title}"
              categoryId: "24"
              privacy: public
              madeForKids: false
              notifySubscribers: true
              embeddable: true
            }}
          }}
        """

    # Instagram Reels
    if "instagram" in service_name:
        return """
          metadata: {
            instagram: {
              type: reel
              shouldShareToFeed: true
            }
          }
        """

    # Facebook Reels
    if "facebook" in service_name:
        return """
          metadata: {
            facebook: {
              type: reel
            }
          }
        """

    # Unknown service
    return ""


# ============================================================
# BUFFER CREATE VIDEO POST
# ============================================================

def create_video_post(
    access_token,
    channel_id,
    text,
    public_url,
    service,
):
    safe_text = escape_graphql_string(text)
    safe_url = escape_graphql_string(public_url)

    service_metadata = build_service_metadata(
        service,
        text,
    )

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
          post {{
            id
            text
            dueAt

            assets {{
              id
              mimeType
              source
            }}
          }}
        }}

        ... on MutationError {{
          message
        }}

      }}
    }}
    """

    response = buffer_graphql(
        access_token,
        mutation,
    )

    create_result = response["data"]["createPost"]

    if "message" in create_result:
        raise RuntimeError(
            create_result["message"]
        )

    if "post" not in create_result:
        raise RuntimeError(
            f"Unexpected Buffer response: "
            f"{create_result}"
        )

    return create_result["post"]


# ============================================================
# CLOUDINARY
# ============================================================

def configure_cloudinary():
    if not (
        CLOUDINARY_CLOUD_NAME
        and CLOUDINARY_API_KEY
        and CLOUDINARY_API_SECRET
    ):
        raise RuntimeError(
            "Cloudinary environment variables are missing."
        )

    cloudinary.config(
        cloud_name=CLOUDINARY_CLOUD_NAME,
        api_key=CLOUDINARY_API_KEY,
        api_secret=CLOUDINARY_API_SECRET,
        secure=True,
    )


def upload_video_to_cloudinary(local_path):
    configure_cloudinary()

    result = cloudinary.uploader.upload(
        local_path,
        resource_type="video",
        folder="animax-social-poster",
    )

    public_url = (
        result.get("secure_url")
        or result.get("url")
    )

    if not public_url:
        raise RuntimeError(
            "Cloudinary did not return a public URL."
        )

    return public_url


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    keyboard = [
        [
            InlineKeyboardButton(
                "🔗 Connect Buffer",
                callback_data="connect",
            )
        ],
        [
            InlineKeyboardButton(
                "📹 Post Video",
                callback_data="post",
            )
        ],
    ]

    await update.message.reply_text(
        "🚀 Animax Universal Social Poster\n\n"
        "1. Connect your Buffer account.\n"
        "2. Send a video.\n"
        "3. Send caption.\n"
        "4. Press POST ALL.",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


async def connect_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await send_buffer_connect_link(
        update.effective_chat.id,
        context.bot,
    )


async def post_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    state = get_state(
        update.effective_chat.id
    )

    state.clear()
    state["waiting_video"] = True

    await update.message.reply_text(
        "📹 Video bhejo."
    )


# ============================================================
# BUFFER OAUTH LINK
# ============================================================

async def send_buffer_connect_link(
    chat_id,
    bot,
):
    if not BUFFER_CLIENT_ID:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "❌ BUFFER_CLIENT_ID missing "
                "on the server."
            ),
        )
        return

    if not BUFFER_CLIENT_SECRET:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "❌ BUFFER_CLIENT_SECRET missing "
                "on the server."
            ),
        )
        return

    if not BUFFER_REDIRECT_URI:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "❌ BUFFER_REDIRECT_URI missing "
                "on the server."
            ),
        )
        return

    verifier = make_verifier()
    code_challenge = make_challenge(
        verifier
    )
    state_value = secrets.token_urlsafe(32)

    db(
        """
        INSERT INTO oauth_states(
            state,
            chat_id,
            verifier,
            created_at
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            state_value,
            chat_id,
            verifier,
            int(time.time()),
        ),
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

    auth_url = (
        f"{BUFFER_AUTH_URL}?"
        f"{urlencode(params)}"
    )

    keyboard = [
        [
            InlineKeyboardButton(
                "🔗 Connect Buffer",
                url=auth_url,
            )
        ]
    ]

    await bot.send_message(
        chat_id=chat_id,
        text=(
            "Buffer connect karne ke liye "
            "button dabao.\n\n"
            "Buffer login → Allow → Telegram par wapas."
        ),
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


# ============================================================
# TELEGRAM BUTTONS
# ============================================================

async def callback_button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    await query.answer()

    chat_id = query.message.chat_id

    # CONNECT BUFFER
    if query.data == "connect":
        await send_buffer_connect_link(
            chat_id,
            context.bot,
        )
        return

    # POST
    if query.data == "post":
        state = get_state(chat_id)

        state.clear()
        state["waiting_video"] = True

        await query.message.reply_text(
            "📹 Video bhejo."
        )
        return

    # POST ALL
    if query.data != "post_all":
        return

    state = get_state(chat_id)

    if not state.get("video_path"):
        await query.message.reply_text(
            "❌ Video missing. /post bhejo."
        )
        return

    if not state.get("caption"):
        await query.message.reply_text(
            "❌ Caption missing."
        )
        return

    try:
        await query.message.reply_text(
            "⏳ Video upload ho raha hai...\n"
            "Uske baad Buffer par publish hoga."
        )

        import asyncio

        # 1. Telegram video -> Cloudinary
        public_url = await asyncio.to_thread(
            upload_video_to_cloudinary,
            state["video_path"],
        )

        # 2. User's Buffer token
        access_token = await asyncio.to_thread(
            get_access_token,
            chat_id,
        )

        # 3. User's connected Buffer channels
        channels = await asyncio.to_thread(
            sync_channels,
            chat_id,
        )

        if not channels:
            raise RuntimeError(
                "No Buffer channels found for this user."
            )

        success = []
        failed = []

        # 4. Publish one post per channel
        for channel in channels:
            service = (
                channel.get("service")
                or "unknown"
            )

            name = (
                channel.get("displayName")
                or channel.get("name")
                or channel["id"]
            )

            try:
                await asyncio.to_thread(
                    create_video_post,
                    access_token,
                    channel["id"],
                    state["caption"],
                    public_url,
                    service,
                )

                success.append(
                    f"✅ {service}: {name}"
                )

            except Exception as exc:
                failed.append(
                    f"❌ {service}: {name}\n"
                    f"   {exc}"
                )

        result_lines = [
            "📊 POST ALL RESULT",
            "",
        ]

        if success:
            result_lines.extend(success)

        if failed:
            result_lines.append("")
            result_lines.extend(failed)

        if not success:
            result_lines.extend([
                "",
                "No channel succeeded.",
            ])

        result_text = "\n".join(
            result_lines
        )

        await query.message.reply_text(
            result_text[:3900]
        )

    except Exception as exc:
        await query.message.reply_text(
            "❌ Posting failed:\n\n"
            f"{type(exc).__name__}: {exc}"
        )

    finally:
        try:
            path = Path(
                state.get(
                    "video_path",
                    "",
                )
            )

            if str(path):
                path.unlink(
                    missing_ok=True
                )

        except Exception:
            pass

        clear_state(chat_id)


# ============================================================
# VIDEO HANDLER
# ============================================================

async def video_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)

    if not state.get("waiting_video"):
        return

    telegram_file = None
    file_name = "video.mp4"

    # Normal Telegram video
    if update.message.video:
        telegram_file = (
            await update.message.video.get_file()
        )

    # Telegram document sent as video
    elif (
        update.message.document
        and (
            update.message.document.mime_type
            or ""
        ).startswith("video/")
    ):
        telegram_file = (
            await update.message.document.get_file()
        )

        file_name = (
            update.message.document.file_name
            or file_name
        )

    if not telegram_file:
        await update.message.reply_text(
            "❌ Please send a video file."
        )
        return

    suffix = (
        Path(file_name).suffix
        or ".mp4"
    )

    local_path = (
        DOWNLOAD_DIR
        / f"{chat_id}_{int(time.time())}{suffix}"
    )

    await telegram_file.download_to_drive(
        str(local_path)
    )

    state["video_path"] = str(
        local_path
    )

    state["waiting_video"] = False
    state["waiting_caption"] = True

    await update.message.reply_text(
        "✅ Video received.\n\n"
        "Ab caption/title bhejo."
    )


# ============================================================
# CAPTION HANDLER
# ============================================================

async def caption_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)

    if not state.get("waiting_caption"):
        return

    caption = (
        update.message.text
        or ""
    ).strip()

    if not caption:
        await update.message.reply_text(
            "❌ Caption empty hai."
        )
        return

    state["caption"] = caption
    state["waiting_caption"] = False

    keyboard = [
        [
            InlineKeyboardButton(
                "🚀 POST ALL",
                callback_data="post_all",
            )
        ]
    ]

    await update.message.reply_text(
        "✅ Ready!\n\n"
        f"{caption}\n\n"
        "POST ALL dabao:",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


# ============================================================
# HEALTH / HOME
# ============================================================

@web.get("/")
def home():
    return (
        "Animax Universal Social Poster "
        "is running."
    )


@web.get("/health")
def health():
    return {
        "status": "ok"
    }


# ============================================================
# BUFFER OAUTH CALLBACK
# ============================================================

@web.get("/buffer/callback")
def buffer_callback():
    code = request.args.get("code")
    state_value = request.args.get("state")
    error = request.args.get("error")

    if error:
        return (
            "<h2>Buffer authorization cancelled</h2>"
            f"<p>{html.escape(error)}</p>"
        ), 400

    if not code or not state_value:
        return (
            "Missing authorization code or state."
        ), 400

    rows = db(
        """
        SELECT chat_id, verifier
        FROM oauth_states
        WHERE state=?
        """,
        (state_value,),
        fetch=True,
    )

    if not rows:
        return (
            "Invalid or expired OAuth state."
        ), 403

    chat_id, code_verifier = rows[0]

    # State is single-use.
    db(
        "DELETE FROM oauth_states WHERE state=?",
        (state_value,),
    )

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
        return (
            "<h2>Buffer token exchange failed</h2>"
            f"<pre>"
            f"{html.escape(response.text)}"
            f"</pre>"
        ), 400

    data = response.json()

    save_tokens(
        chat_id=chat_id,
        access_token=data["access_token"],
        refresh_token=data.get(
            "refresh_token"
        ),
        expires_in=data.get(
            "expires_in"
        ),
        scope=data.get(
            "scope"
        ),
    )

    try:
        channel_count = len(
            sync_channels(chat_id)
        )
    except Exception:
        channel_count = 0

    return (
        "<h2>✅ Buffer connected!</h2>"
        f"<p>Detected "
        f"{channel_count} channel(s).</p>"
        "<p>Return to Telegram.</p>"
    )


# ============================================================
# MAIN
# ============================================================

def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing."
        )

    db_init()

    telegram = (
        Application
        .builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    telegram.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    telegram.add_handler(
        CommandHandler(
            "connect",
            connect_command,
        )
    )

    telegram.add_handler(
        CommandHandler(
            "post",
            post_command,
        )
    )

    telegram.add_handler(
        CallbackQueryHandler(
            callback_button_handler
        )
    )

    telegram.add_handler(
        MessageHandler(
            filters.VIDEO
            | filters.Document.VIDEO,
            video_handler,
        )
    )

    telegram.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            caption_handler,
        )
    )

    # Render requires the HTTP service to listen
    # on 0.0.0.0:$PORT.
    threading.Thread(
        target=lambda: web.run(
            host="0.0.0.0",
            port=PORT,
            debug=False,
            use_reloader=False,
        ),
        daemon=True,
    ).start()

    print("==========================================")
    print("Animax Universal Social Poster Bot")
    print("Telegram + Buffer OAuth + Cloudinary")
    print(f"HTTP port: {PORT}")
    print("==========================================")

    telegram.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
