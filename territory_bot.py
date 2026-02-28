#!/usr/bin/env python3
"""
🗺️ Toshkent Territory Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ Trek Mini App orqali (GPS auto-tracking)
✅ /api/trek_submit — fetch orqali, limit yo'q!
✅ CORS to'liq hal qilindi
✅ WebApp Data (initData) xavfsizlik (HMAC) tekshiruvi qo'shildi
"""

import asyncio
import logging
import os
import json
import math
import hmac
import hashlib
from urllib.parse import parse_qs
from datetime import datetime, timedelta
from contextlib import contextmanager
from aiohttp import web

import sqlite3
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, WebAppInfo
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.constants import ParseMode

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN    = os.getenv("BOT_TOKEN", "8664008696:AAEy6cuhP0yKKQu1Tp-IEm9FwTWCVRCrYOg")
DB_PATH      = os.getenv("DB_PATH", "territory.db")
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://iyusuf1-lang.github.io/my_territory_tash_bot/")

TEAMS = {
    "red":    {"name": "🔴 Qizil",   "emoji": "🔴"},
    "blue":   {"name": "🔵 Ko'k",    "emoji": "🔵"},
    "green":  {"name": "🟢 Yashil",  "emoji": "🟢"},
    "yellow": {"name": "🟡 Sariq",   "emoji": "🟡"},
}

MODE_IDLE   = "idle"
MODE_CIRCLE = "circle"
notif_cache: dict = {}

# Global app reference (trek_submit uchun kerak)
_app: Application = None

# Orqa fon jarayonlari (API, checker) xotiradan o'chib ketmasligi uchun
background_tasks = set()

# ══════════════════════════════════════════════════════
# CORS HEADERS
# ══════════════════════════════════════════════════════

CORS_HEADERS = {
    "Access-Control-Allow-Origin":  "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-Telegram-Init-Data",
}


async def cors_middleware(app, handler):
    async def middleware(request):
        if request.method == "OPTIONS":
            return web.Response(status=200, headers=CORS_HEADERS)
        response = await handler(request)
        for k, v in CORS_HEADERS.items():
            response.headers[k] = v
        return response
    return middleware


# ══════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id        INTEGER PRIMARY KEY,
            username       TEXT,
            first_name     TEXT,
            team           TEXT DEFAULT NULL,
            total_km       REAL DEFAULT 0,
            zones_owned    INTEGER DEFAULT 0,
            zones_taken    INTEGER DEFAULT 0,
            referred_by    INTEGER DEFAULT NULL,
            referral_count INTEGER DEFAULT 0,
            created_at     TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS zones (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id    INTEGER NOT NULL,
            team        TEXT NOT NULL,
            name        TEXT,
            zone_type   TEXT NOT NULL,
            geometry    TEXT NOT NULL,
            center_lat  REAL NOT NULL,
            center_lng  REAL NOT NULL,
            radius_m    REAL,
            area_m2     REAL DEFAULT 0,
            active      INTEGER DEFAULT 1,
            photo_url   TEXT DEFAULT NULL,
            created_at  TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (owner_id) REFERENCES users(user_id)
        );
        CREATE TABLE IF NOT EXISTS zone_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            zone_id     INTEGER NOT NULL,
            from_user   INTEGER,
            from_team   TEXT,
            to_user     INTEGER NOT NULL,
            to_team     TEXT NOT NULL,
            action      TEXT NOT NULL,
            captured_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS treks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            points      TEXT NOT NULL DEFAULT '[]',
            distance_m  REAL DEFAULT 0,
            started_at  TEXT DEFAULT (datetime('now')),
            finished_at TEXT,
            status      TEXT DEFAULT 'active',
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        );
        CREATE TABLE IF NOT EXISTS achievements (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   INTEGER NOT NULL,
            code      TEXT NOT NULL,
            earned_at TEXT DEFAULT (datetime('now')),
            UNIQUE(user_id, code)
        );
        """)
        conn.commit()
    logger.info("✅ DB initialized")


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_user(user_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def upsert_user(user_id: int, username: str, first_name: str):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO users (user_id, username, first_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username, first_name=excluded.first_name
        """, (user_id, username or "", first_name or "Nomsiz"))


def set_team(user_id: int, team: str):
    with get_db() as conn:
        conn.execute("UPDATE users SET team=? WHERE user_id=?", (team, user_id))


# ══════════════════════════════════════════════════════
# TELEGRAM INIT DATA PARSER (SECURE)
# ══════════════════════════════════════════════════════

def parse_init_data(init_data: str) -> dict | None:
    """tg.initData ni HMAC orqali xavfsiz tekshirish va user ma'lumotlarini olish"""
    try:
        parsed = parse_qs(init_data)
        if "hash" not in parsed:
            return None
        
        # Hashni ajratib olish
        hash_val = parsed.pop("hash")[0]
        
        # Parametrlarni alifbo tartibida birlashtirish
        data_check_string = "\n".join(
            f"{k}={parsed[k][0]}" for k in sorted(parsed.keys())
        )
        
        # Token orqali tekshirish kalitini yaratish
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        
        if calc_hash != hash_val:
            logger.warning("🚨 Xavfsizlik: Qalbaki initData aniqlandi!")
            return None

        # Agar hash to'g'ri bo'lsa, user ma'lumotlarini qaytarish
        user_str = parsed.get("user", [None])[0]
        if not user_str:
            return None
        return json.loads(user_str)
    except Exception as e:
        logger.warning(f"initData parse error: {e}")
        return None


# ══════════════════════════════════════════════════════
# GEOMETRY
# ══════════════════════════════════════════════════════

def haversine(lat1, lon1, lat2, lon2) -> float:
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return 2 * R * math.asin(math.sqrt(a))


def point_in_polygon(lat, lng, polygon: list) -> bool:
    n = len(polygon)
    inside = False
    px, py = lng, lat
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]["lng"], polygon[i]["lat"]
        xj, yj = polygon[j]["lng"], polygon[j]["lat"]
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def polygon_centroid(points: list) -> tuple:
    lat = sum(p["lat"] for p in points) / len(points)
    lng = sum(p["lng"] for p in points) / len(points)
    return lat, lng


