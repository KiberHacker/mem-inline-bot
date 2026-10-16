
import os
import uuid
import threading
from pathlib import Path
from flask import Flask, send_from_directory, abort
from PIL import Image, ImageDraw, ImageFont
from telegram import Update, InlineQueryResultPhoto
from telegram.ext import (
    Application, InlineQueryHandler, ContextTypes
)

TOKEN = os.environ["8766182774:AAGX0EslRIJ2h1sxCQbnFAmjmmdynKcAhAQ"]
BASE_URL = os.environ["BASE_URL"].rstrip("/")

ROOT = Path(__file__).parent
TEMPLATES = ROOT / "templates"
OUTPUT = Path("/tmp/meme-output")
OUTPUT.mkdir(exist_ok=True)

web = Flask(__name__)


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

    # Matnni rasm kengligiga mos satrlarga bo'lish
    words = text.split()
    lines = []
    current = ""

    for word in words:
        test = f"{current} {word}".strip()
        if draw.textbbox((0, 0), test, font=font)[2] < image.width * 0.9:
            current = test
        else:
            if current:
                lines.append(current)
            current = word

    if current:
        lines.append(current)

    # Pastki qismga oq, qora konturli matn
    line_height = size + 8
    y = max(5, image.height - (len(lines) * line_height) - 20)

    for line in lines:
        box = draw.textbbox((0, 0), line, font=font, stroke_width=3)
        x = (image.width - (box[2] - box[0])) // 2
        draw.text(
            (x, y), line, font=font, fill="white",
            stroke_width=3, stroke_fill="black"
        )
        y += line_height

    filename = f"{uuid.uuid4().hex}.png"
    image.save(OUTPUT / filename, format="PNG")
    return filename


async def inline_search(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.inline_query.query.strip()

    if not query:
        await update.inline_query.answer(
            [], cache_time=0,
            switch_pm_text="Matn yozib mem yarating",
            switch_pm_parameter="start"
        )
        return

    files = sorted(
        p for p in TEMPLATES.iterdir()
        if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    )[:10]

    results = []

    for i, source in enumerate(files):
        filename = make_meme(source, query)
        url = f"{BASE_URL}/img/{filename}"

        results.append(
            InlineQueryResultPhoto(
                id=uuid.uuid4().hex,
                photo_url=url,
                thumbnail_url=url,
                title=f"Meme {i + 1}",
                description=f"Shablon {i + 1}"
            )
        )

    await update.inline_query.answer(
        results, cache_time=0, is_personal=True
    )


def run_web():
    web.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000")),
        use_reloader=False
    )


if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()

    bot = Application.builder().token(TOKEN).build()
    bot.add_handler(InlineQueryHandler(inline_search))
    bot.run_polling()
