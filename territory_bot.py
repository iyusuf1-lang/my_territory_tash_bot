#!/usr/bin/env python3
"""
🗺️ Toshkent Territory Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━
Yuguruvchilar uchun hududiy o'yin:
  ✅ GPS trek yozish (yurgan yo'l)
  ✅ Doira zona yaratish (markaz + radius)
  ✅ Zona egallash (dushman zonasini aylanib o'tish)
  ✅ Jamoa o'yini (qizil vs ko'k vs yashil vs sariq)
  ✅ Bildirishnomalar (zonangizga tajovuz)
  ✅ Reyting va statistika
  ✅ Zona tarixi
"""

import asyncio
import logging
import os
import json
import math
from datetime import datetime, timedelta
from contextlib import contextmanager

import sqlite3
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.constants import ParseMode

# ══════════════════════════════════════════════════════
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "8664008696:AAEy6cuhP0yKKQu1Tp-IEm9FwTWCVRCrYOg")
DB_PATH   = os.getenv("DB_PATH", "territory.db")

# Jamoalar
TEAMS = {
    "red":    {"name": "🔴 Qizil",   "emoji": "🔴"},
    "blue":   {"name": "🔵 Ko'k",    "emoji": "🔵"},
    "green":  {"name": "🟢 Yashil",  "emoji": "🟢"},
    "yellow": {"name": "🟡 Sariq",   "emoji": "🟡"},
}

# Rejimlar
MODE_IDLE   = "idle"
MODE_TREK   = "trek"
MODE_CIRCLE = "circle"

# Spam oldini olish
notif_cache: dict = {}

# ══════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            first_name  TEXT,
            team        TEXT DEFAULT NULL,
            total_km    REAL DEFAULT 0,
            zones_owned INTEGER DEFAULT 0,
            zones_taken INTEGER DEFAULT 0,
            created_at  TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS zones (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id    INTEGER NOT NULL,
            team        TEXT NOT NULL,
            name        TEXT,
            zone_type   TEXT NOT NULL,   -- 'circle' | 'polygon'
            geometry    TEXT NOT NULL,   -- JSON
            center_lat  REAL NOT NULL,
            center_lng  REAL NOT NULL,
            radius_m    REAL,
            area_m2     REAL DEFAULT 0,
            active      INTEGER DEFAULT 1,
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
            action      TEXT NOT NULL,   -- 'created' | 'captured'
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
    """Ray casting algoritmi"""
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


def calc_trek_distance(points: list) -> float:
    total = 0.0
    for i in range(1, len(points)):
        total += haversine(
            points[i-1]["lat"], points[i-1]["lng"],
            points[i]["lat"],   points[i]["lng"]
        )
    return total


def trek_is_closed(points: list, threshold_m: float = 50) -> bool:
    if len(points) < 8:
        return False
    d = haversine(points[0]["lat"], points[0]["lng"], points[-1]["lat"], points[-1]["lng"])
    return d <= threshold_m


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
    """Zona markazi trek polygon ichida bo'lsa — egallangan"""
    clat = zone["center_lat"]
    clng = zone["center_lng"]
    return point_in_polygon(clat, clng, trek_points)


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


def capture_zone(zone_id, new_owner, new_team) -> dict | None:
    with get_db() as conn:
        z = conn.execute("SELECT * FROM zones WHERE id=?", (zone_id,)).fetchone()
        if not z:
            return None
        z = dict(z)
        conn.execute("UPDATE zones SET owner_id=?, team=? WHERE id=?", (new_owner, new_team, zone_id))
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


def get_zone_history(zone_id) -> list:
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM zone_history WHERE zone_id=? ORDER BY captured_at DESC LIMIT 10",
            (zone_id,)
        ).fetchall()]


# ══════════════════════════════════════════════════════
# TREK OPERATIONS
# ══════════════════════════════════════════════════════

def start_trek(user_id) -> int:
    with get_db() as conn:
        conn.execute("UPDATE treks SET status='cancelled' WHERE user_id=? AND status='active'", (user_id,))
        cur = conn.execute(
            "INSERT INTO treks (user_id, points, started_at) VALUES (?, '[]', datetime('now'))",
            (user_id,)
        )
        return cur.lastrowid


