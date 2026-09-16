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

# Google GenAI SDK
from google import genai

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
# DATABASE
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
# BUFFER CORE
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
    yt_title = escape_graphql_string(first_line[:80])

    if "youtube" in service_name: return f'metadata: {{ youtube: {{ title: "{yt_title}", categoryId: "24", privacy: public, madeForKids: false, notifySubscribers: true, embeddable: true }} }}'
    if "instagram" in service_name: return 'metadata: { instagram: { type: reel, shouldShareToFeed: true } }'
    if "facebook" in service_name: return 'metadata: { facebook: { type: reel } }'
    return ""

def create_video_post(access_token, channel_id, text, public_url, service):
    safe_text, safe_url = escape_graphql_string(text), escape_graphql_string(public_url)
    service_metadata = build_service_metadata(service, text)
    
    sched_str = 'schedulingType: automatic, mode: shareNow'

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

# 🔴 FIX: Eager Upload for Instagram Reels Support (No Loading Delay for Buffer)
def upload_video_to_cloudinary(local_path):
    cloudinary.config(cloud_name=CLOUDINARY_CLOUD_NAME, api_key=CLOUDINARY_API_KEY, api_secret=CLOUDINARY_API_SECRET, secure=True)
    
    eager_options = [{"width": 1080, "height": 1920, "crop": "pad", "background": "black"}]
    
    result = cloudinary.uploader.upload(
        local_path, 
        resource_type="video", 
        folder="animax-social-poster",
        eager=eager_options,
        eager_async=False # Upload hone tak wait karega, taaki direct processed link mile
    )
    
    # Processed (Ready) URL fetch karna Buffer ke liye
    if "eager" in result and len(result["eager"]) > 0:
        url = result["eager"][0].get("secure_url")
    else:
        url = result.get("secure_url")
        
    public_id = result.get("public_id")
    return url, public_id

def get_post_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Post Now", callback_data="sch_now")]
    ])

async def execute_posting(message, chat_id, state):
    public_id = None
    try:
        public_url, public_id = await asyncio.to_thread(upload_video_to_cloudinary, state["video_path"])
        access_token = await asyncio.to_thread(get_access_token, chat_id)
        channels = await asyncio.to_thread(sync_channels, chat_id)
        
        success, failed = [], []
        for ch in channels:
            service = ch.get("service", "unknown")
            name = ch.get("displayName") or ch["name"]
            caption_to_post = state["final_captions"].get(service, state["final_captions"].get("default", ""))
            
            try:
                await asyncio.to_thread(create_video_post, access_token, ch["id"], caption_to_post, public_url, service)
                success.append(f"✅ {service}: {name}")
            except Exception as exc:
                failed.append(f"❌ {service}: {name}\n   {exc}")

        res = ["📊 POST RESULT", ""] + success + ([""] + failed if failed else [])
        await message.reply_text("\n".join(res)[:3900])
        
        # 🔴 FIX: Delete Timer increased to 1 Hour to save Buffer from breaking!
        if public_id:
            async def delete_after_delay(pid):
                await asyncio.sleep(3600) # 3600 seconds = 1 hour
                try:
                    cloudinary.config(cloud_name=CLOUDINARY_CLOUD_NAME, api_key=CLOUDINARY_API_KEY, api_secret=CLOUDINARY_API_SECRET, secure=True)
                    cloudinary.uploader.destroy(pid, resource_type="video")
                    print(f"Cloudinary Auto-Delete: {pid} removed.")
                except Exception as e:
                    pass
            
            asyncio.create_task(delete_after_delay(public_id))
            await message.reply_text("🧹 *Cloudinary storage will auto-clean in 1 hour!*", parse_mode="Markdown")

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
    await update.message.reply_text("🚀 Animax Universal Poster\n\n1. Connect Buffer\n2. Send Video\n3. Choose Caption Mode\n4. Direct Post", reply_markup=InlineKeyboardMarkup(kb))

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
        await query.message.reply_text(f"✅ Selected:\n\n{selected_text}\n\n**Ready to post?**", reply_markup=get_post_keyboard())
        return

    if query.data == "sch_now":
        if not state.get("video_path") or not state.get("final_captions"):
            return await query.message.reply_text("❌ Data missing. Bhejo /post.")
        
        await query.message.reply_text("⏳ Uploading video & Posting right now...")
        await execute_posting(query.message, chat_id, state)
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

    if not state.get("waiting_caption_input"): return
    state["waiting_caption_input"] = False
    mode = state.get("input_mode")

    if mode == "manual":
        state["final_captions"] = {"default": text}
        await update.message.reply_text(f"✅ Ready:\n\n{text}\n\n**Ready to post?**", reply_markup=get_post_keyboard())

    elif mode == "ai_3":
        msg = await update.message.reply_text("⏳ AI is writing 3 variations...")
        try:
            prompt = f"Write 3 highly engaging, viral, and VERY SHORT captions for a video about '{text}'.\nSTRICT RULES:\n- Maximum 80 CHARACTERS TOTAL per caption.\n- Strictly 3 hashtags per caption.\n- Separate each distinct caption exactly using the string '|||'."
            
            client = genai.Client(api_key=GEMINI_API_KEY)
            interaction = await asyncio.to_thread(client.interactions.create, model="gemini-3.8-flash", input=prompt)
            res_text = interaction.output_text
            
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
            prompt = f"Write 3 platform-specific captions for a video about '{text}'. STRICT RULES:\n1. YouTube Shorts: STRICTLY MAX 80 CHARACTERS total and exactly 3 hashtags.\n2. Instagram Reels: Max 2 short lines, 4-5 trending tags.\n3. Facebook Reels: Max 2 short lines, 2-3 relevant tags.\nSeparate exactly like this:\nYOUTUBE_START\n[text]\nYOUTUBE_END\nINSTAGRAM_START\n[text]\nINSTAGRAM_END\nFACEBOOK_START\n[text]\nFACEBOOK_END"
            
            client = genai.Client(api_key=GEMINI_API_KEY)
            interaction = await asyncio.to_thread(client.interactions.create, model="gemini-3.8-flash", input=prompt)
            raw = interaction.output_text
            
            yt = raw.split("YOUTUBE_START")[1].split("YOUTUBE_END")[0].strip() if "YOUTUBE_START" in raw else text
            ig = raw.split("INSTAGRAM_START")[1].split("INSTAGRAM_END")[0].strip() if "INSTAGRAM_START" in raw else text
            fb = raw.split("FACEBOOK_START")[1].split("FACEBOOK_END")[0].strip() if "FACEBOOK_START" in raw else text
            
            state["final_captions"] = {"youtube": yt, "instagram": ig, "facebook": fb, "default": text}
            
            display_text = f"✅ **Platform Captions Ready!**\n\n🔴 **YouTube:**\n{yt}\n\n🟣 **Insta:**\n{ig}\n\n🔵 **FB:**\n{fb}\n\n**Ready to post?**"
            await msg.edit_text(display_text, parse_mode="Markdown", reply_markup=get_post_keyboard())
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
