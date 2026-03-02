#!/usr/bin/env python3
"""
🗺 Toshkent Territory Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ Trek Mini App orqali (GPS auto-tracking)
✅ /api/trek_submit — fetch orqali, limit yo'q!
✅ HMAC xavfsizlik TO'G'RILANDI (key tartib fixed)
✅ Barcha xatolar tuzatilgan
"""

import asyncio
import logging
import os
import json
import math
import hmac
import hashlib
from urllib.parse import unquote
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

_app: Application = None
background_tasks = set()

# ══════════════════════════════════════════════════════
# ACHIEVEMENTS TIZIMI
# ══════════════════════════════════════════════════════

ACHIEVEMENT_LIST = {
    "first_zone":    {"title": "🏁 Birinchi zona",      "desc": "Birinchi zona yaratdingiz!"},
    "walker_1km":    {"title": "🚶 1 km yurish",         "desc": "Jami 1 km yurdingiz"},
    "walker_5km":    {"title": "🏃 5 km yurish",         "desc": "Jami 5 km yurdingiz"},
    "walker_10km":   {"title": "🏅 10 km yurish",        "desc": "Jami 10 km yurdingiz"},
    "conqueror_5":   {"title": "⚔️ 5 zona egallash",     "desc": "5 ta zona egalladingiz"},
    "conqueror_10":  {"title": "🏰 10 zona egallash",    "desc": "10 ta zona egalladingiz"},
    "landlord_3":    {"title": "🗺 3 zonaga egalik",     "desc": "3 ta zonaga ega bo'ldingiz"},
    "landlord_10":   {"title": "👑 10 zonaga egalik",    "desc": "10 ta zonaga ega bo'ldingiz"},
    "referral_3":    {"title": "👥 3 ta referral",       "desc": "3 ta do'stni taklif qildingiz"},
}

# ══════════════════════════════════════════════════════
# CORS HEADERS
# ══════════════════════════════════════════════════════

CORS_HEADERS = {
    "Access-Control-Allow-Origin":  "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-Telegram-Init-Data",
}

@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        return web.Response(status=200, headers=CORS_HEADERS)
    response = await handler(request)
    for k, v in CORS_HEADERS.items():
        response.headers[k] = v
    return response

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
# REFERRAL TIZIMI
# ══════════════════════════════════════════════════════

def get_referral_link(user_id: int, bot_username: str) -> str:
    return f"https://t.me/{bot_username}?start=ref_{user_id}"

def process_referral(new_user_id: int, referrer_id: int):
    if new_user_id == referrer_id:
        return
    with get_db() as conn:
        existing = conn.execute(
            "SELECT referred_by FROM users WHERE user_id=?", (new_user_id,)
        ).fetchone()
        if existing and existing["referred_by"]:
            return
        conn.execute(
            "UPDATE users SET referred_by=? WHERE user_id=?",
            (referrer_id, new_user_id)
        )
        conn.execute(
            "UPDATE users SET referral_count = referral_count + 1 WHERE user_id=?",
            (referrer_id,)
        )

# ══════════════════════════════════════════════════════
# ✅ TELEGRAM INIT DATA PARSER — TO'G'RILANDI!
# ══════════════════════════════════════════════════════

