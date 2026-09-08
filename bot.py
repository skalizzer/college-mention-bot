import asyncio
import logging
import os
import sqlite3
from contextlib import closing

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, BotCommand, BotCommandScopeAllGroupChats
from aiogram.enums import ChatType, ParseMode, ChatMemberStatus
from aiogram.client.default import DefaultBotProperties

# ==== НАСТРОЙКИ ====
# Токен берётся из переменной окружения BOT_TOKEN (задаётся в панели хостинга).
# Так токен не попадает в код и в Git.
BOT_TOKEN = os.environ["BOT_TOKEN"]
DB_PATH = "users.db"

MENTIONS_PER_MESSAGE = 5      # не более 5 упоминаний в одном сообщении
DELETE_AFTER_SECONDS = 15     # через сколько секунд удалять сообщения с упоминаниями
DELAY_BETWEEN_MESSAGES = 0.5  # пауза между отправкой пачек (защита от флуд-лимитов)

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Список команд для меню "/" в Telegram.
BOT_COMMANDS = [
    BotCommand(command="all", description="📣 Упомянуть всех известных участников"),
    BotCommand(command="add", description="➕ Добавить человека по @username"),
    BotCommand(command="root", description="👑 Дать участнику доступ к командам бота"),
    BotCommand(command="unroot", description="🔻 Забрать доступ к командам бота"),
    BotCommand(command="help", description="❓ Список команд и как ими пользоваться"),
]


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
        # Обычные участники, которым админ выдал доступ к /all и /add через
        # команду /root. Храним по username (без @, в нижнем регистре) —
        # так же, как /add хранит людей, которые ещё не писали в чат.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trusted_users (
                chat_id INTEGER,
                username TEXT,
                PRIMARY KEY (chat_id, username)
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
    он что-либо писал в чат. Используется командой /add.
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


def grant_trust(chat_id: int, username: str) -> bool:
    """Выдаёт username-у доступ к /all и /add в этом чате.
    Возвращает True, если добавлено впервые, False — если уже было."""
    username = username.lstrip("@").lower()
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "SELECT 1 FROM trusted_users WHERE chat_id = ? AND username = ?",
            (chat_id, username)
        )
        if cur.fetchone():
            return False
        conn.execute(
            "INSERT INTO trusted_users (chat_id, username) VALUES (?, ?)",
            (chat_id, username)
        )
        conn.commit()
        return True


def revoke_trust(chat_id: int, username: str) -> bool:
    """Забирает у username-а доступ к /all и /add в этом чате.
    Возвращает True, если запись была и удалена, False — если её не было."""
    username = username.lstrip("@").lower()
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "DELETE FROM trusted_users WHERE chat_id = ? AND username = ?",
            (chat_id, username)
        )
        conn.commit()
        return cur.rowcount > 0


def is_trusted(chat_id: int, username: str | None) -> bool:
    """Проверяет, выдавали ли этому username-у доступ через /root."""
    if not username:
        return False
    username = username.lstrip("@").lower()
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "SELECT 1 FROM trusted_users WHERE chat_id = ? AND username = ?",
            (chat_id, username)
        )
        return cur.fetchone() is not None


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


# ---------- ПРОВЕРКА ПРАВ АДМИНИСТРАТОРА ----------

ADMIN_STATUSES = (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)