def polygon_area_m2(points: list) -> float:
    if len(points) < 3:
        return 0
    lat0, lng0 = points[0]["lat"], points[0]["lng"]
    coords = []
    for p in points:
        x = haversine(lat0, lng0, lat0, p["lng"])
        y = haversine(lat0, lng0, p["lat"], lng0)
        coords.append((x, y))
    n = len(coords)
    area = 0
    for i in range(n):
        j = (i + 1) % n
        area += coords[i][0] * coords[j][1]
        area -= coords[j][0] * coords[i][1]
    return abs(area) / 2


def zone_is_captured_by_trek(trek_points: list, zone: dict) -> bool:
    return point_in_polygon(zone["center_lat"], zone["center_lng"], trek_points)


# ══════════════════════════════════════════════════════
# ZONE OPERATIONS
# ══════════════════════════════════════════════════════

def create_zone_circle(user_id, team, lat, lng, radius) -> int:
    geom = json.dumps({"lat": lat, "lng": lng, "radius": radius})
    area = math.pi * radius ** 2
    with get_db() as conn:
        cur = conn.execute("""
            INSERT INTO zones (owner_id, team, zone_type, geometry, center_lat, center_lng, radius_m, area_m2)
            VALUES (?, ?, 'circle', ?, ?, ?, ?, ?)
        """, (user_id, team, geom, lat, lng, radius, area))
        zone_id = cur.lastrowid
        conn.execute("INSERT INTO zone_history (zone_id, to_user, to_team, action) VALUES (?, ?, ?, 'created')",
                     (zone_id, user_id, team))
        conn.execute("UPDATE users SET zones_owned = zones_owned + 1 WHERE user_id=?", (user_id,))
    return zone_id


async def create_zone_circle_with_photo(bot, user_id, team, lat, lng, radius) -> int:
    zone_id = create_zone_circle(user_id, team, lat, lng, radius)
    photo_url = await get_user_photo_url(bot, user_id)
    if photo_url:
        update_zone_photo(zone_id, photo_url)
    return zone_id


def create_zone_polygon(user_id, team, points) -> int:
    geom = json.dumps(points)
    clat, clng = polygon_centroid(points)
    area = polygon_area_m2(points)
    with get_db() as conn:
        cur = conn.execute("""
            INSERT INTO zones (owner_id, team, zone_type, geometry, center_lat, center_lng, area_m2)
            VALUES (?, ?, 'polygon', ?, ?, ?, ?)
        """, (user_id, team, geom, clat, clng, area))
        zone_id = cur.lastrowid
        conn.execute("INSERT INTO zone_history (zone_id, to_user, to_team, action) VALUES (?, ?, ?, 'created')",
                     (zone_id, user_id, team))
        conn.execute("UPDATE users SET zones_owned = zones_owned + 1 WHERE user_id=?", (user_id,))
    return zone_id


async def create_zone_polygon_with_photo(bot, user_id, team, points) -> int:
    zone_id = create_zone_polygon(user_id, team, points)
    photo_url = await get_user_photo_url(bot, user_id)
    if photo_url:
        update_zone_photo(zone_id, photo_url)
    return zone_id


def capture_zone(zone_id, new_owner, new_team) -> dict | None:
    with get_db() as conn:
        z = conn.execute("SELECT * FROM zones WHERE id=?", (zone_id,)).fetchone()
        if not z:
            return None
        z = dict(z)
        conn.execute("UPDATE zones SET owner_id=?, team=?, photo_url=NULL WHERE id=?", (new_owner, new_team, zone_id))
        conn.execute("""
            INSERT INTO zone_history (zone_id, from_user, from_team, to_user, to_team, action)
            VALUES (?, ?, ?, ?, ?, 'captured')
        """, (zone_id, z["owner_id"], z["team"], new_owner, new_team))
        conn.execute("UPDATE users SET zones_owned = MAX(0, zones_owned - 1) WHERE user_id=?", (z["owner_id"],))
        conn.execute("UPDATE users SET zones_owned = zones_owned + 1, zones_taken = zones_taken + 1 WHERE user_id=?", (new_owner,))
    return z


def get_all_zones() -> list:
    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM zones WHERE active=1").fetchall()]


def get_zones_near(lat, lng, radius_m=2000) -> list:
    zones = get_all_zones()
    nearby = []
    for z in zones:
        d = haversine(lat, lng, z["center_lat"], z["center_lng"])
        if d <= radius_m:
            z["distance"] = d
            nearby.append(z)
    return sorted(nearby, key=lambda x: x["distance"])


def get_user_zones(user_id) -> list:
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM zones WHERE owner_id=? AND active=1", (user_id,)
        ).fetchall()]


async def get_user_photo_url(bot, user_id: int) -> str | None:
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        if photos.total_count == 0:
            return None
        file_id = photos.photos[0][-1].file_id
        file = await bot.get_file(file_id)
        return f"https://api.telegram.org/file/bot{bot.token}/{file.file_path}"
    except Exception as e:
        logger.warning(f"Photo fetch error for {user_id}: {e}")
        return None


def update_zone_photo(zone_id: int, photo_url: str):
    with get_db() as conn:
        conn.execute("UPDATE zones SET photo_url=? WHERE id=?", (photo_url, zone_id))


def get_zone_history(zone_id) -> list:
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM zone_history WHERE zone_id=? ORDER BY captured_at DESC LIMIT 10",
            (zone_id,)
        ).fetchall()]


# ══════════════════════════════════════════════════════
# TREK PROCESSING (umumiy funksiya — bot va API uchun)
# ══════════════════════════════════════════════════════