def parse_init_data(init_data: str) -> dict | None:
    """
    Telegram WebApp initData ni xavfsiz tekshirish.
    
    ✅ TO'G'RI HMAC tartib:
        secret_key = HMAC_SHA256(key=BOT_TOKEN, msg="WebAppData")
        hash       = HMAC_SHA256(key=secret_key, msg=data_check_string)
    
    ❌ NOTO'G'RI (avvalgi):
        secret_key = HMAC_SHA256(key="WebAppData", msg=BOT_TOKEN)  ← BU XATO EDI!
    """
    if not init_data:
        logger.error("❌ initData BO'SH keldi!")
        return None
    try:
        logger.info(f"📥 initData uzunligi: {len(init_data)}")

        # Manual parsing — URL encoding muammolarini oldini oladi
        params = {}
        for pair in init_data.split("&"):
            idx = pair.find("=")
            if idx == -1:
                continue
            key = unquote(pair[:idx])
            val = unquote(pair[idx + 1:])
            params[key] = val

        hash_val = params.pop("hash", None)
        if not hash_val:
            logger.error("❌ hash topilmadi initData da!")
            return None

        # data_check_string — sorted, \n bilan ajratilgan
        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(params.items())
        )

        # ✅ TO'G'RI TARTIB (Telegram docs bo'yicha):
        #    secret_key = HMAC_SHA256(key=BOT_TOKEN, msg="WebAppData")
        secret_key = hmac.new(
            BOT_TOKEN.encode(),  # ← key: bot token
            b"WebAppData",       # ← msg: "WebAppData" string
            hashlib.sha256
        ).digest()

        calc_hash = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256
        ).hexdigest()

        logger.info(f"🔐 Hash kutilgan:    {hash_val[:16]}...")
        logger.info(f"🔐 Hash hisoblangan: {calc_hash[:16]}...")

        if calc_hash != hash_val:
            logger.warning("🚨 HMAC MISMATCH! BOT_TOKEN ni tekshiring!")
            logger.debug(f"data_check_string: {repr(data_check_string[:300])}")
            return None

        logger.info("✅ HMAC tekshiruvi muvaffaqiyatli!")
        user_str = params.get("user")
        if not user_str:
            logger.error("❌ 'user' maydoni topilmadi!")
            return None
        return json.loads(user_str)

    except Exception as e:
        logger.error(f"initData parse xatosi: {e}")
        return None

# ══════════════════════════════════════════════════════
# GEOMETRY
# ══════════════════════════════════════════════════════

def haversine(lat1, lon1, lat2, lon2) -> float:
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
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
        dx = haversine(lat0, lng0, lat0, p["lng"])
        if p["lng"] < lng0:
            dx = -dx
        dy = haversine(lat0, lng0, p["lat"], lng0)
        if p["lat"] < lat0:
            dy = -dy
        coords.append((dx, dy))
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
        conn.execute(
            "INSERT INTO zone_history (zone_id, to_user, to_team, action) VALUES (?, ?, ?, 'created')",
            (zone_id, user_id, team)
        )
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
        conn.execute(
            "INSERT INTO zone_history (zone_id, to_user, to_team, action) VALUES (?, ?, ?, 'created')",
            (zone_id, user_id, team)
        )
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
        conn.execute(
            "UPDATE zones SET owner_id=?, team=?, photo_url=NULL WHERE id=?",
            (new_owner, new_team, zone_id)
        )
        conn.execute("""
            INSERT INTO zone_history (zone_id, from_user, from_team, to_user, to_team, action)
            VALUES (?, ?, ?, ?, ?, 'captured')
        """, (zone_id, z["owner_id"], z["team"], new_owner, new_team))
        conn.execute(
            "UPDATE users SET zones_owned = MAX(0, zones_owned - 1) WHERE user_id=?",
            (z["owner_id"],)
        )
        conn.execute(
            "UPDATE users SET zones_owned = zones_owned + 1, zones_taken = zones_taken + 1 WHERE user_id=?",
            (new_owner,)
        )
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
    except Exception:
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
# ACHIEVEMENTS
# ══════════════════════════════════════════════════════

async def check_and_award(user_id: int, bot, db_user: dict):
    if not db_user:
        return

    awards = []

    def try_award(code: str):
        with get_db() as conn:
            try:
                conn.execute(
                    "INSERT INTO achievements (user_id, code) VALUES (?, ?)",
                    (user_id, code)
                )
                awards.append(code)
            except sqlite3.IntegrityError:
                pass

    if db_user["zones_owned"] >= 1:
        try_award("first_zone")
    if db_user["zones_owned"] >= 3:
        try_award("landlord_3")
    if db_user["zones_owned"] >= 10:
        try_award("landlord_10")
    if db_user["zones_taken"] >= 5:
        try_award("conqueror_5")
    if db_user["zones_taken"] >= 10:
        try_award("conqueror_10")
    if db_user["total_km"] >= 1:
        try_award("walker_1km")
    if db_user["total_km"] >= 5:
        try_award("walker_5km")
    if db_user["total_km"] >= 10:
        try_award("walker_10km")
    if db_user["referral_count"] >= 3:
        try_award("referral_3")

    for code in awards:
        ach = ACHIEVEMENT_LIST.get(code)
        if ach:
            try:
                await bot.send_message(
                    chat_id=user_id,
                    text=f"🏅 *Yangi yutuq!*\n\n{ach['title']}\n{ach['desc']}",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                pass

def get_user_achievements(user_id: int) -> list:
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT code, earned_at FROM achievements WHERE user_id=? ORDER BY earned_at DESC",
            (user_id,)
        ).fetchall()]

