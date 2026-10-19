import os
import re
import json
import glob
import time
import base64
import hashlib
import asyncio
import logging
import threading
import urllib.request
from io import BytesIO
from collections import OrderedDict
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, ImageDraw, ImageFont
from telegram import (
    Update, InlineQueryResultPhoto, InlineQueryResultCachedPhoto,
    InputMediaPhoto,
)
from telegram.error import RetryAfter
from telegram.ext import (
    Application, CommandHandler, InlineQueryHandler, ContextTypes,
)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TOKEN", "")
PORT = int(os.environ.get("PORT", "8080"))

TEMPLATES_DIR = "templates"
STORAGE_FILE = "storage_chat.txt"
MAX_TEMPLATES = 30
IMG_WIDTH = 720
JPEG_QUALITY = 85
FONT_DIV = 14            # katta son = kichik font
MAX_BLOCK = 0.35
REQUIRE_DOT = True       # False = nuqta kerak emas
MAX_CACHE = 200
WAIT_SECONDS = 8         # yuklash rejimida kutish

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

FONT_PATHS = [
    "font.ttf",
    "/system/fonts/Roboto-Black.ttf",
    "/system/fonts/Roboto-Bold.ttf",
    "/system/fonts/DroidSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def detect_base_url():
    for name in ("BASE_URL", "PUBLIC_URL", "RENDER_EXTERNAL_URL"):
        v = os.environ.get(name, "").strip()
        if v:
            break
    else:
        v = ""
        for name in ("RAILWAY_PUBLIC_DOMAIN", "KOYEB_PUBLIC_DOMAIN"):
            d = os.environ.get(name, "").strip()
            if d:
                v = d
                break
    if v and not v.startswith("http"):
        v = "https://" + v
    return v.rstrip("/")


BASE_URL = detect_base_url()
URL_MODE = False          # start paytida tekshiriladi

base_images = {}
PATHS = []
img_cache = OrderedDict()
cache_lock = threading.Lock()
file_ids = {}             # (indeks, matn) -> file_id (yuklash rejimi)


# ---------- rasm yasash ----------

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
            Image.new("RGB", (720, 720), c).save(f"{TEMPLATES_DIR}/rang{i}.jpg")


def preload_templates():
    for path in get_templates():
        try:
            img = Image.open(path).convert("RGB")
        except Exception as e:
            logging.warning("Rasm ochilmadi %s: %s", path, e)
            continue
        if img.width != IMG_WIDTH:
            ratio = IMG_WIDTH / img.width
            img = img.resize((IMG_WIDTH, int(img.height * ratio)))
        base_images[path] = img
    PATHS[:] = list(base_images)
    logging.info("Yuklangan shablonlar: %d ta", len(PATHS))


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


# ---------- URL rejimi: matnni URL ichiga joylash ----------

def enc(top, bottom):
    raw = json.dumps([top, bottom], ensure_ascii=False,
                     separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def dec(tok):
    tok += "=" * (-len(tok) % 4)
    top, bottom = json.loads(base64.urlsafe_b64decode(tok).decode())
    return str(top), str(bottom)


def get_image(idx, tok):
    key = (idx, tok)
    with cache_lock:
        if key in img_cache:
            img_cache.move_to_end(key)
            return img_cache[key]
    top, bottom = dec(tok)
    data = make_meme(PATHS[idx], top, bottom)
    with cache_lock:
        img_cache[key] = data
        while len(img_cache) > MAX_CACHE:
            img_cache.popitem(last=False)
    return data


URL_RE = re.compile(r"/m/(\d+)/([A-Za-z0-9_-]+)\.jpg")


class Handler(BaseHTTPRequestHandler):
    def _serve(self, head):
        path = self.path.split("?", 1)[0]
        ctype = "image/jpeg"
        if path in ("/", "/health"):
            body, ctype = b"ok", "text/plain"
        else:
            m = URL_RE.fullmatch(path)
            if not m or int(m.group(1)) >= len(PATHS):
                self.send_error(404)
                return
            try:
                body = get_image(int(m.group(1)), m.group(2))
            except Exception as e:
                logging.warning("Rasm yasashda xato: %s", e)
                self.send_error(400)
                return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def do_GET(self):
        self._serve(False)

    def do_HEAD(self):
        self._serve(True)

    def log_message(self, *args):
        pass


def start_web():
    try:
        server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError as e:
        logging.warning("Web-server ochilmadi (port %s): %s", PORT, e)
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logging.info("Web-server %s portda ishlayapti", PORT)


def check_url():
    """BASE_URL/health ochilsa URL rejimi yoqiladi, bo'lmasa yuklash rejimi."""
    global URL_MODE
    if not BASE_URL:
        logging.info("REJIM: yuklash (BASE_URL topilmadi)")
        return
    last = None
    for _ in range(6):
        try:
            req = urllib.request.Request(
                BASE_URL + "/health", headers={"User-Agent": "memebot"}
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                if r.read(10).strip() == b"ok":
                    URL_MODE = True
                    logging.info("REJIM: URL (tez) -> %s", BASE_URL)
                    return
                last = "javob 'ok' emas"
        except Exception as e:
            last = e
        time.sleep(5)
    logging.warning("REJIM: yuklash. %s/health ochilmadi: %s", BASE_URL, last)


# ---------- yuklash rejimi (zaxira) ----------

def read_storage():
    try:
        with open(STORAGE_FILE) as f:
            return int(f.read().strip())
    except Exception:
        return None


async def delete_later(bot, chat_id, message_ids):
    for mid in message_ids:
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass


async def upload_chunk(bot, storage_id, idxs, q, top, bottom):
    datas = [make_meme(PATHS[i], top, bottom) for i in idxs]
    tm = dict(read_timeout=40, write_timeout=40,
              connect_timeout=20, pool_timeout=20)
    msgs = None
    for _ in range(3):
        try:
            if len(datas) == 1:
                msgs = [await bot.send_photo(storage_id, datas[0], **tm)]
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
    for i, msg in zip(idxs, msgs):
        file_ids[(i, q)] = msg.photo[-1].file_id
    asyncio.create_task(
        delete_later(bot, storage_id, [m.message_id for m in msgs])
    )


async def answer_upload_mode(context, iq, q, top, bottom):
    storage_id = read_storage()
    if storage_id is None:
        logging.warning("Botga /start yozing (rasm saqlash chati yo'q)")
        await iq.answer([], cache_time=0)
        return
    missing = [i for i in range(len(PATHS)) if (i, q) not in file_ids]
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
    for i in range(len(PATHS)):
        fid = file_ids.get((i, q))
        if fid:
            rid = hashlib.md5(f"{i}{q}".encode()).hexdigest()
            results.append(
                InlineQueryResultCachedPhoto(id=rid, photo_file_id=fid)
            )
    await iq.answer(results, cache_time=0, is_personal=True)


# ---------- Telegram ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        with open(STORAGE_FILE, "w") as f:
            f.write(str(update.effective_chat.id))
    except Exception as e:
        logging.warning("Chat ID saqlanmadi: %s", e)
    await update.message.reply_text(
        "Tayyor! Istalgan chatda yozing:\n"
        "@denismembot tepa matn | pastki matn.\n"
        "Oxiriga nuqta (.) qo'yishni unutmang."
    )


async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    iq = update.inline_query
    q = iq.query.strip()
    logging.info("Inline so'rov: %r (rejim: %s)", q,
                 "URL" if URL_MODE else "yuklash")
    if REQUIRE_DOT:
        if not q.endswith("."):
            await iq.answer([], cache_time=0)
            return
        q = q[:-1].strip()
    if not q:
        await iq.answer([], cache_time=0)
        return

    parts = q.split("|", 1)
    top = parts[0].strip()
    bottom = parts[1].strip() if len(parts) > 1 else ""

    try:
        if URL_MODE:
            tok = enc(top, bottom)
            results = []
            for i, p in enumerate(PATHS):
                url = f"{BASE_URL}/m/{i}/{tok}.jpg"
                w, h = base_images[p].size
                rid = hashlib.md5(f"{i}{q}".encode()).hexdigest()
                results.append(InlineQueryResultPhoto(
                    id=rid, photo_url=url, thumbnail_url=url,
                    photo_width=w, photo_height=h,
                ))
            await iq.answer(results, cache_time=10, is_personal=True)
        else:
            await answer_upload_mode(context, iq, q, top, bottom)
    except Exception as e:
        logging.warning("Javob berib bo'lmadi: %s", e)


def main():
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN topilmadi! Sozlamalarga qo'shing.")
    ensure_templates()
    preload_templates()
    start_web()
    threading.Thread(target=check_url, daemon=True).start()
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
    app.add_handler(InlineQueryHandler(inline_query))
    app.run_polling()


if __name__ == "__main__":
    main()
