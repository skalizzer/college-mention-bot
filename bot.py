import asyncio
import hashlib
import logging
import os
import sqlite3
from contextlib import closing

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    BotCommand,
    BotCommandScopeAllGroupChats,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
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
        # команду /root. Структура зеркалит таблицу users: если человека
        # выбрали из списка (кнопками) — сохраняется его настоящий user_id;
        # если ввели @username вручную для человека, который ещё не писал
        # в чат — используется стабильный псевдо-id (см. _pseudo_id).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trusted_users (
                chat_id INTEGER,
                user_id INTEGER,
                username TEXT,
                full_name TEXT,
                PRIMARY KEY (chat_id, user_id)
            )
        """)
        # Миграция со старой схемы (chat_id, username) без user_id, если
        # бот уже работал с предыдущей версией этого файла.
        existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(trusted_users)")}
        if "user_id" not in existing_columns:
            conn.execute("ALTER TABLE trusted_users RENAME TO trusted_users_old")
            conn.execute("""
                CREATE TABLE trusted_users (
                    chat_id INTEGER,
                    user_id INTEGER,
                    username TEXT,
                    full_name TEXT,
                    PRIMARY KEY (chat_id, user_id)
                )
            """)
            for chat_id, username in conn.execute("SELECT chat_id, username FROM trusted_users_old"):
                pseudo_id = _pseudo_id(username)
                conn.execute("""
                    INSERT OR IGNORE INTO trusted_users (chat_id, user_id, username, full_name)
                    VALUES (?, ?, ?, ?)
                """, (chat_id, pseudo_id, username, username))
            conn.execute("DROP TABLE trusted_users_old")
        conn.commit()


def _pseudo_id(username: str) -> int:
    """Стабильный отрицательный «псевдо-id» на основе username — не зависит
    от перезапуска процесса (в отличие от встроенного hash(), который
    рандомизируется между запусками Python). Используется для людей,
    добавленных вручную по username (через /add или /root), у которых
    бот ещё не знает настоящий user_id."""
    digest = hashlib.sha256(username.encode("utf-8")).hexdigest()
    return -(int(digest[:12], 16) % (10 ** 9) + 1)


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

        # Стабильный отрицательный "псевдо-id" на основе username, чтобы не
        # конфликтовать с настоящими user_id (они всегда положительные) и
        # чтобы можно было хранить несколько username-заглушек.
        pseudo_id = _pseudo_id(username)
        conn.execute("""
            INSERT INTO users (chat_id, user_id, username, full_name, thread_id)
            VALUES (?, ?, ?, ?, 0)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                username=excluded.username
        """, (chat_id, pseudo_id, username, username))
        conn.commit()
        return True


def grant_trust_by_username(chat_id: int, username: str) -> bool:
    """Выдаёт доступ к /all и /add человеку по username вручную (для тех,
    кто ещё не писал в чат — используется псевдо-id, как в /add).
    Возвращает True, если добавлено впервые, False — если уже было."""
    username = username.lstrip("@").lower()
    with closing(sqlite3.connect(DB_PATH)) as conn:
        # Если человек уже есть в базе (реальный user_id, писал в чат) —
        # выдаём доступ на реальный id, а не на псевдо-id.
        cur = conn.execute(
            "SELECT user_id, username, full_name FROM users WHERE chat_id = ? AND LOWER(username) = ?",
            (chat_id, username)
        )
        row = cur.fetchone()
        if row:
            user_id, real_username, full_name = row
        else:
            user_id, real_username, full_name = _pseudo_id(username), username, username

        cur = conn.execute(
            "SELECT 1 FROM trusted_users WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id)
        )
        if cur.fetchone():
            return False

        conn.execute("""
            INSERT INTO trusted_users (chat_id, user_id, username, full_name)
            VALUES (?, ?, ?, ?)
        """, (chat_id, user_id, real_username, full_name))
        conn.commit()
        return True


def grant_trust_by_user_id(chat_id: int, user_id: int, username: str | None, full_name: str) -> bool:
    """Выдаёт доступ конкретному, уже известному боту участнику (выбранному
    из списка кнопками). Возвращает True, если добавлено впервые."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "SELECT 1 FROM trusted_users WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id)
        )
        if cur.fetchone():
            return False
        conn.execute("""
            INSERT INTO trusted_users (chat_id, user_id, username, full_name)
            VALUES (?, ?, ?, ?)
        """, (chat_id, user_id, username, full_name))
        conn.commit()
        return True


def revoke_trust_by_username(chat_id: int, username: str) -> bool:
    """Забирает доступ у человека по username. Возвращает True, если запись
    была и удалена, False — если её не было."""
    username = username.lstrip("@").lower()
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "DELETE FROM trusted_users WHERE chat_id = ? AND LOWER(username) = ?",
            (chat_id, username)
        )
        conn.commit()
        return cur.rowcount > 0


