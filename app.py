import os
import uuid
import asyncio
import threading
import logging
from pathlib import Path

from flask import Flask, send_from_directory, abort
from PIL import Image, ImageDraw, ImageFont

from telegram import Update, InlineQueryResultPhoto
from telegram.ext import (
    Application,
    CommandHandler,
    InlineQueryHandler,
    ContextTypes,
)

# LOG SETTINGS
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("MemeBot")

# RENDER ENVIRONMENT
TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ["BASE_URL"].rstrip("/")

ROOT = Path(__file__).parent
TEMPLATES = ROOT / "templates"
OUTPUT = Path("/tmp/meme-output")

TEMPLATES.mkdir(exist_ok=True)
OUTPUT.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

web = Flask(__name__)


@web.get("/")
def home():
    return "Meme bot is running!"


@web.get("/img/<filename>")
def get_image(filename):
    if not filename.endswith(".png") or Path(filename).name != filename:
        abort(404)

    path = OUTPUT / filename
    if not path.is_file():
        abort(404)

    return send_from_directory(OUTPUT, filename)


# START COMMAND
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(
            "Salom! 👋 Meme bot ishlayapti!\n\n"
            "Istalgan chatda quyidagicha yozing:\n"
            "@denismembot salom\n\n"
            "So'ng chiqqan memlardan birini tanlang."
        )


# CREATE MEME IMAGE
def make_meme(source, text):
    logger.info("Rasm ochilmoqda: %s", source.name)

    with Image.open(source) as original:
        image = original.convert("RGB")

    draw = ImageDraw.Draw(image)

    font_path = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    )
    size = max(18, image.width // 12)

    try:
        font = ImageFont.truetype(font_path, size)
    except OSError:
        font = ImageFont.load_default()

    # Matnni qatorlarga ajratish
    lines = []
    current = ""

    for word in text.split():
        candidate = f"{current} {word}".strip()
        box = draw.textbbox((0, 0), candidate, font=font)

        if box[2] - box[0] <= image.width * 0.9:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word

    if current:
        lines.append(current)

    if not lines:
        lines = [text]

    line_height = size + 8
    max_lines = max(1, (image.height - 20) // line_height)
    lines = lines[:max_lines]

    y = max(5, image.height - len(lines) * line_height - 20)

    for line in lines:
        box = draw.textbbox(
            (0, 0), line, font=font, stroke_width=3
        )
        text_width = box[2] - box[0]
        x = max(5, (image.width - text_width) // 2)

        draw.text(
            (x, y),
            line,
            font=font,
            fill="white",
            stroke_width=3,
            stroke_fill="black",
        )
        y += line_height

    filename = f"{uuid.uuid4().hex}.png"
    output_path = OUTPUT / filename

    image.save(output_path, format="PNG")

    logger.info("Rasm saqlandi: %s", output_path)
    return filename


# INLINE SEARCH
async def inline_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    inline_query = update.inline_query

    if inline_query is None:
        return

    query = inline_query.query.strip()
    logger.info("INLINE QUERY RECEIVED: %r", query)

    if not query:
        await inline_query.answer([], cache_time=0)
        return

    files = sorted(
        p for p in TEMPLATES.iterdir()
        if p.is_file()
        and p.suffix.lower() in ALLOWED_EXTENSIONS
    )[:3]

    logger.info("Found %d template images", len(files))

    if not files:
        logger.error("templates papkasida rasm topilmadi")
        await inline_query.answer([], cache_time=0)
        return

    # Telegram kutib qolmasligi uchun rasmlarni alohida
    # thread'da tayyorlaymiz; har bir bosqich log qilinadi.
    results = []

    for index, source in enumerate(files, start=1):
        try:
            logger.info("Rasm tayyorlash boshlandi: %s", source.name)

            filename = await asyncio.wait_for(
                asyncio.to_thread(make_meme, source, query),
                timeout=15,
            )

            logger.info("Rasm tayyorlandi: %s", filename)

            url = f"{BASE_URL}/img/{filename}"
            logger.info("Rasm URL tayyorlandi: %s", url)

            results.append(
                InlineQueryResultPhoto(
                    id=uuid.uuid4().hex,
                    photo_url=url,
                    thumbnail_url=url,
                    title=f"Meme {index}",
                    description=f"Matn: {query[:100]}",
                )
            )

        except asyncio.TimeoutError:
            logger.error(
                "Rasm tayyorlash 15 soniyada tugamadi: %s",
                source.name,
            )

        except Exception:
            logger.exception(
                "Failed to create meme from %s", source.name
            )

    logger.info("Sending %d inline results", len(results))

    # Javobni Telegram'ga yuborish
    try:
        await inline_query.answer(
            results,
            cache_time=0,
            is_personal=True,
        )
        logger.info("INLINE JAVOB TELEGRAMGA YUBORILDI")

    except Exception:
        logger.exception("INLINE JAVOB YUBORISHDA XATO")


# ERROR HANDLER
async def on_error(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.error("Telegram bot error", exc_info=context.error)


# WEB SERVER
def run_web():
    web.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000")),
        use_reloader=False,
    )


# START BOT
def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    threading.Thread(
        target=run_web,
        daemon=True,
        name="web-server",
    ).start()

    bot = Application.builder().token(TOKEN).build()

    bot.add_handler(CommandHandler("start", start))
    bot.add_handler(InlineQueryHandler(inline_search))
    bot.add_error_handler(on_error)

    logger.info("Meme bot starting...")

    bot.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
