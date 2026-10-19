import os
import glob
import hashlib
import asyncio
import logging
from io import BytesIO
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont
from telegram import Update, InlineQueryResultCachedPhoto, InputMediaPhoto
from telegram.error import RetryAfter
from telegram.ext import (
    Application, CommandHandler, InlineQueryHandler,
    MessageHandler, filters, ContextTypes,
)

TOKEN = os.environ.get("BOT_TOKEN") or os.environ.get("TOKEN", "")
BASE_URL = os.environ.get("BASE_URL", "").strip().rstrip("/")
PORT = int(os.environ.get("PORT", "8080"))
STORAGE_FILE = "storage_chat.txt"
TEMPLATES_DIR = "templates"
MAX_TEMPLATES = 10       # RAM kam bo'lsa 6-8 qiling
IMG_WIDTH = 700
JPEG_QUALITY = 80
FONT_DIV = 14            # katta son = kichik font
MAX_BLOCK = 0.35         # matn balandlikning ko'pi bilan 35% i
WAIT_SECONDS = 8

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

FONT_PATHS = [
    "font.ttf",
    "/system/fonts/Roboto-Black.ttf",
    "/system/fonts/Roboto-Bold.ttf",
    "/system/fonts/DroidSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

cache = {}         # (shablon, matn) -> file_id
base_images = {}   # shablon yo'li -> tayyor rasm


@lru_cache(maxsize=64)
def get_font(size):
    for path in FONT_PATHS:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def get_templates():
    files = []
    for ext in ("jpg", "jpeg", "png", "JPG", "JPEG", "PNG"):
        files += glob.glob(f"{TEMPLATES_DIR}/*.{ext}")
    return sorted(set(files))[:MAX_TEMPLATES]


def ensure_templates():
    os.makedirs(TEMPLATES_DIR, exist_ok=True)
    if not get_templates():
        colors = [(40, 40, 160), (160, 40, 40), (40, 130, 60)]
        for i, c in enumerate(colors, 1):
            Image.new("RGB", (600, 600), c).save(f"{TEMPLATES_DIR}/rang{i}.jpg")


def preload_templates():
    for path in get_templates():
        try:
            img = Image.open(path).convert("RGB")
        except Exception as e:
            logging.warning("Rasm ochilmadi %s: %s", path, e)
            continue
        if img.width > IMG_WIDTH:
            ratio = IMG_WIDTH / img.width
            img = img.resize((IMG_WIDTH, int(img.height * ratio)))
        base_images[path] = img
    logging.info("Yuklangan shablonlar: %d ta", len(base_images))


def split_word(draw, word, font, max_w):
    parts, cur = [], ""
    for ch in word:
        if draw.textlength(cur + ch, font=font) <= max_w:
            cur += ch
        else:
            parts.append(cur)
            cur = ch
    parts.append(cur)
    return parts


def wrap(draw, text, font, max_w):
    lines, line = [], ""
    for word in text.split():
        if draw.textlength(word, font=font) > max_w:
            if line:
                lines.append(line)
                line = ""
            parts = split_word(draw, word, font, max_w)
            lines.extend(parts[:-1])
            line = parts[-1]
            continue
        test = f"{line} {word}".strip()
        if draw.textlength(test, font=font) <= max_w:
            line = test
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def draw_block(draw, text, W, H, top):
    text = text.upper()
    max_h = int(H * MAX_BLOCK)
    size = max(10, W // FONT_DIV)
    while True:
        font = get_font(size)
        stroke = max(1, size // 14)
        lines = wrap(draw, text, font, W - 24 - 2 * stroke)
        line_h = int(size * 1.15)
        if len(lines) * line_h <= max_h or size <= 9:
            break
        size -= 1

    max_lines = max(1, max_h // line_h)
    lines = lines[:max_lines]

    total = len(lines) * line_h
    y = 6 if top else H - total - 6
    for ln in lines:
        draw.text(
            (W / 2, y + stroke), ln, font=font, fill="white",
            stroke_width=stroke, stroke_fill="black", anchor="mt",
        )
        y += line_h


def make_meme(template_path, top_text, bottom_text):
    img = base_images[template_path].copy()
    draw = ImageDraw.Draw(img)
    if top_text:
        draw_block(draw, top_text, img.width, img.height, top=True)
    if bottom_text:
        draw_block(draw, bottom_text, img.width, img.height, top=False)
    buf = BytesIO()
    img.save(buf, "JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with open(STORAGE_FILE, "w") as f:
        f.write(str(update.effective_chat.id))
    await update.message.reply_text(
        "Tayyor! Endi istalgan chatda yozing:\n"
        "@denismembot tepa matn | pastki matn.\n"
        "Oxiriga nuqta (.) qo'yishni unutmang."
    )


async def save_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.channel_post.chat.id
    with open(STORAGE_FILE, "w") as f:
        f.write(str(chat_id))
    await context.bot.send_message(chat_id, "Saqlandi ✅")


async def delete_later(bot, chat_id, message_ids):
    for mid in message_ids:
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass


async def upload_chunk(bot, storage_id, chunk, q, top, bottom):
    datas = [make_meme(p, top, bottom) for p in chunk]
    tm = dict(read_timeout=40, write_timeout=40,
              connect_timeout=20, pool_timeout=20)
    msgs = None
    for _ in range(3):
        try:
            if len(datas) == 1:
                m = await bot.send_photo(storage_id, datas[0], **tm)
                msgs = [m]
            else:
                media = [InputMediaPhoto(media=d) for d in datas]
                msgs = await bot.send_media_group(storage_id, media, **tm)
            break
        except RetryAfter as e:
            logging.info("Limit, %s soniya kutaman", e.retry_after)
            await asyncio.sleep(e.retry_after + 1)
        except Exception as e:
            logging.warning("Yuborishda xato: %s", e)
            await asyncio.sleep(1)
    if not msgs:
        return
    for path, msg in zip(chunk, msgs):
        cache[(path, q)] = msg.photo[-1].file_id
    asyncio.create_task(
        delete_later(bot, storage_id, [m.message_id for m in msgs])
    )


async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    iq = update.inline_query
    q = iq.query.strip()
    if not q.endswith(".") or not os.path.exists(STORAGE_FILE):
        await iq.answer([], cache_time=0)
        return
    q = q[:-1].strip()
    if not q:
        await iq.answer([], cache_time=0)
        return

    with open(STORAGE_FILE) as f:
        storage_id = int(f.read().strip())

    parts = q.split("|", 1)
    top = parts[0].strip()
    bottom = parts[1].strip() if len(parts) > 1 else ""

    paths = list(base_images)
    missing = [p for p in paths if (p, q) not in cache]

    chunks = [missing[i:i + 10] for i in range(0, len(missing), 10)]
    if chunks:
        work = asyncio.gather(
            *[upload_chunk(context.bot, storage_id, c, q, top, bottom)
              for c in chunks],
            return_exceptions=True,
        )
        try:
            await asyncio.wait_for(asyncio.shield(work), timeout=WAIT_SECONDS)
        except asyncio.TimeoutError:
            logging.info("Vaqt tugadi, borini yuboraman")

    results = []
    for p in paths:
        file_id = cache.get((p, q))
        if not file_id:
            continue
        rid = hashlib.md5(f"{p}{q}".encode()).hexdigest()
        results.append(InlineQueryResultCachedPhoto(id=rid, photo_file_id=file_id))

    try:
        await iq.answer(results, cache_time=0, is_personal=True)
    except Exception as e:
        logging.warning("Javob berib bo'lmadi: %s", e)


def main():
    if not TOKEN:
        raise SystemExit(
            "BOT_TOKEN topilmadi! Server sozlamalariga BOT_TOKEN ni qo'shing."
        )
    ensure_templates()
    preload_templates()
    app = (
        Application.builder()
        .token(TOKEN)
        .concurrent_updates(True)
        .connection_pool_size(32)
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(20)
        .pool_timeout(20)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(
        filters.UpdateType.CHANNEL_POSTS & filters.Regex(r"^/start"),
        save_channel,
    ))
    app.add_handler(InlineQueryHandler(inline_query))
        if BASE_URL:
        path = hashlib.sha256(TOKEN.encode()).hexdigest()[:32]
        logging.info("Webhook rejimi: %s", BASE_URL)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=path,
            webhook_url=f"{BASE_URL}/{path}",
            drop_pending_updates=True,
        )
    else:
        logging.info("Polling rejimi")
        app.run_polling()


if __name__ == "__main__":
    main()