def revoke_trust_by_user_id(chat_id: int, user_id: int) -> bool:
    """Забирает доступ у конкретного user_id. Возвращает True, если запись
    была и удалена."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "DELETE FROM trusted_users WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id)
        )
        conn.commit()
        return cur.rowcount > 0


def is_trusted(chat_id: int, user_id: int, username: str | None) -> bool:
    """Проверяет, выдавали ли этому человеку доступ через /root — по
    настоящему user_id или (для тех, кого добавили по username до того,
    как они написали в чат) по username."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "SELECT 1 FROM trusted_users WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id)
        )
        if cur.fetchone():
            return True
        if username:
            cur = conn.execute(
                "SELECT 1 FROM trusted_users WHERE chat_id = ? AND LOWER(username) = ?",
                (chat_id, username.lower())
            )
            if cur.fetchone():
                return True
        return False


def get_trusted(chat_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "SELECT user_id, username, full_name FROM trusted_users WHERE chat_id = ?",
            (chat_id,)
        )
        return cur.fetchall()


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
    if is_trusted(message.chat.id, message.from_user.id, message.from_user.username):
        return True
    await message.answer(
        "🚫 Эта команда доступна только админам или участникам, которым "
        "выдан доступ через /root."
    )
    return False


# ---------- ВЫБОР ЛЮДЕЙ КНОПКАМИ (/root и /unroot без аргументов) ----------
# Когда команду нажимают из меню "/", Telegram отправляет её сразу же, без
# аргументов — ввести @username до отправки невозможно, так устроен сам
# Telegram (это не ограничение бота). Поэтому если /root или /unroot пришли
# без аргументов, бот вместо текстовой подсказки показывает список людей с
# кнопками: можно отметить одного или нескольких, потом нажать "Готово".
#
# Состояние выбора хранится в памяти процесса, привязано к конкретному
# сообщению с кнопками (chat_id, message_id) и живёт до перезапуска бота —
# этого достаточно, так как выбор обычно завершается за несколько секунд.

PENDING_SELECTIONS: dict[tuple[int, int], dict] = {}


def _display_name(username: str | None, full_name: str | None) -> str:
    if username:
        return f"@{username}"
    return full_name or "Без имени"