def add_trek_point(user_id, lat, lng) -> dict | None:
    with get_db() as conn:
        trek = conn.execute(
            "SELECT * FROM treks WHERE user_id=? AND status='active'", (user_id,)
        ).fetchone()
        if not trek:
            return None
        trek = dict(trek)
        points = json.loads(trek["points"])
        points.append({"lat": lat, "lng": lng, "time": datetime.now().isoformat()})
        dist = calc_trek_distance(points)
        conn.execute("UPDATE treks SET points=?, distance_m=? WHERE id=?",
                     (json.dumps(points), dist, trek["id"]))
        return {"trek_id": trek["id"], "points": points, "distance_m": dist, "is_closed": trek_is_closed(points)}


def finish_trek(user_id) -> dict | None:
    with get_db() as conn:
        trek = conn.execute(
            "SELECT * FROM treks WHERE user_id=? AND status='active'", (user_id,)
        ).fetchone()
        if not trek:
            return None
        trek = dict(trek)
        points = json.loads(trek["points"])
        dist = calc_trek_distance(points)
        conn.execute(
            "UPDATE treks SET status='finished', finished_at=datetime('now'), distance_m=? WHERE id=?",
            (dist, trek["id"])
        )
        conn.execute("UPDATE users SET total_km = total_km + ? WHERE user_id=?", (dist/1000, user_id))
        return {"points": points, "distance_m": dist}


# ══════════════════════════════════════════════════════
# KEYBOARDS
# ══════════════════════════════════════════════════════

def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        [KeyboardButton("📍 Joylashuvni yuborish", request_location=True)],
        [KeyboardButton("▶️ Trek boshlash"), KeyboardButton("⏹ Trek tugatish")],
        [KeyboardButton("🗺 Zonalarim"),     KeyboardButton("📊 Statistika")],
        [KeyboardButton("🏆 Reyting"),       KeyboardButton("❓ Yordam")],
    ], resize_keyboard=True)


def trek_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        [KeyboardButton("📍 Joylashuvni yuborish", request_location=True)],
        [KeyboardButton("⏹ Trek tugatish")],
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
        [InlineKeyboardButton("⭕ Doira zona yaratish",     callback_data="zone:circle")],
        [InlineKeyboardButton("🔷 Trek orqali zona yaratish", callback_data="zone:trek")],
        [InlineKeyboardButton("❌ Bekor",                   callback_data="zone:cancel")],
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


# ══════════════════════════════════════════════════════
# HANDLERS
# ══════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user.id, user.username or "", user.first_name or "")
    db_user = get_user(user.id)

    if not db_user or not db_user["team"]:
        await update.message.reply_text(
            "👋 *Toshkent Territory o'yiniga xush kelibsiz!*\n\n"
            "🏃 *Qanday ishlaydi:*\n"
            "• GPS trek yozib aylana yasang → hudud siznikiiga o'tadi\n"
            "• Joylashuvdan doira zona yarating\n"
            "• Dushman zonasini aylanib o'ting → zona siznikiiga o'tadi\n"
            "• Kimdir zonangizga yaqinlashsa → xabar olasiz!\n\n"
            "🎽 *Avval jamoa tanlang:*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=team_kb(),
        )
    else:
        team = TEAMS[db_user["team"]]
        await update.message.reply_text(
            f"👋 Qaytib keldingiz!\n{team['emoji']} Jamoa: {team['name']}",
            reply_markup=main_menu_kb(),
        )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ *Qo'llanma*\n\n"
        "*1️⃣ Trek orqali zona:*\n"
        "▶️ Trek boshlang → 📍 Joylashuvni yuboring → Aylana yasab boshqa nuqtaga qaytib keling → ⏹ Trek tugatish\n\n"
        "*2️⃣ Doira zona:*\n"
        "📍 Joylashuvni yuboring → Doira zona yaratish → Radius tanlang\n\n"
        "*⚔️ Zona egallash:*\n"
        "Dushman zonasini aylanib o'ting (zona markazi trek ichida bo'lsin)\n\n"
        "*🔔 Bildirishnoma:*\n"
        "Kimdir 200m yaqinlashsa — xabar olasiz\n\n"
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
    # Supports both /history 5 and /history_5
    if len(args) > 1:
        zone_id_str = args[1]
    else:
        # /history_5 formatini ham qo'llab-quvvatlash
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

    # ── Trek rejimi ──
    if mode == MODE_TREK:
        result = add_trek_point(user_id, lat, lng)
        if result:
            pts     = len(result["points"])
            dist_km = result["distance_m"] / 1000
            msg     = f"📍 Nuqta #{pts} | 📏 {dist_km:.3f} km"
            if result["is_closed"]:
                msg += "\n\n✅ *Trek yopiq! ⏹ Trek tugatish tugmasini bosing.*"
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        return

    # ── Doira markaz ──
    if mode == MODE_CIRCLE:
        ctx.user_data["circle_lat"] = lat
        ctx.user_data["circle_lng"] = lng
        await update.message.reply_text(
            f"📍 Markaz: `{lat:.5f}, {lng:.5f}`\n\nRadius tanlang:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=radius_kb(),
        )
        return

    # ── Oddiy joylashuv ──
    ctx.user_data["last_lat"] = lat
    ctx.user_data["last_lng"] = lng
    nearby = get_zones_near(lat, lng, 500)
    team   = TEAMS[db_user["team"]]

    msg = f"📍 Joylashuv qabul qilindi.\n{team['emoji']} Jamoa: {team['name']}\n"
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
            tp = "⭕" if z["zone_type"] == "circle" else "🔷"
            nm_z = z["name"] or f"#{z['id']}"
            msg += f"  {tp} {nm_z} — {owner_txt} ({z['distance']:.0f}m)\n"

    await update.message.reply_text(
        msg + "\nNima qilmoqchisiz?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=zone_create_kb(),
    )


