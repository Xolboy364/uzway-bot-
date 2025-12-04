"""
premium_autobot.py
Ultra-premium Telegram Avto E'lon Bot — Termux uchun
python-telegram-bot v20.6 bilan ishlaydi, Pillow kerak emas
"""

import os
import re
import io
import json
import time
import aiosqlite
import hashlib
import asyncio
import datetime
import aiohttp
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    LabeledPrice,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    filters,
    PreCheckoutQueryHandler,
)
from telegram.helpers import escape_markdown

load_dotenv()

# -------------- CONFIG --------------
TOKEN = os.getenv("TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID") or 0)
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/your_channel")
BOT_USERNAME = os.getenv("BOT_USERNAME", "my_autobot")
PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN")  # masalan: 123456:TEST:123456
SIGHT_USER = os.getenv("SIGHT_USER")
SIGHT_SECRET = os.getenv("SIGHT_SECRET")

DB_FILE = os.getenv("DB_FILE", "bot_data.db")
MEDIA_DIR = os.getenv("MEDIA_DIR", "media_cache")
MAX_PHOTOS = 6
AD_LIFETIME_DAYS = int(os.getenv("AD_LIFETIME_DAYS", "7"))
RATE_LIMIT_PER_HOUR = int(os.getenv("RATE_LIMIT_PER_HOUR", "3"))
ADMIN_USERS = set(map(int, os.getenv("ADMIN_USERS", "").split(","))) if os.getenv("ADMIN_USERS") else set()

# Conversation states
(
    PHOTOS, PURPOSE, BRAND, MODEL, YEAR, MILEAGE, FUEL, GEARBOX, COLOR,
    PAINT, TECH, PRICE, CONDITION, DELIVERY, PHONE, LOCATION, COMMENT,
    CONFIRM, PAYMENT,
) = range(19)

# -------------- UTILITIES --------------
os.makedirs(MEDIA_DIR, exist_ok=True)

def now_iso():
    return datetime.datetime.utcnow().isoformat()

def normalize_price(text: str) -> str:
    digits = re.sub(r"[^\d]", "", text or "")
    return digits or "0"

def valid_phone(text: str) -> bool:
    return bool(re.match(r"^\+?\d[\d\s\-]{7,}$", text.strip()))

def valid_year(text: str) -> bool:
    try:
        y = int(re.sub(r"\D", "", text))
        return 1900 <= y <= datetime.datetime.now().year + 1
    except:
        return False

BLOCK_WORDS = ["porn", "porno", "sex", "seks", "xxx", "nude", "naked", "narcotic", "narkotik", "heroin", "cocaine", "weed", "qurol", "bomb", "bomba", "voyaga", "18+"]
BLOCK_PATTERNS = [re.compile(rf"\b{re.escape(w)}\b", re.I) for w in BLOCK_WORDS]

def is_text_safe(text: str) -> bool:
    if not text:
        return True
    return not any(p.search(text) for p in BLOCK_PATTERNS)

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

async def save_photo_bytes(file_bytes: bytes, prefix="img") -> dict:
    digest = sha256_bytes(file_bytes)
    filename = f"{prefix}_{digest}.jpg"
    path = os.path.join(MEDIA_DIR, filename)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(file_bytes)
    return {"path": path, "hash": digest}

