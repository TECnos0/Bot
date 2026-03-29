#!/usr/bin/env python3
"""
Ghost Crew - Telegram Group Guardian Bot
Converted from TypeScript to Python for fps.ms VPS
Single file deployment ready
"""

import os
import asyncio
import logging
import re
import html
from typing import Dict, Set, Optional, Any, List, Tuple, Union
from dataclasses import dataclass
from pathlib import Path
import json
from datetime import datetime
import signal
import sys
from functools import wraps

import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ChatJoinRequestHandler, filters, ContextTypes
from telegram.constants import ParseMode, ChatType, ChatMemberStatus
from telegram.error import TelegramError
import structlog

# ─── Constants ────────────────────────────────────────────────────────────────
OWNER_ID = 8566750408
CHANNEL_USERNAME = "@class11th_Channel"
CHANNEL_LINK = "https://t.me/+5jFYIjx_KcdkM2Jl"

# Default media file IDs
START_WELCOME_VIDEO_FILE_ID = "BAACAgUAAxkBAANTacCRC2-et3Xsuh7iv4CaxDlB1rEAAhIbAAK7ughW8Lqw6ulviB06BA"
DEFAULT_GROUP_WELCOME_VIDEO_FILE_ID = "BAACAgUAAxkBAANgacC9iKXvA2m7cU4cve--QMaBPCAAAoAgAAKD5whWbIh-cvPOk8I6BA"

# ─── Pydantic Models ─────────────────────────────────────────────────────────
class HealthCheckResponse(BaseModel):
    status: str = "ok"

# ─── Logging Setup (Pino-style) ──────────────────────────────────────────────
def setup_logger():
    """Production-ready structured logging like pino"""
    is_production = os.getenv("NODE_ENV") == "production"
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    
    # Configure structlog for structured logging
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer() if is_production else structlog.dev.ConsoleRenderer(colors=True),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    
    logger = structlog.get_logger("ghost-crew")
    
    # Python stdlib logging integration
    logging.basicConfig(
        level=getattr(logging, log_level),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )
    
    return logger

logger = setup_logger()

# ─── Data Classes ────────────────────────────────────────────────────────────
@dataclass
class WelcomeMedia:
    file_id: str
    media_type: str  # photo|video|audio|document|animation|sticker

@dataclass
class WelcomeConfig:
    text: Optional[str] = None
    media: Optional[WelcomeMedia] = None

@dataclass
class BlockedUserInfo:
    first_name: str
    last_name: Optional[str] = None

@dataclass
class ParsedTemplate:
    text: str
    no_notif: bool
    protect_content: bool
    media_spoiler: bool
    enable_preview: bool
    preview_above: bool
    rules_new_row: bool
    rules_same_row: bool

# ─── Global State (In-memory like TS) ────────────────────────────────────────
verified_channel_members: Set[int] = set()
auto_approve_enabled: Dict[int, bool] = {}
started_in_dm: Set[int] = set()
group_message_count: Dict[int, int] = {}
blocked_users_per_group: Dict[int, Dict[int, BlockedUserInfo]] = {}

group_welcome_config: Dict[int, WelcomeConfig] = {}
group_dm_welcome_config: Dict[int, WelcomeConfig] = {}
group_rules_link: Dict[int, str] = {}

live_group_welcome_video_file_id = DEFAULT_GROUP_WELCOME_VIDEO_FILE_ID
bot_username = ""
start_welcome_caption = ""

# ─── FastAPI App Setup ───────────────────────────────────────────────────────
app = FastAPI(title="Ghost Crew API", version="1.0.0")

# CORS middleware (like TS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Request logging middleware (pino-http equivalent)
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = datetime.now()
    client_ip = request.client.host if request.client else "unknown"
    
    try:
        response = await call_next(request)
        process_time = (datetime.now() - start_time).total_seconds()
        
        logger.info(
            "HTTP request",
            method=request.method,
            url=str(request.url),
            status_code=response.status_code,
            client_ip=client_ip,
            process_time=process_time,
        )
        return response
    except Exception as e:
        logger.error("HTTP request error", exc_info=e, method=request.method, url=str(request.url))
        raise

# ─── Health Check Route (identical to TS) ────────────────────────────────────
@app.get("/api/healthz", response_model=HealthCheckResponse)
async def healthz():
    """Health check endpoint - identical to TypeScript version"""
    return HealthCheckResponse(status="ok")

# ─── Utility Functions ───────────────────────────────────────────────────────
def escape_html(text: str) -> str:
    """Escape HTML special characters"""
    return html.escape(text)

async def is_group_admin_or_owner(application: Application, group_id: int, user_id: int) -> bool:
    """Check if user is admin or owner of group"""
    try:
        member = await application.bot.get_chat_member(group_id, user_id)
        return member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER, ChatMemberStatus.CREATOR]
    except:
        return False

async def is_channel_member(application: Application, user_id: int) -> bool:
    """Check if user is member of verification channel"""
    try:
        member = await application.bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
    except:
        return False

def mark_blocked(group_id: int, user_id: int, info: BlockedUserInfo):
    """Mark user as blocked in group"""
    if group_id not in blocked_users_per_group:
        blocked_users_per_group[group_id] = {}
    blocked_users_per_group[group_id][user_id] = info

def unmark_blocked(group_id: int, user_id: int):
    """Unmark user as blocked"""
    if group_id in blocked_users_per_group:
        blocked_users_per_group[group_id].pop(user_id, None)

def is_blocked_error(err: Exception) -> bool:
    """Check if error indicates user blocked bot"""
    msg = str(err).lower()
    return any(x in msg for x in [
        "bot was blocked by the user",
        "user is deactivated", 
        "chat not found"
    ])

