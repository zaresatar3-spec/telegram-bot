"""
بات تلگرام برای مدیریت و تبلیغ کانال شخصی
------------------------------------------
قابلیت‌ها:
1) گرفتن متن از مالک بات (در چت خصوصی) و اضافه کردن به صف انتشار
2) تولید پست تبلیغاتی با هوش مصنوعی (Claude) بر اساس یک موضوع
3) انتشار زمان‌بندی‌شده‌ی پست‌ها در کانال
4) خوندن کامنت‌های زیر پست‌های کانال (در گروه گفتگوی لینک‌شده) و
   ارسال یک پیش‌نویس پاسخ برای تایید مالک (به‌جای پاسخ خودکار بدون نظارت)

نکته امنیتی: هیچ پاسخی به‌صورت کاملاً خودکار برای عموم ارسال نمی‌شود مگر
اینکه AUTO_REPLY_MODE را روی "auto" بگذارید. پیش‌فرض حالت "review" است.
"""

import os
import json
import logging
import asyncio
from datetime import datetime, time as dtime
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

import anthropic

# ---------------------------------------------------------------------------
# تنظیمات (از متغیرهای محیطی خوانده می‌شود؛ در فایل .env یا پنل Railway ست کنید)
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CHANNEL_ID = os.environ["CHANNEL_ID"]                      # مثل: @my_channel یا -100xxxxxxxxxx
OWNER_ID = int(os.environ["OWNER_ID"])                      # آیدی عددی خودتان (از @userinfobot بگیرید)
DISCUSSION_GROUP_ID = os.environ.get("DISCUSSION_GROUP_ID")  # گروه گفتگوی لینک‌شده به کانال (اختیاری)
AUTO_REPLY_MODE = os.environ.get("AUTO_REPLY_MODE", "review")  # "review" یا "auto"
POST_TIMES = os.environ.get("POST_TIMES", "09:00,18:00")   # ساعات پیش‌فرض انتشار خودکار از صف

DATA_FILE = Path(__file__).parent / "queue.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
log = logging.getLogger("telegram_channel_bot")

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ---------------------------------------------------------------------------
# ذخیره‌سازی ساده‌ی صف پست‌ها روی فایل JSON
# ---------------------------------------------------------------------------
def load_queue() -> list[str]:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return []


def save_queue(queue: list[str]) -> None:
    DATA_FILE.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")


def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user is None or update.effective_user.id != OWNER_ID:
            return
        return await func(update, context)
    return wrapper


# ---------------------------------------------------------------------------
# دستورات مالک بات
# ---------------------------------------------------------------------------
@owner_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! دستورات:\n"
        "/add <متن> - اضافه کردن پست به صف\n"
        "/generate <موضوع> - تولید پست تبلیغاتی با AI\n"
        "/queue - نمایش صف فعلی\n"
        "/postnow <شماره> - انتشار فوری یک آیتم از صف\n"
        "/clear - خالی کردن صف\n\n"
        "هر متن ساده‌ای هم که برام بفرستی (بدون دستور)، بهت پیشنهاد می‌دم که به صف اضافه بشه."
    )


@owner_only
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.partition(" ")[2].strip()
    if not text:
        await update.message.reply_text("بعد از /add متن پستت رو بنویس.")
        return
    queue = load_queue()
    queue.append(text)
    save_queue(queue)
    await update.message.reply_text(f"✅ اضافه شد. الان صف {len(queue)} آیتم داره.")


@owner_only
async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    queue = load_queue()
    if not queue:
        await update.message.reply_text("صف خالیه.")
        return
    lines = [f"{i+1}. {t[:80]}{'…' if len(t) > 80 else ''}" for i, t in enumerate(queue)]
    await update.message.reply_text("📋 صف پست‌ها:\n" + "\n".join(lines))


@owner_only
async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_queue([])
    await update.message.reply_text("صف خالی شد.")


@owner_only
async def cmd_postnow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("استفاده: /postnow <شماره آیتم در صف>")
        return
    idx = int(args[0]) - 1
    queue = load_queue()
    if idx < 0 or idx >= len(queue):
        await update.message.reply_text("شماره نامعتبره.")
        return
    text = queue.pop(idx)
    save_queue(queue)
    await context.bot.send_message(chat_id=CHANNEL_ID, text=text)
    await update.message.reply_text("✅ توی کانال پست شد.")


@owner_only
async def cmd_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = update.message.text.partition(" ")[2].strip()
    if not topic:
        await update.message.reply_text("بعد از /generate موضوع پست رو بنویس. مثال:\n/generate تخفیف پاییزه فروشگاه")
        return

    await update.message.reply_text("⏳ در حال تولید متن...")
    draft = await generate_ad_copy(topic)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📤 پست کن", callback_data="post_now"),
            InlineKeyboardButton("➕ به صف اضافه کن", callback_data="add_queue"),
            InlineKeyboardButton("❌ دور بریز", callback_data="discard"),
        ]
    ])
    context.user_data["draft"] = draft
    await update.message.reply_text(draft, reply_markup=keyboard)


