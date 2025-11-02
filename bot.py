import os
import time
import logging
import requests
from fastapi import FastAPI, Request, Response
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("aiuta-bot")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
AIUTA_API_BASE = os.getenv("AIUTA_API_BASE", "https://api.aiuta.com")
AIUTA_API_KEY = os.getenv("AIUTA_API_KEY")
MODEL_PRESET = os.getenv("AIUTA_STUDIO_MODEL_PRESET", "default_woman")
VARIANTS = int(os.getenv("AIUTA_VARIANTS", "3"))
PUBLIC_URL = os.getenv("PUBLIC_URL")
AIUTA_SUB_ID = os.getenv("AIUTA_SUBSCRIPTION_ID")  # <-- добавили

if not BOT_TOKEN or not AIUTA_API_KEY:
    raise SystemExit("⚠️  Заполни TELEGRAM_BOT_TOKEN и AIUTA_API_KEY в .env/Environment")

# --- Telegram Application ---
application = Application.builder().token(BOT_TOKEN).build()

# --- Aiuta helpers ---
def aiuta_headers():
    h = {
        "X-API-Key": AIUTA_API_KEY,
        "Accept": "application/json",
    }
    # для бесплатного плана иногда обязателен x-user-id
    if AIUTA_SUB_ID:
        h["x-user-id"] = AIUTA_SUB_ID
    return h

def aiuta_upload_product_image(image_bytes: bytes, filename: str = "item.jpg") -> dict:
    url = f"{AIUTA_API_BASE}/studio/v1/product_images"
    files = {"file": (filename, image_bytes, "image/jpeg")}
    r = requests.post(url, headers=aiuta_headers(), files=files, timeout=60)
    if not r.ok:
        log.error("Ошибка AIUTA API (upload): %s", r.text)
    r.raise_for_status()
    return r.json()

def aiuta_create_on_figure_operation(product_image_id: str, model_preset: str, variants: int) -> dict:
    url = f"{AIUTA_API_BASE}/studio/v1/generations"
    payload = {
        "type": "on_figure",
        "product_image_id": product_image_id,
        "model_preset": model_preset,
        "variants": variants
    }
    r = requests.post(url, headers={**aiuta_headers(), "Content-Type": "application/json"}, json=payload, timeout=60)
    if not r.ok:
        log.error("Ошибка AIUTA API (generation): %s", r.text)
    r.raise_for_status()
    return r.json()

def aiuta_poll_operation(operation_id: str, timeout_sec: int = 240, interval_sec: int = 4) -> dict:
    url = f"{AIUTA_API_BASE}/studio/v1/operations/{operation_id}"
    start = time.time()
    while True:
        r = requests.get(url, headers=aiuta_headers(), timeout=30)
        if not r.ok:
            log.error("Ошибка AIUTA API (poll): %s", r.text)
        r.raise_for_status()
        data = r.json()
        status = data.get("status")
        if status in ("SUCCESS", "FAILED"):
            return data
        if time.time() - start > timeout_sec:
            raise TimeoutError("Aiuta operation timeout")
        time.sleep(interval_sec)

def aiuta_extract_generated_urls(operation_payload: dict) -> list:
    urls = []
    results = operation_payload.get("results") or operation_payload.get("generated_images") or []
    for it in results:
        url = it.get("url") or it.get("image_url")
        if url:
            urls.append(url)
    return urls

# --- Telegram handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет 👋 Пришли фото одежды (на вешалке, на полу и т.д.) — я покажу, как она будет смотреться на модели!\n"
        "Советы: простой фон и без сильных складок 😉"
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.photo:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_photo")

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    img_bytes = await file.download_as_bytearray()

    try:
        up = aiuta_upload_product_image(bytes(img_bytes), filename=f"{photo.file_unique_id}.jpg")
        product_image_id = up.get("id") or up.get("image_id")
        if not product_image_id:
            raise RuntimeError("Не получили id загруженного изображения от Aiuta")

        gen = aiuta_create_on_figure_operation(product_image_id, MODEL_PRESET, VARIANTS)
        operation_id = gen.get("operation_id") or gen.get("id")
        if not operation_id:
            raise RuntimeError("Не получили operation_id от Aiuta")

        await update.message.reply_text("🧵 Обрабатываю изображение...")
        op = aiuta_poll_operation(operation_id, timeout_sec=240, interval_sec=4)

        if op.get("status") == "FAILED":
            await update.message.reply_text("⚠️ Генерация не удалась. Попробуй другое фото.")
            return

        urls = aiuta_extract_generated_urls(op)
        if not urls:
            await update.message.reply_text("⚠️ Aiuta не вернула изображений. Попробуй другое фото.")
            return

        for u in urls[:3]:
            await update.message.reply_photo(u)

    except Exception as e:
        log.exception("Ошибка обработки фото: %s", e)
        await update.message.reply_text(f"⚠️ Ошибка при обработке изображения.")

application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.PHOTO, handle_photo))

# --- FastAPI + webhook ---
app = FastAPI()

@app.on_event("startup")
async def on_startup():
    if not PUBLIC_URL:
        log.warning("PUBLIC_URL не задан — webhook не активирован (локальный режим).")
        return
    webhook_url = f"{PUBLIC_URL}/webhook/{BOT_TOKEN}"
    await application.initialize()     # важно для PTB 21.x
    await application.bot.set_webhook(url=webhook_url)
    log.info(f"Webhook set to: {webhook_url}")

@app.on_event("shutdown")
async def on_shutdown():
    await application.shutdown()

@app.post(f"/webhook/{BOT_TOKEN}")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, application.bot)
    # Application уже инициализирован на старте
    await application.process_update(update)
    return Response(status_code=200)
