import asyncio
import base64
import hashlib
import html
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
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
# ENV CONFIG
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
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

PORT = int(os.getenv("PORT", "10000"))
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

BUFFER_AUTH_URL = "https://auth.buffer.com/auth"
BUFFER_TOKEN_URL = "https://auth.buffer.com/token"
BUFFER_API_URL = "https://api.buffer.com"
BUFFER_SCOPES = "account:read posts:read posts:write offline_access"

web = Flask(__name__)
memory = {}
memory_lock = threading.Lock()

# ============================================================
# DATABASE (MONGODB)
# ============================================================

if not MONGO_URI: raise RuntimeError("MONGO_URI is missing in environment variables.")
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["animax_social_bot"]

# ============================================================
# TELEGRAM STATE
# ============================================================

def get_state(chat_id):
    with memory_lock: return memory.setdefault(chat_id, {})

def clear_state(chat_id):
    with memory_lock: memory.pop(chat_id, None)

# ============================================================
# DIRECT GOOGLE GEMINI API (FAST, CRASH-FREE, NO PACKAGES)
# ============================================================

def generate_ai_text(prompt):
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY missing hai.")
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    headers = {"Content-Type": "application/json"}
    
    resp = requests.post(url, json=payload, headers=headers, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Google API Error: {resp.text}")
    
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]

# ============================================================
# BUFFER CORE FUNCTIONS
# ============================================================

def make_verifier(): return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
def make_challenge(verifier): return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")

def save_tokens(chat_id, access_token, refresh_token=None, expires_in=None, scope=None):
    expires_at = int(time.time()) + int(expires_in) if expires_in else None
    db.tokens.update_one({"chat_id": chat_id}, {"$set": {"access_token": access_token, "refresh_token": refresh_token, "expires_at": expires_at, "scope": scope}}, upsert=True)

def delete_tokens(chat_id):
    db.tokens.delete_one({"chat_id": chat_id})
    db.channels.delete_many({"chat_id": chat_id})

def refresh_buffer_token(chat_id, refresh_token):
    response = requests.post(BUFFER_TOKEN_URL, data={"client_id": BUFFER_CLIENT_ID, "client_secret": BUFFER_CLIENT_SECRET, "grant_type": "refresh_token", "refresh_token": refresh_token}, timeout=60)
    if response.status_code >= 400: raise RuntimeError("Buffer refresh failed")
    data = response.json()
    save_tokens(chat_id, data["access_token"], data.get("refresh_token"), data.get("expires_in"), data.get("scope"))
    return data["access_token"]

def get_access_token(chat_id):
    row = db.tokens.find_one({"chat_id": chat_id})
    if not row: raise RuntimeError("Buffer is not connected.")
    if row.get("expires_at") and row.get("expires_at") <= int(time.time()) + 60:
        return refresh_buffer_token(chat_id, row["refresh_token"])
    return row["access_token"]

def buffer_graphql(access_token, query):
    response = requests.post(BUFFER_API_URL, headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}, json={"query": query}, timeout=90)
    if response.status_code >= 400: raise RuntimeError(f"Buffer API Error: {response.text}")
    return response.json()

def sync_channels(chat_id):
    access_token = get_access_token(chat_id)
    org_data = buffer_graphql(access_token, "query { account { organizations { id name } } }")
    found = []
    for org in org_data["data"]["account"]["organizations"]:
        safe_org_id = org["id"].replace("\\", "\\\\").replace('"', '\\"')
        chan_data = buffer_graphql(access_token, f'query {{ channels(input: {{ organizationId: "{safe_org_id}" }}) {{ id name displayName service }} }}')
        found.extend(chan_data["data"]["channels"])
    db.channels.delete_many({"chat_id": chat_id})
    for ch in found:
        db.channels.insert_one({"chat_id": chat_id, "channel_id": ch["id"], "name": ch.get("displayName") or ch.get("name") or "", "service": ch.get("service") or ""})
    return found