# ══════════════════════════════════════════════════════
# TEXT HANDLER
# ══════════════════════════════════════════════════════

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text    = update.message.text
    user_id = update.effective_user.id

    # /history_5 kabi komandalar
    if text.startswith("/history"):
        await cmd_history(update, ctx)
        return

    if text == "▶️ Trek boshlash":
        db_user = get_user(user_id)
        if not db_user or not db_user["team"]:
            await update.message.reply_text("Avval /start bosing!")
            return
        ctx.user_data["mode"] = MODE_TREK
        start_trek(user_id)
        team = TEAMS[db_user["team"]]
        await update.message.reply_text(
            f"▶️ *Trek boshlandi!*\n\n"
            f"{team['emoji']} Jamoa: {team['name']}\n\n"
            f"📍 Joylashuvingizni muntazam yuboring.\n"
            f"Aylana yasab boshlang'ich nuqtaga qaytib keling.\n"
            f"Keyin ⏹ *Trek tugatish* tugmasini bosing.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=trek_menu_kb(),
        )

    elif text == "⏹ Trek tugatish":
        await handle_finish_trek(update, ctx)

    elif text == "🗺 Zonalarim":
        await cmd_zones(update, ctx)

    elif text == "📊 Statistika":
        await cmd_stats(update, ctx)

    elif text == "🏆 Reyting":
        await cmd_leaderboard(update, ctx)

    elif text == "❓ Yordam":
        await cmd_help(update, ctx)

    else:
        await update.message.reply_text("❓ Tushunmadim.", reply_markup=main_menu_kb())