def extract_media_from_message(msg: Update) -> Optional[WelcomeMedia]:
    """Extract media from Telegram message"""
    if msg.message.photo:
        largest = msg.message.photo[-1]
        return WelcomeMedia(file_id=largest.file_id, media_type="photo")
    if msg.message.video:
        return WelcomeMedia(file_id=msg.message.video.file_id, media_type="video")
    if msg.message.audio:
        return WelcomeMedia(file_id=msg.message.audio.file_id, media_type="audio")
    if msg.message.document:
        return WelcomeMedia(file_id=msg.message.document.file_id, media_type="document")
    if msg.message.animation:
        return WelcomeMedia(file_id=msg.message.animation.file_id, media_type="animation")
    if msg.message.sticker:
        return WelcomeMedia(file_id=msg.message.sticker.file_id, media_type="sticker")
    return None

# ─── Template Parsing (100% identical to TS) ─────────────────────────────────
def parse_template(
    raw: str, 
    user: Any, 
    chat: Any
) -> ParsedTemplate:
    """Parse welcome template with all directives - identical to TypeScript"""
    text = raw
    
    # Parse directives
    no_notif = "{nonotif}" in text
    protect_content = "{protect}" in text
    media_spoiler = "{mediaspoiler}" in text
    enable_preview = "{preview}" in text or "{preview:top}" in text
    preview_above = "{preview:top}" in text
    rules_new_row = "{rules}" in text and "{rules:same}" not in text
    rules_same_row = "{rules:same}" in text
    
    # Remove directives
    text = (text
        .replace("{nonotif}", "")
        .replace("{protect}", "")
        .replace("{mediaspoiler}", "")
        .replace("{preview:top}", "")
        .replace("{preview}", "")
        .replace("{rules:same}", "")
        .replace("{rules}", "")
        .strip()
    )
    
    # Template variables
    first_name = escape_html(user.first_name or "")
    last_name = escape_html(user.last_name or "")
    full_name = f"{first_name} {last_name}".strip() if last_name else first_name
    mention_html = f'<a href="tg://user?id={user.id}">{first_name}</a>'
    username_display = f"@{user.username}" if user.username else mention_html
    chat_name = escape_html(chat.title or getattr(chat, 'first_name', 'this chat') or "this chat")
    
    text = (text
        .replace("{first}", first_name)
        .replace("{last}", last_name)
        .replace("{fullname}", full_name)
        .replace("{username}", username_display)
        .replace("{mention}", mention_html)
        .replace("{id}", str(user.id))
        .replace("{chatname}", chat_name)
    )
    
    return ParsedTemplate(
        text=text,
        no_notif=no_notif,
        protect_content=protect_content,
        media_spoiler=media_spoiler,
        enable_preview=enable_preview,
        preview_above=preview_above,
        rules_new_row=rules_new_row,
        rules_same_row=rules_same_row
    )

# ─── CHECKPOINT 1 COMPLETE (200 lines) ───────────────────────────────────────
def build_keyboard(
    parsed: ParsedTemplate,
    rules_link: Optional[str] = None,
    extra_rows: Optional[List[List[InlineKeyboardButton]]] = None
) -> List[List[InlineKeyboardButton]]:
    """Build inline keyboard - identical to TypeScript"""
    keyboard = extra_rows.copy() if extra_rows else []
    
    if rules_link:
        rules_btn = InlineKeyboardButton(text="📋 Rules", url=rules_link)
        
        if parsed.rules_same_row:
            if keyboard and keyboard[-1]:
                keyboard[-1].append(rules_btn)
            else:
                keyboard.append([rules_btn])
        elif parsed.rules_new_row:
            keyboard.append([rules_btn])
    
    return keyboard

async def send_from_config(
    application: Application,
    chat_id: int,
    config: WelcomeConfig,
    user: Any,
    chat: Any,
    rules_link: Optional[str] = None,
    extra_keyboard_rows: Optional[List[List[InlineKeyboardButton]]] = None
):
    """Send welcome message from config - 100% identical to TS"""
    raw_text = config.text or ""
    parsed = parse_template(raw_text, user, chat)
    keyboard = build_keyboard(parsed, rules_link, extra_keyboard_rows)
    
    base_options = {
        "parse_mode": ParseMode.HTML,
    }
    
    if parsed.no_notif:
        base_options["disable_notification"] = True
    if parsed.protect_content:
        base_options["protect_content"] = True
    if not parsed.enable_preview:
        base_options["link_preview_options"] = {"is_disabled": True}
    if keyboard:
        base_options["reply_markup"] = InlineKeyboardMarkup(keyboard)
    
    media = config.media
    
    if not media:
        await application.bot.send_message(chat_id, parsed.text or "👋", **base_options)
        return
    
    caption_options = base_options.copy()
    caption_options["caption"] = parsed.text or None
    
    if parsed.media_spoiler:
        caption_options["has_spoiler"] = True
    
    try:
        if media.media_type == "photo":
            await application.bot.send_photo(chat_id, media.file_id, **caption_options)
        elif media.media_type == "video":
            await application.bot.send_video(chat_id, media.file_id, **caption_options)
        elif media.media_type == "audio":
            await application.bot.send_audio(chat_id, media.file_id, **caption_options)
        elif media.media_type == "document":
            await application.bot.send_document(chat_id, media.file_id, **caption_options)
        elif media.media_type == "animation":
            await application.bot.send_animation(chat_id, media.file_id, **caption_options)
        elif media.media_type == "sticker":
            sticker_options = {
                **{k: v for k, v in base_options.items() if k in ["disable_notification", "protect_content"]},
            }
            await application.bot.send_sticker(chat_id, media.file_id, **sticker_options)
            if parsed.text:
                await application.bot.send_message(chat_id, parsed.text, **base_options)
    except Exception as e:
        logger.warning(f"Failed to send media welcome: {e}", chat_id=chat_id)
        # Fallback to text
        await application.bot.send_message(chat_id, parsed.text or "👋", **base_options)

async def send_channel_join_prompt(application: Application, chat_id: int):
    """Send channel join verification prompt"""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(text="K𝚒𝚗𝚐 𝙹𝙾𝚈𝙱𝙾𝚈", url=CHANNEL_LINK)],
        [InlineKeyboardButton(text="✔️ ᎷᎬᎡᎶᎬᎠ", callback_data=f"check_channel:{chat_id}")],
    ])
    
    await application.bot.send_message(
        chat_id,
        "💕 𝘫𝗼𝗶𝗻 𝗼𝘂𝗿 Channel 𝗳𝗼𝗿 𝗰𝗵𝗮𝘁𝘀 & 𝘂𝗽𝗱𝗮𝘁𝗲𝘀. ╰(*°▽°*)╯",
        reply_markup=keyboard
    )