def escape_graphql_string(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\r", "\\r").replace("\n", "\\n")

def build_service_metadata(service, text):
    service_name = (service or "").strip().lower()
    first_line = (text or "").splitlines()[0].strip() if text else "New Short"
    yt_title = escape_graphql_string(first_line[:90])

    if "youtube" in service_name: return f'metadata: {{ youtube: {{ title: "{yt_title}", categoryId: "24", privacy: public, madeForKids: false, notifySubscribers: true, embeddable: true }} }}'
    if "instagram" in service_name: return 'metadata: { instagram: { type: reel, shouldShareToFeed: true } }'
    if "facebook" in service_name: return 'metadata: { facebook: { type: reel } }'
    return ""

def create_video_post(access_token, channel_id, text, public_url, service, scheduled_at=None):
    safe_text, safe_url = escape_graphql_string(text), escape_graphql_string(public_url)
    service_metadata = build_service_metadata(service, text)
    
    # 🔴 FIX: UPPERCASE ENUMS FOR BUFFER SCHEDULING
    if scheduled_at:
        sched_str = f'schedulingType: CUSTOM, mode: CUSTOM, scheduledAt: {scheduled_at}'
    else:
        sched_str = 'schedulingType: AUTOMATIC, mode: SHARE_NOW'

    mutation = f'''
    mutation CreateVideoPost {{
      createPost(
        input: {{ text: "{safe_text}", channelId: "{channel_id}", {sched_str}, assets: [{{ video: {{ url: "{safe_url}", metadata: {{ thumbnailOffset: 2000 }} }} }}] {service_metadata} }}
      ) {{ ... on PostActionSuccess {{ post {{ id }} }} ... on MutationError {{ message }} }}
    }}'''
    
    response = buffer_graphql(access_token, mutation)
    if "errors" in response: raise RuntimeError(f"Buffer Error: {response['errors'][0].get('message', 'Format Error')}")
    if "data" not in response or "createPost" not in response["data"]: raise RuntimeError(f"Unexpected structure: {response}")
    if "message" in response["data"]["createPost"]: raise RuntimeError(response["data"]["createPost"]["message"])
    return response["data"]["createPost"]["post"]

def upload_video_to_cloudinary(local_path):
    cloudinary.config(cloud_name=CLOUDINARY_CLOUD_NAME, api_key=CLOUDINARY_API_KEY, api_secret=CLOUDINARY_API_SECRET, secure=True)
    result = cloudinary.uploader.upload(local_path, resource_type="video", folder="animax-social-poster", transformation=[{"width": 1080, "height": 1920, "crop": "pad", "background": "black"}, {"quality": "auto", "fetch_format": "mp4"}])
    return result.get("secure_url") or result.get("url")

def get_schedule_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Post Now", callback_data="sch_now")],
        [InlineKeyboardButton("🕒 In 3 Hours", callback_data="sch_3"), InlineKeyboardButton("🕒 In 12 Hours", callback_data="sch_12")],
        [InlineKeyboardButton("✏️ Manual Custom Time", callback_data="sch_custom")]
    ])

async def execute_posting(message, chat_id, state, scheduled_at):
    try:
        public_url = await asyncio.to_thread(upload_video_to_cloudinary, state["video_path"])
        access_token = await asyncio.to_thread(get_access_token, chat_id)
        channels = await asyncio.to_thread(sync_channels, chat_id)
        
        success, failed = [], []
        for ch in channels:
            service = ch.get("service", "unknown")
            name = ch.get("displayName") or ch["name"]
            
            caption_to_post = state["final_captions"].get(service, state["final_captions"].get("default", ""))
            
            try:
                await asyncio.to_thread(create_video_post, access_token, ch["id"], caption_to_post, public_url, service, scheduled_at)
                success.append(f"✅ {service}: {name}")
            except Exception as exc:
                failed.append(f"❌ {service}: {name}\n   {exc}")

        res = ["📊 POST RESULT", ""] + success + ([""] + failed if failed else [])
        await message.reply_text("\n".join(res)[:3900])
    except Exception as e:
        await message.reply_text(f"❌ Error: {e}")
    finally:
        try: Path(state.get("video_path", "")).unlink(missing_ok=True)
        except: pass
        clear_state(chat_id)