def _build_picker_keyboard(candidates: list[tuple[int, str | None, str | None]], selected: set[int]) -> InlineKeyboardMarkup:
    rows = []
    for user_id, username, full_name in candidates:
        mark = "✅ " if user_id in selected else "☐ "
        rows.append([InlineKeyboardButton(
            text=mark + _display_name(username, full_name),
            callback_data=f"pt:{user_id}",
        )])
    rows.append([
        InlineKeyboardButton(text="✅ Готово", callback_data="pd"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="pc"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def start_picker(message: Message, action: str, candidates: list[tuple[int, str | None, str | None]], empty_text: str, prompt_text: str):
    """Показывает список кандидатов с кнопками-чекбоксами.
    action — 'root' (выдать доступ) или 'unroot' (забрать доступ)."""
    if not candidates:
        await message.answer(empty_text)
        return

    sent = await message.answer(
        prompt_text,
        reply_markup=_build_picker_keyboard(candidates, selected=set()),
    )
    PENDING_SELECTIONS[(sent.chat.id, sent.message_id)] = {
        "action": action,
        "admin_id": message.from_user.id,
        "candidates": {uid: (uname, fname) for uid, uname, fname in candidates},
        "selected": set(),
    }


@dp.callback_query(F.data.startswith("pt:"))
async def handle_picker_toggle(callback: CallbackQuery):
    key = (callback.message.chat.id, callback.message.message_id)
    state = PENDING_SELECTIONS.get(key)
    if not state:
        await callback.answer("Список устарел, вызовите команду заново.", show_alert=True)
        return
    if callback.from_user.id != state["admin_id"]:
        await callback.answer("Выбирать может только тот, кто вызвал команду.", show_alert=True)
        return

    user_id = int(callback.data.split(":", 1)[1])
    if user_id in state["selected"]:
        state["selected"].discard(user_id)
    else:
        state["selected"].add(user_id)

    candidates = [(uid, uname, fname) for uid, (uname, fname) in state["candidates"].items()]
    await callback.message.edit_reply_markup(
        reply_markup=_build_picker_keyboard(candidates, state["selected"])
    )
    await callback.answer()


@dp.callback_query(F.data == "pc")
async def handle_picker_cancel(callback: CallbackQuery):
    key = (callback.message.chat.id, callback.message.message_id)
    state = PENDING_SELECTIONS.pop(key, None)
    if state and callback.from_user.id != state["admin_id"]:
        PENDING_SELECTIONS[key] = state
        await callback.answer("Отменить может только тот, кто вызвал команду.", show_alert=True)
        return
    await callback.message.edit_text("❌ Отменено.")
    await callback.answer()


@dp.callback_query(F.data == "pd")
async def handle_picker_done(callback: CallbackQuery):
    key = (callback.message.chat.id, callback.message.message_id)
    state = PENDING_SELECTIONS.get(key)
    if not state:
        await callback.answer("Список устарел, вызовите команду заново.", show_alert=True)
        return
    if callback.from_user.id != state["admin_id"]:
        await callback.answer("Подтвердить может только тот, кто вызвал команду.", show_alert=True)
        return

    # На всякий случай перепроверяем права — вдруг за время выбора человек
    # перестал быть админом.
    if not await is_admin(callback.message.chat.id, state["admin_id"]):
        await callback.message.edit_text("🚫 Права администратора больше не подтверждены, отменено.")
        del PENDING_SELECTIONS[key]
        await callback.answer()
        return

    chat_id = callback.message.chat.id
    selected_ids = state["selected"]
    names = []

    if not selected_ids:
        await callback.answer("Никто не выбран.", show_alert=True)
        return

    for user_id in selected_ids:
        username, full_name = state["candidates"][user_id]
        if state["action"] == "root":
            grant_trust_by_user_id(chat_id, user_id, username, full_name)
        else:
            revoke_trust_by_user_id(chat_id, user_id)
        names.append(_display_name(username, full_name))

    verb = "Доступ к /all и /add выдан" if state["action"] == "root" else "Доступ забран у"
    icon = "👑" if state["action"] == "root" else "🔻"
    await callback.message.edit_text(f"{icon} {verb}: " + ", ".join(names))

    del PENDING_SELECTIONS[key]
    await callback.answer()


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
        "он сам писал в чат. Только вручную по username — бот ещё не знает "
        "об этом человеке, выбрать его из списка нельзя.\n"
        "🔒 Админы и участники с доступом через /root.\n\n"
        "👑 <b>/root</b> [@username ...]\n"
        "Дать обычному участнику доступ к /all и /add, без выдачи ему прав "
        "администратора в Telegram. Без аргументов — покажет список "
        "известных участников с кнопками для выбора.\n"
        "🔒 Только для админов.\n\n"
        "🔻 <b>/unroot</b> [@username ...]\n"
        "Забрать ранее выданный через /root доступ. Без аргументов — "
        "покажет список тех, у кого есть доступ, с кнопками для выбора.\n"
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
            "✏️ /add — для людей, которых бот ещё не видел в чате, поэтому "
            "выбрать их из списка нельзя (бот о них ничего не знает, кроме "
            "введённого username).\n"
            "Укажите username через пробел, например:\n"
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
    Telegram. Два способа использования:
        /root @ivan_petrov @anna_k   — вручную по username
        /root                         — покажет список известных боту
                                         участников с кнопками для выбора
    Вызывать может только админ/создатель чата — сама эта команда защищена
    require_admin, а не require_access, чтобы обычные участники не могли
    выдавать доступ друг другу."""
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    if not await require_admin(message):
        return

    parts = message.text.split()[1:]
    usernames = [p for p in parts if p.startswith("@") and len(p) > 1]

    if not usernames:
        # Команда вызвана без аргументов (например, тапом из меню "/") —
        # показываем список уже известных боту участников с кнопками.
        known = get_users(message.chat.id)
        trusted_ids = {uid for uid, _, _ in get_trusted(message.chat.id)}
        candidates = [(uid, uname, fname) for uid, uname, fname in known if uid not in trusted_ids]
        await start_picker(
            message,
            action="root",
            candidates=candidates,
            empty_text=(
                "🤷 Пока некому выдавать доступ: либо бот ещё никого не знает, "
                "либо доступ уже есть у всех известных участников.\n"
                "Если нужный человек ещё не писал в чат — добавьте его вручную:\n"
                "<code>/root @username</code>"
            ),
            prompt_text="👑 Отметьте, кому выдать доступ к /all и /add, затем нажмите «Готово»:",
        )
        return

    granted, already_have = [], []
    for uname in usernames:
        if grant_trust_by_username(message.chat.id, uname):
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
    Два способа использования:
        /unroot @ivan_petrov @anna_k — вручную по username
        /unroot                       — покажет список тех, у кого сейчас
                                         есть доступ, с кнопками для выбора
    Только для админов/создателя чата."""
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    if not await require_admin(message):
        return

    parts = message.text.split()[1:]
    usernames = [p for p in parts if p.startswith("@") and len(p) > 1]

    if not usernames:
        candidates = get_trusted(message.chat.id)
        await start_picker(
            message,
            action="unroot",
            candidates=candidates,
            empty_text="🤷 Пока ни у кого нет доступа, выданного через /root.",
            prompt_text="🔻 Отметьте, у кого забрать доступ, затем нажмите «Готово»:",
        )
        return

    revoked, not_found = [], []
    for uname in usernames:
        if revoke_trust_by_username(message.chat.id, uname):
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