async def generate_ad_copy(topic: str) -> str:
    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": (
                f"یک پست تبلیغاتی کوتاه و جذاب به زبان فارسی برای کانال تلگرام "
                f"درباره‌ی موضوع زیر بنویس. لحن دوستانه و متقاعدکننده باشه، "
                f"از ایموجی مناسب استفاده کن، حداکثر ۶ خط.\n\nموضوع: {topic}"
            ),
        }],
    )
    return response.content[0].text.strip()


async def on_draft_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != OWNER_ID:
        return

    draft = context.user_data.get("draft")
    if not draft:
        await query.edit_message_text("این پیش‌نویس دیگه معتبر نیست.")
        return

    if query.data == "post_now":
        await context.bot.send_message(chat_id=CHANNEL_ID, text=draft)
        await query.edit_message_text("✅ پست شد توی کانال.")
    elif query.data == "add_queue":
        queue = load_queue()
        queue.append(draft)
        save_queue(queue)
        await query.edit_message_text("➕ به صف اضافه شد.")
    else:
        await query.edit_message_text("❌ دور ریخته شد.")
    context.user_data.pop("draft", None)


@owner_only
async def on_owner_free_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # پیام آزاد (بدون دستور) از مالک -> پیشنهاد اضافه شدن به صف
    text = update.message.text
    context.user_data["pending_add"] = text
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("➕ اضافه به صف", callback_data="freeadd_yes"),
        InlineKeyboardButton("لغو", callback_data="freeadd_no"),
    ]])
    await update.message.reply_text("این متن رو به صف پست‌ها اضافه کنم؟", reply_markup=keyboard)


async def on_freeadd_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != OWNER_ID:
        return
    if query.data == "freeadd_yes":
        text = context.user_data.get("pending_add")
        if text:
            queue = load_queue()
            queue.append(text)
            save_queue(queue)
            await query.edit_message_text("✅ اضافه شد.")
    else:
        await query.edit_message_text("لغو شد.")
    context.user_data.pop("pending_add", None)


# ---------------------------------------------------------------------------
# خوندن کامنت‌های گروه گفتگوی لینک‌شده به کانال
# ---------------------------------------------------------------------------
async def on_discussion_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not DISCUSSION_GROUP_ID:
        return
    if str(update.effective_chat.id) != str(DISCUSSION_GROUP_ID):
        return
    if update.effective_user and update.effective_user.is_bot:
        return

    comment = update.message.text or ""
    if not comment.strip():
        return

    reply_draft = await generate_reply(comment)

    if AUTO_REPLY_MODE == "auto":
        await update.message.reply_text(reply_draft)
    else:
        # حالت پیش‌فرض: پیش‌نویس پاسخ رو برای تایید مالک می‌فرستیم، نه پاسخ مستقیم به کاربر
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=(
                f"💬 کامنت جدید:\n«{comment}»\n\n"
                f"🤖 پیشنهاد پاسخ:\n{reply_draft}\n\n"
                f"(برای فعال کردن پاسخ خودکار، AUTO_REPLY_MODE=auto رو تنظیم کن)"
            ),
        )


async def generate_reply(comment: str) -> str:
    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": (
                f"به این کامنت کاربر زیر کانال تلگرام، یک پاسخ کوتاه، مودبانه و "
                f"دوستانه به فارسی بده:\n\n«{comment}»"
            ),
        }],
    )
    return response.content[0].text.strip()


# ---------------------------------------------------------------------------
# انتشار زمان‌بندی‌شده‌ی خودکار از صف
# ---------------------------------------------------------------------------
async def scheduled_post_job(context: ContextTypes.DEFAULT_TYPE):
    queue = load_queue()
    if not queue:
        return
    text = queue.pop(0)
    save_queue(queue)
    await context.bot.send_message(chat_id=CHANNEL_ID, text=text)
    await context.bot.send_message(chat_id=OWNER_ID, text=f"⏰ پست زمان‌بندی‌شده منتشر شد:\n{text[:200]}")


def setup_schedule(app: Application):
    for t in POST_TIMES.split(","):
        t = t.strip()
        if not t:
            continue
        hour, minute = map(int, t.split(":"))
        app.job_queue.run_daily(scheduled_post_job, time=dtime(hour=hour, minute=minute))
        log.info("زمان‌بندی روزانه اضافه شد: %02d:%02d", hour, minute)


# ---------------------------------------------------------------------------
# راه‌اندازی
# ---------------------------------------------------------------------------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("postnow", cmd_postnow))
    app.add_handler(CommandHandler("generate", cmd_generate))

    app.add_handler(CallbackQueryHandler(on_draft_button, pattern="^(post_now|add_queue|discard)$"))
    app.add_handler(CallbackQueryHandler(on_freeadd_button, pattern="^(freeadd_yes|freeadd_no)$"))

    # پیام‌های خصوصی متنی مالک که دستور نیستن
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_owner_free_text
    ))

    # پیام‌های گروه گفتگوی لینک‌شده (کامنت‌ها)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS, on_discussion_message
    ))

    setup_schedule(app)

    log.info("بات در حال اجراست...")
    app.run_polling()


if __name__ == "__main__":
    main()