# ============================================================
# COMMAND HANDLERS
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton("🔗 Connect Buffer", callback_data="connect")], [InlineKeyboardButton("📹 Post Video", callback_data="post")], [InlineKeyboardButton("🔌 Disconnect Buffer", callback_data="disconnect")]]
    await update.message.reply_text("🚀 Animax Universal Poster\n\n1. Connect Buffer\n2. Send Video\n3. Choose Caption Mode\n4. Post or Schedule", reply_markup=InlineKeyboardMarkup(kb))

async def connect_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    verifier = make_verifier()
    state_val = secrets.token_urlsafe(32)
    db.oauth_states.insert_one({"state": state_val, "chat_id": chat_id, "verifier": verifier, "created_at": int(time.time())})
    params = {"client_id": BUFFER_CLIENT_ID, "redirect_uri": BUFFER_REDIRECT_URI, "response_type": "code", "scope": BUFFER_SCOPES, "state": state_val, "code_challenge": make_challenge(verifier), "code_challenge_method": "S256", "prompt": "consent"}
    await update.message.reply_text("🔗 Connect Buffer:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Login", url=f"{BUFFER_AUTH_URL}?{urlencode(params)}")]]))

async def post_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    state.clear()
    state["waiting_video"] = True
    await update.message.reply_text("📹 Bhejo apni video.")

# ============================================================
# AI & TELEGRAM LOGIC
# ============================================================

async def callback_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    state = get_state(chat_id)

    if query.data == "connect":
        verifier = make_verifier()
        state_val = secrets.token_urlsafe(32)
        db.oauth_states.insert_one({"state": state_val, "chat_id": chat_id, "verifier": verifier, "created_at": int(time.time())})
        params = {"client_id": BUFFER_CLIENT_ID, "redirect_uri": BUFFER_REDIRECT_URI, "response_type": "code", "scope": BUFFER_SCOPES, "state": state_val, "code_challenge": make_challenge(verifier), "code_challenge_method": "S256", "prompt": "consent"}
        await context.bot.send_message(chat_id, "🔗 Connect Buffer:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Login", url=f"{BUFFER_AUTH_URL}?{urlencode(params)}")]]))
        return

    if query.data == "disconnect":
        delete_tokens(chat_id)
        await query.message.reply_text("✅ Disconnected! Connect again to switch accounts.")
        return

    if query.data == "post":
        state.clear()
        state["waiting_video"] = True
        await query.message.reply_text("📹 Bhejo apni video.")
        return

    if query.data == "mode_manual":
        state["input_mode"] = "manual"
        state["waiting_caption_input"] = True
        await query.message.reply_text("✍️ Apna caption type karke bhejo:")
        return

    if query.data == "mode_ai_3":
        if not GEMINI_API_KEY: return await query.message.reply_text("❌ GEMINI_API_KEY missing hai.")
        state["input_mode"] = "ai_3"
        state["waiting_caption_input"] = True
        await query.message.reply_text("🤖 Topic batao (e.g., 'anime status'):")
        return

    if query.data == "mode_ai_plat":
        if not GEMINI_API_KEY: return await query.message.reply_text("❌ GEMINI_API_KEY missing hai.")
        state["input_mode"] = "ai_plat"
        state["waiting_caption_input"] = True
        await query.message.reply_text("🌍 Topic batao. Main YT, Insta aur FB ke liye alag captions likhunga:")
        return

    if query.data.startswith("opt_"):
        idx = int(query.data.split("_")[1])
        selected_text = state.get("ai_options", [])[idx]
        state["final_captions"] = {"default": selected_text}
        await query.message.reply_text(f"✅ Selected:\n\n{selected_text}\n\n**Kab post karna hai?**", reply_markup=get_schedule_keyboard())
        return

    if query.data.startswith("sch_"):
        if not state.get("video_path") or not state.get("final_captions"):
            return await query.message.reply_text("❌ Data missing. Bhejo /post.")
        
        if query.data == "sch_custom":
            state["waiting_schedule_time"] = True
            await query.message.reply_text("✏️ **Custom Time Set Karein**\n\n⚠️ *Note: Buffer API ke niyam anusaar aapko schedule time current time se kam se kam 30 minute aage ka rakhna hoga.*\n\nIs format mein apna time bhejein: `YYYY-MM-DD HH:MM` (24-hour time)\nExample: `2026-09-15 14:30`", parse_mode="Markdown")
            return

        scheduled_at = None
        if query.data != "sch_now":
            hours = int(query.data.split("_")[1])
            future_time = datetime.now(timezone.utc) + timedelta(hours=hours)
            scheduled_at = int(future_time.timestamp())
            await query.message.reply_text(f"⏳ Uploading... Post scheduled for {hours} hours from now!")
        else:
            await query.message.reply_text("⏳ Uploading video & Posting right now...")

        await execute_posting(query.message, chat_id, state, scheduled_at)
        return