# ══════════════════════════════════════════════════════
# TREK PROCESSING
# ══════════════════════════════════════════════════════

async def process_trek(bot, user_id: int, points: list, team: str, closed: bool, dist_m: float) -> str:
    if not points or len(points) < 5:
        return "❗️ Trek juda qisqa (kamida 5 nuqta kerak)."

    db_user = get_user(user_id)
    if not db_user:
        return "❗️ Foydalanuvchi topilmadi. /start bosing."

    if db_user.get("team"):
        team = db_user["team"]

    if not team or team not in TEAMS:
        return "❗️ Jamoa tanlanmagan. /start bosing."

    dist_km = dist_m / 1000

    with get_db() as conn:
        conn.execute(
            "UPDATE treks SET status='cancelled' WHERE user_id=? AND status='active'",
            (user_id,)
        )
        conn.execute(
            "INSERT INTO treks (user_id, points, distance_m, started_at, finished_at, status) "
            "VALUES (?, ?, ?, datetime('now'), datetime('now'), 'finished')",
            (user_id, json.dumps(points), dist_m)
        )
        conn.execute("UPDATE users SET total_km = total_km + ? WHERE user_id=?", (dist_km, user_id))

    msg = f"⏹️ *Trek yakunlandi!*\n📏 {dist_km:.3f} km | 📍 {len(points)} nuqta\n"

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
                    z_name = old.get("name") or f"Zona #{old['id']}"
                    try:
                        await bot.send_message(
                            chat_id=old["owner_id"],
                            text=(
                                f"⚔️ *Zonangiz egallandi!*\n\n"
                                f"🏴 {z_name}\n"
                                f"{team_info['emoji']} {db_user['first_name']} tomonidan!\n\n"
                                f"Qaytarib oling! 💪"
                            ),
                            parse_mode=ParseMode.MARKDOWN,
                        )
                    except Exception:
                        pass

        updated_user = get_user(user_id)
        await check_and_award(user_id, bot, updated_user)

        te = TEAMS[team]
        msg += (
            f"\n✅ *Zona yaratildi #{zone_id}*\n"
            f"{te['emoji']} {te['name']}\n"
            f"📐 Maydon: {area / 10000:.4f} ga\n"
        )
        if captured:
            msg += f"\n⚔️ *{len(captured)} zona egallandi!*\n"
            for c in captured:
                te_c = TEAMS.get(c["team"], {"emoji": "❓"})["emoji"]
                c_name = c.get("name") or f"#{c['id']}"
                msg += f"  {te_c} {c_name}\n"
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
        [InlineKeyboardButton("⭕️ Doira zona yaratish", callback_data="zone:circle")],
        [InlineKeyboardButton("❌ Bekor",               callback_data="zone:cancel")],
    ])

def radius_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("50m",  callback_data="radius:50"),
            InlineKeyboardButton("100m", callback_data="radius:100"),
            InlineKeyboardButton("200m", callback_data="radius:200"),
        ],
        [
            InlineKeyboardButton("300m", callback_data="radius:300"),
            InlineKeyboardButton("500m", callback_data="radius:500"),
            InlineKeyboardButton("1km",  callback_data="radius:1000"),
        ],
        [InlineKeyboardButton("❌ Bekor", callback_data="zone:cancel")],
    ])