async def is_admin(chat_id: int, user_id: int) -> bool:
    """Проверяет, является ли пользователь админом/создателем чата.
    Работает в группах и супергруппах через getChatMember."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ADMIN_STATUSES
    except Exception as e:
        logging.warning(f"Не удалось проверить права пользователя {user_id} в чате {chat_id}: {e}")
        return False


async def require_admin(message: Message) -> bool:
    """Строгая проверка — только для команд, которые должны оставаться
    исключительно в руках админов (например, выдача/отзыв доступа через
    /root и /unroot). Если прав нет — отвечает пользователю и возвращает False."""
    if not message.from_user:
        return False
    if not await is_admin(message.chat.id, message.from_user.id):
        await message.answer("🚫 Эта команда доступна только администраторам чата.")
        return False
    return True


async def require_access(message: Message) -> bool:
    """Проверка для команд /all и /add: пропускает админов чата, а также
    обычных участников, которым доступ выдали через /root.
    Если доступа нет — отвечает пользователю и возвращает False."""
    if not message.from_user:
        return False
    if await is_admin(message.chat.id, message.from_user.id):
        return True
    if is_trusted(message.chat.id, message.from_user.username):
        return True
    await message.answer(
        "🚫 Эта команда доступна только админам или участникам, которым "
        "выдан доступ через /root."
    )
    return False


# ---------- MIDDLEWARE: ОТСЛЕЖИВАНИЕ ПОЛЬЗОВАТЕЛЕЙ ----------
# Раньше это было сделано отдельным хендлером-"перехватчиком", который
# ловил вообще любое сообщение и не давал командам (/all, /add) доходить
# до своих настоящих обработчиков. Через middleware отслеживание происходит
# "по пути" и не мешает дальнейшей обработке команд.

class UserTrackerMiddleware:
    async def __call__(self, handler, message: Message, data: dict):
        if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            if message.from_user and not message.from_user.is_bot:
                save_user(
                    chat_id=message.chat.id,
                    user_id=message.from_user.id,
                    username=message.from_user.username,
                    full_name=message.from_user.full_name,
                )
        return await handler(message, data)


dp.message.middleware(UserTrackerMiddleware())


# ---------- ХЕНДЛЕРЫ ----------

@dp.message(Command("help", "старт", "start"))
async def handle_help(message: Message):
    """Справка по всем командам бота. Доступна всем — это просто список
    команд, а не действие над чатом."""
    text = (
        "🤖 <b>Команды бота</b>\n\n"
        "📣 <b>/all</b> (он же /everyone, /упомянуть)\n"
        "Упомянуть всех известных боту участников чата.\n"
        "🔒 Админы и участники с доступом через /root.\n\n"
        "➕ <b>/add</b> @username1 @username2 ...\n"
        "Добавить человека в список упоминаний по username, без того чтобы "
        "он сам писал в чат.\n"
        "🔒 Админы и участники с доступом через /root.\n\n"
        "👑 <b>/root</b> @username1 @username2 ...\n"
        "Дать обычному участнику доступ к /all и /add, без выдачи ему прав "
        "администратора в Telegram.\n"
        "🔒 Только для админов.\n\n"
        "🔻 <b>/unroot</b> @username1 @username2 ...\n"
        "Забрать ранее выданный через /root доступ.\n"
        "🔒 Только для админов.\n\n"
        "❓ <b>/help</b>\n"
        "Показать это сообщение."
    )
    await message.answer(text)


@dp.message(Command("add"))
async def handle_add_mention(message: Message):
    """Ручное добавление человека в список упоминаний по username, без того
    чтобы он сам писал в чат. Использование:
        /add @ivan_petrov @anna_k @sidorov
    Можно перечислить сразу несколько username через пробел."""
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    if not await require_access(message):
        return

    parts = message.text.split()[1:]  # всё, кроме самой команды
    usernames = [p for p in parts if p.startswith("@") and len(p) > 1]

    if not usernames:
        await message.answer(
            "✏️ Укажите username через пробел, например:\n"
            "<code>/add @ivan_petrov @anna_k</code>"
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
        reply_lines.append("✅ Добавлены: " + ", ".join(added))
    if already_have:
        reply_lines.append("ℹ️ Уже были в списке: " + ", ".join(already_have))
    await message.answer("\n".join(reply_lines))


@dp.message(Command("root"))
async def handle_grant_root(message: Message):
    """Выдаёт обычным участникам доступ к /all и /add, без прав админа
    Telegram. Использование:
        /root @ivan_petrov @anna_k
    Вызывать может только админ/создатель чата — сама эта команда правами
    require_admin, а не require_access, чтобы обычные участники не могли
    выдавать доступ друг другу."""
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    if not await require_admin(message):
        return

    parts = message.text.split()[1:]
    usernames = [p for p in parts if p.startswith("@") and len(p) > 1]

    if not usernames:
        await message.answer(
            "✏️ Укажите username через пробел, например:\n"
            "<code>/root @ivan_petrov @anna_k</code>"
        )
        return

    granted, already_have = [], []
    for uname in usernames:
        if grant_trust(message.chat.id, uname):
            granted.append(uname)
        else:
            already_have.append(uname)

    reply_lines = []
    if granted:
        reply_lines.append("👑 Доступ к /all и /add выдан: " + ", ".join(granted))
    if already_have:
        reply_lines.append("ℹ️ Уже имели доступ: " + ", ".join(already_have))
    await message.answer("\n".join(reply_lines))


@dp.message(Command("unroot"))
async def handle_revoke_root(message: Message):
    """Забирает у обычных участников доступ, ранее выданный через /root.
    Использование:
        /unroot @ivan_petrov @anna_k
    Только для админов/создателя чата."""
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    if not await require_admin(message):
        return

    parts = message.text.split()[1:]
    usernames = [p for p in parts if p.startswith("@") and len(p) > 1]

    if not usernames:
        await message.answer(
            "✏️ Укажите username через пробел, например:\n"
            "<code>/unroot @ivan_petrov @anna_k</code>"
        )
        return

    revoked, not_found = [], []
    for uname in usernames:
        if revoke_trust(message.chat.id, uname):
            revoked.append(uname)
        else:
            not_found.append(uname)

    reply_lines = []
    if revoked:
        reply_lines.append("🔻 Доступ забран у: " + ", ".join(revoked))
    if not_found:
        reply_lines.append("ℹ️ У них и так не было доступа: " + ", ".join(not_found))
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
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.answer("⚠️ Эта команда работает только в группах.")
        return

    if not await require_access(message):
        return

    thread_id = message.message_thread_id  # тема, в которой вызвали команду

    users = get_users(message.chat.id)
    if not users:
        await message.answer("🤷 Пока никого не знаю. Пусть люди сначала что-нибудь напишут в чате.")
        return

    # Убираем автора команды из списка, если хотите не упоминать самого себя — раскомментируйте:
    # users = [u for u in users if u[0] != message.from_user.id]

    sent_message_ids = []

    for chunk in chunk_list(users, MENTIONS_PER_MESSAGE):
        mentions = [make_mention(uid, uname, fname) for uid, uname, fname in chunk]
        text = "📣 " + " ".join(mentions)

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
    # Регистрируем команды, чтобы они показывались в меню "/" в группах.
    await bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeAllGroupChats())
    # allowed_updates перечисляем явно: по умолчанию aiogram не запрашивает
    # chat_member-события (вход/выход участников), их нужно включить отдельно.
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
