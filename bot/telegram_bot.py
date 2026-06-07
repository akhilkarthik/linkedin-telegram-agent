import os
import re
import asyncio
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from llm.groq_client import chat, parse_datetime
from linkedin.poster import post_to_linkedin
from db.schedule_store import add_post, get_pending, cancel_post
from db.workspace import load_workspace, save_workspace, add_item, get_items_context, get_item_by_type
from utils.url_fetcher import fetch_article
from utils.gmail_sender import send_email
from utils.notion_client import create_page as notion_create_page

_LAST_POST = "last_post"
_LAST_EMAIL = "last_email"
_AWAITING_SCHEDULE = "awaiting_schedule"
_WS = "_workspace"
_WS_SHA = "_workspace_sha"
IST = timezone(timedelta(hours=5, minutes=30))
URL_RE = re.compile(r'https?://[^\s]+')


# ── Workspace helpers ──────────────────────────────────────────────────────────

async def _get_workspace(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> dict:
    if _WS not in context.user_data:
        try:
            ws, sha = await asyncio.to_thread(load_workspace, user_id)
        except Exception:
            ws, sha = {"user_id": user_id, "history": [], "items": []}, None
        context.user_data[_WS] = ws
        context.user_data[_WS_SHA] = sha
    return context.user_data[_WS]


async def _save_ws(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    ws = context.user_data.get(_WS)
    if not ws:
        return
    try:
        sha = context.user_data.get(_WS_SHA)
        new_sha = await asyncio.to_thread(save_workspace, user_id, ws, sha)
        context.user_data[_WS_SHA] = new_sha
    except Exception as e:
        print(f"Workspace save failed: {e}")


# ── Extraction helpers ─────────────────────────────────────────────────────────

def _extract_post(text: str):
    match = re.search(r'<linkedin_post>(.*?)</linkedin_post>', text, re.DOTALL)
    if match:
        post = match.group(1).strip()
        return post, text.replace(match.group(0), '').strip(), None
    match = re.search(r'<schedule_post datetime="([^"]+)">(.*?)</schedule_post>', text, re.DOTALL)
    if match:
        return match.group(2).strip(), text.replace(match.group(0), '').strip(), match.group(1).strip()
    return None, text, None


def _extract_notion_note(text: str):
    match = re.search(r'<notion_note title="([^"]+)">(.*?)</notion_note>', text, re.DOTALL)
    if match:
        return {"title": match.group(1).strip(), "content": match.group(2).strip()}, text.replace(match.group(0), '').strip()
    return None, text


def _extract_email(text: str):
    match = re.search(r'<email_draft to="([^"]+)" subject="([^"]+)">(.*?)</email_draft>', text, re.DOTALL)
    if match:
        return {"to": match.group(1).strip(), "subject": match.group(2).strip(), "body": match.group(3).strip()}, text.replace(match.group(0), '').strip()
    return None, text


# ── Keyboards ──────────────────────────────────────────────────────────────────

def _post_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Post Now", callback_data="post"),
         InlineKeyboardButton("Schedule", callback_data="schedule")],
        [InlineKeyboardButton("Regenerate", callback_data="regen"),
         InlineKeyboardButton("Edit", callback_data="edit"),
         InlineKeyboardButton("Save to Notion", callback_data="save_notion")]
    ])


def _email_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Send Email", callback_data="send_email"),
        InlineKeyboardButton("Edit", callback_data="edit_email"),
    ]])


def _format_ist(iso_str: str) -> str:
    return datetime.fromisoformat(iso_str).astimezone(IST).strftime("%d %b %Y at %I:%M %p IST")


# ── Commands ───────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ws = await _get_workspace(user_id, context)
    ws["history"] = []
    context.user_data.pop(_LAST_POST, None)
    context.user_data.pop(_AWAITING_SCHEDULE, None)
    asyncio.create_task(_save_ws(user_id, context))
    await update.message.reply_text(
        "Hey Akhil! I'm Laura, your personal assistant.\n\n"
        "Talk to me like you'd talk to a colleague — I'll figure out what you need.\n\n"
        "I remember everything across our sessions — posts, notes, emails, all of it.\n\n"
        "What are we working on?"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "What I can do:\n\n"
        "LinkedIn:\n"
        "  'Write a post about [topic]'\n"
        "  'Post that LinkedIn thing I gave you earlier'\n"
        "  'Schedule it for tonight 9pm'\n\n"
        "Email:\n"
        "  'Send an email to x@gmail.com about [topic]'\n\n"
        "Notion:\n"
        "  'Save a note about [anything]'\n"
        "  'Remember this: [content]'\n\n"
        "Memory:\n"
        "  I remember all posts, notes and emails across sessions\n"
        "  'What posts have you saved?' — I'll list them\n\n"
        "/memory — see all saved items\n"
        "/scheduled — pending scheduled posts\n"
        "/clear — reset conversation (keeps saved items)\n"
        "/start — full reset"
    )