async def build_default_welcome_caption(
    application: Application,
    group_id: int,
    user_id: int,
    first_name: str,
    last_name: Optional[str] = None
) -> str:
    """Build default welcome caption with group info"""
    full_name = f"{first_name} {last_name}".strip() if last_name else first_name
    user_mention = f'<a href="tg://user?id={user_id}">{html.escape(full_name)}</a>'
    
    chat_title = "the group"
    owner_name = "—"
    admins_text = "—"
    bio = ""
    
    try:
        chat = await application.bot.get_chat(group_id)
        chat_title = chat.title or chat_title
        bio = getattr(chat, 'description', '') or ""
    except:
        pass
    
    try:
        admins = await application.bot.get_chat_administrators(group_id)
        owner = next((a for a in admins if a.status == ChatMemberStatus.OWNER), None)
        admin_list = [a for a in admins if a.status == ChatMemberStatus.ADMINISTRATOR]
        
        if owner:
            owner_name = html.escape(f"{owner.user.first_name} {owner.user.last_name or ''}".strip())
        admins_text = ", ".join(html.escape(a.user.first_name) for a in admin_list[:5]) or "—"
    except:
        pass
    
    bio_line = f'\n💬 "{html.escape(bio)}"\n' if bio else ""
    
    return f"""
✨ 𝑾𝑬𝑳𝑪𝑶𝑴𝑬 ✨

👤 {user_mention}
Welcome to <b>{html.escape(chat_title)}</b>

👑 Owner: {owner_name}
🛡 Admins: {admins_text}
{bio_line}
⚡ Stay active • Respect all • Enjoy"""

async def send_dm_welcome(
    application: Application,
    user_id: int,
    group_id: int,
    user: Any
) -> bool:
    """Send DM welcome to new member"""
    rules_link = group_rules_link.get(group_id)
    
    try:
        chat = await application.bot.get_chat(group_id)
    except:
        chat = type('Chat', (), {'id': group_id, 'title': 'Group'})()
    
    # Custom DM config
    dm_config = group_dm_welcome_config.get(group_id)
    if dm_config:
        try:
            await send_from_config(application, user_id, dm_config, user, chat, rules_link)
            logger.info("Sent custom DM welcome", user_id=user_id, group_id=group_id)
            return True
        except Exception as e:
            if is_blocked_error(e):
                return False
            logger.warning("Custom DM failed, trying default", user_id=user_id)
    
    # Default welcome
    caption = await build_default_welcome_caption(
        application, group_id, user_id, user.first_name, user.last_name
    )
    
    try:
        await application.bot.send_video(
            user_id, live_group_welcome_video_file_id, 
            caption=caption, parse_mode=ParseMode.HTML
        )
        logger.info("Sent default video DM", user_id=user_id, group_id=group_id)
    except Exception as e:
        if is_blocked_error(e):
            return False
        logger.warning("Video failed, trying text", user_id=user_id)
        try:
            await application.bot.send_message(user_id, caption, parse_mode=ParseMode.HTML)
            logger.info("Sent text DM fallback", user_id=user_id)
        except Exception as e2:
            if is_blocked_error(e2):
                return False
            logger.warning("DM failed completely", user_id=user_id, error=str(e2))
            return False
    
    # Unblock and send start instruction
    unmark_blocked(group_id, user_id)
    try:
        await application.bot.send_message(
            user_id, "👆 Press /start to use the bot and unlock more features!"
        )
    except:
        pass
    
    return True

async def send_group_welcome_in_group(
    application: Application,
    group_id: int,
    user: Any
):
    """Send welcome message inside group chat"""
    config = group_welcome_config.get(group_id)
    if not config:
        return
    
    rules_link = group_rules_link.get(group_id)
    
    try:
        chat = await application.bot.get_chat(group_id)
    except:
        chat = type('Chat', (), {'id': group_id})()
    
    try:
        await send_from_config(application, group_id, config, user, chat, rules_link)
        logger.info("Sent group welcome", user_id=user.id, group_id=group_id)
    except Exception as e:
        logger.warning("Group welcome failed", user_id=user.id, group_id=group_id, error=str(e))

async def tag_blocked_users_in_group(application: Application, group_id: int):
    """Tag blocked users in group chat"""
    blocked = blocked_users_per_group.get(group_id, {})
    if not blocked:
        return
    
    entries = list(blocked.items())
    prefix = "🔔 "
    suffix = (
        "\n\n⚠️ Important: You haven't started the bot in DM yet!\n\n"
        "The bot cannot send you welcome messages and important group updates.\n\n"
        f"👉 Click @{bot_username} → press Start — it only takes 2 seconds! 🚀"
    )
    
    text = prefix
    entities = []
    
    for uid, info in entries:
        name = f"{info.first_name} {info.last_name or ''}".strip()
        offset = len(text)
        entities.append({
            "type": "text_mention",
            "offset": offset,
            "length": len(name),
            "user": {"id": uid, "is_bot": False, "first_name": info.first_name}
        })
        text += name + " "
    
    text += suffix
    
    try:
        await application.bot.send_message(group_id, text, entities=entities)
    except Exception as e:
        logger.warning("Failed to tag blocked users", group_id=group_id, error=str(e))

