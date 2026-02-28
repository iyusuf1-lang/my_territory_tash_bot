
"""
Bot ga qo'shish kerak bo'lgan o'zgarishlar:

1. handle_webapp_data funksiyasini yangilash
2. Trek boshlash tugmasini Mini App ga o'zgartirish
"""

# ══════════════════════════════════════════════════════
# 1. handle_webapp_data — trek_finished qo'shish
# ══════════════════════════════════════════════════════

async def handle_webapp_data(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Mini App dan kelgan ma'lumotlarni qabul qilish"""
    user_id = update.effective_user.id
    try:
        data = json.loads(update.message.web_app_data.data)
    except Exception:
        return

    action = data.get("action")

    # ── Onboarding tugadi ──
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
            await update.message.reply_text(
                "🎽 *Jamoangizni tanlang:*",
                parse_mode="Markdown",
                reply_markup=kb,
            )

    # ── 🆕 Trek yakunlandi Mini App dan ──
    elif action == "trek_finished":
        points   = data.get("points", [])
        team     = data.get("team", "")
        closed   = data.get("closed", False)
        dist_m   = data.get("distance", 0)

        if not points or len(points) < 5:
            await update.message.reply_text(
                "❗ Trek juda qisqa yoki nuqtalar yetarli emas.",
                reply_markup=main_menu_kb(),
            )
            return

        # DB dagi userni tekshirish
        db_user = get_user(user_id)
        if not db_user:
            upsert_user(user_id, update.effective_user.username or "", update.effective_user.first_name or "")
            db_user = get_user(user_id)

        # Agar bot da jamoa o'zgartirilgan bo'lsa — bot jamosini ishlatamiz
        if db_user and db_user.get("team"):
            team = db_user["team"]

        if not team or team not in TEAMS:
            await update.message.reply_text("❗ Jamoa tanlanmagan. /start bosing.")
            return

        dist_km = dist_m / 1000

        # Trek ni DB ga saqlash
        with get_db() as conn:
            conn.execute("UPDATE treks SET status='cancelled' WHERE user_id=? AND status='active'", (user_id,))
            cur = conn.execute(
                "INSERT INTO treks (user_id, points, distance_m, started_at, finished_at, status) "
                "VALUES (?, ?, ?, datetime('now'), datetime('now'), 'finished')",
                (user_id, json.dumps(points), dist_m)
            )
            conn.execute("UPDATE users SET total_km = total_km + ? WHERE user_id=?", (dist_km, user_id))

        msg = f"⏹ *Trek yakunlandi!*\n📏 {dist_km:.3f} km | 📍 {len(points)} nuqta\n"

        if closed:
            zone_id  = await create_zone_polygon_with_photo(update.get_bot(), user_id, team, points)
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

            await check_and_award(user_id, update.get_bot(), get_user(user_id))

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
                    zone_label = c["name"] or f"#{c['id']}"
                    msg += f"  {te_c} {zone_label}\n"
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

        await update.message.reply_text(
            msg,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_kb(),
        )


# ══════════════════════════════════════════════════════
# 2. handle_text — Trek boshlash tugmasi o'zgarishi
# ══════════════════════════════════════════════════════

# handle_text ichidagi "▶️ Trek boshlash" blokini mana shu bilan almashtiring:

"""
    if text == "▶️ Trek boshlash":
        db_user = get_user(user_id)
        if not db_user or not db_user["team"]:
            await update.message.reply_text("Avval /start bosing!")
            return

        # Mini App URL — trek.html
        mini_app_url = os.getenv("MINI_APP_URL", "https://iyusuf1-lang.github.io/my_territory_tash_bot/")
        trek_url = mini_app_url.rstrip("/") + "/trek.html"

        # Jamoani URL ga qo'shish (optional)
        team_param = db_user.get("team", "")
        if team_param:
            trek_url += f"?team={team_param}"

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "🗺 Trekni boshlash",
                web_app={"url": trek_url}
            )
        ]])

        team_info = TEAMS.get(db_user["team"], {"emoji": "❓", "name": "?"})
        await update.message.reply_text(
            f"▶️ *Trek boshlash*\n\n"
            f"{team_info['emoji']} Jamoa: {team_info['name']}\n\n"
            f"📱 Mini App ochiladi:\n"
            f"• GPS avtomatik yoqiladi\n"
            f"• Xaritada yo'lingiz chiziladi\n"
            f"• Aylana yopilganda zona yaratiladi\n\n"
            f"👇 Quyidagi tugmani bosing:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
        )
"""

# ══════════════════════════════════════════════════════
# 3. trek.html ni GitHub Pages ga joylash
# ══════════════════════════════════════════════════════
"""
trek.html faylini:
  https://iyusuf1-lang.github.io/my_territory_tash_bot/trek.html

ga joylash kerak.

Buning uchun:
  1. GitHub repo: iyusuf1-lang/my_territory_tash_bot
  2. trek.html faylini repo ga push qiling
  3. GitHub Pages avtomatik deploy qiladi
"""