async def process_trek(bot, user_id: int, points: list, team: str, closed: bool, dist_m: float) -> str:
    """Trek ni ishlash: zona yaratish, km qo'shish, xabar matnini qaytarish"""

    if not points or len(points) < 5:
        return "❗ Trek juda qisqa (kamida 5 nuqta kerak)."

    db_user = get_user(user_id)
    if not db_user:
        return "❗ Foydalanuvchi topilmadi. /start bosing."

    # Bot dagi jamoani ustunlik qilish
    if db_user.get("team"):
        team = db_user["team"]

    if not team or team not in TEAMS:
        return "❗ Jamoa tanlanmagan. /start bosing."

    dist_km = dist_m / 1000

    # Trek ni DB ga saqlash
    with get_db() as conn:
        conn.execute("UPDATE treks SET status='cancelled' WHERE user_id=? AND status='active'", (user_id,))
        conn.execute(
            "INSERT INTO treks (user_id, points, distance_m, started_at, finished_at, status) "
            "VALUES (?, ?, ?, datetime('now'), datetime('now'), 'finished')",
            (user_id, json.dumps(points), dist_m)
        )
        conn.execute("UPDATE users SET total_km = total_km + ? WHERE user_id=?", (dist_km, user_id))

    msg = f"⏹ *Trek yakunlandi!*\n📏 {dist_km:.3f} km | 📍 {len(points)} nuqta\n"

    if closed:
        zone_id = await create_zone_polygon_with_photo(bot, user_id, team, points)
        area = polygon_area_m2(points)
        captured = []

        for z in get_all_zones():
            if z["owner_id"] == user_id or z["id"] == zone_id:
                continue
            if zone_is_captured_by_trek(points, z):
                old = capture_zone(z["id"], user_id, team)
                if old:
                    captured.append(old)
                    team_info = TEAMS[team]
                    z_name = old["name"] or f"Zona #{old['id']}"
                    try:
                        await bot.send_message(
                            chat_id=old["owner_id"],
                            text=f"⚔️ *Zonangiz egallandi!*\n\n"
                                 f"🏴 {z_name}\n"
                                 f"{team_info['emoji']} {db_user['first_name']} tomonidan!\n\n"
                                 f"Qaytarib oling! 💪",
                            parse_mode=ParseMode.MARKDOWN,
                        )
                    except Exception as e:
                        logger.warning(f"Capture notif error: {e}")

        await check_and_award(user_id, bot, get_user(user_id))

        te = TEAMS[team]
        msg += (
            f"\n✅ *Zona yaratildi #{zone_id}*\n"
            f"{te['emoji']} {te['name']}\n"
            f"📐 Maydon: {area/10000:.4f} ga\n"
        )
        if captured:
            msg += f"\n⚔️ *{len(captured)} zona egallandi!*\n"
            for c in captured:
                te_c = TEAMS.get(c["team"], {"emoji": "❓"})["emoji"]
                msg += f"  {te_c} {c['name'] or '#' + str(c['id'])}\n"
    else:
        d_close = haversine(
            points[0]["lat"], points[0]["lng"],
            points[-1]["lat"], points[-1]["lng"]
        )
        msg += (
            f"\n⚠️ *Trek yopiq emas.*\n"
            f"Boshlang'ich nuqtaga: {d_close:.0f}m qoldi.\n"
            f"50m yaqinlashganda yopiq hisoblanadi."
        )

    return msg


# ══════════════════════════════════════════════════════
# KEYBOARDS
# ══════════════════════════════════════════════════════

def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        [KeyboardButton("📍 Joylashuvni yuborish", request_location=True)],
        [KeyboardButton("▶️ Trek boshlash"), KeyboardButton("🗺 Zonalarim")],
        [KeyboardButton("🌍 Xarita"),        KeyboardButton("📊 Statistika")],
        [KeyboardButton("🏆 Reyting"),       KeyboardButton("🏅 Yutuqlar")],
        [KeyboardButton("👥 Referral"),      KeyboardButton("❓ Yordam")],
    ], resize_keyboard=True)


def team_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔴 Qizil",  callback_data="team:red"),
         InlineKeyboardButton("🔵 Ko'k",   callback_data="team:blue")],
        [InlineKeyboardButton("🟢 Yashil", callback_data="team:green"),
         InlineKeyboardButton("🟡 Sariq",  callback_data="team:yellow")],
    ])


def zone_create_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⭕ Doira zona yaratish", callback_data="zone:circle")],
        [InlineKeyboardButton("❌ Bekor",               callback_data="zone:cancel")],
    ])


def radius_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("50m",  callback_data="radius:50"),
         InlineKeyboardButton("100m", callback_data="radius:100"),
         InlineKeyboardButton("200m", callback_data="radius:200")],
        [InlineKeyboardButton("300m", callback_data="radius:300"),
         InlineKeyboardButton("500m", callback_data="radius:500"),
         InlineKeyboardButton("1km",  callback_data="radius:1000")],
        [InlineKeyboardButton("❌ Bekor", callback_data="zone:cancel")],
    ])


def trek_miniapp_kb(team: str = "") -> ReplyKeyboardMarkup:
    trek_url = MINI_APP_URL.rstrip("/") + "/trek.html"
    if team:
        trek_url += f"?team={team}"
    return ReplyKeyboardMarkup([[
        KeyboardButton("🗺 Trekni boshlash", web_app=WebAppInfo(url=trek_url))
    ]], resize_keyboard=True, one_time_keyboard=True)


# ══════════════════════════════════════════════════════
# ONBOARDING
# ══════════════════════════════════════════════════════

async def send_onboarding_miniapp(chat_id: int, bot):
    onboarding_url = MINI_APP_URL.rstrip("/") + "/onboarding.html"
    kb_miniapp = InlineKeyboardMarkup([[
        InlineKeyboardButton("🚀 O'yin haqida ko'proq", web_app={"url": onboarding_url})
    ]])
    await bot.send_message(
        chat_id,
        "🎯 *TERRITORY TASHKENT*\n\n"
        "Toshkentdagi hududiy o'yinga xush kelibsiz!\n\n"
        "👇 O'yin haqida bilib oling:",
        parse_mode="Markdown",
        reply_markup=kb_miniapp,
    )
    kb_team = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔴 Qizil", callback_data="team:red"),
         InlineKeyboardButton("🔵 Ko'k",  callback_data="team:blue")],
        [InlineKeyboardButton("🟢 Yashil", callback_data="team:green"),
         InlineKeyboardButton("🟡 Sariq",  callback_data="team:yellow")],
    ])
    await bot.send_message(
        chat_id,
        "🎽 *Jamoangizni tanlang va boshlang:*",
        parse_mode="Markdown",
        reply_markup=kb_team,
    )