# ─── Template Help Text (identical) ──────────────────────────────────────────
TEMPLATE_HELP = """
<b>📝 Template Variables:</b>
<code>{first}</code> — First name
<code>{last}</code> — Last name
<code>{fullname}</code> — Full name
<code>{username}</code> — @username (or mention if none)
<code>{mention}</code> — Clickable first name
<code>{id}</code> — User ID
<code>{chatname}</code> — Chat/group name

<b>⚙️ Directives (added anywhere in text):</b>
<code>{rules}</code> — Adds a Rules button (new row)
<code>{rules:same}</code> — Adds a Rules button (same row as previous)
<code>{preview}</code> — Enable link preview
<code>{preview:top}</code> — Link preview above text
<code>{nonotif}</code> — Send silently (no notification)
<code>{protect}</code> — Protect from forwarding/screenshot
<code>{mediaspoiler}</code> — Mark media as spoiler

<b>📌 Example:</b>
<code>Hello {mention}! Welcome to {chatname} 🎉{nonotif}</code>

<b>📎 To set media:</b> Reply to a photo/video/audio/file with the command and your template text as the message caption."""

# ─── CHECKPOINT 2 COMPLETE (Lines 201-400) ───────────────────────────────────
# ─── Bot Handlers ────────────────────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command - identical to TypeScript"""
    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = user.id if user else chat_id
    first_name = user.first_name or "there"
    
    if update.effective_chat.type != "private":
        me = await context.bot.get_me()
        try:
            await context.bot.send_message(
                chat_id,
                f"👋 Hey {first_name}! Please start the bot in DM first.\n👉 Click @{me.username} and press Start.",
                reply_to_message_id=update.message.message_id
            )
        except:
            pass
        return
    
    started_in_dm.add(user_id)
    
    default_caption = (
        f"🕸️ 𝑾𝒆𝒍𝒄𝒐𝒎𝒆, <b>{first_name}</b> 𝒕𝒐 𝑶𝒖𝒓 𝑪𝒐𝒎𝒎𝒖𝒏𝒊𝒕𝒚 🕸️\n\n"
        "✦ <b>ᎶᎾᏚᎢ ᏟᎡᎬᎳ</b> — ʏᴏᴜʀ ɢʀᴏᴜᴘ ɢᴜᴀʀᴅɪᴀɴ 🛡️\n\n"
        "⚡ ᴀᴜᴛᴏ-ᴀᴘᴘʀᴏᴠᴇ  ·  👋 ᴡᴇʟᴄᴏᴍᴇꜱ  ·  🛡️ ᴀᴅᴍɪɴ ᴛᴏᴏʟꜱ"
    )
    
    caption = fill_start_caption(start_welcome_caption or default_caption, user)
    
    start_buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ ᴀᴅᴅ ᴍᴇ ɪɴ ʏᴏᴜʀ ɢʀᴏᴜᴘ ➕", url=f"https://t.me/{bot_username}?startgroup=true")],
        [
            InlineKeyboardButton("「 ɢʀᴏᴜᴘ 」", url="https://t.me/+9-l1zaYpo5YyNjJl"),
            InlineKeyboardButton("「 ᴜᴘᴅᴀᴛᴇꜱ 」", url="https://t.me/+5jFYIjx_KcdkM2Jl"),
        ],
        [InlineKeyboardButton("✦ ʜᴇʟᴘ & ᴄᴏᴍᴍᴀɴᴅꜱ ✦", callback_data="help_commands")],
    ])
    
    try:
        await context.bot.send_video(
            chat_id, START_WELCOME_VIDEO_FILE_ID,
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=start_buttons
        )
    except Exception as e:
        logger.warning("Start video failed", error=str(e))
        try:
            await context.bot.send_message(
                chat_id, caption,
                parse_mode=ParseMode.HTML,
                reply_markup=start_buttons
            )
        except:
            pass

def fill_start_caption(template: str, user: Any) -> str:
    """Fill /start caption template"""
    first_name = html.escape(user.first_name or "")
    last_name = html.escape(user.last_name or "")
    full_name = f"{first_name} {last_name}".strip() if last_name else first_name
    mention = f'<a href="tg://user?id={user.id}">{first_name}</a>'
    username = f"@{user.username}" if user.username else mention
    
    return (template
        .replace("{first}", first_name)
        .replace("{last}", last_name)
        .replace("{fullname}", full_name)
        .replace("{mention}", mention)
        .replace("{username}", username)
        .replace("{id}", str(user.id))
    )

async def owner_welcome_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner-only /owner_welcome command"""
    user_id = update.effective_user.id if update.effective_user else 0
    if user_id != OWNER_ID:
        return  # Silently ignore
    
    chat_id = update.effective_chat.id
    reply_msg = update.message.reply_to_message
    match = re.match(r"/(?:!)?owner_welcome(?:\s+([\s\S]+))?", update.message.text or "")
    
    if not reply_msg or not reply_msg.video:
        help_text = (
            "📹 Reply to a video with <code>/owner_welcome your caption here</code> to set the /start welcome.\n\n"
            "<b>Supported fillings:</b>\n"
            "<code>{first}</code> — First name\n"
            "<code>{last}</code> — Last name\n"
            "<code>{fullname}</code> — Full name\n"
            "<code>{mention}</code> — Clickable first name\n"
            "<code>{username}</code> — @username (or mention if none)\n"
            "<code>{id}</code> — User ID"
        )
        await context.bot.send_message(chat_id, help_text, parse_mode=ParseMode.HTML)
        return
    
    global START_WELCOME_VIDEO_FILE_ID, start_welcome_caption
    START_WELCOME_VIDEO_FILE_ID = reply_msg.video.file_id
    start_welcome_caption = match.group(1) or reply_msg.caption or ""
    
    logger.info("Owner updated start welcome", file_id=START_WELCOME_VIDEO_FILE_ID)
    
    preview = start_welcome_caption or "(default caption with user's first name)"
    await context.bot.send_message(
        chat_id,
        f"✅ /start welcome updated!\n\n📹 New video set.\n✏️ Caption: <code>{html.escape(preview)}</code>",
        parse_mode=ParseMode.HTML
    )

async def msg_owner_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forward user message to owner"""
    chat_id = update.effective_chat.id
    user = update.effective_user
    if not user:
        return
    
    match = re.match(r"/(?:!)?msg_owner(?:\s+([\s\S]+))?", update.message.text or "")
    user_msg = match.group(1)
    
    if not user_msg:
        await context.bot.send_message(
            chat_id,
            "❌ Please include a message.\n\nUsage: <code>/msg_owner your message here</code>",
            parse_mode=ParseMode.HTML,
            reply_to_message_id=update.message.message_id
        )
        return
    
    first_name = html.escape(user.first_name or "")
    last_name = f" {html.escape(user.last_name or '')}" if user.last_name else ""
    full_name = f"{first_name}{last_name}"
    username = f"@{user.username}" if user.username else "—"
    chat_info = (
        "Direct Message" if update.effective_chat.type == "private"
        else f"{html.escape(update.effective_chat.title or 'Unknown')} ({update.effective_chat.type})"
    )
    
    forward_text = (
        f"📩 <b>New message from user</b>\n\n"
        f"👤 <b>Name:</b> {full_name}\n"
        f"🔖 <b>Username:</b> {username}\n"
        f"🆔 <b>ID:</b> <code>{user.id}</code>\n"
        f"💬 <b>From:</b> {chat_info}\n\n"
        f"✉️ <b>Message:</b>\n{html.escape(user_msg)}"
    )
    
    try:
        await context.bot.send_message(OWNER_ID, forward_text, parse_mode=ParseMode.HTML)
        
        # Forward reply context if exists
        if update.message.reply_to_message:
            await context.bot.forward_message(
                OWNER_ID, chat_id, update.message.reply_to_message.message_id
            )
        
        await context.bot.send_message(
            chat_id,
            "✅ Your message has been sent to the owner. They'll get back to you soon!",
            reply_to_message_id=update.message.message_id
        )
        logger.info("User messaged owner", user_id=user.id)
    except Exception as e:
        logger.warning("Failed to forward to owner", error=str(e))
        await context.bot.send_message(
            chat_id,
            "❌ Could not deliver your message. Please try again later.",
            reply_to_message_id=update.message.message_id
        )

