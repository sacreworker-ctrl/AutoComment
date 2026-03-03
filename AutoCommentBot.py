import asyncio
import hashlib
import hmac
import os
import random
import sqlite3
import string
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import threading

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG — замени на свои значения
# ─────────────────────────────────────────────────────────────────────────────
BOT_TOKEN   = "8429828841:AAFMbWdXUXzGeC8MnYM4-Chkbvf6MhdnESk"   # токен от @BotFather
ADMIN_ID    = 8220741481                # твой Telegram user_id
SECRET_KEY  = "AutoComment_v2.0_HMAC_secret_2026"  # секрет для генерации ключей
DB_PATH     = "keys.db"

# Сроки действия
DURATIONS = {
    "12h":  12 * 3600,
    "1d":   1  * 86400,
    "3d":   3  * 86400,
    "7d":   7  * 86400,
    "21d":  21 * 86400,
    "30d":  30 * 86400,
}

DURATION_LABELS = {
    "12h":  "12 часов",
    "1d":   "1 день",
    "3d":   "3 дня",
    "7d":   "7 дней",
    "21d":  "21 день",
    "30d":  "30 дней",
}

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

# ─────────────────────────────────────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────────────────────────────────────
def db_connect():
    return sqlite3.connect(DB_PATH)

def db_init():
    with db_connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS keys (
                key         TEXT PRIMARY KEY,
                user_id     INTEGER NOT NULL,
                duration    INTEGER NOT NULL,
                issued_at   INTEGER NOT NULL,
                expires_at  INTEGER NOT NULL,
                activated   INTEGER DEFAULT 0,
                activated_at INTEGER DEFAULT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
                joined_at   INTEGER NOT NULL
            )
        """)
        con.commit()

def db_upsert_user(user_id: int, username: str, first_name: str):
    with db_connect() as con:
        con.execute("""
            INSERT OR IGNORE INTO users (user_id, username, first_name, joined_at)
            VALUES (?, ?, ?, ?)
        """, (user_id, username, first_name, int(datetime.now().timestamp())))
        con.execute("""
            UPDATE users SET username=?, first_name=? WHERE user_id=?
        """, (username, first_name, user_id))
        con.commit()

def db_add_key(key: str, user_id: int, duration: int):
    now = int(datetime.now().timestamp())
    expires = now + duration
    with db_connect() as con:
        con.execute("""
            INSERT INTO keys (key, user_id, duration, issued_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
        """, (key, user_id, duration, now, expires))
        con.commit()

def db_get_user_keys(user_id: int):
    now = int(datetime.now().timestamp())
    # clean expired
    with db_connect() as con:
        con.execute("DELETE FROM keys WHERE expires_at < ?", (now,))
        con.commit()
    with db_connect() as con:
        cur = con.execute("""
            SELECT key, duration, issued_at, expires_at, activated
            FROM keys WHERE user_id=? ORDER BY issued_at DESC
        """, (user_id,))
        return cur.fetchall()

def db_get_all_keys():
    now = int(datetime.now().timestamp())
    with db_connect() as con:
        con.execute("DELETE FROM keys WHERE expires_at < ?", (now,))
        con.commit()
    with db_connect() as con:
        cur = con.execute("""
            SELECT key, user_id, duration, issued_at, expires_at, activated
            FROM keys ORDER BY issued_at DESC
        """)
        return cur.fetchall()

def db_revoke_key(key: str) -> bool:
    with db_connect() as con:
        cur = con.execute("DELETE FROM keys WHERE key=?", (key,))
        con.commit()
        return cur.rowcount > 0

def db_get_user(user_id: int):
    with db_connect() as con:
        cur = con.execute("SELECT user_id, username, first_name FROM users WHERE user_id=?", (user_id,))
        return cur.fetchone()

def db_key_exists(key: str) -> bool:
    with db_connect() as con:
        cur = con.execute("SELECT 1 FROM keys WHERE key=?", (key,))
        return cur.fetchone() is not None

# ─────────────────────────────────────────────────────────────────────────────
# Key generation — HMAC-SHA256
# Тот же алгоритм нужно реализовать в AutoComment для проверки
# ─────────────────────────────────────────────────────────────────────────────
def generate_key(user_id: int, duration: int, issued_at: int) -> str:
    """
    Генерирует уникальный ключ на основе HMAC-SHA256.
    AutoComment.exe проверяет подпись тем же методом.
    """
    # Добавляем случайный суффикс чтобы ключи одного юзера не совпадали
    rand = ''.join(random.choices(string.ascii_letters + string.digits, k=4))
    msg = f"{user_id}:{duration}:{issued_at}:{rand}"
    sig = hmac.new(SECRET_KEY.encode(), msg.encode(), hashlib.sha256).hexdigest()
    # Берём первые 8 символов подписи + rand = итоговый ключ
    key = f"{sig[:8]}_{rand}"
    return key

def make_unique_key(user_id: int, duration: int) -> str:
    issued_at = int(datetime.now().timestamp())
    for _ in range(10):
        key = generate_key(user_id, duration, issued_at)
        if not db_key_exists(key):
            return key
        issued_at += 1
    # fallback
    return generate_key(user_id, duration, issued_at + random.randint(100, 999))

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def fmt_time(seconds: int) -> str:
    if seconds <= 0:
        return "истёк"
    d = seconds // 86400
    h = (seconds % 86400) // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if d > 0:
        return f"{d}д {h:02d}ч {m:02d}м"
    return f"{h:02d}ч {m:02d}м {s:02d}с"

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

# ─────────────────────────────────────────────────────────────────────────────
# /start
# ─────────────────────────────────────────────────────────────────────────────
@dp.message(Command("start"))
async def cmd_start(msg: Message):
    db_upsert_user(msg.from_user.id, msg.from_user.username or "", msg.from_user.first_name or "")
    text = (
        f"Приветствуем в <b>AutoCommentClient!</b> \n\n"
        f"Используй /help чтобы начать пользование ботом."
    )
    await msg.answer(text, parse_mode="HTML")

@dp.message(Command("help"))
async def cmd_help(msg: Message):
    db_upsert_user(msg.from_user.id, msg.from_user.username or "", msg.from_user.first_name or "")
    text = (
        f"<b>AutoComment.exe</b> - программа для автоматизации комментирования в любой соц-сети. " +
        "Для работы вам понадобится сама программа (скачать -> @DepensierProgs) и ключ доступа.\n" +
        f"Приобрести ключ доступа - @depensier\n\n"
        f"Список доступных команд:\n\n"
        f"/start - начать работу с ботом\n"
        f"/profile - ваш профиль"
    )
    await msg.answer(text, parse_mode="HTML")

@dp.message(Command("/manual"))
async def cmd_manual(msg: Message):
    db_upsert_user(msg.from_user.id, msg.from_user.username or "", msg.from_user.first_name or "")
    text = (
        f"Полный актуальный мануал (инструкцию) для программы получите тут:\n"
        f"@AuCommManual"
    )
    await msg.answer(text, parse_mode="HTML")
# ─────────────────────────────────────────────────────────────────────────────
# /profile
# ─────────────────────────────────────────────────────────────────────────────
@dp.message(Command("profile"))
async def cmd_profile(msg: Message):
    db_upsert_user(msg.from_user.id, msg.from_user.username or "", msg.from_user.first_name or "")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Мои ключи", callback_data="my_keys")]
    ])
    text = (
        f"👤 <b>Профиль</b>\n\n"
        f"ID: <code>{msg.from_user.id}</code>\n"
        f"Имя: {msg.from_user.first_name}\n"
        f"Username: @{msg.from_user.username or 'нет'}"
    )
    await msg.answer(text, parse_mode="HTML", reply_markup=kb)

# ─────────────────────────────────────────────────────────────────────────────
# Callback: my_keys
# ─────────────────────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "my_keys")
async def cb_my_keys(call: CallbackQuery):
    keys = db_get_user_keys(call.from_user.id)
    now = int(datetime.now().timestamp())

    if not keys:
        await call.message.edit_text(
            "🔑 <b>Мои ключи</b>\n\nУ вас нет активных ключей.\nОбратитесь к администратору для покупки доступа.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="back_profile")]
            ])
        )
        return

    lines = ["🔑 <b>Мои ключи</b>\n"]
    for key, duration, issued_at, expires_at, activated in keys:
        remaining = expires_at - now
        status = "✅ Активирован" if activated else "⏳ Не активирован"
        lines.append(
            f"<code>{key}</code>\n"
            f"  Срок: {fmt_time(duration)}\n"
            f"  Осталось: {fmt_time(remaining)}\n"
            f"  Статус: {status}\n"
        )

    await call.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_profile")]
        ])
    )

@dp.callback_query(F.data == "back_profile")
async def cb_back_profile(call: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Мои ключи", callback_data="my_keys")]
    ])
    text = (
        f"👤 <b>Профиль</b>\n\n"
        f"ID: <code>{call.from_user.id}</code>\n"
        f"Имя: {call.from_user.first_name}\n"
        f"Username: @{call.from_user.username or 'нет'}"
    )
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

# ─────────────────────────────────────────────────────────────────────────────
# ADMIN: /addkey [user_id] [срок]
# Пример: /addkey 123456789 7d
# ─────────────────────────────────────────────────────────────────────────────
@dp.message(Command("addkey"))
async def cmd_addkey(msg: Message):
    if not is_admin(msg.from_user.id):
        await msg.answer("❌ Нет доступа.")
        return

    parts = msg.text.split()
    if len(parts) != 3:
        await msg.answer(
            "❌ Формат: /addkey [user_id] [срок]\n"
            "Сроки: 12h, 1d, 3d, 7d, 21d, 30d\n"
            "Пример: /addkey 123456789 7d"
        )
        return

    try:
        target_id = int(parts[1])
    except ValueError:
        await msg.answer("❌ Неверный user_id.")
        return

    dur_str = parts[2].lower()
    if dur_str not in DURATIONS:
        await msg.answer(f"❌ Неверный срок. Доступны: {', '.join(DURATIONS.keys())}")
        return

    duration = DURATIONS[dur_str]
    key = make_unique_key(target_id, duration)
    db_add_key(key, target_id, duration)

    # Уведомить пользователя
    try:
        await bot.send_message(
            target_id,
            f"🎉 Вам выдан ключ для <b>AutoComment v2.0</b>!\n\n"
            f"🔑 Ключ: <code>{key}</code>\n"
            f"⏱ Срок: <b>{DURATION_LABELS[dur_str]}</b>\n\n"
            f"Введите ключ в программу AutoComment для активации.\n"
            f"Ключ действует с момента выдачи.",
            parse_mode="HTML"
        )
        user_notified = "✅ Пользователь уведомлён"
    except Exception:
        user_notified = "⚠️ Не удалось уведомить пользователя (он не начал диалог с ботом)"

    await msg.answer(
        f"✅ Ключ выдан!\n\n"
        f"Пользователь: <code>{target_id}</code>\n"
        f"Ключ: <code>{key}</code>\n"
        f"Срок: {DURATION_LABELS[dur_str]}\n\n"
        f"{user_notified}",
        parse_mode="HTML"
    )

# ─────────────────────────────────────────────────────────────────────────────
# ADMIN: /keys — список всех активных ключей
# ─────────────────────────────────────────────────────────────────────────────
@dp.message(Command("keys"))
async def cmd_keys(msg: Message):
    if not is_admin(msg.from_user.id):
        await msg.answer("❌ Нет доступа.")
        return

    keys = db_get_all_keys()
    if not keys:
        await msg.answer("📋 Нет активных ключей.")
        return

    now = int(datetime.now().timestamp())
    lines = [f"📋 <b>Все активные ключи ({len(keys)}):</b>\n"]
    for key, user_id, duration, issued_at, expires_at, activated in keys:
        remaining = expires_at - now
        lines.append(
            f"<code>{key}</code>\n"
            f"  Юзер: <code>{user_id}</code> | Осталось: {fmt_time(remaining)} | {'✅' if activated else '⏳'}\n"
        )

    await msg.answer("\n".join(lines), parse_mode="HTML")

# ─────────────────────────────────────────────────────────────────────────────
# ADMIN: /revoke [key] — отозвать ключ
# ─────────────────────────────────────────────────────────────────────────────
@dp.message(Command("revoke"))
async def cmd_revoke(msg: Message):
    if not is_admin(msg.from_user.id):
        await msg.answer("❌ Нет доступа.")
        return

    parts = msg.text.split()
    if len(parts) != 2:
        await msg.answer("❌ Формат: /revoke [ключ]\nПример: /revoke abc12345_xyz1")
        return

    key = parts[1]
    if db_revoke_key(key):
        await msg.answer(f"✅ Ключ <code>{key}</code> отозван.", parse_mode="HTML")
    else:
        await msg.answer(f"❌ Ключ <code>{key}</code> не найден.", parse_mode="HTML")

# ─────────────────────────────────────────────────────────────────────────────
# ADMIN: /help — список команд
# ─────────────────────────────────────────────────────────────────────────────
@dp.message(Command("help"))
async def cmd_help(msg: Message):
    if not is_admin(msg.from_user.id):
        await msg.answer("Команды:\n/start — начало\n/profile — профиль и ключи")
        return

    await msg.answer(
        "🔧 <b>Команды администратора:</b>\n\n"
        "/addkey [user_id] [срок] — выдать ключ\n"
        "  Сроки: 12h, 1d, 3d, 7d, 21d, 30d\n\n"
        "/keys — все активные ключи\n"
        "/revoke [ключ] — отозвать ключ\n\n"
        "👤 <b>Команды пользователя:</b>\n\n"
        "/start — начало\n"
        "/profile — профиль и ключи",
        parse_mode="HTML"
    )

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
async def main():
    db_init()
    # Запускаем Flask в отдельном потоке
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    print("Bot started... Flask on :8080")
    await dp.start_polling(bot)

flask_app = Flask(__name__)

@flask_app.route("/validate")
def validate_key():
    key = request.args.get("key", "").strip()
    if not key:
        return jsonify({"status": "error", "message": "No key provided"}), 400

    now = int(datetime.now().timestamp())

    with db_connect() as con:
        cur = con.execute(
            "SELECT duration, issued_at, expires_at, activated FROM keys WHERE key=?",
            (key,)
        )
        row = cur.fetchone()

    if not row:
        return jsonify({"status": "error", "message": "Key not found"}), 404

    duration, issued_at, expires_at, activated = row

    if now >= expires_at:
        # Удаляем истёкший ключ
        with db_connect() as con:
            con.execute("DELETE FROM keys WHERE key=?", (key,))
            con.commit()
        return jsonify({"status": "error", "message": "Key expired"}), 403

    remaining = expires_at - now

    # Помечаем как активированный
    if not activated:
        with db_connect() as con:
            con.execute(
                "UPDATE keys SET activated=1, activated_at=? WHERE key=?",
                (now, key)
            )
            con.commit()

    return jsonify({
        "status": "OK",
        "remaining": remaining,
        "expires_at": expires_at
    })

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    asyncio.run(main())