# ══════════════════════════════════════════════════════
# COMMAND HANDLERS
# ══════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user.id, user.username or "", user.first_name or "")
    db_user = get_user(user.id)

    args = ctx.args
    if args and args[0].startswith("ref_"):
        try:
            referrer_id = int(args[0].split("_")[1])
            if apply_referral(user.id, referrer_id):
                referrer = get_user(referrer_id)
                if referrer:
                    try:
                        await ctx.bot.send_message(
                            chat_id=referrer_id,
                            text=f"👥 *{user.first_name}* sizning havolangiz orqali qo'shildi!",
                            parse_mode="Markdown",
                        )
                        await check_and_award(referrer_id, ctx.bot, get_user(referrer_id))
                    except Exception:
                        pass
        except (ValueError, IndexError):
            pass

    if not db_user or not db_user["team"]:
        await send_onboarding_miniapp(user.id, ctx.bot)
    else:
        team = TEAMS[db_user["team"]]
        await update.message.reply_text(
            f"👋 *Xush kelibsiz, {db_user['first_name']}!*\n"
            f"{team['emoji']} Jamoa: {team['name']}\n\n"
            f"🗺 Zonalar: *{db_user['zones_owned']}* ta\n"
            f"🏃 Masofa: *{db_user['total_km']:.1f}* km\n"
            f"⚔️ Egallangan: *{db_user['zones_taken']}* ta",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(),
        )


async def cmd_map(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🌍 Xaritani ochish", web_app={"url": MINI_APP_URL})
    ]])
    await update.message.reply_text(
        "🌍 *Territory Xaritasi*\n\nBarcha zonalarni real-time ko'ring!",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ *Qo'llanma*\n\n"
        "*1️⃣ Trek orqali zona:*\n"
        "▶️ Trek boshlash → Mini App ochiladi → GPS avtomatik yoqiladi → "
        "Aylana yasab yuring → Trek tugatish\n\n"
        "*2️⃣ Doira zona:*\n"
        "📍 Joylashuvni yuboring → Doira zona yaratish → Radius tanlang\n\n"
        "*⚔️ Zona egallash:*\n"
        "Dushman zonasini aylanib o'ting (zona markazi ichingizda bo'lsin)\n\n"
        "*📊 Komandalar:*\n"
        "/stats — statistika\n"
        "/zones — zonalarim\n"
        "/leaderboard — reyting\n"
        "/team — jamoa o'zgartirish\n"
        "/history [zona\\_id] — zona tarixi",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(),
    )


async def cmd_team(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎽 Jamoa tanlang:", reply_markup=team_kb())


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db_user = get_user(user_id)
    if not db_user:
        await update.message.reply_text("Avval /start bosing!")
        return
    zones = get_user_zones(user_id)
    team  = TEAMS.get(db_user["team"] or "", {"emoji": "❓", "name": "Tanlanmagan"})
    total_area = sum(z["area_m2"] for z in zones)
    circles  = sum(1 for z in zones if z["zone_type"] == "circle")
    polygons = sum(1 for z in zones if z["zone_type"] == "polygon")
    await update.message.reply_text(
        f"📊 *Statistika*\n\n"
        f"👤 {db_user['first_name']}\n"
        f"{team['emoji']} {team['name']}\n\n"
        f"🗺 *Zonalar:* {db_user['zones_owned']} ta\n"
        f"   ⭕ Doira: {circles} | 🔷 Polygon: {polygons}\n"
        f"   📐 Maydon: {total_area/10000:.3f} ga\n"
        f"⚔️ Egallangan: {db_user['zones_taken']} ta\n\n"
        f"🏃 Jami masofa: {db_user['total_km']:.2f} km",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(),
    )


async def cmd_leaderboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with get_db() as conn:
        rows = conn.execute("""
            SELECT first_name, username, team, zones_owned, zones_taken, total_km
            FROM users ORDER BY zones_owned DESC LIMIT 10
        """).fetchall()
        team_rows = conn.execute("""
            SELECT team, SUM(zones_owned) as tz, COUNT(*) as pl
            FROM users WHERE team IS NOT NULL GROUP BY team ORDER BY tz DESC
        """).fetchall()
    text = "🏆 *TOP-10 O'yinchilar*\n\n"
    medals = ["🥇","🥈","🥉"]
    for i, r in enumerate(rows, 1):
        te = TEAMS[r["team"]]["emoji"] if r["team"] and r["team"] in TEAMS else "❓"
        nm = r["first_name"] or r["username"] or "Nomsiz"
        md = medals[i-1] if i <= 3 else f"{i}."
        text += f"{md} {te} *{nm}*  🗺{r['zones_owned']} ⚔️{r['zones_taken']} 🏃{r['total_km']:.1f}km\n"
    if team_rows:
        text += "\n━━━━━━━━\n🎽 *Jamoa reytingi:*\n"
        for r in team_rows:
            if r["team"] in TEAMS:
                t = TEAMS[r["team"]]
                text += f"{t['emoji']} {t['name']}: {r['tz'] or 0} zona ({r['pl']} o'yinchi)\n"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb())


async def cmd_zones(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    zones = get_user_zones(user_id)
    if not zones:
        await update.message.reply_text(
            "🗺 Hali zona yo'q.\n📍 Joylashuvni yuboring yoki trek boshlang!",
            reply_markup=main_menu_kb(),
        )
        return
    text = f"🗺 *Sizning zonalaringiz ({len(zones)} ta)*\n\n"
    for i, z in enumerate(zones, 1):
        tp   = "⭕" if z["zone_type"] == "circle" else "🔷"
        nm   = z["name"] or f"Zona #{z['id']}"
        area = z["area_m2"]
        ar   = f"{area:.0f}m²" if area < 10000 else f"{area/10000:.2f}ga"
        text += f"{i}. {tp} *{nm}* — {ar}\n"
        text += f"   📅 {z['created_at'][:10]} | /history_{z['id']}\n\n"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb())


async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = update.message.text.split()
    if len(args) > 1:
        zone_id_str = args[1]
    else:
        raw = update.message.text.lstrip("/").replace("history_", "").replace("history", "")
        zone_id_str = raw.strip()
    if not zone_id_str.isdigit():
        await update.message.reply_text("Foydalanish: /history [zona_id]")
        return
    zone_id = int(zone_id_str)
    history = get_zone_history(zone_id)
    if not history:
        await update.message.reply_text(f"Zona #{zone_id} tarixi topilmadi.")
        return
    text = f"📜 *Zona #{zone_id} tarixi*\n\n"
    for h in history:
        action = "🆕 Yaratildi" if h["action"] == "created" else "⚔️ Egallandi"
        to_u   = get_user(h["to_user"])
        to_nm  = to_u["first_name"] if to_u else "?"
        to_te  = TEAMS.get(h["to_team"], {"emoji": "❓"})["emoji"]
        dt     = h["captured_at"][:16]
        text  += f"{action} — {to_te} {to_nm} ({dt})\n"
        if h["action"] == "captured" and h["from_user"]:
            from_u  = get_user(h["from_user"])
            from_nm = from_u["first_name"] if from_u else "?"
            from_te = TEAMS.get(h["from_team"], {"emoji": "❓"})["emoji"]
            text   += f"  ← {from_te} {from_nm} dan\n"
        text += "\n"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_achievements(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    earned = get_user_achievements(user_id)
    earned_codes = {a["code"] for a in earned}
    text = "🏅 *Yutuqlar*\n\n"
    text += f"✅ Qozonilgan: {len(earned)}/{len(ACHIEVEMENTS)}\n\n"
    for code, ach in ACHIEVEMENTS.items():
        mark = "✅" if code in earned_codes else "🔒"
        text += f"{mark} {ach['name']}\n   _{ach['desc']}_\n\n"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu_kb())


