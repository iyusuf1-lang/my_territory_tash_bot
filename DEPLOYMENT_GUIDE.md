# 🚀 Territory Bot v2.0 - Deployment Guide

## 📦 Yangilangan Fayllar

### 1. territory_bot.py (Backend)
**Asosiy o'zgarishlar:**
- ✅ `import time` qo'shildi
- ✅ `INIT_DATA_MAX_AGE` environment variable support
- ✅ BOT_TOKEN validation on startup
- ✅ `parse_init_data()` - auth_date expire check
- ✅ `api_trek_submit()` - better error messages
- ✅ SESSION_EXPIRED error code
- ✅ Improved logging

**Yangi environment variables:**
```
INIT_DATA_MAX_AGE=3600    # optional, default: 3600 (1 hour)
```

---

## 🔧 GitHub'ga Deploy Qilish

### Qadam 1: GitHub Repository'ga Push

```bash
# Local git repository'da
cd my_territory_tash_bot

# Yangi faylni copy qiling
cp /path/to/territory_bot.py territory_bot.py

# Git'ga qo'shing
git add territory_bot.py
git commit -m "fix: initData expiration check + better error handling"
git push origin main
```

### Qadam 2: Railway'da Auto-Deploy

Railway GitHub bilan bog'langan bo'lsa, avtomatik redeploy bo'ladi.

Agar manual deploy kerak bo'lsa:
```bash
railway up
```

### Qadam 3: Railway Variables'ni Tekshirish

1. Railway Dashboard'ga kiring
2. Territory bot project'ni oching
3. **Variables** tab'ga o'ting
4. Quyidagi variables mavjudligini tekshiring:

```
BOT_TOKEN=8664008696:AAEy6cuhP0yKKQu1Tp-IEm9FwTWCVRCrYOg
DB_PATH=territory.db
MINI_APP_URL=https://iyusuf1-lang.github.io/my_territory_tash_bot/
INIT_DATA_MAX_AGE=3600
```

**MUHIM:** Agar `BOT_TOKEN` yo'q bo'lsa, bot ishlamaydi!

### Qadam 4: Logs'ni Tekshirish

Deploy'dan keyin:
```bash
railway logs --tail 50
```

Quyidagi xabarlarni ko'rishingiz kerak:
```
✅ BOT_TOKEN sozlangan: 8664008696:AAEy6...
⏰ initData max age: 3600 soniya
✅ DB initialized
🤖 Bot polling boshlandi...
🌐 Web server ishga tushdi: port 8080
```

---

## 🧪 Test Qilish

### Test 1: Normal Trek (Tez trek)
1. Botni oching
2. Trek boshlang
3. 2-3 daqiqa yuring
4. Finish bosing
5. ✅ "Zona yaratildi!" xabari kelishi kerak

### Test 2: Expire Test (Optional - 1 soat kutish kerak)
1. Botni oching
2. Trek boshlang
3. **1 soat kutib turing** (yoki phone clock'ni o'zgartiring)
4. Finish bosing
5. ✅ Frontend: "⏰ Sessiya tugadi" xabari
6. ✅ Backend logs: "❌ initData EXPIRE BO'LGAN!"

### Test 3: Logs Monitoring
Railway logs'da quyidagilarni kuzating:

**Successful request:**
```
📥 initData uzunligi: 245
⏰ initData age: 123s (max: 3600s)
✅ HMAC tekshiruvi muvaffaqiyatli!
✅ User authenticated: 123456 - Muhammadyusuf
✅ Auth OK: user_id=123456, name=Muhammadyusuf
```

**Expired request:**
```
📥 initData uzunligi: 245
⏰ initData age: 3750s (max: 3600s)
❌ initData EXPIRE BO'LGAN! Age: 3750s > 3600s
   💡 Foydalanuvchi botni qayta ochishi kerak!
❌ Auth failed! Error code: SESSION_EXPIRED
```

---

## 📊 O'zgarishlar Timeline

| O'zgarish | Old Version | New Version |
|-----------|-------------|-------------|
| initData expiration | ❌ Tekshirilmaydi | ✅ 1 soat limit |
| Error messages | Generic "Unauthorized" | Aniq "Sessiya tugadi" |
| BOT_TOKEN | Hardcoded fallback | Validated on startup |
| Logging | Basic | Detailed + debugging |
| Clock skew | ❌ None | ✅ 60s tolerance |

---

## 🔄 Rollback (Agar muammo bo'lsa)

Agar yangi versiyada muammo bo'lsa, eski versiyaga qaytish:

```bash
git revert HEAD
git push origin main
```

Yoki Railway dashboard'da "Rollback" tugmasini bosing.

---

## 💡 Qo'shimcha Tavsiyalar

### 1. Frontend'ni ham yangilash (Trek.html)

Agar frontend'ni ham yangilamoqchi bo'lsangiz:

```javascript
// trek.html - sendResult funksiyasiga qo'shing
function checkInitDataFresh() {
  const params = new URLSearchParams(tg.initData);
  const authDate = parseInt(params.get('auth_date'));
  const age = Math.floor(Date.now() / 1000) - authDate;
  
  if (age > 3500) {  // 58 minutes
    alert('⏰ Sessiya tugayapti! Tez finish bosing yoki botni qayta oching.');
    return false;
  }
  return true;
}

// sendResult'da ishlatish
if (!checkInitDataFresh()) {
  alert('⏰ Sessiya tugadi! Botni qayta oching.');
  tg?.close();
  return;
}
```

### 2. Monitoring Dashboard (Optional)

Railway'da "Observability" tab'da metrics'ni kuzating:
- Request count
- Error rate
- Response time

### 3. User Communication

Foydalanuvchilarga yangilikni bildiring:
```
📢 Yangilik!

Territory bot yangilandi! 🎉

✅ Sessiya tizimi qo'shildi
✅ Xatolar aniqroq ko'rsatiladi
✅ Bot barqarorroq ishlaydi

Agar muammo bo'lsa, botni yoping va qayta oching.
```

---

## ❓ FAQ

**Q: initData nima uchun expire bo'ladi?**
A: Telegram xavfsizlik uchun. Har 1 soatda user botni qayta ochishi kerak.

**Q: Foydalanuvchi 1 soatdan ko'proq yuradigan bo'lsa?**
A: Frontend warning ko'rsatadi va finish bosishni tavsiya qiladi. Agar kech bo'lsa, botni qayta ochish kerak.

**Q: INIT_DATA_MAX_AGE'ni oshirsam bo'ladimi?**
A: Texnik jihatdan ha, lekin **tavsiya etilmaydi**. Telegram 1 soatdan ko'proq qo'llab-quvvatlamaydi.

**Q: Test qilish uchun 1 soat kutish kerakmi?**
A: Yo'q, phone clock'ni o'zgartiring yoki `INIT_DATA_MAX_AGE=60` qilib test qiling (60 soniya).

---

## 🎯 Success Criteria

Deploy muvaffaqiyatli bo'lsa:
- ✅ Bot ishlab turibdi (Railway logs "🤖 Bot polling boshlandi")
- ✅ Normal trek ishlayapti
- ✅ Expire handling ishlayapti (test qilsangiz)
- ✅ Logs aniq va tushunarli
- ✅ Xatolar kamaydi

---

## 📞 Yordam

Muammo bo'lsa:
1. Railway logs'ni tekshiring
2. BOT_TOKEN'ni tekshiring
3. test_hmac.py'ni ishlating
4. QUICK_FIX_GUIDE.md'ni o'qing

Omad! 🚀
