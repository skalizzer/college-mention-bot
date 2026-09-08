import asyncio
import logging
import os
import sqlite3
from contextlib import closing

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.enums import ChatType, ParseMode
from aiogram.client.default import DefaultBotProperties

# ==== НАСТРОЙКИ ====
# Токен берётся из переменной окружения BOT_TOKEN (задаётся в панели хостинга,
# см. поле "Bot Token" на Amvera). Так токен не попадает в код и в Git.
BOT_TOKEN = os.environ["BOT_TOKEN"]
DB_PATH = "users.db"

MENTIONS_PER_MESSAGE = 5      # не более 5 упоминаний в одном сообщении
DELETE_AFTER_SECONDS = 15     # через сколько секунд удалять сообщения с упоминаниями
DELAY_BETWEEN_MESSAGES = 0.5  # пауза между отправкой пачек (защита от флуд-лимитов)

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


# ---------- РАБОТА С БАЗОЙ ДАННЫХ ----------

def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER,
                thread_id INTEGER,
                user_id INTEGER,
                username TEXT,
                full_name TEXT,
                PRIMARY KEY (chat_id, user_id)
            )
        """)
        conn.commit()


def save_user(chat_id: int, user_id: int, username: str | None, full_name: str):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("""
            INSERT INTO users (chat_id, user_id, username, full_name, thread_id)
            VALUES (?, ?, ?, ?, 0)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                username=excluded.username,
                full_name=excluded.full_name
        """, (chat_id, user_id, username, full_name))
        conn.commit()


def get_users(chat_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "SELECT user_id, username, full_name FROM users WHERE chat_id = ?",
            (chat_id,)
        )
        return cur.fetchall()


def remove_user(chat_id: int, user_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "DELETE FROM users WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id)
        )
        conn.commit()


def save_username_only(chat_id: int, username: str):
    """Добавляет человека в базу по username, без user_id и без того, чтобы
    он что-либо писал в чат. Используется командой /addmention.
    Настоящий user_id неизвестен — упоминание будет идти просто через
    @username (см. make_mention), это работает всегда, в отличие от
    ссылок tg://user?id=, которым нужен реальный, «увиденный» ботом id."""
    username = username.lstrip("@").lower()
    with closing(sqlite3.connect(DB_PATH)) as conn:
        # Проверяем — вдруг такой username уже есть в базе с реальным user_id
        # (человек уже писал в чат) — тогда трогать не нужно.
        cur = conn.execute(
            "SELECT user_id FROM users WHERE chat_id = ? AND LOWER(username) = ?",
            (chat_id, username)
        )
        if cur.fetchone():
            return False  # уже есть

        # Генерируем отрицательный "псевдо-id" на основе username, чтобы не
        # конфликтовать с настоящими user_id (они всегда положительные) и
        # чтобы можно было хранить несколько username-заглушек.
        pseudo_id = -abs(hash(username)) % (10 ** 9)
        conn.execute("""
            INSERT INTO users (chat_id, user_id, username, full_name, thread_id)
            VALUES (?, ?, ?, ?, 0)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                username=excluded.username
        """, (chat_id, pseudo_id, username, username))
        conn.commit()
        return True


# ---------- ЛОГИКА УПОМИНАНИЙ ----------

def make_mention(user_id: int, username: str | None, full_name: str) -> str:
    """Формирует HTML-упоминание. Если есть username — просто @username,
    иначе кликабельная ссылка-упоминание по user_id (работает даже без username)."""
    if username:
        return f"@{username}"
    safe_name = full_name.replace("<", "").replace(">", "") or "Участник"
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'