async def cmd_referral(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db_user = get_user(user_id)
    if not db_user:
        await update.message.reply_text("Avval /start bosing!")
        return
    bot_info = await ctx.bot.get_me()
    link = get_referral_link(user_id, bot_info.username)
    ref_count = db_user.get("referral_count", 0)
    await update.message.reply_text(
        f"👥 *Referral tizimi*\n\n"
        f"🔗 Sizning havola:\n`{link}`\n\n"
        f"👤 Taklif qilganlar: *{ref_count}* ta",
        parse_mode="Markdown",
        reply_markup=main_menu_kb(),
    )


# ══════════════════════════════════════════════════════
# LOCATION HANDLER
# ══════════════════════════════════════════════════════

async def handle_location(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db_user = get_user(user_id)
    if not db_user or not db_user["team"]:
        await update.message.reply_text("❗ Avval jamoa tanlang!", reply_markup=team_kb())
        return
    lat  = update.message.location.latitude
    lng  = update.message.location.longitude
    mode = ctx.user_data.get("mode", MODE_IDLE)
    if mode == MODE_CIRCLE:
        ctx.user_data["circle_lat"] = lat
        ctx.user_data["circle_lng"] = lng
        await update.message.reply_text(
            f"📍 Markaz: `{lat:.5f}, {lng:.5f}`\n\nRadius tanlang:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=radius_kb(),
        )
        return
    ctx.user_data["last_lat"] = lat
    ctx.user_data["last_lng"] = lng
    nearby = get_zones_near(lat, lng, 500)
    team   = TEAMS[db_user["team"]]
    msg = f"📍 *Joylashuv qabul qilindi*\n{team['emoji']} {team['name']}\n"
    if nearby:
        msg += f"\n🔍 *500m ichida {len(nearby)} zona:*\n"
        for z in nearby[:5]:
            if z["owner_id"] == user_id:
                owner_txt = "👑 Sizniki"
            else:
                own = get_user(z["owner_id"])
                te  = TEAMS.get(z["team"], {"emoji": "❓"})["emoji"]
                nm  = own["first_name"] if own else "?"
                owner_txt = f"{te} {nm}"
            tp   = "⭕" if z["zone_type"] == "circle" else "🔷"
            nm_z = z["name"] or f"#{z['id']}"
            msg += f"  {tp} {nm_z} — {owner_txt} ({z['distance']:.0f}m)\n"
    else:
        msg += "\n🔍 Yaqin atrofda zona yo'q."
    msg += "\n\nZona yaratish uchun:"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=zone_create_kb())


# ══════════════════════════════════════════════════════
# TEXT HANDLER
# ══════════════════════════════════════════════════════

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text    = update.message.text
    user_id = update.effective_user.id

    if text.startswith("/history"):
        await cmd_history(update, ctx)
        return

    if text == "▶️ Trek boshlash":
        db_user = get_user(user_id)
        if not db_user:
            await update.message.reply_text("Avval /start bosing!")
            return
        if not db_user["team"]:
            await update.message.reply_text("❗ Avval jamoa tanlang!", reply_markup=team_kb())
            return
        team_info = TEAMS[db_user["team"]]
        await update.message.reply_text(
            f"▶️ *Trek boshlash*\n\n"
            f"{team_info['emoji']} Jamoa: {team_info['name']}\n\n"
            f"📱 *Mini App da GPS avtomatik yoqiladi:*\n"
            f"• Xaritada yo'lingiz chiziladi\n"
            f"• Aylana yopilganda zona ko'rinadi\n"
            f"• Tugatganda bot ga avtomatik yuboriladi\n\n"
            f"👇 Quyidagi tugmani bosing:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=trek_miniapp_kb(db_user["team"]),
        )
    elif text == "🗺 Zonalarim":   await cmd_zones(update, ctx)
    elif text == "📊 Statistika":  await cmd_stats(update, ctx)
    elif text == "🏆 Reyting":     await cmd_leaderboard(update, ctx)
    elif text == "🌍 Xarita":      await cmd_map(update, ctx)
    elif text == "🏅 Yutuqlar":    await cmd_achievements(update, ctx)
    elif text == "👥 Referral":    await cmd_referral(update, ctx)
    elif text == "❓ Yordam":      await cmd_help(update, ctx)
    else:
        await update.message.reply_text(
            "❓ Tushunmadim. Quyidagi tugmalardan foydalaning.",
            reply_markup=main_menu_kb(),
        )


# ══════════════════════════════════════════════════════
# WEBAPP DATA HANDLER
# ══════════════════════════════════════════════════════

async def handle_webapp_data(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        data = json.loads(update.message.web_app_data.data)
    except Exception:
        logger.warning("WebApp data parse error")
        return

    action = data.get("action")

    if action == "onboarding_done":
        upsert_user(user_id, update.effective_user.username or "", update.effective_user.first_name or "")
        db_user = get_user(user_id)
        if not db_user or not db_user["team"]:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔴 Qizil", callback_data="team:red"),
                 InlineKeyboardButton("🔵 Ko'k",  callback_data="team:blue")],
                [InlineKeyboardButton("🟢 Yashil", callback_data="team:green"),
                 InlineKeyboardButton("🟡 Sariq",  callback_data="team:yellow")],
            ])
            await update.message.reply_text("🎽 *Jamoangizni tanlang:*", parse_mode="Markdown", reply_markup=kb)

    elif action == "trek_finished":
        points = data.get("points", [])
        team   = data.get("team", "")
        closed = data.get("closed", False)
        dist_m = data.get("distance", 0)
        upsert_user(user_id, update.effective_user.username or "", update.effective_user.first_name or "")
        msg = await process_trek(update.get_bot(), user_id, points, team, closed, dist_m)
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb())