def trek_miniapp_kb(team: str = "") -> ReplyKeyboardMarkup:
    trek_url = MINI_APP_URL.rstrip("/") + "/trek.html"
    if team:
        trek_url += f"?team={team}"
    return ReplyKeyboardMarkup(
        [[KeyboardButton("🗺 Trekni boshlash", web_app=WebAppInfo(url=trek_url))]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

# ══════════════════════════════════════════════════════
# COMMAND HANDLERS
# ══════════════════════════════════════════════════════

async def send_onboarding_miniapp(chat_id: int, bot):
    onboarding_url = MINI_APP_URL.rstrip("/") + "/onboarding.html"
    kb_miniapp = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 O'yin haqida ko'proq", web_app={"url": onboarding_url})]
    ])
    await bot.send_message(
        chat_id,
        "🎯 *TERRITORY TASHKENT*\n\n👇 O'yin haqida bilib oling:",
        parse_mode="Markdown",
        reply_markup=kb_miniapp,
    )
    await bot.send_message(
        chat_id,
        "🎽 *Jamoangizni tanlang:*",
        parse_mode="Markdown",
        reply_markup=team_kb(),
    )

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user.id, user.username or "", user.first_name or "")

    if ctx.args and len(ctx.args) > 0:
        arg = ctx.args[0]
        if arg.startswith("ref_"):
            try:
                referrer_id = int(arg.replace("ref_", ""))
                process_referral(user.id, referrer_id)
                logger.info(f"👥 Referral: {user.id} -> {referrer_id}")
            except ValueError:
                pass

    db_user = get_user(user.id)
    if not db_user or not db_user["team"]:
        await send_onboarding_miniapp(user.id, ctx.bot)
    else:
        team = TEAMS[db_user["team"]]
        await update.message.reply_text(
            f"👋 *Xush kelibsiz, {db_user['first_name']}!*\n"
            f"{team['emoji']} Jamoa: {team['name']}",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(),
        )

async def cmd_map(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🌍 *Territory Xaritasi*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🌍 Xaritani ochish", web_app={"url": MINI_APP_URL})]
        ]),
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "❓ *Territory Tashkent — Qo'llanma*\n\n"
        "🎯 *Maqsad:* Toshkent xaritasida hududlarni egallang!\n\n"
        "▶️ *Trek:* \"Trek boshlash\" tugmasini bosib, yuring.\n"
        "Boshlang'ich nuqtaga qaytib kelganingizda zona yaratiladi.\n\n"
        "📍 *Doira zona:* Joylashuvni yuboring va radius tanlang.\n\n"
        "⚔️ *Egallash:* Boshqa o'yinchining zonasi ichidan trek qiling.\n\n"
        "🏅 *Yutuqlar:* Yuring, zona yarating, do'stlarni taklif qiling!\n\n"
        "👥 *Referral:* Do'stlaringizni taklif qilib bonus oling."
    )
    await update.message.reply_text(help_text, parse_mode="Markdown", reply_markup=main_menu_kb())

async def cmd_team(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎽 Jamoa tanlang:", reply_markup=team_kb())

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db_user = get_user(user_id)
    if not db_user:
        return await update.message.reply_text("❗️ /start bosing.")
    team = TEAMS.get(db_user["team"] or "", {"emoji": "❓", "name": "Tanlanmagan"})
    await update.message.reply_text(
        f"📊 *Statistika*\n\n"
        f"👤 {db_user['first_name']}\n"
        f"{team['emoji']} {team['name']}\n\n"
        f"🗺 *Zonalar:* {db_user['zones_owned']} ta\n"
        f"⚔️ *Egallangan:* {db_user['zones_taken']} ta\n"
        f"🏃 *Jami masofa:* {db_user['total_km']:.2f} km\n"
        f"👥 *Referrallar:* {db_user['referral_count']} ta",
        parse_mode="Markdown",
    )

async def cmd_leaderboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT first_name, username, team, zones_owned, zones_taken, total_km "
            "FROM users ORDER BY zones_owned DESC LIMIT 10"
        ).fetchall()
    if not rows:
        return await update.message.reply_text("📋 Hali o'yinchilar yo'q.")
    text = "🏆 *TOP-10 O'yinchilar*\n\n"
    for i, r in enumerate(rows, 1):
        te = TEAMS[r["team"]]["emoji"] if r["team"] and r["team"] in TEAMS else "❓"
        text += f"{i}. {te} *{r['first_name']}*  🗺{r['zones_owned']} ⚔️{r['zones_taken']} 🏃{r['total_km']:.1f}km\n"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu_kb())