async def memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ws = await _get_workspace(user_id, context)
    items = ws.get("items", [])
    if not items:
        await update.message.reply_text("No saved items yet.")
        return
    lines = ["Saved items:\n"]
    for item in reversed(items):
        label = item["type"].replace("_", " ").title()
        lines.append(f"[{item['id']}] {label}: \"{item['label']}\" — {item['saved_at']}")
    await update.message.reply_text("\n".join(lines))


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ws = await _get_workspace(user_id, context)
    ws["history"] = []
    context.user_data.pop(_LAST_POST, None)
    context.user_data.pop(_AWAITING_SCHEDULE, None)
    asyncio.create_task(_save_ws(user_id, context))
    await update.message.reply_text("Conversation cleared. Saved items and memory are kept.")


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your chat ID: {update.effective_user.id}")


async def note_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = ' '.join(context.args) if context.args else 'Quick Note'
    await update.message.chat.send_action("typing")
    try:
        url = notion_create_page(title, "Quick note created via Laura.")
        await update.message.reply_text(f"Note saved to Notion!\n\n{url}")
    except Exception as e:
        await update.message.reply_text(f"Failed: {e}")


async def scheduled_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        pending = get_pending(user_id)
    except Exception as e:
        await update.message.reply_text(f"Could not fetch: {e}")
        return
    if not pending:
        await update.message.reply_text("No scheduled posts pending.")
        return
    lines = ["Scheduled posts:\n"]
    for p in pending:
        lines.append(f"ID: {p['id']}\nTime: {_format_ist(p['scheduled_at'])}\n{p['post'][:80]}...\n")
    lines.append("To cancel: send 'cancel <id>'")
    await update.message.reply_text("\n".join(lines))


# ── Main message handler ───────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text
    user_id = update.effective_user.id

    if user_input.lower().startswith("cancel "):
        post_id = user_input.split(" ", 1)[1].strip()
        try:
            ok = cancel_post(user_id, post_id)
            await update.message.reply_text("Cancelled." if ok else "Post not found or already published.")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")
        return

    ws = await _get_workspace(user_id, context)
    history = ws["history"]

    # Awaiting schedule time
    if context.user_data.get(_AWAITING_SCHEDULE):
        context.user_data.pop(_AWAITING_SCHEDULE)
        post = context.user_data.get(_LAST_POST) or (get_item_by_type(ws, "linkedin_post") or {}).get("content")
        if not post:
            await update.message.reply_text("No post found. Generate a post first.")
            return
        await update.message.chat.send_action("typing")
        try:
            iso_dt = await parse_datetime(user_input)
            post_id = add_post(user_id, post, iso_dt)
            await update.message.reply_text(
                f"Scheduled for {_format_ist(iso_dt)}\n\nID: {post_id}\nI'll post it and notify you when it goes live."
            )
        except Exception as e:
            await update.message.reply_text(f"Could not schedule: {e}")
        return

    # URL detection
    url_match = URL_RE.search(user_input)
    if url_match:
        url = url_match.group(0)
        await update.message.reply_text("Fetching article...")
        try:
            title, content = fetch_article(url)
            extra = user_input.replace(url, '').strip()
            prefix = extra + "\n\n" if extra else "Create a LinkedIn post based on this article.\n\n"
            user_input = f"{prefix}Article title: {title}\n\nContent:\n{content}"
        except Exception as e:
            await update.message.reply_text(f"Could not fetch article: {e}")
            return

    history.append({"role": "user", "content": user_input})
    await update.message.chat.send_action("typing")

    try:
        response = await chat(history, get_items_context(ws))
        history.append({"role": "assistant", "content": response})
        asyncio.create_task(_save_ws(user_id, context))

        # Notion note
        note, surrounding = _extract_notion_note(response)
        if note:
            if surrounding:
                await update.message.reply_text(surrounding)
            try:
                notion_url = notion_create_page(note["title"], note["content"])
                add_item(ws, "notion_note", note["content"], note["title"])
                asyncio.create_task(_save_ws(user_id, context))
                await update.message.reply_text(f"Saved to Notion: {note['title']}\n\n{notion_url}")
            except Exception as e:
                await update.message.reply_text(f"Could not save to Notion: {e}")
            return

        # Email draft
        email, surrounding = _extract_email(response)
        if email:
            context.user_data[_LAST_EMAIL] = email
            add_item(ws, "email_draft", email["body"], f"To {email['to']}: {email['subject']}")
            asyncio.create_task(_save_ws(user_id, context))
            if surrounding:
                await update.message.reply_text(surrounding)
            preview = f"To: {email['to']}\nSubject: {email['subject']}\n{'─'*30}\n\n{email['body']}"
            await update.message.reply_text(preview, reply_markup=_email_keyboard())
            return

        post, surrounding, scheduled_dt = _extract_post(response)

        if post and scheduled_dt:
            try:
                post_id = add_post(user_id, post, scheduled_dt)
                label = next((m["content"][:50] for m in reversed(history) if m["role"] == "user"), "LinkedIn post")
                add_item(ws, "linkedin_post", post, label)
                asyncio.create_task(_save_ws(user_id, context))
                msg = (surrounding + "\n\n") if surrounding else ""
                msg += f"Scheduled for {_format_ist(scheduled_dt)}\nID: {post_id}"
                await update.message.reply_text(msg)
            except Exception as e:
                await update.message.reply_text(f"Could not schedule: {e}")

        elif post:
            context.user_data[_LAST_POST] = post
            label = next((m["content"][:50] for m in reversed(history[:-2]) if m["role"] == "user"), "LinkedIn post")
            add_item(ws, "linkedin_post", post, label)
            asyncio.create_task(_save_ws(user_id, context))
            await update.message.reply_text(f"{'─'*30}\n\n{post}\n\n{'─'*30}", reply_markup=_post_keyboard())
            if surrounding:
                await update.message.reply_text(surrounding)

        else:
            await update.message.reply_text(response)

    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