# ══════════════════════════════════════════════════════
# CALLBACK HANDLER
# ══════════════════════════════════════════════════════

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q       = update.callback_query
    await q.answer()
    data    = q.data
    user_id = update.effective_user.id

    if data.startswith("team:"):
        team_key = data.split(":")[1]
        if team_key not in TEAMS:
            return
        set_team(user_id, team_key)
        team = TEAMS[team_key]
        await q.edit_message_text(
            f"✅ *Jamoa tanlandi: {team['name']}*\n\nO'yin boshlashingiz mumkin!",
            parse_mode=ParseMode.MARKDOWN,
        )
        await q.message.reply_text("Asosiy menyu:", reply_markup=main_menu_kb())

    elif data == "zone:circle":
        ctx.user_data["mode"] = MODE_CIRCLE
        await q.edit_message_text("⭕ *Doira zona*\n\n📍 Markaz nuqtani yuboring:", parse_mode=ParseMode.MARKDOWN)

    elif data == "zone:cancel":
        ctx.user_data["mode"] = MODE_IDLE
        await q.edit_message_text("❌ Bekor qilindi.")
        await q.message.reply_text("Asosiy menyu:", reply_markup=main_menu_kb())

    elif data.startswith("radius:"):
        radius  = float(data.split(":")[1])
        lat     = ctx.user_data.get("circle_lat")
        lng     = ctx.user_data.get("circle_lng")
        if not lat or not lng:
            await q.edit_message_text("❗ Markaz topilmadi. Qaytadan joylashuv yuboring.")
            return
        db_user = get_user(user_id)
        if not db_user or not db_user["team"]:
            await q.edit_message_text("❗ Jamoa tanlanmagan. /start bosing.")
            return
        team    = db_user["team"]
        zone_id = await create_zone_circle_with_photo(ctx.bot, user_id, team, lat, lng, radius)
        ctx.user_data["mode"] = MODE_IDLE
        ctx.user_data.pop("circle_lat", None)
        ctx.user_data.pop("circle_lng", None)
        te = TEAMS[team]
        await q.edit_message_text(
            f"✅ *Zona yaratildi #{zone_id}*\n\n"
            f"{te['emoji']} {te['name']}\n"
            f"📍 `{lat:.5f}, {lng:.5f}`\n"
            f"📏 Radius: {radius:.0f}m\n"
            f"📐 Maydon: {math.pi*radius**2/10000:.4f} ga",
            parse_mode=ParseMode.MARKDOWN,
        )
        await q.message.reply_text("Asosiy menyu:", reply_markup=main_menu_kb())


# ══════════════════════════════════════════════════════
# BACKGROUND TASKS
# ══════════════════════════════════════════════════════

async def invasion_checker(app):
    while True:
        try:
            with get_db() as conn:
                treks = conn.execute("SELECT * FROM treks WHERE status='active'").fetchall()
                for trek in treks:
                    trek  = dict(trek)
                    pts   = json.loads(trek["points"])
                    if not pts: continue
                    last  = pts[-1]
                    uid   = trek["user_id"]
                    udata = get_user(uid)
                    if not udata: continue
                    near_zones = get_zones_near(last["lat"], last["lng"], 200)
                    for z in near_zones:
                        if z["owner_id"] == uid: continue
                        key    = f"{z['id']}_{uid}"
                        last_t = notif_cache.get(key)
                        if last_t and (datetime.now() - last_t).seconds < 3600: continue
                        notif_cache[key] = datetime.now()
                        te_att = TEAMS.get(udata["team"], {"emoji": "❓"})
                        z_name = z["name"] or f"Zona #{z['id']}"
                        try:
                            await app.bot.send_message(
                                chat_id=z["owner_id"],
                                text=f"⚠️ *Zonangizga tajovuz!*\n\n🏴 {z_name}\n"
                                     f"{te_att['emoji']} {udata['first_name']} {z['distance']:.0f}m yaqinlashmoqda!\n\n"
                                     f"Tezroq qaytib himoya qiling! 🏃",
                                parse_mode=ParseMode.MARKDOWN,
                            )
                        except Exception as e:
                            logger.warning(f"Invasion notif error: {e}")
        except Exception as e:
            logger.error(f"Invasion checker error: {e}")
        await asyncio.sleep(60)


ZONE_EXPIRE_DAYS = int(os.getenv("ZONE_EXPIRE_DAYS", "7"))


async def expire_old_zones(app):
    while True:
        try:
            with get_db() as conn:
                old_zones = conn.execute("""
                    SELECT z.*, u.first_name, u.team as owner_team
                    FROM zones z JOIN users u ON z.owner_id = u.user_id
                    WHERE z.active = 1
                    AND z.created_at < datetime('now', ?)
                    AND z.id NOT IN (
                        SELECT DISTINCT zone_id FROM zone_history
                        WHERE action = 'captured' AND captured_at > datetime('now', ?)
                    )
                """, (f"-{ZONE_EXPIRE_DAYS} days", f"-{ZONE_EXPIRE_DAYS} days")).fetchall()
                for zone in old_zones:
                    zone = dict(zone)
                    conn.execute("UPDATE zones SET owner_id=0, team='neutral' WHERE id=?", (zone["id"],))
                    conn.execute("UPDATE users SET zones_owned = MAX(0, zones_owned-1) WHERE user_id=?", (zone["owner_id"],))
                    conn.execute("""
                        INSERT INTO zone_history (zone_id, from_user, from_team, to_user, to_team, action)
                        VALUES (?, ?, ?, 0, 'neutral', 'expired')
                    """, (zone["id"], zone["owner_id"], zone["owner_team"]))
                    zone_name = zone["name"] or f"Zona #{zone['id']}"
                    try:
                        await app.bot.send_message(
                            chat_id=zone["owner_id"],
                            text=f"⏱ *Zona eskirdi!*\n\n🏴 {zone_name} neytral bo'ldi.\n"
                                 f"_{ZONE_EXPIRE_DAYS} kun davomida himoya qilinmadi._\n\n"
                                 f"Zonangizni qayta egallab oling! 🏃",
                            parse_mode="Markdown",
                        )
                    except Exception as e:
                        logger.warning(f"Expire notify error: {e}")
        except Exception as e:
            logger.error(f"Expire checker error: {e}")
        await asyncio.sleep(3600)