def chunk_list(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


async def delete_later(chat_id: int, message_id: int, delay: int):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logging.warning(f"Не удалось удалить сообщение {message_id}: {e}")


# ---------- ХЕНДЛЕРЫ ----------

@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def track_users(message: Message):
    """Запоминаем каждого написавшего — иначе бот физически не может
    узнать список участников чата (ограничение Telegram Bot API)."""
    if message.from_user and not message.from_user.is_bot:
        save_user(
            chat_id=message.chat.id,
            user_id=message.from_user.id,
            username=message.from_user.username,
            full_name=message.from_user.full_name,
        )

    # Команда должна обрабатываться и здесь, если не хотите отдельный хендлер —
    # но ниже сделан отдельный хендлер с приоритетом через Command()
    await handle_mention_all(message)


@dp.message(Command("addmention"))
async def handle_add_mention(message: Message):
    """Ручное добавление человека в список упоминаний по username, без того
    чтобы он сам писал в чат. Использование:
        /addmention @ivan_petrov @anna_k @sidorov
    Можно перечислить сразу несколько username через пробел."""
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    parts = message.text.split()[1:]  # всё, кроме самой команды
    usernames = [p for p in parts if p.startswith("@") and len(p) > 1]

    if not usernames:
        await message.answer(
            "Укажите username через пробел, например:\n"
            "/addmention @ivan_petrov @anna_k"
        )
        return

    added, already_have = [], []
    for uname in usernames:
        if save_username_only(message.chat.id, uname):
            added.append(uname)
        else:
            already_have.append(uname)

    reply_lines = []
    if added:
        reply_lines.append("Добавлены: " + ", ".join(added))
    if already_have:
        reply_lines.append("Уже были в списке: " + ", ".join(already_have))
    await message.answer("\n".join(reply_lines))


@dp.chat_member()
async def on_member_left(update):
    """Когда участник покидает чат (сам вышел или был исключён), убираем
    его из базы, чтобы бот больше не пытался его упоминать.
    Для получения этих событий боту нужны права администратора в группе."""
    new_status = update.new_chat_member.status
    if new_status in ("left", "kicked"):
        remove_user(update.chat.id, update.new_chat_member.user.id)
        logging.info(
            f"Пользователь {update.new_chat_member.user.id} убран из базы "
            f"(вышел/исключён из чата {update.chat.id})"
        )


@dp.message(Command("all", "everyone", "упомянуть"))
async def handle_mention_all(message: Message):
    if message.text is None or not message.text.startswith(("/all", "/everyone", "/упомянуть")):
        return  # это вызов из track_users не по команде — просто выходим

    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.answer("Эта команда работает только в группах.")
        return

    thread_id = message.message_thread_id  # тема, в которой вызвали команду

    users = get_users(message.chat.id)
    if not users:
        await message.answer("Пока никого не знаю. Пусть люди сначала что-нибудь напишут в чате.")
        return

    # Убираем автора команды из списка, если хотите не упоминать самого себя — раскомментируйте:
    # users = [u for u in users if u[0] != message.from_user.id]

    sent_message_ids = []

    for chunk in chunk_list(users, MENTIONS_PER_MESSAGE):
        mentions = [make_mention(uid, uname, fname) for uid, uname, fname in chunk]
        text = " ".join(mentions)

        try:
            sent = await bot.send_message(
                chat_id=message.chat.id,
                text=text,
                message_thread_id=thread_id,
            )
            sent_message_ids.append(sent.message_id)
        except Exception as e:
            logging.error(f"Ошибка отправки: {e}")

        await asyncio.sleep(DELAY_BETWEEN_MESSAGES)

    # удаляем исходную команду тоже (по желанию)
    try:
        await message.delete()
    except Exception:
        pass

    # запускаем отложенное удаление всех сообщений с упоминаниями
    for msg_id in sent_message_ids:
        asyncio.create_task(delete_later(message.chat.id, msg_id, DELETE_AFTER_SECONDS))


async def main():
    init_db()
    # allowed_updates перечисляем явно: по умолчанию aiogram не запрашивает
    # chat_member-события (вход/выход участников), их нужно включить отдельно.
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