# -------------- DATABASE --------------
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS ads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author_id INTEGER,
            ad_number INTEGER,
            data_json TEXT,
            created_at TEXT,
            posted_at TEXT,
            channel_message_id INTEGER,
            is_active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ad_id INTEGER,
            file_hash TEXT,
            file_path TEXT
        );
        CREATE TABLE IF NOT EXISTS reactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ad_id INTEGER,
            type TEXT,
            count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS rate_limits (
            user_id INTEGER,
            window_start INTEGER,
            count INTEGER,
            PRIMARY KEY(user_id, window_start)
        );
        """)
        await db.commit()

# -------------- NSFW CHECK (SightEngine) --------------
async def is_photo_safe_sight(photo_bytes: bytes, threshold=0.75, timeout=8):
    if not SIGHT_USER or not SIGHT_SECRET:
        return True
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            data = {
                "models": "nudity-2.0,offensive,gore",
                "api_user": SIGHT_USER,
                "api_secret": SIGHT_SECRET
            }
            files = {"media": photo_bytes}
            async with s.post("https://api.sightengine.com/1.0/check.json", data=data, files=files) as resp:
                if resp.status != 200:
                    return False
                res = await resp.json()
                nud = res.get("nudity", {})
                score = max(
                    nud.get("sexual_activity", 0),
                    nud.get("porn", 0),
                    nud.get("suggestive", 0),
                    res.get("weapon", 0) or 0,
                    res.get("gore_prob", 0) or 0,
                )
                return score < threshold
    except Exception:
        return False

# -------------- RATE LIMIT --------------
async def rate_limit_check(user_id: int, db):
    now = int(time.time())
    window = now - (now % 3600)
    async with db.execute("SELECT count FROM rate_limits WHERE user_id=? AND window_start=?", (user_id, window)) as cur:
        row = await cur.fetchone()
    if row:
        cnt = row[0]
        if cnt >= RATE_LIMIT_PER_HOUR:
            return False
        await db.execute("UPDATE rate_limits SET count=count+1 WHERE user_id=? AND window_start=?", (user_id, window))
    else:
        await db.execute("INSERT OR REPLACE INTO rate_limits(user_id,window_start,count) VALUES(?,?,?)", (user_id, window, 1))
    await db.commit()
    return True

# -------------- AD TEXT --------------
def generate_ad_text(data: dict, ad_number: int) -> str:
    purpose_icon = {"Sotish": "🟢 Sotiladi", "Ijaraga berish": "🟡 Ijaraga beriladi", "Almashish": "🔄 Almashish"}.get(data.get("purpose"), "📌 E'lon")
    condition_icon = {"yangi": "🆕 Yangi", "ideal": "✨ Ideal", "yaxshi": "👍 Yaxshi", "o‘rtacha": "👌 O‘rtacha", "yomon": "❌ Yomon"}.get(data.get("condition", "").lower(), "ℹ Holati")
    paint_icon = {"kraska bo‘lgan": "🎨 Kraska bo‘lgan", "kraska bo‘lmagan": "🟢 Kraska bo‘lmagan", "petno bor": "⚠️ Petno bor"}.get(data.get("paint", "").lower(), "🎨 Kraska haqida ma'lumot yo‘q")
    tech_icon = {"ideal": "✨ Ideal", "yaxshi": "👍 Yaxshi", "o‘rtacha": "👌 O‘rtacha", "yomon": "❌ Yomon"}.get(data.get("tech", "").lower(), "ℹ Texnik holat")

    ad = (
        f"#{str(ad_number).zfill(4)} • <b>{escape_markdown(data.get('brand',''), version=2)} {escape_markdown(data.get('model',''), version=2)}</b>\n\n"
        f"{purpose_icon}\n\n"
        f"💰 Narxi: <b>{escape_markdown(data.get('price','–'), version=2)} so‘m</b>\n"
        f"📅 Yili: {escape_markdown(data.get('year','–'), version=2)}\n"
        f"🛣 Probegi: {escape_markdown(data.get('mileage','–'), version=2)} km\n"
        f"⛽ Yoqilg‘i: {escape_markdown(data.get('fuel','–'), version=2)}\n"
        f"⚙️ Korobka: {escape_markdown(data.get('gearbox','–'), version=2)}\n"
        f"🎨 Rangi: {escape_markdown(data.get('color','–'), version=2)}\n\n"
        f"📌 Holati: {condition_icon}\n{paint_icon}\n{tech_icon}\n"
        f"🚚 Yetkazib berish: {escape_markdown(data.get('delivery','–'), version=2)}\n\n"
        f"📍 Joylashuv: {escape_markdown(data.get('location','–'), version=2)}\n"
        f"📞 Aloqa: <code>{escape_markdown(data.get('phone','–'), version=2)}</code>\n\n"
        + (f"💬 Izoh: {escape_markdown(data.get('comment',''), version=2)}\n" if data.get('comment') else "")
        + f"🗓 {datetime.datetime.now().strftime('%d.%m.%Y | %H:%M')}\n"
          f"━━━━━━━━━━━━━━\n"
          f"@uzwaykanal  |  @{BOT_USERNAME}\n\n"
          f"⚠️ DIQQAT: Kanal faqat eʼlon joylash uchun. Oldi-sotdi majburiyatini o‘z zimmasiga olmaydi."
    )
    return ad

# -------------- BACKGROUND TASK --------------
async def background_tasks(app: Application):
    # v20.6 da wait_until_ready yo'q, shuning uchun bot ishga tushganini kutamiz
    while not app.running:
        await asyncio.sleep(1)
    print("Background task ishga tushdi (e'lonlarni o'chirish)")
    while True:
        try:
            async with aiosqlite.connect(DB_FILE) as db:
                cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=AD_LIFETIME_DAYS)).isoformat()
                await db.execute(
                    "UPDATE ads SET is_active=0 WHERE is_active=1 AND posted_at IS NOT NULL AND posted_at<?",
                    (cutoff,),
                )
                await db.commit()
        except Exception as e:
            print("Background error:", e)
        await asyncio.sleep(3600)

# -------------- HANDLERS --------------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users(id,username,first_name,last_name,created_at) VALUES(?,?,?,?,?)",
            (uid, update.effective_user.username, update.effective_user.first_name, update.effective_user.last_name, now_iso())
        )
        await db.commit()

    await update.message.reply_text(
        "Assalomu alaykum! 🚗\n@uzwaykanal — Avto e'lon botiga xush kelibsiz!\nYangi e'lon yaratish uchun tugmani bosing.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Yangi e'lon", callback_data="new_ad")]])
    )

async def new_ad_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id

    async with aiosqlite.connect(DB_FILE) as db:
        if not await rate_limit_check(uid, db):
            await q.edit_message_text("Siz soatiga cheklovdan oshdingiz. Biroz kutib qayta urinib ko'ring.")
            return ConversationHandler.END

    context.user_data.clear()
    context.user_data["photos"] = []
    await q.edit_message_text(
        "📸 Rasmlarni yuboring (1-6 ta). Rasmlar tugagach «Rasmlar tayyor» tugmasini bosing.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Rasmlar tayyor", callback_data="photos_done")]])
    )
    return PHOTOS

async def photos_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        return PHOTOS
    if len(context.user_data.get("photos", [])) >= MAX_PHOTOS:
        await update.message.reply_text(f"❗ Maksimum {MAX_PHOTOS} ta rasm.")
        return PHOTOS

    file = update.message.photo[-1]
    b = await (await context.bot.get_file(file.file_id)).download_as_bytearray()

    safe = await is_photo_safe_sight(bytes(b))
    if not safe:
        await update.message.reply_text("❌ Rasm taqiqlangan deb topildi. Siz bloklandingiz.")
        return ConversationHandler.END

    saved = await save_photo_bytes(bytes(b))
    if saved["hash"] in [p["hash"] for p in context.user_data.get("photos", [])]:
        await update.message.reply_text("❗ Bu rasm avval yuborilgan.")
        return PHOTOS

    context.user_data["photos"].append(saved)
    await update.message.reply_text(f"✅ {len(context.user_data['photos'])}/{MAX_PHOTOS} rasm qabul qilindi.")
    return PHOTOS

async def photos_done_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not context.user_data.get("photos"):
        await q.edit_message_text("❌ Kamida 1 ta rasm kerak!")
        return PHOTOS

    await q.edit_message_text("📌 E'lon maqsadini tanlang:", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Sotish", callback_data="p_sotish")],
        [InlineKeyboardButton("🟡 Ijaraga berish", callback_data="p_ijara")],
        [InlineKeyboardButton("🔄 Almashish", callback_data="p_almashish")],
    ]))
    return PURPOSE

async def purpose_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    mapping = {"p_sotish": "Sotish", "p_ijara": "Ijaraga berish", "p_almashish": "Almashish"}
    context.user_data["purpose"] = mapping.get(q.data, "Sotish")
    await q.edit_message_text("🚗 Markasini yozing (masalan: Chevrolet, Toyota)")
    return BRAND

# Keyingi qadamlar uchun umumiy funksiya
async def get_text(update: Update, context: ContextTypes.DEFAULT_TYPE, field: str, prompt: str, next_state: int):
    text = update.message.text.strip() if update.message.text else ""
    if not text:
        await update.message.reply_text("❗ Bo'sh yuborildi, qayta yozing.")
        return next_state
    if not is_text_safe(text):
        await update.message.reply_text("❗ Taqiqlangan so'z bor. Bloklandingiz.")
        return ConversationHandler.END

    if field == "phone" and not valid_phone(text):
        await update.message.reply_text("❌ Telefon formati noto'g'ri (masalan: +998901234567)")
        return PHONE
    if field == "year" and not valid_year(text):
        await update.message.reply_text("❌ Yil noto'g'ri (1900-hozirgi yilgacha).")
        return YEAR
    if field == "price":
        text = normalize_price(text)

    context.user_data[field] = text
    await update.message.reply_text(prompt)
    return next_state

async def brand_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await get_text(update, context, "brand", "🚗 Modelini yozing (masalan: Cobalt, Spark)", MODEL)

async def model_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await get_text(update, context, "model", "📅 Yilini yozing (masalan: 2022)", YEAR)

async def year_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await get_text(update, context, "year", "🛣 Probegini yozing (km)", MILEAGE)

async def mileage_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await get_text(update, context, "mileage", "⛽ Yoqilg‘i turini yozing (Benzin, Dizel...)", FUEL)

async def fuel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await get_text(update, context, "fuel", "⚙️ Korobka turini yozing (Avtomat, Mexanika)", GEARBOX)

async def gearbox_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await get_text(update, context, "gearbox", "🎨 Rangini yozing (Oq, Qora...)", COLOR)

async def color_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await get_text(update, context, "color", "🎨 Kraska holatini yozing (bo‘lgan/bo‘lmagan)", PAINT)

async def paint_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await get_text(update, context, "paint", "🔧 Texnik holatini yozing (Ideal, Yaxshi...)", TECH)

async def tech_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await get_text(update, context, "tech", "💰 Narxini yozing (faqat raqamlar)", PRICE)

async def price_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await get_text(update, context, "price", "📌 Umumiy holatini yozing (Yangi, Ideal...)", CONDITION)

async def condition_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await get_text(update, context, "condition", "🚚 Yetkazib berish bor/yo‘q? (Bor, Yo‘q, Kelishiladi)", DELIVERY)

async def delivery_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await get_text(update, context, "delivery", "📞 Telefon raqamingizni yozing", PHONE)

async def phone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await get_text(update, context, "phone", "📍 Joylashuvni yozing (shahar/viloyat)", LOCATION)

async def location_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await get_text(update, context, "location", "💬 Qo‘shimcha izoh (ixtiyoriy)", COMMENT)

async def comment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip() if update.message.text else ""
    if text and not is_text_safe(text):
        await update.message.reply_text("❗ Izohda taqiqlangan so‘z bor.")
        return ConversationHandler.END
    context.user_data["comment"] = text

    # Saqlash
    async with aiosqlite.connect(DB_FILE) as db:
        data_json = json.dumps(context.user_data, ensure_ascii=False)
        cur = await db.execute("INSERT INTO ads (author_id, data_json, created_at) VALUES (?,?,?)",
                               (update.effective_user.id, data_json, now_iso()))
        await db.commit()
        ad_id = cur.lastrowid
        context.user_data["ad_number"] = ad_id

        for p in context.user_data.get("photos", []):
            await db.execute("INSERT INTO photos (ad_id, file_hash, file_path) VALUES (?,?,?)",
                             (ad_id, p["hash"], p["path"]))
        await db.commit()

    ad_text = generate_ad_text(context.user_data, ad_id)
    context.user_data["ad_text"] = ad_text

    await update.message.reply_text("✅ E'lon tayyor! Tekshirib ko‘ring:", parse_mode="HTML")
    if context.user_data["photos"]:
        with open(context.user_data["photos"][0]["path"], "rb") as f:
            await update.message.reply_photo(f, caption=ad_text, parse_mode="HTML")
    else:
        await update.message.reply_text(ad_text, parse_mode="HTML")

    await update.message.reply_text("Tasdiqlash uchun tugmani bosing:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("👀 Ko‘rish", callback_data="preview"), InlineKeyboardButton("✏ Tahrirlash", callback_data="edit")],
            [InlineKeyboardButton("✅ Tasdiqlash va to‘lov", callback_data="pay")]
        ]))
    return CONFIRM

async def confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "preview":
        await q.message.reply_text(context.user_data.get("ad_text", ""), parse_mode="HTML")
        return CONFIRM
    if q.data == "edit":
        await q.edit_message_text("Tahrirlash hali yo‘q. Yangi e'lon yarating.")
        return ConversationHandler.END
    if q.data == "pay":
        prices = [LabeledPrice("E'lon joylash (7 kun)", 15000 * 100)]  # 15 000 so‘m
        payload = f"ad_{update.effective_user.id}_{context.user_data['ad_number']}"
        await context.bot.send_invoice(
            chat_id=update.effective_chat.id,
            title="Avto e'lon joylash",
            description="7 kun kanalga joylanadi",
            payload=payload,
            provider_token=PROVIDER_TOKEN,
            currency="UZS",
            prices=prices,
        )
        return PAYMENT

async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    if not query.invoice_payload.startswith("ad_"):
        await query.answer(ok=False, error_message="Xato!")
        return
    await query.answer(ok=True)

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payload = update.message.successful_payment.invoice_payload
    _, uid, ad_id = payload.split("_")
    ad_id = int(ad_id)

    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT data_json FROM ads WHERE id=?", (ad_id,)) as cur:
            row = await cur.fetchone()
        data = json.loads(row[0])

        async with db.execute("SELECT file_path FROM photos WHERE ad_id=?", (ad_id,)) as cur:
            photos = [row[0] async for row in cur]

        ad_text = generate_ad_text(data, ad_id)

        sent = None
        if photos:
            media = [InputMediaPhoto(open(p, "rb") if i > 0 else open(photos[0], "rb"), caption=ad_text, parse_mode="HTML") for i, p in enumerate(photos)]
            sent = await context.bot.send_media_group(CHANNEL_ID, media)
        else:
            sent = await context.bot.send_message(CHANNEL_ID, ad_text, parse_mode="HTML")

        message_id = sent[0].message_id if photos else sent.message_id

        await db.execute("UPDATE ads SET posted_at=?, channel_message_id=? WHERE id=?", (now_iso(), message_id, ad_id))
        for t in ["like", "good", "bad"]:
            await db.execute("INSERT INTO reactions (ad_id, type, count) VALUES (?,?,0)", (ad_id, t))
        await db.commit()

    await context.bot.send_message(
        CHANNEL_ID,
        "Baholang:",
        reply_to_message_id=message_id,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❤️ 0", callback_data=f"r_like_{ad_id}"),
            InlineKeyboardButton("👍 0", callback_data=f"r_good_{ad_id}"),
            InlineKeyboardButton("👎 0", callback_data=f"r_bad_{ad_id}"),
        ]])
    )

    await update.message.reply_text("✅ To‘lov qabul qilindi! E'lon kanalga joylandi.")
    context.user_data.clear()

async def reaction_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not q.data.startswith("r_"):
        return
    typ, ad_id = q.data.split("_")[1:]
    ad_id = int(ad_id)

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("UPDATE reactions SET count=count+1 WHERE ad_id=? AND type=?", (ad_id, typ))
        await db.commit()
        rows = await (await db.execute("SELECT type,count FROM reactions WHERE ad_id=?", (ad_id,))).fetchall()
    counts = {r[0]: r[1] for r in rows}
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"❤️ {counts.get('like',0)}", callback_data=f"r_like_{ad_id}"),
        InlineKeyboardButton(f"👍 {counts.get('good',0)}", callback_data=f"r_good_{ad_id}"),
        InlineKeyboardButton(f"👎 {counts.get('bad',0)}", callback_data=f"r_bad_{ad_id}"),
    ]])
    await q.edit_message_reply_markup(reply_markup=kb)

# Admin buyruqlari
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USERS:
        return
    async with aiosqlite.connect(DB_FILE) as db:
        total = (await (await db.execute("SELECT COUNT(*) FROM ads")).fetchone())[0]
        active = (await (await db.execute("SELECT COUNT(*) FROM ads WHERE is_active=1")).fetchone())[0]
        users = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
    await update.message.reply_text(f"Jami e'lon: {total}\nFaol: {active}\nFoydalanuvchilar: {users}")

# -------------- MAIN --------------
async def main():
    await init_db()
    app = Application.builder().token(TOKEN).build()

    # Background task
    asyncio.create_task(background_tasks(app))

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(new_ad_start, "^new_ad$")],
        states={
            PHOTOS: [MessageHandler(filters.PHOTO, photos_handler), CallbackQueryHandler(photos_done_handler, "^photos_done$")],
            PURPOSE: [CallbackQueryHandler(purpose_handler, "^p_")],
            BRAND: [MessageHandler(filters.TEXT & ~filters.COMMAND, brand_handler)],
            MODEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, model_handler)],
            YEAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, year_handler)],
            MILEAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, mileage_handler)],
            FUEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, fuel_handler)],
            GEARBOX: [MessageHandler(filters.TEXT & ~filters.COMMAND, gearbox_handler)],
            COLOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, color_handler)],
            PAINT: [MessageHandler(filters.TEXT & ~filters.COMMAND, paint_handler)],
            TECH: [MessageHandler(filters.TEXT & ~filters.COMMAND, tech_handler)],
            PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, price_handler)],
            CONDITION: [MessageHandler(filters.TEXT & ~filters.COMMAND, condition_handler)],
            DELIVERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, delivery_handler)],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, phone_handler)],
            LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, location_handler)],
            COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, comment_handler)],
            CONFIRM: [CallbackQueryHandler(confirm_handler, "^(preview|edit|pay)$")],
            PAYMENT: [PreCheckoutQueryHandler(precheckout_handler), MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler)],
        },
        fallbacks=[],
    )

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(reaction_handler, "^r_"))
    app.add_handler(CommandHandler("stats", stats))

    print("Premium Autobot ishga tushdi...")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
