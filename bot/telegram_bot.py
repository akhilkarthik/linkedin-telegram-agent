import os
import re
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from llm.groq_client import chat, parse_datetime
from linkedin.poster import post_to_linkedin
from db.schedule_store import add_post, get_pending, cancel_post
from utils.url_fetcher import fetch_article
from utils.gmail_sender import send_email

_LAST_POST = "last_post"
_LAST_EMAIL = "last_email"
_HISTORY = "history"
_AWAITING_SCHEDULE = "awaiting_schedule"
MAX_HISTORY = 30
IST = timezone(timedelta(hours=5, minutes=30))
URL_RE = re.compile(r'https?://[^\s]+')


def _extract_post(text: str):
    match = re.search(r'<linkedin_post>(.*?)</linkedin_post>', text, re.DOTALL)
    if match:
        post = match.group(1).strip()
        surrounding = text.replace(match.group(0), '').strip()
        return post, surrounding, None
    match = re.search(r'<schedule_post datetime="([^"]+)">(.*?)</schedule_post>', text, re.DOTALL)
    if match:
        dt = match.group(1).strip()
        post = match.group(2).strip()
        surrounding = text.replace(match.group(0), '').strip()
        return post, surrounding, dt
    return None, text, None


def _extract_email(text: str):
    match = re.search(r'<email_draft to="([^"]+)" subject="([^"]+)">(.*?)</email_draft>', text, re.DOTALL)
    if match:
        to = match.group(1).strip()
        subject = match.group(2).strip()
        body = match.group(3).strip()
        surrounding = text.replace(match.group(0), '').strip()
        return {"to": to, "subject": subject, "body": body}, surrounding
    return None, text


def _email_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Send Email", callback_data="send_email"),
        InlineKeyboardButton("Edit", callback_data="edit_email"),
    ]])


def _post_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Post Now", callback_data="post"),
        InlineKeyboardButton("Schedule", callback_data="schedule"),
        InlineKeyboardButton("Regenerate", callback_data="regen"),
        InlineKeyboardButton("Edit", callback_data="edit"),
    ]])


def _format_ist(iso_str: str) -> str:
    dt = datetime.fromisoformat(iso_str).astimezone(IST)
    return dt.strftime("%d %b %Y at %I:%M %p IST")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data[_HISTORY] = []
    context.user_data.pop(_LAST_POST, None)
    context.user_data.pop(_AWAITING_SCHEDULE, None)
    await update.message.reply_text(
        "Hey! I'm Laura, your personal assistant.\n\n"
        "I can help you with:\n"
        "- LinkedIn posts — create, edit, rewrite, post, schedule\n"
        "- Edit or rewrite any content\n"
        "- Summarize articles or papers\n"
        "- Answer questions on any topic\n"
        "- Write emails, messages, anything\n\n"
        "Just talk to me naturally.\n\n"
        "/scheduled — view pending scheduled posts\n"
        "/help — all commands\n"
        "/clear — reset conversation"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "What I can do:\n\n"
        "LinkedIn:\n"
        "  'Write a LinkedIn post about [topic]'\n"
        "  'Write a post about X and schedule it for tonight 9pm'\n"
        "  'Make it shorter / more casual / bolder'\n\n"
        "Content:\n"
        "  'Edit this: [paste your text]'\n"
        "  'Summarize: [paste article]'\n"
        "  'Rewrite this in a professional tone'\n\n"
        "General:\n"
        "  Ask me anything — I remember our conversation\n\n"
        "/scheduled — view & cancel pending scheduled posts\n"
        "/clear — wipe conversation history\n"
        "/start — fresh start"
    )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your chat ID: {update.effective_user.id}")


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data[_HISTORY] = []
    context.user_data.pop(_LAST_POST, None)
    context.user_data.pop(_AWAITING_SCHEDULE, None)
    await update.message.reply_text("Cleared. Fresh start.")