async def cmd_zones(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    zones = get_user_zones(update.effective_user.id)
    if not zones:
        return await update.message.reply_text("🗺 Hali zona yo'q.", reply_markup=main_menu_kb())
    text = f"🗺 *Zonalar ({len(zones)} ta)*\n\n"
    for i, z in enumerate(zones, 1):
        z_name = z.get("name") or f"Zona #{z['id']}"
        text += f"{i}. *{z_name}* — /history_{z['id']}\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        zone_id = int(update.message.text.split("_")[1])
        history = get_zone_history(zone_id)
        if not history:
            return await update.message.reply_text("Tarix topilmadi.")
        text = f"📜 *Zona #{zone_id} tarixi*\n\n"
        for h in history:
            text += f"• {h['action']} — {h['to_team']} ({h['captured_at'][:16]})\n"
        await update.message.reply_text(text, parse_mode="Markdown")
    except (IndexError, ValueError):
        await update.message.reply_text("❗️ Noto'g'ri format. /history_ID yozing.")

async def cmd_achievements(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_achs = get_user_achievements(user_id)

    if not user_achs:
        return await update.message.reply_text(
            "🏅 *Yutuqlar*\n\nHali yutuqlar yo'q. Trek qiling va zona yarating!",
            parse_mode="Markdown",
        )

    text = "🏅 *Sizning yutuqlaringiz:*\n\n"
    for a in user_achs:
        ach = ACHIEVEMENT_LIST.get(a["code"], {})
        title = ach.get("title", a["code"])
        desc = ach.get("desc", "")
        text += f"{title}\n  _{desc}_\n  📅 {a['earned_at'][:10]}\n\n"

    earned_codes = {a["code"] for a in user_achs}
    remaining = [v for k, v in ACHIEVEMENT_LIST.items() if k not in earned_codes]
    if remaining:
        text += f"🔒 *Qolgan: {len(remaining)} ta*\n"
        for r in remaining[:5]:
            text += f"  {r['title']}\n"

    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_referral(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db_user = get_user(user_id)
    bot_info = await ctx.bot.get_me()
    link = get_referral_link(user_id, bot_info.username)
    ref_count = db_user["referral_count"] if db_user else 0
    await update.message.reply_text(
        f"👥 *Referral tizimi*\n\n"
        f"🔗 Sizning havola:\n`{link}`\n\n"
        f"👤 Taklif qilganlar: *{ref_count}* ta\n\n"
        f"Do'stlaringizni taklif qiling!",
        parse_mode="Markdown",
    )

# ══════════════════════════════════════════════════════
# MESSAGE & CALLBACK HANDLERS
# ══════════════════════════════════════════════════════

async def handle_location(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db_user = get_user(user_id)
    if not db_user or not db_user["team"]:
        return await update.message.reply_text("❗️ Avval jamoa tanlang!", reply_markup=team_kb())
    lat, lng = update.message.location.latitude, update.message.location.longitude
    if ctx.user_data.get("mode") == MODE_CIRCLE:
        ctx.user_data.update({"circle_lat": lat, "circle_lng": lng})
        return await update.message.reply_text("📍 Radius tanlang:", reply_markup=radius_kb())
    await update.message.reply_text(
        "📍 Joylashuv qabul qilindi.\nZona yaratish:",
        reply_markup=zone_create_kb(),
    )

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id

    if text == "▶️ Trek boshlash":
        db_user = get_user(user_id)
        if not db_user or not db_user["team"]:
            return await update.message.reply_text("Avval jamoa tanlang!", reply_markup=team_kb())
        await update.message.reply_text(
            "👇 Quyidagi tugmani bosing:",
            reply_markup=trek_miniapp_kb(db_user["team"]),
        )
    elif text == "🗺 Zonalarim":
        await cmd_zones(update, ctx)
    elif text == "📊 Statistika":
        await cmd_stats(update, ctx)
    elif text == "🏆 Reyting":
        await cmd_leaderboard(update, ctx)
    elif text == "🌍 Xarita":
        await cmd_map(update, ctx)
    elif text == "🏅 Yutuqlar":
        await cmd_achievements(update, ctx)
    elif text == "👥 Referral":
        await cmd_referral(update, ctx)
    elif text == "❓ Yordam":
        await cmd_help(update, ctx)

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user_id = update.effective_user.id
    await q.answer()

    if q.data.startswith("team:"):
        team_key = q.data.split(":")[1]
        set_team(user_id, team_key)
        await q.edit_message_text(f"✅ Jamoa tanlandi: {TEAMS[team_key]['name']}")
        await q.message.reply_text("Asosiy menyu:", reply_markup=main_menu_kb())

    elif q.data == "zone:circle":
        ctx.user_data["mode"] = MODE_CIRCLE
        await q.edit_message_text("⭕️ Nuqtani yuboring (📍 Joylashuvni yuborish tugmasi):")

    elif q.data == "zone:cancel":
        ctx.user_data["mode"] = MODE_IDLE
        await q.edit_message_text("❌ Bekor qilindi.")

    elif q.data.startswith("radius:"):
        lat = ctx.user_data.get("circle_lat")
        lng = ctx.user_data.get("circle_lng")
        if not lat or not lng:
            return await q.edit_message_text("❗️ Markaz topilmadi. Avval joylashuvni yuboring.")
        db_user = get_user(user_id)
        if not db_user or not db_user["team"]:
            return await q.edit_message_text("❗️ Avval jamoa tanlang!")
        radius = float(q.data.split(":")[1])
        zone_id = await create_zone_circle_with_photo(ctx.bot, user_id, db_user["team"], lat, lng, radius)
        ctx.user_data["mode"] = MODE_IDLE

        updated_user = get_user(user_id)
        await check_and_award(user_id, ctx.bot, updated_user)

        area = math.pi * radius ** 2
        await q.edit_message_text(
            f"✅ *Zona yaratildi #{zone_id}!*\n\n"
            f"⭕️ Radius: {radius:.0f}m\n"
            f"📐 Maydon: {area / 10000:.4f} ga",
            parse_mode=ParseMode.MARKDOWN,
        )

# ══════════════════════════════════════════════════════
# WEB API SERVER
# ══════════════════════════════════════════════════════

async def api_trek_submit(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.Response(
            text=json.dumps({"ok": False, "error": "Invalid JSON"}),
            status=400,
            content_type="application/json",
            headers=CORS_HEADERS,
        )

    init_data = body.get("init_data", "")
    user_info = parse_init_data(init_data)

    if not user_info:
        logger.error(f"❌ Auth failed! init_data uzunligi: {len(init_data)}")
        return web.Response(
            text=json.dumps({"ok": False, "error": "Unauthorized — Yaroqsiz yoxud soxta initData"}),
            status=401,
            content_type="application/json",
            headers=CORS_HEADERS,
        )

    user_id    = user_info.get("id")
    first_name = user_info.get("first_name", "")
    username   = user_info.get("username", "")

    logger.info(f"✅ Auth OK: user_id={user_id}, name={first_name}")
    upsert_user(user_id, username, first_name)

    points = body.get("points", [])
    team   = body.get("team", "")
    closed = body.get("closed", False)
    dist_m = body.get("distance", 0)

    msg = await process_trek(_app.bot, user_id, points, team, closed, dist_m)

    try:
        await _app.bot.send_message(
            chat_id=user_id,
            text=msg,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_kb(),
        )
        logger.info(f"📤 Bot xabar yuborildi: user_id={user_id}")
    except Exception as e:
        logger.error(f"❌ Bot message error: {e}")

    return web.Response(
        text=json.dumps({"ok": True, "message": "Trek qabul qilindi!"}),
        content_type="application/json",
        headers=CORS_HEADERS,
    )

async def api_zones(request: web.Request) -> web.Response:
    zones = get_all_zones()
    return web.Response(
        text=json.dumps(zones, default=str),
        content_type="application/json",
        headers=CORS_HEADERS,
    )

async def api_health(request: web.Request) -> web.Response:
    return web.Response(text="OK", headers=CORS_HEADERS)

async def start_web_server():
    app_web = web.Application(middlewares=[cors_middleware])
    app_web.router.add_route(
        "OPTIONS", "/api/trek_submit",
        lambda r: web.Response(status=200, headers=CORS_HEADERS),
    )
    app_web.router.add_post("/api/trek_submit", api_trek_submit)
    app_web.router.add_get("/api/zones", api_zones)
    app_web.router.add_get("/health", api_health)

    runner = web.AppRunner(app_web)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"🌐 Web server ishga tushdi: port {port}")

    while True:
        await asyncio.sleep(3600)

async def on_startup(app: Application) -> None:
    global _app
    _app = app
    task = asyncio.create_task(start_web_server())
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    logger.info("🚀 Bot ishga tushdi!")

# ══════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════

def main():
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN topilmadi! Railway Variables da sozlang.")
        return

    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(on_startup).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("team", cmd_team))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(CommandHandler("zones", cmd_zones))
    app.add_handler(CommandHandler("map", cmd_map))
    app.add_handler(CommandHandler("achievements", cmd_achievements))
    app.add_handler(CommandHandler("referral", cmd_referral))

    app.add_handler(MessageHandler(filters.Regex(r"^/history_\d+"), cmd_history))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("🤖 Bot polling boshlandi...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
