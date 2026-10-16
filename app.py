import os
import uuid
import asyncio
import threading
from pathlib import Path

from flask import Flask, send_from_directory, abort
from PIL import Image, ImageDraw, ImageFont

from telegram import Update, InlineQueryResultPhoto
from telegram.ext import (
    Application,
    InlineQueryHandler,
    ContextTypes,
)

# Environment variables
TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ["BASE_URL"].rstrip("/")

# Folders
ROOT = Path(__file__).parent
TEMPLATES = ROOT / "templates"
OUTPUT = Path("/tmp/meme-output")

TEMPLATES.mkdir(exist_ok=True)
OUTPUT.mkdir(parents=True, exist_ok=True)

# Web server
web = Flask(__name__)


@web.get("/")
def home():
    return "Meme bot is running!"


@web.get("/img/<filename>")
def get_image(filename):
    if not filename.endswith(".png"):
        abort(404)

    return send_from_directory(OUTPUT, filename)


def make_meme(source, text):
    image = Image.open(source).convert("RGB")
    draw = ImageDraw.Draw(image)

    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    size = max(18, image.width // 12)

    try:
        font = ImageFont.truetype(font_path, size)
    except OSError:
        font = ImageFont.load_default()

    # Wrap text to fit the image
    words = text.split()
    lines = []
    current = ""

    for word in words:
        test = f"{current} {word}".strip()
        box = draw.textbbox((0, 0), test, font=font)

        if box[2] - box[0] <= image.width * 0.9:
            current = test
        else:
            if current:
                lines.append(current)
            current = word

    if current:
        lines.append(current)

    # Draw text near the bottom
    line_height = size + 8
    total_height = len(lines) * line_height
    y = max(5, image.height - total_height - 20)

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


async def inline_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.inline_query.query.strip()

    if not query:
        await update.inline_query.answer(
            [],
            cache_time=0,
        )
        return

    files = sorted(
        p for p in TEMPLATES.iterdir()
        if p.is_file()
        and p.suffix.lower() in {
            ".jpg", ".jpeg", ".png", ".webp"
        }
    )[:10]

    results = []

    for i, source in enumerate(files):
        try:
            filename = make_meme(source, query)
            url = f"{BASE_URL}/img/{filename}"

            results.append(
                InlineQueryResultPhoto(
                    id=uuid.uuid4().hex,
                    photo_url=url,
                    thumbnail_url=url,
                    title=f"Meme {i + 1}",
                    description=f"Shablon {i + 1}",
                )
            )
        except Exception as error:
            print(f"Rasm xatosi ({source.name}): {error}")

    await update.inline_query.answer(
        results,
        cache_time=0,
        is_personal=True,
    )


def run_web():
    web.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000")),
        use_reloader=False,
    )


if __name__ == "__main__":
    # Fix for Python versions without a current event loop
    asyncio.set_event_loop(asyncio.new_event_loop())

    # Start image server
    threading.Thread(
        target=run_web,
        daemon=True,
    ).start()

    # Start Telegram bot
    bot = Application.builder().token(TOKEN).build()
    bot.add_handler(InlineQueryHandler(inline_search))

    print("Meme bot starting...")
    bot.run_polling()