async def video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    if not state.get("waiting_video"): return
    
    file = await update.message.video.get_file() if update.message.video else (await update.message.document.get_file() if update.message.document else None)
    if not file: return await update.message.reply_text("❌ Send a valid video.")

    local_path = str(DOWNLOAD_DIR / f"{chat_id}_{int(time.time())}.mp4")
    await file.download_to_drive(local_path)
    state["video_path"] = local_path
    state["waiting_video"] = False

    kb = [[InlineKeyboardButton("✍️ Manual Caption", callback_data="mode_manual")], [InlineKeyboardButton("🤖 AI: 3 Options (Best SEO)", callback_data="mode_ai_3")], [InlineKeyboardButton("🌍 AI: Platform Specific (YT/IG/FB)", callback_data="mode_ai_plat")]]
    await update.message.reply_text("✅ Video aagayi! Caption kaise likhna hai?", reply_markup=InlineKeyboardMarkup(kb))

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    text = update.message.text.strip()

    if state.get("waiting_schedule_time"):
        try:
            user_dt = datetime.strptime(text, "%Y-%m-%d %H:%M")
            ist_tz = timezone(timedelta(hours=5, minutes=30))
            user_dt_aware = user_dt.replace(tzinfo=ist_tz)
            if user_dt_aware < datetime.now(ist_tz) + timedelta(minutes=30):
                return await update.message.reply_text("❌ **Error:** Time abhi ke time se kam se kam 30 minute aage ka hona chahiye.\n\nPhir se naya time type karein (Jaise: `2026-09-15 14:30`):", parse_mode="Markdown")
            
            state["waiting_schedule_time"] = False
            scheduled_at = int(user_dt_aware.timestamp())
            await update.message.reply_text(f"⏳ Uploading... Post scheduled for {text} (IST)!")
            await execute_posting(update.message, chat_id, state, scheduled_at)
        except ValueError:
            await update.message.reply_text("❌ Galat format! Kripya sahi format mein likhein:\n`YYYY-MM-DD HH:MM`\n(Jaise: `2026-09-15 14:30`)", parse_mode="Markdown")
        return

    if not state.get("waiting_caption_input"): return
    state["waiting_caption_input"] = False
    mode = state.get("input_mode")

    if mode == "manual":
        state["final_captions"] = {"default": text}
        await update.message.reply_text(f"✅ Ready:\n\n{text}\n\n**Kab post karna hai?**", reply_markup=get_schedule_keyboard())

    elif mode == "ai_3":
        msg = await update.message.reply_text("⏳ AI is writing 3 variations...")
        try:
            # 🔴 FIX: STRICT SHORT LIMITS FOR OPTIONS
            prompt = f"Write 3 highly engaging, viral, and VERY SHORT captions for a video about '{text}'.\nSTRICT RULES:\n- Maximum 15-20 words per caption.\n- Maximum 3 hashtags per caption.\n- Separate each distinct caption exactly using the string '|||'."
            
            res_text = await asyncio.to_thread(generate_ai_text, prompt)
            
            options = [opt.strip() for opt in res_text.split("|||") if opt.strip()]
            if len(options) < 3: options = [res_text, res_text, res_text]
            
            state["ai_options"] = options
            kb = [[InlineKeyboardButton(f"Select Option {i+1}", callback_data=f"opt_{i}")] for i in range(3)]
            formatted_text = "\n\n".join([f"**Option {i+1}:**\n{opt}" for i, opt in enumerate(options)])
            await msg.edit_text(f"🤖 Here are 3 options:\n\n{formatted_text}", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        except Exception as e:
            await msg.edit_text(f"❌ AI Error: {e}")

    elif mode == "ai_plat":
        msg = await update.message.reply_text("⏳ AI is writing for YT, Insta & FB...")
        try:
            # 🔴 FIX: STRICT SHORT LIMITS FOR PLATFORMS
            prompt = f"Write 3 platform-specific captions for a video about '{text}'.\nSTRICT RULES:\n1. YouTube Shorts: STRICTLY MAX 80 CHARACTERS total. Only 1 short hook and 3 hashtags.\n2. Instagram Reels: Max 2 short lines, 4-5 trending tags.\n3. Facebook Reels: Max 2 short lines, 2-3 relevant tags.\nSeparate exactly like this:\nYOUTUBE_START\n[text]\nYOUTUBE_END\nINSTAGRAM_START\n[text]\nINSTAGRAM_END\nFACEBOOK_START\n[text]\nFACEBOOK_END"
            
            res_text = await asyncio.to_thread(generate_ai_text, prompt)
            raw = res_text
            
            yt = raw.split("YOUTUBE_START")[1].split("YOUTUBE_END")[0].strip() if "YOUTUBE_START" in raw else text
            ig = raw.split("INSTAGRAM_START")[1].split("INSTAGRAM_END")[0].strip() if "INSTAGRAM_START" in raw else text
            fb = raw.split("FACEBOOK_START")[1].split("FACEBOOK_END")[0].strip() if "FACEBOOK_START" in raw else text
            
            state["final_captions"] = {"youtube": yt, "instagram": ig, "facebook": fb, "default": text}
            
            display_text = f"✅ **Platform Captions Ready!**\n\n🔴 **YouTube:**\n{yt}\n\n🟣 **Insta:**\n{ig}\n\n🔵 **FB:**\n{fb}\n\n**Kab post karna hai?**"
            await msg.edit_text(display_text, parse_mode="Markdown", reply_markup=get_schedule_keyboard())
        except Exception as e:
            await msg.edit_text(f"❌ AI Error: {e}")

# ============================================================
# WEB ROUTES & MAIN
# ============================================================
@web.get("/")
def home(): return "Bot is running."

@web.get("/health")
def health(): return {"status": "ok"}

@web.get("/buffer/callback")
def buffer_callback():
    code, state_value, error = request.args.get("code"), request.args.get("state"), request.args.get("error")
    if error: return "Auth cancelled", 400
    row = db.oauth_states.find_one({"state": state_value})
    if not row: return "Invalid state", 403
    db.oauth_states.delete_one({"state": state_value})
    response = requests.post(BUFFER_TOKEN_URL, data={"client_id": BUFFER_CLIENT_ID, "client_secret": BUFFER_CLIENT_SECRET, "grant_type": "authorization_code", "code": code, "redirect_uri": BUFFER_REDIRECT_URI, "code_verifier": row["verifier"]}, timeout=60)
    data = response.json()
    save_tokens(row["chat_id"], data["access_token"], data.get("refresh_token"), data.get("expires_in"), data.get("scope"))
    return "<h2>✅ Buffer connected! Return to Telegram.</h2>"

def main():
    telegram = Application.builder().token(TELEGRAM_BOT_TOKEN).read_timeout(60).write_timeout(60).connect_timeout(60).pool_timeout(60).build()
    
    telegram.add_handler(CommandHandler("start", start_command))
    telegram.add_handler(CommandHandler("connect", connect_command))
    telegram.add_handler(CommandHandler("post", post_command))
    
    telegram.add_handler(CallbackQueryHandler(callback_button_handler))
    telegram.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, video_handler))
    telegram.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    
    threading.Thread(target=lambda: web.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False), daemon=True).start()
    telegram.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__": main()