async def setwelcome_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin /setwelcome command (group welcome)"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    
    if update.effective_chat.type == "private":
        await context.bot.send_message(chat_id, "❌ Use /setdmwelcome to set the DM welcome message.")
        return
    
    is_admin = await is_group_admin_or_owner(context.application, chat_id, user_id)
    if not is_admin:
        await context.bot.send_message(chat_id, "❌ Only admins can set the welcome message.")
        return
    
    reply_msg = update.message.reply_to_message
    match = re.match(r"/(?:!)?setwelcome(?:\s+([\s\S]+))?", update.message.text or "")
    text_arg = match.group(1)
    
    text = None
    media = None
    
    if reply_msg:
        media = extract_media_from_message(update)
        text = reply_msg.caption or reply_msg.text or text_arg
    else:
        text = text_arg
    
    if not text and not media:
        help_text = (
            "ℹ️ <b>How to set group welcome:</b>\n\n"
            "• <code>/setwelcome Hello {mention}! Welcome to {chatname} 🎉</code>\n"
            "• Or reply to a photo/video/file with <code>/setwelcome your text here</code>\n\n"
            f"{TEMPLATE_HELP}"
        )
        await context.bot.send_message(chat_id, help_text, parse_mode=ParseMode.HTML)
        return
    
    group_welcome_config[chat_id] = WelcomeConfig(text=text, media=media)
    logger.info("Group welcome updated", chat_id=chat_id)
    
    await context.bot.send_message(
        chat_id,
        "✅ Group welcome message updated! It will be sent in the group when someone joins.",
        parse_mode=ParseMode.HTML
    )

async def setdmwelcome_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin /setdmwelcome command (private DM welcome)"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    
    if update.effective_chat.type == "private":
        await context.bot.send_message(chat_id, "❌ Use this command inside the group you want to configure.")
        return
    
    is_admin = await is_group_admin_or_owner(context.application, chat_id, user_id)
    if not is_admin:
        await context.bot.send_message(chat_id, "❌ Only admins can set the DM welcome message.")
        return
    
    reply_msg = update.message.reply_to_message
    match = re.match(r"/(?:!)?setdmwelcome(?:\s+([\s\S]+))?", update.message.text or "")
    text_arg = match.group(1)
    
    text = None
    media = None
    
    if reply_msg:
        media = extract_media_from_message(update)
        text = reply_msg.caption or reply_msg.text or text_arg
    else:
        text = text_arg
    
    if not text and not media:
        help_text = (
            "ℹ️ <b>How to set the DM welcome (sent privately to new joiners):</b>\n\n"
            "• <code>/setdmwelcome Hello {first}! Welcome to {chatname} 🎉</code>\n"
            "• Or reply to a photo/video/file with <code>/setdmwelcome your text here</code>\n\n"
            f"{TEMPLATE_HELP}"
        )
        await context.bot.send_message(chat_id, help_text, parse_mode=ParseMode.HTML)
        return
    
    group_dm_welcome_config[chat_id] = WelcomeConfig(text=text, media=media)
    logger.info("Group DM welcome updated", chat_id=chat_id)
    
    await context.bot.send_message(
        chat_id,
        "✅ DM welcome updated! New members who join this group will receive this message privately.",
        parse_mode=ParseMode.HTML
    )

# ─── CHECKPOINT 3 COMPLETE (Lines 401-600) ───────────────────────────────────
async def resetwelcome_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin /resetwelcome command"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    
    if update.effective_chat.type == "private":
        await context.bot.send_message(chat_id, "❌ Use /resetdmwelcome to reset the DM welcome.")
        return
    
    is_admin = await is_group_admin_or_owner(context.application, chat_id, user_id)
    if not is_admin:
        await context.bot.send_message(chat_id, "❌ Only admins can reset the welcome message.")
        return
    
    group_welcome_config.pop(chat_id, None)
    await context.bot.send_message(chat_id, "✅ Group welcome reset to default.")

async def resetdmwelcome_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin /resetdmwelcome command"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    
    if update.effective_chat.type == "private":
        await context.bot.send_message(chat_id, "❌ Use this command inside the group you want to configure.")
        return
    
    is_admin = await is_group_admin_or_owner(context.application, chat_id, user_id)
    if not is_admin:
        await context.bot.send_message(chat_id, "❌ Only admins can reset the DM welcome.")
        return
    
    group_dm_welcome_config.pop(chat_id, None)
    await context.bot.send_message(chat_id, "✅ DM welcome for this group reset to default.")