async def handle_finish_trek(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db_user = get_user(user_id)
    ctx.user_data["mode"] = MODE_IDLE

    result = finish_trek(user_id)
    if not result or len(result["points"]) < 5:
        await update.message.reply_text(
            "❗ Trek juda qisqa yoki boshlangani yo'q.",
            reply_markup=main_menu_kb(),
        )
        return

    points  = result["points"]
    dist_km = result["distance_m"] / 1000
    closed  = trek_is_closed(points)

    msg = f"⏹ *Trek yakunlandi!*\n📏 {dist_km:.3f} km | 📍 {len(points)} nuqta\n"

    if closed:
        team     = db_user["team"]
        zone_id  = create_zone_polygon(user_id, team, points)
        area     = polygon_area_m2(points)
        captured = []

        all_zones = get_all_zones()
        for z in all_zones:
            if z["owner_id"] == user_id or z["id"] == zone_id:
                continue
            if zone_is_captured_by_trek(points, z):
                old = capture_zone(z["id"], user_id, team)
                if old:
                    captured.append(old)
                    # Eski egaga bildirishnoma
                    team_info = TEAMS[team]
                    z_name    = old["name"] or f"Zona #{old['id']}"
                    try:
                        await update.get_bot().send_message(
                            chat_id=old["owner_id"],
                            text=f"⚔️ *Zonangiz egallandi!*\n\n"
                                 f"🏴 {z_name}\n"
                                 f"{team_info['emoji']} {db_user['first_name']} tomonidan!\n\n"
                                 f"Qaytarib oling! 💪",
                            parse_mode=ParseMode.MARKDOWN,
                        )
                    except Exception as e:
                        logger.warning(f"Capture notif error: {e}")

        msg += (
            f"\n✅ *Zona yaratildi #{zone_id}*\n"
            f"📐 Maydon: {area/10000:.4f} ga\n"
        )
        if captured:
            msg += f"\n⚔️ *{len(captured)} zona egallandi!*\n"
            for c in captured:
                te = TEAMS.get(c["team"], {"emoji": "❓"})["emoji"]
                zone_label = c["name"] or f"#{c['id']}"
                msg += f"  {te} {zone_label}\n"
    else:
        d_close = haversine(points[0]["lat"], points[0]["lng"], points[-1]["lat"], points[-1]["lng"])
        msg += (
            f"\n⚠️ *Trek yopiq emas.*\n"
            f"Boshlang'ich nuqtaga: {d_close:.0f}m qoldi.\n"
            f"50m yaqinlashganda yopiq hisoblanadi."
        )

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
        await q.edit_message_text(
            "⭕ *Doira zona*\n\n📍 Markaz nuqtani yuboring (joylashuvni yuboring):",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif data == "zone:trek":
        ctx.user_data["mode"] = MODE_TREK
        start_trek(user_id)
        await q.edit_message_text(
            "▶️ *Trek boshlandi!*\n\n"
            "📍 Joylashuvingizni muntazam yuboring.\n"
            "Aylana yasab boshlang'ich nuqtaga qaytib keling.\n"
            "Keyin ⏹ Trek tugatish.",
            parse_mode=ParseMode.MARKDOWN,
        )
        await q.message.reply_text("Trek rejimi:", reply_markup=trek_menu_kb())

    elif data == "zone:cancel":
        ctx.user_data["mode"] = MODE_IDLE
        await q.edit_message_text("❌ Bekor qilindi.")
        await q.message.reply_text("Asosiy menyu:", reply_markup=main_menu_kb())

    elif data.startswith("radius:"):
        radius = float(data.split(":")[1])
        lat    = ctx.user_data.get("circle_lat")
        lng    = ctx.user_data.get("circle_lng")

        if not lat or not lng:
            await q.edit_message_text("❗ Markaz topilmadi. Qaytadan joylashuv yuboring.")
            return

        db_user = get_user(user_id)
        if not db_user or not db_user["team"]:
            await q.edit_message_text("❗ Jamoa tanlanmagan. /start bosing.")
            return

        team    = db_user["team"]
        zone_id = create_zone_circle(user_id, team, lat, lng, radius)
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
# BACKGROUND: Tajovuz tekshiruvi
# ══════════════════════════════════════════════════════

async def invasion_checker(app):
    """Har 60s da aktiv trekdagi o'yinchilar boshqa zonalarga yaqinmi — tekshirish"""
    while True:
        try:
            with get_db() as conn:
                treks = conn.execute(
                    "SELECT * FROM treks WHERE status='active'"
                ).fetchall()
                for trek in treks:
                    trek  = dict(trek)
                    pts   = json.loads(trek["points"])
                    if not pts:
                        continue
                    last  = pts[-1]
                    uid   = trek["user_id"]
                    udata = get_user(uid)
                    if not udata:
                        continue

                    near_zones = get_zones_near(last["lat"], last["lng"], 200)
                    for z in near_zones:
                        if z["owner_id"] == uid:
                            continue
                        # Spam oldini olish: 1 soatda bir marta
                        key = f"{z['id']}_{uid}"
                        last_t = notif_cache.get(key)
                        if last_t and (datetime.now() - last_t).seconds < 3600:
                            continue
                        notif_cache[key] = datetime.now()

                        te_att = TEAMS.get(udata["team"], {"emoji": "❓"})
                        z_name = z["name"] or f"Zona #{z['id']}"
                        try:
                            await app.bot.send_message(
                                chat_id=z["owner_id"],
                                text=f"⚠️ *Zonangizga tajovuz!*\n\n"
                                     f"🏴 {z_name}\n"
                                     f"{te_att['emoji']} {udata['first_name']} "
                                     f"{z['distance']:.0f}m yaqinlashmoqda!\n\n"
                                     f"Tezroq qaytib himoya qiling! 🏃",
                                parse_mode=ParseMode.MARKDOWN,
                            )
                        except Exception as e:
                            logger.warning(f"Invasion notif error: {e}")
        except Exception as e:
            logger.error(f"Invasion checker error: {e}")
        await asyncio.sleep(60)


# ══════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════

def main():
    init_db()

    async def post_init(application: Application):
        application.create_task(invasion_checker(application))

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("team",        cmd_team))
    app.add_handler(CommandHandler("stats",       cmd_stats))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(CommandHandler("zones",       cmd_zones))
    app.add_handler(CommandHandler("history",     cmd_history))
    app.add_handler(MessageHandler(filters.LOCATION,                handle_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("🗺️ Territory Bot ishga tushdi!")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