# ══════════════════════════════════════════════════════
# WEB API SERVER
# ══════════════════════════════════════════════════════

MINI_APP_PORT = int(os.getenv("PORT", "8080"))


async def api_trek_submit(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.Response(
            text=json.dumps({"ok": False, "error": "Invalid JSON"}),
            status=400, content_type="application/json", headers=CORS_HEADERS
        )

    # tg.initData dan user_id olish (endi xavfsiz HMAC tekshiruvidan o'tadi)
    init_data = body.get("init_data", "")
    user_info = parse_init_data(init_data) if init_data else None

    if not user_info:
        return web.Response(
            text=json.dumps({"ok": False, "error": "Unauthorized — Yaroqsiz yoxud soxta initData"}),
            status=401, content_type="application/json", headers=CORS_HEADERS
        )

    user_id    = user_info.get("id")
    first_name = user_info.get("first_name", "")
    username   = user_info.get("username", "")

    if not user_id:
        return web.Response(
            text=json.dumps({"ok": False, "error": "user_id topilmadi"}),
            status=400, content_type="application/json", headers=CORS_HEADERS
        )

    upsert_user(user_id, username, first_name)

    points = body.get("points", [])
    team   = body.get("team", "")
    closed = body.get("closed", False)
    dist_m = body.get("distance", 0)

    logger.info(f"Trek submit: user={user_id} points={len(points)} closed={closed} dist={dist_m:.0f}m")

    msg = await process_trek(_app.bot, user_id, points, team, closed, dist_m)

    try:
        await _app.bot.send_message(
            chat_id=user_id,
            text=msg,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_kb(),
        )
    except Exception as e:
        logger.error(f"Bot message error: {e}")
        return web.Response(
            text=json.dumps({"ok": False, "error": f"Bot xabar yubora olmadi: {e}"}),
            status=500, content_type="application/json", headers=CORS_HEADERS
        )

    return web.Response(
        text=json.dumps({"ok": True, "message": "Trek qabul qilindi!"}),
        content_type="application/json", headers=CORS_HEADERS
    )


async def api_zones(request: web.Request) -> web.Response:
    try:
        zones = get_all_zones()
        result = []
        for z in zones:
            owner = get_user(z["owner_id"]) if z["owner_id"] else None
            team_info = TEAMS.get(z["team"], {"emoji": "❓", "name": "Neytral"})
            geom = json.loads(z["geometry"])
            result.append({
                "id": z["id"], "type": z["zone_type"],
                "team": z["team"], "team_name": team_info["name"], "team_emoji": team_info["emoji"],
                "owner_id": z["owner_id"], "owner_name": owner["first_name"] if owner else "Neytral",
                "photo_url": z["photo_url"],
                "center_lat": z["center_lat"], "center_lng": z["center_lng"],
                "radius_m": z.get("radius_m"), "geometry": geom,
                "area_m2": z["area_m2"], "created_at": z["created_at"],
            })
        return web.Response(text=json.dumps(result, ensure_ascii=False),
                            content_type="application/json", headers=CORS_HEADERS)
    except Exception as e:
        return web.Response(text=json.dumps({"error": str(e)}), status=500, headers=CORS_HEADERS)


async def api_active_treks(request: web.Request) -> web.Response:
    try:
        with get_db() as conn:
            treks = conn.execute("""
                SELECT t.*, u.first_name, u.team FROM treks t
                JOIN users u ON t.user_id=u.user_id WHERE t.status='active'
            """).fetchall()
        result = []
        for t in treks:
            t = dict(t)
            points = json.loads(t["points"])
            if len(points) < 2: continue
            team_info = TEAMS.get(t["team"], {"emoji":"❓"})
            result.append({
                "user_id": t["user_id"], "owner_name": t["first_name"],
                "team": t["team"], "team_emoji": team_info["emoji"],
                "points": points[-100:], "distance_m": t["distance_m"],
            })
        return web.Response(text=json.dumps(result, ensure_ascii=False),
                            content_type="application/json", headers=CORS_HEADERS)
    except Exception as e:
        return web.Response(text=json.dumps({"error": str(e)}), status=500, headers=CORS_HEADERS)


async def api_active_players(request: web.Request) -> web.Response:
    try:
        with get_db() as conn:
            treks = conn.execute("""
                SELECT t.user_id, t.points, u.first_name, u.team FROM treks t
                JOIN users u ON t.user_id=u.user_id WHERE t.status='active'
            """).fetchall()
        result = []
        for t in treks:
            t = dict(t)
            points = json.loads(t["points"])
            if not points: continue
            last = points[-1]
            team_info = TEAMS.get(t["team"], {"emoji":"❓","name":"?"})
            result.append({
                "user_id": t["user_id"], "name": t["first_name"],
                "team": t["team"], "team_emoji": team_info["emoji"],
                "lat": last["lat"], "lng": last["lng"],
            })
        return web.Response(text=json.dumps(result, ensure_ascii=False),
                            content_type="application/json", headers=CORS_HEADERS)
    except Exception as e:
        return web.Response(text=json.dumps({"error": str(e)}), status=500, headers=CORS_HEADERS)


async def api_user(request: web.Request) -> web.Response:
    try:
        user_id = int(request.rel_url.query.get("user_id", 0))
        if not user_id:
            return web.Response(text=json.dumps({"error": "user_id required"}), status=400, headers=CORS_HEADERS)
        db_user = get_user(user_id)
        if not db_user:
            return web.Response(text=json.dumps({"error": "not found"}), status=404, headers=CORS_HEADERS)
        zones = get_user_zones(user_id)
        team_info = TEAMS.get(db_user["team"], {"emoji": "❓", "name": "Yo'q"})
        result = {
            "user_id": user_id, "first_name": db_user["first_name"],
            "team": db_user["team"], "team_name": team_info["name"], "team_emoji": team_info["emoji"],
            "zones_owned": db_user["zones_owned"], "total_km": db_user["total_km"],
            "zones": [{"id": z["id"], "type": z["zone_type"],
                       "center_lat": z["center_lat"], "center_lng": z["center_lng"],
                       "radius_m": z.get("radius_m"), "geometry": json.loads(z["geometry"]),
                       "area_m2": z["area_m2"]} for z in zones],
        }
        return web.Response(text=json.dumps(result, ensure_ascii=False),
                            content_type="application/json", headers=CORS_HEADERS)
    except Exception as e:
        return web.Response(text=json.dumps({"error": str(e)}), status=500, headers=CORS_HEADERS)


async def api_health(request: web.Request) -> web.Response:
    return web.Response(text="OK", headers=CORS_HEADERS)


async def start_web_server():
    app_web = web.Application(middlewares=[cors_middleware])
    app_web.router.add_route("OPTIONS", "/api/trek_submit",    lambda r: web.Response(status=200, headers=CORS_HEADERS))
    app_web.router.add_post("/api/trek_submit",   api_trek_submit)
    app_web.router.add_get("/api/zones",          api_zones)
    app_web.router.add_get("/api/user",           api_user)
    app_web.router.add_get("/api/active_treks",   api_active_treks)
    app_web.router.add_get("/api/active_players", api_active_players)
    app_web.router.add_get("/health",             api_health)
    runner = web.AppRunner(app_web)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", MINI_APP_PORT)
    await site.start()
    logger.info(f"🌐 API server started on port {MINI_APP_PORT}")
    while True:
        await asyncio.sleep(3600)


# ══════════════════════════════════════════════════════
# ACHIEVEMENTS
# ══════════════════════════════════════════════════════

ACHIEVEMENTS = {
    "first_zone":    {"name": "🏴 Birinchi zona",     "desc": "Birinchi zonangizni yaratingiz"},
    "zone_x5":       {"name": "🗺 Kartograf",          "desc": "5 ta zona yarating"},
    "zone_x20":      {"name": "🌍 Hududboz",           "desc": "20 ta zona yarating"},
    "first_capture": {"name": "⚔️ Birinchi hujum",     "desc": "Birinchi dushman zonasini egallang"},
    "capture_x5":    {"name": "🔥 Tajovuzkor",         "desc": "5 ta zona egallang"},
    "capture_x10":   {"name": "💀 Bosqinchi",          "desc": "10 ta zona egallang"},
    "km_10":         {"name": "🏃 10km yugurdingiz",    "desc": "Jami 10 km yuring"},
    "km_50":         {"name": "🚴 50km masofachi",      "desc": "Jami 50 km yuring"},
    "km_100":        {"name": "🏅 100km legend",        "desc": "Jami 100 km yuring"},
    "referral_1":    {"name": "👥 Do'st taklif qildi", "desc": "1 do'st taklif qiling"},
    "referral_5":    {"name": "👨‍👩‍👧‍👦 Jamoa quruvchi",   "desc": "5 do'st taklif qiling"},
}


def get_user_achievements(user_id: int) -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT code, earned_at FROM achievements WHERE user_id=? ORDER BY earned_at", (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def award_achievement(user_id: int, code: str) -> bool:
    try:
        with get_db() as conn:
            conn.execute("INSERT OR IGNORE INTO achievements (user_id, code) VALUES (?, ?)", (user_id, code))
            return conn.execute("SELECT changes()").fetchone()[0] > 0
    except Exception:
        return False


async def check_and_award(user_id: int, bot, db_user: dict = None):
    if not db_user:
        db_user = get_user(user_id)
    if not db_user:
        return
    checks = [
        (db_user.get("zones_owned", 0) >= 1,    "first_zone"),
        (db_user.get("zones_owned", 0) >= 5,    "zone_x5"),
        (db_user.get("zones_owned", 0) >= 20,   "zone_x20"),
        (db_user.get("zones_taken", 0) >= 1,    "first_capture"),
        (db_user.get("zones_taken", 0) >= 5,    "capture_x5"),
        (db_user.get("zones_taken", 0) >= 10,   "capture_x10"),
        (db_user.get("total_km", 0) >= 10,      "km_10"),
        (db_user.get("total_km", 0) >= 50,      "km_50"),
        (db_user.get("total_km", 0) >= 100,     "km_100"),
        (db_user.get("referral_count", 0) >= 1, "referral_1"),
        (db_user.get("referral_count", 0) >= 5, "referral_5"),
    ]
    for condition, code in checks:
        if condition and award_achievement(user_id, code):
            ach = ACHIEVEMENTS.get(code, {})
            try:
                await bot.send_message(
                    chat_id=user_id,
                    text=f"🏅 *Yangi yutuq!*\n\n{ach.get('name', code)}\n_{ach.get('desc', '')}_",
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.warning(f"Achievement notify error: {e}")


# ══════════════════════════════════════════════════════
# REFERRAL
# ══════════════════════════════════════════════════════

def apply_referral(new_user_id: int, referrer_id: int) -> bool:
    if new_user_id == referrer_id:
        return False
    with get_db() as conn:
        user = conn.execute("SELECT referred_by FROM users WHERE user_id=?", (new_user_id,)).fetchone()
        if not user or user["referred_by"]:
            return False
        conn.execute("UPDATE users SET referred_by=? WHERE user_id=?", (referrer_id, new_user_id))
        conn.execute("UPDATE users SET referral_count = referral_count + 1 WHERE user_id=?", (referrer_id,))
        return True


def get_referral_link(user_id: int, bot_username: str) -> str:
    return f"https://t.me/{bot_username}?start=ref_{user_id}"


# ══════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════

async def on_startup(app: Application) -> None:
    global _app
    _app = app
    
    # Tasklarni saqlab qolish (o'chib ketmasligi uchun)
    task1 = asyncio.create_task(invasion_checker(app))
    task2 = asyncio.create_task(expire_old_zones(app))
    task3 = asyncio.create_task(start_web_server())
    
    background_tasks.update({task1, task2, task3})


def main():
    init_db()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(on_startup)
        .build()
    )
    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("help",         cmd_help))
    app.add_handler(CommandHandler("team",         cmd_team))
    app.add_handler(CommandHandler("stats",        cmd_stats))
    app.add_handler(CommandHandler("leaderboard",  cmd_leaderboard))
    app.add_handler(CommandHandler("zones",        cmd_zones))
    app.add_handler(CommandHandler("history",      cmd_history))
    app.add_handler(CommandHandler("map",          cmd_map))
    app.add_handler(CommandHandler("achievements", cmd_achievements))
    app.add_handler(CommandHandler("referral",     cmd_referral))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))
    logger.info("🗺️ Territory Bot ishga tushdi!")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