async def getwelcome_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin /getwelcome command"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    
    if update.effective_chat.type == "private":
        await context.bot.send_message(chat_id, "❌ Use /getdmwelcome to view the DM welcome.")
        return
    
    is_admin = await is_group_admin_or_owner(context.application, chat_id, user_id)
    if not is_admin:
        await context.bot.send_message(chat_id, "❌ Only admins can view the welcome settings.")
        return
    
    config = group_welcome_config.get(chat_id)
    if not config:
        await context.bot.send_message(chat_id, "ℹ️ No custom group welcome set. Using the default welcome message.")
        return
    
    media_info = ""
    if config.media:
        media_info = f"\n📎 Media: {config.media.media_type} (<code>{config.media.file_id[:20]}…</code>)"
    
    await context.bot.send_message(
        chat_id,
        f"<b>Current group welcome template:</b>\n\n<code>{html.escape(config.text or '(no text)')}</code>{media_info}",
        parse_mode=ParseMode.HTML
    )

async def getdmwelcome_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin /getdmwelcome command"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    
    if update.effective_chat.type == "private":
        await context.bot.send_message(chat_id, "❌ Use this command inside the group you want to check.")
        return
    
    is_admin = await is_group_admin_or_owner(context.application, chat_id, user_id)
    if not is_admin:
        await context.bot.send_message(chat_id, "❌ Only admins can view the DM welcome settings.")
        return
    
    config = group_dm_welcome_config.get(chat_id)
    if not config:
        await context.bot.send_message(chat_id, "ℹ️ No custom DM welcome set for this group. Using the default welcome.")
        return
    
    media_info = ""
    if config.media:
        media_info = f"\n📎 Media: {config.media.media_type} (<code>{config.media.file_id[:20]}…</code>)"
    
    await context.bot.send_message(
        chat_id,
        f"<b>Current DM welcome template for this group:</b>\n\n<code>{html.escape(config.text or '(no text)')}</code>{media_info}",
        parse_mode=ParseMode.HTML
    )

async def setrules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin /setrules command"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    
    if update.effective_chat.type == "private":
        await context.bot.send_message(chat_id, "❌ This command only works inside a group.")
        return
    
    is_admin = await is_group_admin_or_owner(context.application, chat_id, user_id)
    if not is_admin:
        await context.bot.send_message(chat_id, "❌ Only admins can set the rules link.")
        return
    
    match = re.match(r"/(?:!)?setrules(?:\s+(\S+))?", update.message.text or "")
    link = match.group(1)
    
    if not link:
        current = group_rules_link.get(chat_id)
        msg = (
            f"ℹ️ Current rules link: {current}\n\nTo change: <code>/setrules https://t.me/...</code>"
            if current else
            "ℹ️ No rules link set.\n\nUsage: <code>/setrules https://t.me/...</code>"
        )
        await context.bot.send_message(chat_id, msg, parse_mode=ParseMode.HTML)
        return
    
    group_rules_link[chat_id] = link
    await context.bot.send_message(
        chat_id,
        "✅ Rules link set! Use <code>{rules}</code> or <code>{rules:same}</code> in your welcome template to show it as a button.",
        parse_mode=ParseMode.HTML
    )

async def delrules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin /delrules command"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    
    if update.effective_chat.type == "private":
        await context.bot.send_message(chat_id, "❌ This command only works inside a group.")
        return
    
    is_admin = await is_group_admin_or_owner(context.application, chat_id, user_id)
    if not is_admin:
        await context.bot.send_message(chat_id, "❌ Only admins can remove the rules link.")
        return
    
    group_rules_link.pop(chat_id, None)
    await context.bot.send_message(chat_id, "✅ Rules link removed.")

