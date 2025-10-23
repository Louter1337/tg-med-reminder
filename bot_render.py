
import os
import asyncio
import logging
import random
from datetime import datetime, timedelta, time as dtime
from io import BytesIO
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, ContextTypes,
    ChatJoinRequestHandler
)
import httpx

# ---------- ЛОГИ ----------
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
log = logging.getLogger("med-reminder-render")

# ---------- КОНФИГ ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")      # "-1001234567890" или "@your_channel"
ALMATY_TZ = ZoneInfo("Asia/Almaty")
REMINDER_TIME = dtime(hour=8, minute=0, tzinfo=ALMATY_TZ)  # 08:00 по Алматы
CAT_PROB = float(os.getenv("CAT_PROB", "0.30"))

LOVE_PHRASES = [
    "💖 Любимая, не забудь, пожалуйста, выпить лекарство.",
    "💊 Забота о тебе — главное. Прими лекарство, солнышко.",
    "✨ Ты у меня самая лучшая. Время для лекарства!",
    "🥰 Нежно напоминаю: таблеточка — и день станет лучше.",
    "🌸 Пусть это маленькое напоминание сохранит твоё здоровье.",
    "🤍 Береги себя, пожалуйста. Пора принять лекарство.",
    "🌞 Доброе утро, любовь! Лекарство ждёт тебя."
]

REMINDER_TEXT_TPL = (
    "{heart}\n"
    "<b>Напоминание</b>: пора принять лекарство.\n\n"
    "{phrase}\n\n"
    "Если что-то нужно — напиши мне ❤️"
)

# ---------- УТИЛИТЫ ----------
async def _fetch_random_cat_bytes() -> BytesIO | None:
    url = "https://cataas.com/cat"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            return BytesIO(r.content)
    except Exception as e:
        log.warning("Не удалось получить котика: %s", e)
        return None

async def _post_reminder_via_bot(bot):
    chat_id = CHANNEL_ID
    phrase = random.choice(LOVE_PHRASES)
    heart = random.choice(["❤️", "💖", "💗", "💕", "💞", "🩷"])
    text = REMINDER_TEXT_TPL.format(heart=heart, phrase=phrase)

    if random.random() < CAT_PROB:
        cat = await _fetch_random_cat_bytes()
        if cat is not None:
            try:
                await bot.send_photo(chat_id=chat_id, photo=cat, caption=text, parse_mode=ParseMode.HTML)
                return
            except Exception as e:
                log.warning("Не удалось отправить фото котика: %s", e)

    await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)

def _seconds_until_next_run(tz: ZoneInfo, hhmm: dtime) -> float:
    now = datetime.now(tz)
    target = now.replace(hour=hhmm.hour, minute=hhmm.minute, second=0, microsecond=0)
    if now >= target:
        target = target + timedelta(days=1)
    return (target - now).total_seconds()

async def daily_scheduler(application: Application):
    # простой планировщик без JobQueue
    while True:
        delay = _seconds_until_next_run(ALMATY_TZ, REMINDER_TIME)
        log.info("До следующего поста: %.0f сек.", delay)
        try:
            await asyncio.sleep(delay)
            await _post_reminder_via_bot(application.bot)
            # небольшой буфер, чтобы не попасть в ту же минуту при задержках
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("Ошибка в планировщике: %s", e)
            await asyncio.sleep(5)

# ---------- КОМАНДЫ БОТА ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я публикую напоминания в канал каждый день в 08:00 (Алматы).\n"
        "Команды:\n"
        "• /test — тестовый пост в канал сейчас\n"
        "• /invite — создать ссылку по заявке на вступление"
    )

async def test_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _post_reminder_via_bot(context.bot)
    await update.message.reply_text("Отправил тестовое напоминание в канал.")

async def invite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        link = await context.bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            creates_join_request=True,
            name="Напоминания о лекарстве"
        )
        await update.message.reply_text(f"Ссылка по заявке на вступление:\n{link.invite_link}")
    except Exception as e:
        await update.message.reply_text(f"Не смог создать ссылку. Проверь права бота в канале. Ошибка: {e}")

# Авто-одобрение заявок в канал
async def approve_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        cj = update.chat_join_request
        await context.bot.approve_chat_join_request(chat_id=cj.chat.id, user_id=cj.from_user.id)
        log.info("Одобрена заявка пользователя %s в чат %s", cj.from_user.id, cj.chat.id)
    except Exception as e:
        log.warning("Не удалось одобрить заявку: %s", e)

# ---------- HTTP-СЕРВЕР ДЛЯ RENDER ----------
async def start_http_server():
    from aiohttp import web

    async def healthz(request):
        return web.Response(text="ok")

    async def home(request):
        return web.Response(text="med-reminder is running")

    app = web.Application()
    app.router.add_get("/", home)
    app.router.add_get("/healthz", healthz)

    port = int(os.getenv("PORT") or 10000)  # Render выдаёт PORT автоматически
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    log.info("HTTP server started on 0.0.0.0:%s", port)

    # держим сервер живым
    await asyncio.Event().wait()

# ---------- MAIN ----------
async def main():
    if not BOT_TOKEN or not CHANNEL_ID:
        raise RuntimeError("Нужны переменные окружения BOT_TOKEN и CHANNEL_ID")

    application: Application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("test", test_cmd))
    application.add_handler(CommandHandler("invite", invite_cmd))
    application.add_handler(ChatJoinRequestHandler(approve_join))

    # --- старт Telegram (polling) ---
    await application.initialize()
    # Если когда-то включали вебхук — снимем
    await application.bot.delete_webhook(drop_pending_updates=True)
    # Запускаем polling и обработчики
    if application.updater is None:
        # На всякий случай: если Updater не инициализировался, используем run_polling в отдельном таске
        async def _run_polling_blocking():
            await application.start()
            await application.run_polling()
        polling_task = asyncio.create_task(_run_polling_blocking())
    else:
        await application.updater.start_polling()
        await application.start()
        polling_task = None
    log.info("Telegram bot polling started")

    # --- параллельно поднимаем HTTP-сервер и планировщик ---
    http_task = asyncio.create_task(start_http_server())
    sched_task = asyncio.create_task(daily_scheduler(application))

    try:
        await asyncio.gather(http_task, sched_task, *( [polling_task] if polling_task else [] ))
    finally:
        if polling_task:
            polling_task.cancel()
        await application.stop()
        await application.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
