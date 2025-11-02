import os
import logging
import requests
from fastapi import FastAPI, Request, Response
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from dotenv import load_dotenv

# --- Загрузка переменных окружения ---
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
AIUTA_API_BASE = os.getenv("AIUTA_API_BASE")
AIUTA_API_KEY = os.getenv("AIUTA_API_KEY")
AIUTA_STUDIO_MODEL_PRESET = os.getenv("AIUTA_STUDIO_MODEL_PRESET", "default_woman")
AIUTA_VARIANTS = int(os.getenv("AIUTA_VARIANTS", "3"))
PUBLIC_URL = os.getenv("PUBLIC_URL")

# --- Логирование ---
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("fashion-bot")

# --- Telegram handlers ---
async def start(update: Update, context):
    await update.message.reply_text("Привет 👋 Пришли фото одежды — покажу, как она будет смотреться на модели!")

async def handle_photo(update: Update, context):
    photo = update.message.photo[-1]
    file = await photo.get_file()
    file_path = await file.download_to_drive()
    log.info(f"Фото скачано: {file_path}")

    await update.message.reply_text("🧵 Обрабатываю изображение...")

    try:
        with open(file_path, "rb") as f:
            headers = {"X-API-Key": AIUTA_API_KEY}
            files = {"image": f}
            data = {
                "preset": AIUTA_STUDIO_MODEL_PRESET,
                "variants": AIUTA_VARIANTS
            }
            response = requests.post(f"{AIUTA_API_BASE}/studio/tryon", headers=headers, files=files, data=data)

        if response.status_code == 200:
            images = response.json().get("images", [])
            for img in images:
                await update.message.reply_photo(img)
        else:
            log.error(f"Ошибка AIUTA API: {response.text}")
            await update.message.reply_text("⚠️ Ошибка при обработке изображения.")
    except Exception as e:
        log.exception("Ошибка обработки:")
        await update.message.reply_text("Произошла ошибка при обработке. Попробуй позже.")

# --- Создание Telegram приложения ---
application = Application.builder().token(BOT_TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.PHOTO, handle_photo))

# --- FastAPI + webhook ---
app = FastAPI()

@app.on_event("startup")
async def on_startup():
    if not PUBLIC_URL:
        log.warning("PUBLIC_URL не задан — webhook не активирован (локальный режим).")
        return
    await application.initialize()
    webhook_url = f"{PUBLIC_URL}/webhook/{BOT_TOKEN}"
    await application.bot.set_webhook(url=webhook_url)
    await application.start()
    log.info(f"Webhook set to: {webhook_url}")

@app.on_event("shutdown")
async def on_shutdown():
    await application.stop()
    await application.shutdown()
    log.info("Telegram Application stopped.")

@app.get("/")
async def root():
    return {"ok": True, "service": "fashion-bot"}

@app.post(f"/webhook/{BOT_TOKEN}")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return Response(status_code=200)