async def auto_approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin /auto_approve command"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    
    if update.effective_chat.type == "private":
        await context.bot.send_message(chat_id, "❌ This command only works inside a group.")
        return
    
    if user_id not in started_in_dm:
        me = await context.bot.get_me()
        await context.bot.send_message(
            chat_id,
            f"⚠️ Start the bot in DM first.\n👉
Click @{me.username} and press Start.",
            reply_to_message_id=update.message.message_id
        )
        return
    
    is_admin = await is_group_admin_or_owner(context.application, chat_id, user_id)
    if not is_admin:
        await context.bot.send_message(chat_id, "❌ Only admins and the owner can use this command.")
        return
    
    match = re.match(r"/(?:!)?auto_approve(?:\s+(on|off))?", update.message.text or "")
    arg = match.group(1)
    
    if not arg:
        current = auto_approve_enabled.get(chat_id, True)
        await context.bot.send_message(
            chat_id,
            f"ℹ️ Auto-approve is currently <b>{'ON' if current else 'OFF'}</b>.\nUsage: /auto_approve on | off",
            parse_mode=ParseMode.HTML
        )
        return
    
    enable = arg.lower() == "on"
    auto_approve_enabled[chat_id] = enable
    logger.info("Auto-approve toggled", chat_id=chat_id, enable=enable)
    
    status = "ON — join requests will be approved automatically." if enable else "OFF — join requests will not be approved."
    await context.bot.send_message(
        chat_id,
        f"✅ Auto-approve is now <b>{'ON' if enable else 'OFF'}</b>\n🔴 {status}",
        parse_mode=ParseMode.HTML
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help and commands menu"""
    chat_id = update.effective_chat.id
    from_id = update.effective_user.id if update.effective_user else 0
    
    owner_line = "\n🔑 /owner_welcome — Set the /start welcome video" if from_id == OWNER_ID else ""
    
    text = (
        "<b>Help</b>\n\n"
        "Hello! ✨\n"
        "I am <b>ᎶᎾᏚᎢ ᏟᎡᎬᎳ</b>, your trusty group guardian bot! 🛡️\n"
        "Here to help you manage your chats, keep things safe, and maintain order effortlessly.\n\n"
        "I come with powerful features:\n"
        "🚫 <b>Auto-Approve</b> – Instantly approve join requests.\n"
        "👋 <b>Welcome System</b> – Greet every new member beautifully.\n"
        "📩 <b>Owner Contact</b> – Members can reach the owner directly.\n"
        "🛡️ <b>Admin Tools</b> – Full group management controls.\n\n"
        "<b>Useful Commands:</b>\n"
        "/start – Activate me and see me in action.\n"
        "/help – Shows this helpful guide anytime.\n"
        "/msg_owner – Contact the bot owner directly."
        f"{owner_line}\n\n"
        "<i>All commands also work with <b>!</b> instead of <b>/</b></i>\n\n"
        "If you run into bugs or have questions, reach out at @Class11th_channel."
    )
    
    await context.bot.send_message(
        chat_id, text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("👋 Welcome Commands", callback_data="show_welcome_cmds")]
        ])
    )

# ─── Command Patterns (support both / and !) ──────────────────────────────────
COMMAND_PATTERNS = {
    "start": r"/(?:!)?start\b",
    "owner_welcome": r"/(?:!)?owner_welcome(?:\s+([\s\S]+))?",
    "msg_owner": r"/(?:!)?msg_owner(?:\s+([\s\S]+))?", 
    "setwelcome": r"/(?:!)?setwelcome(?:\s+([\s\S]+))?",
    "setdmwelcome": r"/(?:!)?setdmwelcome(?:\s+([\s\S]+))?",
    "resetwelcome": r"/(?:!)?resetwelcome\b",
    "resetdmwelcome": r"/(?:!)?resetdmwelcome\b",
    "getwelcome": r"/(?:!)?getwelcome\b",
    "getdmwelcome": r"/(?:!)?getdmwelcome\b",
    "setrules": r"/(?:!)?setrules(?:\s+(\S+))?",
    "delrules": r"/(?:!)?delrules\b",
    "auto_approve": r"/(?:!)?auto_approve(?:\s+(on|off))?",
    "help": r"/(?:!)?(help|commands)\b",
}

# ─── CHECKPOINT 4 COMPLETE (Lines 601-800) ───────────────────────────────────
async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle callback queries"""
    query = update.callback_query
    if not query:
        return
    
    try:
        data = query.data or ""
        await query.answer()
        
        if data == "show_welcome_cmds":
            welcome_text = (
                "<b>👋 Welcome Commands</b>\n"
                "<i>(use all of these inside your group)</i>\n\n"
                "<b>In-group welcome</b> — shown in the chat when someone joins:\n"
                "/setwelcome &lt;text&gt; — set welcome (or reply to media)\n"
                "/getwelcome — view current template\n"
                "/resetwelcome — reset to default\n\n"
                "<b>DM welcome</b> — sent privately to new joiners:\n"
                "/setdmwelcome &lt;text&gt; — set DM welcome (or reply to media)\n"
                "/getdmwelcome — view current template\n"
                "/resetdmwelcome — reset to default\n\n"
                "<b>Other welcome tools:</b>\n"
                "/setrules &lt;link&gt; — set rules URL for the {rules} button\n"
                "/delrules — remove rules link\n"
                "/auto_approve on|off — auto-approve join requests\n\n"
                "<b>📝 Template fillings:</b>\n"
                "<code>{first}</code> · <code>{last}</code> · <code>{fullname}</code>\n"
                "<code>{mention}</code> · <code>{username}</code> · <code>{id}</code> · <code>{chatname}</code>\n"
                "<code>{rules}</code> · <code>{rules:same}</code> · <code>{nonotif}</code>\n"
                "<code>{protect}</code> · <code>{mediaspoiler}</code> · <code>{preview}</code>\n\n"
                "<i>All commands also work with <b>!</b> instead of <b>/</b></i>"
            )
            
            await context.bot.send_message(
                query.message.chat.id,
                welcome_text,
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Back to Help", callback_data="show_help")]
                ])
            )
            return
        
        if data in ["show_help", "help_commands"]:
            chat_id = query.message.chat.id
            from_id = query.from_user.id
            
            owner_line = "\n🔑 /owner_welcome — Set the /start welcome video" if from_id == OWNER_ID else ""
            
            help_text = (
                "<b>Help</b>\n\n"
                "Hello! ✨\n"
                "I am <b>ᎶᎾᏚᎢ ᏟᎡᎬᎳ</b>, your trusty group guardian bot! 🛡️\n"
                "Here to help you manage your chats, keep things safe, and maintain order effortlessly.\n\n"
                "I come with powerful features:\n"
                "🚫 <b>Auto-Approve</b> – Instantly approve join requests.\n"
                "👋 <b>Welcome System</b> – Greet every new member beautifully.\n"
                "📩 <b>Owner Contact</b> – Members can reach the owner directly.\n"
                "🛡️ <b>Admin Tools</b> – Full group management controls.\n\n"
                "<b>Useful Commands:</b>\n"
                "/start – Activate me and see me in action.\n"
                "/help – Shows this helpful guide anytime.\n"
                "/msg_owner – Contact the bot owner directly."
                f"{owner_line}\n\n"
                "<i>All commands also work with <b>!</b> instead of <b>/</b></i>\n\n"
                "If you run into bugs or have questions, reach out at @Class11th_channel."
            )
            
            await context.bot.send_message(
                chat_id, help_text,
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("👋 Welcome Commands", callback_data="show_welcome_cmds")]
                ])
            )
            
    except Exception as e:
        logger.warning("Callback query error", error=str(e))

