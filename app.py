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
    InlineQueryHandler,
    CommandHandler,
    ContextTypes,
)

# Logging
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("MemeBot")

# Environment variables
TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ["BASE_URL"].rstrip("/")

# Folders
ROOT = Path(__file__).parent
TEMPLATES = ROOT / "templates"
OUTPUT = Path("/tmp/meme-output")

TEMPLATES.mkdir(exist_ok=True)
OUTPUT.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

# Web server
web = Flask(__name__)


@web.get("/")
def home():
    return "Meme bot is running!"


@web.get("/img/<filename>")
def get_image(filename):
    if not filename.endswith(".png") or Path(filename).name != filename:
        abort(404)

    if not (OUTPUT / filename).is_file():
        abort(404)

    return send_from_directory(OUTPUT, filename)


# /start command
async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "Salom! 👋 Meme bot ishlayapti!\n\n"
        "Mem yaratish uchun istalgan Telegram chatida:\n"
        "@denismembot salom\n\n"
        "deb yozing va chiqqan memlardan birini tanlang."
    )


def make_meme(source, text):
    image = Image.open(source).convert("RGB")
    draw = ImageDraw.Draw(image)

    font_path = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    )
    size = max(18, image.width // 12)

    try:
        font = ImageFont.truetype(font_path, size)
    except OSError:
        font = ImageFont.load_default()

    # Wrap text
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
    image.save(OUTPUT / filename, format="PNG")
    return filename


# Inline meme search
async def inline_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.inline_query.query.strip()
    logger.info("INLINE QUERY RECEIVED: %r", query)

    if not query:
        await update.inline_query.answer([], cache_time=0)
        return

    files = sorted(
        p for p in TEMPLATES.iterdir()
        if p.is_file()
        and p.suffix.lower() in ALLOWED_EXTENSIONS
    )[:10]

    logger.info("Found %d template images", len(files))

    results = []

    for index, source in enumerate(files, start=1):
        try:
            filename = make_meme(source, query)
            url = f"{BASE_URL}/img/{filename}"

            results.append(
                InlineQueryResultPhoto(
                    id=uuid.uuid4().hex,
                    photo_url=url,
                    thumbnail_url=url,
                    title=f"Meme {index}",
                    description=f"Shablon {index}",
                )
            )
        except Exception:
            logger.exception(
                "Meme yaratishda xato: %s", source.name
            )

    logger.info("Sending %d inline results", len(results))

    await update.inline_query.answer(
        results,
        cache_time=0,
        is_personal=True,
    )


# Error logging
async def on_error(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.error("Telegram bot error", exc_info=context.error)


def run_web():
    web.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000")),
        use_reloader=False,
    )


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