async def scheduled_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        pending = get_pending(user_id)
    except Exception as e:
        await update.message.reply_text(f"Could not fetch scheduled posts: {e}")
        return

    if not pending:
        await update.message.reply_text("No scheduled posts pending.")
        return

    lines = ["Scheduled posts:\n"]
    for p in pending:
        lines.append(f"ID: {p['id']}\nTime: {_format_ist(p['scheduled_at'])}\nPost: {p['post'][:80]}...\n")

    lines.append("To cancel: send 'cancel <id>'")
    await update.message.reply_text("\n".join(lines))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text
    user_id = update.effective_user.id

    # Handle cancel command
    if user_input.lower().startswith("cancel "):
        post_id = user_input.split(" ", 1)[1].strip()
        try:
            ok = cancel_post(user_id, post_id)
            await update.message.reply_text("Cancelled." if ok else "Post not found or already published.")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")
        return

    # Handle scheduling state — user just told us when to post
    if context.user_data.get(_AWAITING_SCHEDULE):
        context.user_data.pop(_AWAITING_SCHEDULE)
        post = context.user_data.get(_LAST_POST)
        if not post:
            await update.message.reply_text("No post found. Generate a post first.")
            return
        await update.message.chat.send_action("typing")
        try:
            iso_dt = await parse_datetime(user_input)
            post_id = add_post(user_id, post, iso_dt)
            await update.message.reply_text(
                f"Scheduled for {_format_ist(iso_dt)}\n\nID: {post_id}\n\nI'll post it and notify you when it goes live."
            )
        except Exception as e:
            await update.message.reply_text(f"Could not schedule: {e}")
        return

    # URL detection — fetch article and enrich the message
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

    history = context.user_data.get(_HISTORY, [])
    history.append({"role": "user", "content": user_input})
    await update.message.chat.send_action("typing")

    try:
        response = await chat(history)
        history.append({"role": "assistant", "content": response})

        if len(history) > MAX_HISTORY:
            history = history[-MAX_HISTORY:]
        context.user_data[_HISTORY] = history

        # Check for email draft first
        email, surrounding = _extract_email(response)
        if email:
            context.user_data[_LAST_EMAIL] = email
            if surrounding:
                await update.message.reply_text(surrounding)
            preview = (
                f"To: {email['to']}\n"
                f"Subject: {email['subject']}\n"
                f"{'─' * 30}\n\n"
                f"{email['body']}"
            )
            await update.message.reply_text(preview, reply_markup=_email_keyboard())
            return

        post, surrounding, scheduled_dt = _extract_post(response)

        if post and scheduled_dt:
            # LLM scheduled it directly in one shot
            try:
                post_id = add_post(user_id, post, scheduled_dt)
                msg = surrounding + "\n\n" if surrounding else ""
                msg += f"Scheduled for {_format_ist(scheduled_dt)}\n\nID: {post_id}\nPost preview:\n\n{post[:150]}..."
                await update.message.reply_text(msg)
            except Exception as e:
                await update.message.reply_text(f"Could not schedule: {e}")

        elif post:
            context.user_data[_LAST_POST] = post
            if surrounding:
                await update.message.reply_text(surrounding)
            await update.message.reply_text(
                f"{'─' * 30}\n\n{post}\n\n{'─' * 30}",
                reply_markup=_post_keyboard()
            )
        else:
            await update.message.reply_text(response)

    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "post":
        post = context.user_data.get(_LAST_POST)
        if not post:
            await query.edit_message_text("No post to publish. Ask me to write one first.")
            return
        await query.edit_message_text("Posting to LinkedIn...")
        try:
            post_id = post_to_linkedin(post)
            await query.edit_message_text(f"Posted to LinkedIn!\n\nPost ID: {post_id}")
        except Exception as e:
            await query.edit_message_text(f"Failed: {e}")

    elif query.data == "schedule":
        post = context.user_data.get(_LAST_POST)
        if not post:
            await query.edit_message_text("No post to schedule. Generate a post first.")
            return
        context.user_data[_AWAITING_SCHEDULE] = True
        await query.edit_message_text(
            "When should I post this?\n\n"
            "Examples:\n"
            "- tonight 9pm\n"
            "- tomorrow morning 8am\n"
            "- June 25 9:30pm\n"
            "- next Monday 10am"
        )

    elif query.data == "regen":
        history = context.user_data.get(_HISTORY, [])
        if not history:
            await query.edit_message_text("No context found. Send a new request.")
            return
        await query.edit_message_text("Regenerating...")
        try:
            history.append({"role": "user", "content": "Regenerate the LinkedIn post with a completely different hook and angle."})
            response = await chat(history)
            history.append({"role": "assistant", "content": response})
            context.user_data[_HISTORY] = history[-MAX_HISTORY:]

            post, surrounding, _ = _extract_post(response)
            if post:
                context.user_data[_LAST_POST] = post

            display = post or response
            await query.edit_message_text(
                f"{'─' * 30}\n\n{display}\n\n{'─' * 30}",
                reply_markup=_post_keyboard()
            )
        except Exception as e:
            await query.edit_message_text(f"Error: {e}")

    elif query.data == "send_email":
        email = context.user_data.get(_LAST_EMAIL)
        if not email:
            await query.edit_message_text("No email draft found.")
            return
        await query.edit_message_text("Sending email...")
        try:
            send_email(email["to"], email["subject"], email["body"])
            await query.edit_message_text(f"Email sent to {email['to']}!")
        except Exception as e:
            await query.edit_message_text(f"Failed to send: {e}")

    elif query.data == "edit_email":
        email = context.user_data.get(_LAST_EMAIL, {})
        await query.edit_message_text(
            f"To: {email.get('to', '')}\n"
            f"Subject: {email.get('subject', '')}\n"
            f"{'─' * 30}\n\n{email.get('body', '')}\n\n"
            "How should I edit it?"
        )

    elif query.data == "edit":
        post = context.user_data.get(_LAST_POST, '')
        await query.edit_message_text(
            f"{'─' * 30}\n\n{post}\n\n{'─' * 30}\n\n"
            "How should I edit it? Examples:\n"
            "- Make it shorter\n"
            "- More casual tone\n"
            "- Add a story angle\n"
            "- Stronger opening hook\n"
            "- Add emojis"
        )


def run_bot():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN not set in .env")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("scheduled", scheduled_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    webhook_url = os.getenv("WEBHOOK_URL")
    if webhook_url:
        port = int(os.getenv("PORT", 8000))
        print(f"Starting webhook mode on port {port}")
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path="/webhook",
            webhook_url=f"{webhook_url}/webhook",
        )
    else:
        print("Bot is running in polling mode... Press Ctrl+C to stop.")
        app.run_polling()