async def chat_join_request_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle chat join requests (auto-approve)"""
    req = update.chat_join_request
    if not req:
        return
    
    user_id = req.from_user.id
    group_id = req.chat.id
    
    should_approve = auto_approve_enabled.get(group_id, True)
    if not should_approve:
        logger.info("Auto-approve OFF", user_id=user_id, group_id=group_id)
        return
    
    try:
        await context.bot.approve_chat_join_request(group_id, user_id)
        logger.info("Auto-approved join request", user_id=user_id, group_id=group_id)
        
        dm_sent = await send_dm_welcome(context.application, user_id, group_id, req.from_user)
        if not dm_sent:
            mark_blocked(group_id, user_id, BlockedUserInfo(
                first_name=req.from_user.first_name or "",
                last_name=req.from_user.last_name
            ))
    except Exception as e:
        logger.error("Join request approval failed", user_id=user_id, group_id=group_id, error=str(e))

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """General message handler"""
    msg = update.message
    if not msg:
        return
    
    global live_group_welcome_video_file_id
    
    # Live update default welcome video (DM video messages)
    if msg.video and msg.chat.type == "private":
        live_group_welcome_video_file_id = msg.video.file_id
        logger.info("Default group welcome video updated live", file_id=live_group_welcome_video_file_id)
    
    # New chat members (direct join, not join request)
    if msg.new_chat_members:
        group_id = msg.chat.id
        for member in msg.new_chat_members:
            if member.is_bot:
                continue
            
            # Group welcome
            await send_group_welcome_in_group(context.application, group_id, member)
            
            # DM welcome
            dm_sent = await send_dm_welcome(context.application, member.id, group_id, member)
            if not dm_sent:
                mark_blocked(group_id, member.id, BlockedUserInfo(
                    first_name=member.first_name or "",
                    last_name=member.last_name
                ))
        return
    
    # Group message counter for blocked user tagging
    is_group = msg.chat.type in ["group", "supergroup"]
    if is_group:
        group_id = msg.chat.id
        user_id = msg.from_user.id if msg.from_user else 0
        
        if not user_id or msg.from_user.is_bot:
            return
        
        # Unblock if user started bot in DM
        if user_id in started_in_dm:
            unmark_blocked(group_id, user_id)
        
        # Message counter for tagging blocked users
        count = group_message_count.get(group_id, 0) + 1
        group_message_count[group_id] = count
        
        if count % 50 == 0:
            await tag_blocked_users_in_group(context.application, group_id)

# ─── Command Dispatcher ──────────────────────────────────────────────────────
async def command_dispatcher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Universal command handler matching all patterns"""
    text = update.message.text or ""
    
    for cmd_name, pattern in COMMAND_PATTERNS.items():
        if re.match(pattern, text):
            if cmd_name == "start":
                await start_command(update, context)
            elif cmd_name == "owner_welcome":
                await owner_welcome_command(update, context)
            elif cmd_name == "msg_owner":
                await msg_owner_command(update, context)
            elif cmd_name == "setwelcome":
                await setwelcome_command(update, context)
            elif cmd_name == "setdmwelcome":
                await setdmwelcome_command(update, context)
            elif cmd_name == "resetwelcome":
                await resetwelcome_command(update, context)
            elif cmd_name == "resetdmwelcome":
                await resetdmwelcome_command(update, context)
            elif cmd_name == "getwelcome":
                await getwelcome_command(update, context)
            elif cmd_name == "getdmwelcome":
                await getdmwelcome_command(update, context)
            elif cmd_name == "setrules":
                await setrules_command(update, context)
            elif cmd_name == "delrules":
                await delrules_command(update, context)
            elif cmd_name == "auto_approve":
                await auto_approve_command(update, context)
            elif cmd_name == "help":
                await help_command(update, context)
            break

# ─── Error Handler ──────────────────────────────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Handle telegram bot errors"""
    logger.error("Telegram error", update=update, error=context.error)

# ─── Bot Startup ─────────────────────────────────────────────────────────────
async def start_captcha_bot(token: str):
    """Start Telegram bot - identical to TypeScript startCaptchaBot"""
    global bot_username
    
    application = Application.builder().token(token).build()
    
    # Get bot info
    me = await application.bot.get_me()
    bot_username = me.username or ""
    logger.info("Telegram bot started", bot_username=bot_username)
    
    # Handlers
    application.add_handler(MessageHandler(filters.Regex(r".*"), command_dispatcher))
    application.add_handler(CallbackQueryHandler(callback_query_handler))
    application.add_handler(ChatJoinRequestHandler(chat_join_request_handler))
    application.add_error_handler(error_handler)
    
    # Start polling
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    
    logger.info("Bot polling started successfully")
    return application

# ─── Graceful Shutdown ───────────────────────────────────────────────────────
def signal_handler(signum, frame):
    """Handle shutdown signals"""
    logger.info("Shutdown signal received", signal=signum)
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ─── CHECKPOINT 5 COMPLETE (Lines 801-1000) ──────────────────────────────────
# ─── Main Server Functions ───────────────────────────────────────────────────
async def start_servers():
    """Start both web server and Telegram bot"""
    raw_port = os.getenv("PORT")
    if not raw_port:
        raise RuntimeError("PORT environment variable is required")
    
    try:
        port = int(raw_port)
        if port <= 0 or port > 65535:
            raise ValueError(f"Invalid PORT: {raw_port}")
    except ValueError as e:
        raise RuntimeError(f"Invalid PORT value: '{raw_port}'") from e
    
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        logger.warning("TELEGRAM_BOT_TOKEN not set — Telegram bot will not start")
    
    # Start tasks
    tasks = []
    
    # Web server config
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level="warning",  # Use our custom logging
        access_log=False,
    )
    uvicorn_server = uvicorn.Server(config)
    
    # Start bot if token provided
    bot_application = None
    if bot_token:
        bot_application = asyncio.create_task(start_captcha_bot(bot_token))
        tasks.append(bot_application)
    
    logger.info("Starting servers", port=port, bot_enabled=bool(bot_token))
    
    # Start web server
    web_task = asyncio.create_task(uvicorn_server.serve())
    tasks.append(web_task)
    
    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        logger.info("Shutting down gracefully...")
    finally:
        if bot_application:
            bot_application.cancel()
        await uvicorn_server.shutdown()

def main():
    """Main entry point - identical to TypeScript index.ts"""
    try:
        asyncio.run(start_servers())
    except KeyboardError:
        logger.info("Shutdown complete")
    except Exception as e:
        logger.error("Fatal error", error=str(e))
        sys.exit(1)

if __name__ == "__main__":
    main()

            