# ── Callback handler ───────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    ws = await _get_workspace(user_id, context)

    if query.data == "post":
        post = context.user_data.get(_LAST_POST) or (get_item_by_type(ws, "linkedin_post") or {}).get("content")
        if not post:
            await query.edit_message_text("No post found. Ask me to write one first.")
            return
        await query.edit_message_text("Posting to LinkedIn...")
        try:
            post_id = post_to_linkedin(post)
            await query.edit_message_text(f"Posted to LinkedIn!\n\nPost ID: {post_id}")
        except Exception as e:
            await query.edit_message_text(f"Failed: {e}")

    elif query.data == "schedule":
        post = context.user_data.get(_LAST_POST) or (get_item_by_type(ws, "linkedin_post") or {}).get("content")
        if not post:
            await query.edit_message_text("No post to schedule.")
            return
        context.user_data[_AWAITING_SCHEDULE] = True
        await query.edit_message_text(
            "When should I post this?\n\nExamples:\n- tonight 9pm\n- tomorrow 8am\n- June 25 9:30pm"
        )

    elif query.data == "regen":
        history = ws["history"]
        if not history:
            await query.edit_message_text("No context found.")
            return
        await query.edit_message_text("Regenerating...")
        try:
            history.append({"role": "user", "content": "Regenerate with a completely different hook and angle."})
            response = await chat(history, get_items_context(ws))
            history.append({"role": "assistant", "content": response})
            post, surrounding, _ = _extract_post(response)
            if post:
                context.user_data[_LAST_POST] = post
                add_item(ws, "linkedin_post", post, "Regenerated post")
            asyncio.create_task(_save_ws(user_id, context))
            await query.edit_message_text(
                f"{'─'*30}\n\n{post or response}\n\n{'─'*30}", reply_markup=_post_keyboard()
            )
        except Exception as e:
            await query.edit_message_text(f"Error: {e}")

    elif query.data == "save_notion":
        post = context.user_data.get(_LAST_POST) or (get_item_by_type(ws, "linkedin_post") or {}).get("content")
        if not post:
            await query.edit_message_text("No post to save.")
            return
        history = ws["history"]
        title = next((m["content"][:60] for m in reversed(history) if m["role"] == "user"), "LinkedIn Post")
        try:
            url = notion_create_page(title, post)
            await query.edit_message_text(f"Saved to Notion!\n\n{url}")
        except Exception as e:
            await query.edit_message_text(f"Failed: {e}")

    elif query.data == "send_email":
        email = context.user_data.get(_LAST_EMAIL)
        if not email:
            await query.edit_message_text("No email draft found.")
            return
        await query.edit_message_text("Sending...")
        try:
            send_email(email["to"], email["subject"], email["body"])
            await query.edit_message_text(f"Email sent to {email['to']}!")
        except Exception as e:
            await query.edit_message_text(f"Failed: {e}")

    elif query.data == "edit_email":
        email = context.user_data.get(_LAST_EMAIL, {})
        await query.edit_message_text(
            f"To: {email.get('to','')}\nSubject: {email.get('subject','')}\n{'─'*30}\n\n{email.get('body','')}\n\nHow should I edit it?"
        )

    elif query.data == "edit":
        post = context.user_data.get(_LAST_POST, '') or (get_item_by_type(ws, "linkedin_post") or {}).get("content", '')
        await query.edit_message_text(
            f"{'─'*30}\n\n{post}\n\n{'─'*30}\n\nHow should I edit it?\n- Shorter\n- More casual\n- Story angle\n- Stronger hook"
        )


# ── Run ────────────────────────────────────────────────────────────────────────

def run_bot():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN not set in .env")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("note", note_command))
    app.add_handler(CommandHandler("memory", memory_command))
    app.add_handler(CommandHandler("scheduled", scheduled_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    webhook_url = os.getenv("WEBHOOK_URL")
    if webhook_url:
        port = int(os.getenv("PORT", 8000))
        app.run_webhook(listen="0.0.0.0", port=port, url_path="/webhook", webhook_url=f"{webhook_url}/webhook")
    else:
        print("Bot is running in polling mode...")
        app.run_polling()
