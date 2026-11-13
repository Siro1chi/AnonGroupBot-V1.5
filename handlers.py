"""Базовые хендлеры: меню, гримерка, комнаты, анонимный чат, ролевое форматирование, шёпот."""
from __future__ import annotations

import html
import re
from typing import Optional

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Message,
)

import storage
from storage import (
    rooms, user_room, global_names,
    get_identity, format_nick, parse_nick_arg, is_nick_allowed,
    has_global_name, find_room_by_name, make_room, new_room_id,
    verify_password, hash_password,
)

router = Router(name="handlers")

MAX_NAME_LEN = 30
MAX_ROOM_NAME_LEN = 40

# только в памяти
grimer_mode: dict[int, Optional[str]] = {}        # "local" | "global" | None
pending_room_type: dict[int, str] = {}            # room_type если создание отложено


# ---------- FSM ----------

class CreateRoomFSM(StatesGroup):
    waiting_password = State()
    waiting_name = State()


# ---------- Хелперы ----------

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🏠 Regular", callback_data="create:regular"),
            InlineKeyboardButton(text="🎭 Roleplay", callback_data="create:roleplay"),
            InlineKeyboardButton(text="🐉 DND", callback_data="create:dnd"),
        ],
        [
            InlineKeyboardButton(text="📋 Список", callback_data="list"),
            InlineKeyboardButton(text="✏️ Имя", callback_data="setname_help"),
        ],
    ])


async def _notify_room(bot, room: dict, text: str, exclude: Optional[int] = None):
    for uid in room["members"]:
        if uid == exclude:
            continue
        try:
            await bot.send_message(uid, text)
        except Exception:
            pass


def _ctx(user_id: int) -> Optional[tuple[str, dict]]:
    rid = user_room.get(user_id)
    if not rid:
        return None
    room = rooms.get(rid)
    if not room or user_id not in room["members"]:
        return None
    return rid, room


def _is_gm(user_id: int, room: dict) -> bool:
    return room.get("created_by") == user_id


# ---------- /start, /menu ----------

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 <b>AnonGroupBot</b>\n\n"
        "Telegram-личность скрыта всегда. Виден только ник и символ.\n\n"
        "Выберите действие:",
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Главное меню:", reply_markup=main_menu_kb())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    uid = message.from_user.id
    await state.clear()
    grimer_mode.pop(uid, None)
    pending_room_type.pop(uid, None)
    await message.answer("Отменено.", reply_markup=main_menu_kb())


@router.callback_query(F.data == "list")
async def cb_list(callback: CallbackQuery):
    await callback.answer()
    if not rooms:
        await callback.message.edit_text("Нет комнат. Создайте первую!", reply_markup=main_menu_kb())
        return
    lines = []
    for r in rooms.values():
        lock = "🔒" if r["password_hash"] else "🔓"
        icon = {"regular": "🏠", "roleplay": "🎭", "dnd": "🐉"}[r["type"]]
        lines.append(f"{icon} {lock} «{r['name']}» — {len(r['members'])} уч.")
    await callback.message.edit_text(
        "📋 <b>Комнаты:</b>\n\n" + "\n".join(lines) + "\n\nВойти: /join &lt;название&gt;",
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data == "setname_help")
async def cb_setname_help(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "Задать имя:\n"
        "/setname — войти в глобальную гримерку\n"
        "/nick ВашеИмя\n"
        "/symbol 🔥 (опционально)\n"
        "/setname — выйти\n\n"
        "Внутри конкретной комнаты — /look."
    )


# ---------- Создание комнаты ----------

@router.callback_query(F.data.startswith("create:"))
async def cb_create(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    room_type = callback.data.split(":", 1)[1]
    await callback.answer()

    if user_room.get(uid):
        room = rooms.get(user_room[uid])
        name = room["name"] if room else "?"
        await callback.message.answer(f"❌ Вы уже в комнате «{name}». Сначала /leave.")
        return

    if not has_global_name(uid):
        pending_room_type[uid] = room_type
        await callback.message.answer(
            "❌ У вас нет имени.\n\n"
            "Задайте: /setname → /nick ВашеИмя → /setname.\n"
            "После этого создание продолжится автоматически."
        )
        return

    await state.set_state(CreateRoomFSM.waiting_password)
    await state.update_data(room_type=room_type)
    await callback.message.answer("Введите пароль комнаты (или «нет» для без пароля):")


@router.message(CreateRoomFSM.waiting_password)
async def create_password(message: Message, state: FSMContext):
    raw = message.text.strip()
    password = None if raw.lower() == "нет" else raw
    if password and len(password) > 50:
        await message.answer("❌ Пароль слишком длинный (макс 50)")
        return
    await state.update_data(password=password)
    await state.set_state(CreateRoomFSM.waiting_name)
    await message.answer("Введите название комнаты:")


@router.message(CreateRoomFSM.waiting_name)
async def create_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("Название не может быть пустым")
        return
    if len(name) > MAX_ROOM_NAME_LEN:
        await message.answer(f"❌ Максимум {MAX_ROOM_NAME_LEN} символов")
        return
    if find_room_by_name(name):
        await message.answer(f"❌ Комната «{name}» уже существует")
        return

    data = await state.get_data()
    room_type = data["room_type"]
    password = data.get("password")
    uid = message.from_user.id

    rid = new_room_id()
    room = make_room(room_type, name, password, uid)
    room["members"].append(uid)
    rooms[rid] = room
    user_room[uid] = rid
    await state.clear()

    label = {"regular": "Regular", "roleplay": "Roleplay", "dnd": "DND"}[room_type]
    await message.answer(
        f"✅ Комната «{name}» ({label}) создана!\n"
        f"Вы — ГМ.\n\n"
        f"Сменить имя в этой комнате: /look\n"
        f"Выйти: /leave",
        reply_markup=main_menu_kb(),
    )


# ---------- Гримерка ----------

@router.message(Command("setname"))
async def cmd_setname(message: Message, state: FSMContext):
    uid = message.from_user.id
    current = grimer_mode.get(uid)

    if current == "global":
        grimer_mode[uid] = None
        if uid in pending_room_type:
            room_type = pending_room_type.pop(uid)
            if user_room.get(uid):
                await message.answer("❌ Вы уже в комнате, создание отменено.")
                return
            await state.set_state(CreateRoomFSM.waiting_password)
            await state.update_data(room_type=room_type)
            await message.answer("Введите пароль комнаты (или «нет»):")
            return
        await message.answer("Вы вышли из глобальной гримерки.")
        return

    grimer_mode[uid] = "global"
    g = global_names.get(uid, {})
    await message.answer(
        f"✏️ <b>Глобальная гримерка</b>\n"
        f"Имя: {html.escape(g.get('name') or '—')}\n"
        f"Символ: {html.escape(g.get('symbol') or '—')}\n\n"
        f"/nick НовоеИмя\n"
        f"/symbol 🔥 (пусто — убрать)\n"
        f"/setname — выйти",
        parse_mode="HTML",
    )


@router.message(Command("look"))
async def cmd_look(message: Message):
    uid = message.from_user.id
    rid = user_room.get(uid)
    if not rid:
        await message.answer("❌ Вы не в комнате. Сначала /join или /create.")
        return

    if grimer_mode.get(uid) == "local":
        grimer_mode[uid] = None
        await message.answer("Вы вышли из локальной гримерки.")
        return

    grimer_mode[uid] = "local"
    name, symbol = get_identity(uid, rid)
    await message.answer(
        f"✏️ <b>Локальная гримерка</b> (только для этой комнаты)\n"
        f"Имя: {html.escape(name)}\n"
        f"Символ: {html.escape(symbol or '—')}\n\n"
        f"/nick НовоеИмя\n"
        f"/symbol 🔥 (пусто — убрать)\n"
        f"/look — выйти",
        parse_mode="HTML",
    )


@router.message(Command("nick"))
async def cmd_nick(message: Message):
    uid = message.from_user.id
    mode = grimer_mode.get(uid)
    if not mode:
        await message.answer("❌ Сначала /setname (глобально) или /look (в комнате).")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("❌ /nick ВашеИмя")
        return
    name = parts[1].strip()
    if len(name) > MAX_NAME_LEN:
        await message.answer(f"❌ Максимум {MAX_NAME_LEN} символов.")
        return

    if mode == "global":
        g = global_names.setdefault(uid, {"name": name, "symbol": None})
        g["name"] = name
        await message.answer(f"✅ Глобальное имя: {html.escape(name)}", parse_mode="HTML")
        return

    rid = user_room.get(uid)
    if not rid:
        await message.answer("❌ Вы не в комнате.")
        return
    room = rooms[rid]
    ident = room["local_identities"].setdefault(uid, {"name": name, "symbol": None})
    ok, err = is_nick_allowed(rid, name, ident.get("symbol"), exclude_user=uid)
    if not ok:
        await message.answer(f"❌ {err}")
        return
    ident["name"] = name
    await message.answer(f"✅ Имя в этой комнате: {html.escape(name)}", parse_mode="HTML")


@router.message(Command("symbol"))
async def cmd_symbol(message: Message):
    uid = message.from_user.id
    mode = grimer_mode.get(uid)
    if not mode:
        await message.answer("❌ Сначала /setname или /look.")
        return

    parts = message.text.split(maxsplit=1)
    raw = parts[1].strip() if len(parts) > 1 else ""

    if raw:
        if len(raw) > 8 or raw.isalnum():
            await message.answer("❌ Символ — один эмодзи (не буква/цифра).")
            return
        symbol: Optional[str] = raw
    else:
        symbol = None

    if mode == "global":
        g = global_names.setdefault(uid, {"name": "Безымянный", "symbol": None})
        g["symbol"] = symbol
        await message.answer(f"✅ Глобальный символ: {html.escape(symbol or '—')}", parse_mode="HTML")
        return

    rid = user_room.get(uid)
    if not rid:
        await message.answer("❌ Вы не в комнате.")
        return
    room = rooms[rid]
    ident = room["local_identities"].setdefault(uid, {})
    cur_name = ident.get("name") or get_identity(uid, rid)[0]
    ok, err = is_nick_allowed(rid, cur_name, symbol, exclude_user=uid)
    if not ok:
        await message.answer(f"❌ {err}")
        return
    ident["symbol"] = symbol
    if "name" not in ident:
        ident["name"] = cur_name
    await message.answer(f"✅ Символ в этой комнате: {html.escape(symbol or '—')}", parse_mode="HTML")


# ---------- Join / Leave / List ----------

@router.message(Command("list"))
async def cmd_list(message: Message):
    if not rooms:
        await message.answer("Нет комнат. Создайте: /menu")
        return
    lines = []
    for r in rooms.values():
        lock = "🔒" if r["password_hash"] else "🔓"
        icon = {"regular": "🏠", "roleplay": "🎭", "dnd": "🐉"}[r["type"]]
        lines.append(f"{icon} {lock} «{r['name']}» — {len(r['members'])} уч.")
    await message.answer("📋 <b>Комнаты:</b>\n\n" + "\n".join(lines), parse_mode="HTML")


@router.message(Command("join"))
async def cmd_join(message: Message):
    args = message.text.split(maxsplit=2)
    if len(args) < 2:
        await message.answer("❌ /join Название [пароль]")
        return
    await _do_join(message, args[1].strip(), args[2].strip() if len(args) > 2 else None)


async def _do_join(message: Message, name: str, password: Optional[str]):
    uid = message.from_user.id
    target = find_room_by_name(name)
    if not target:
        await message.answer(f"❌ Комната «{name}» не найдена")
        return
    rid, room = target

    if room["password_hash"]:
        if not password or not verify_password(password, room["password_hash"]):
            await message.answer("❌ Неверный пароль или не указан")
            return

    if not has_global_name(uid):
        await message.answer(
            "⚠️ У вас нет глобального имени. В комнате вы будете «Безымянный».\n"
            "Смените: /setname → /nick ВашеИмя → /setname"
        )

    old_rid = user_room.get(uid)
    if old_rid and old_rid != rid:
        await _leave_room(message.bot, uid, old_rid)

    if uid not in room["members"]:
        room["members"].append(uid)
    user_room[uid] = rid

    n, s = get_identity(uid, rid)
    nick = format_nick(n, s)
    await message.answer(
        f"✅ Вы в комнате «{room['name']}».\n"
        f"Ник: <b>{html.escape(nick)}</b>\n"
        f"Сменить имя: /look",
        parse_mode="HTML",
    )
    await _notify_room(message.bot, room, f"🌸 {html.escape(nick)} присоединился(ась)", exclude=uid)

    # предупреждение о коллизии ников
    collision = False
    for other in room["members"]:
        if other == uid:
            continue
        if get_identity(other, rid) == (n, s):
            collision = True
            break
    if collision:
        await message.answer(
            "⚠️ Ваш ник совпадает с другим игроком. Команды будут требовать символ.\n"
            "Смените: /look → /nick НовоеИмя"
        )


async def _leave_room(bot, uid: int, rid: str):
    room = rooms.get(rid)
    user_room.pop(uid, None)
    if not room:
        return
    if uid in room["members"]:
        room["members"].remove(uid)
        n, s = get_identity(uid, rid)
        nick = format_nick(n, s)
        await _notify_room(bot, room, f"👋 {html.escape(nick)} покинул(а) комнату")
    room["local_identities"].pop(uid, None)


@router.message(Command("leave"))
async def cmd_leave(message: Message):
    uid = message.from_user.id
    rid = user_room.get(uid)
    if not rid:
        await message.answer("❌ Вы не в комнате")
        return
    await _leave_room(message.bot, uid, rid)
    await message.answer("✅ Вы покинули комнату")


# ---------- GM: rename / setpassword / kick ----------

@router.message(Command("rename"))
async def cmd_rename(message: Message):
    uid = message.from_user.id
    ctx = _ctx(uid)
    if not ctx:
        await message.answer("❌ Вы не в комнате")
        return
    rid, room = ctx
    if not _is_gm(uid, room):
        await message.answer("❌ Только ГМ")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("❌ /rename НовоеИмя")
        return
    new_name = parts[1].strip()
    if len(new_name) > MAX_ROOM_NAME_LEN:
        await message.answer(f"❌ Максимум {MAX_ROOM_NAME_LEN} символов")
        return
    found = find_room_by_name(new_name)
    if found and found[0] != rid:
        await message.answer("❌ Название занято")
        return
    old = room["name"]
    room["name"] = new_name
    await message.answer(f"✅ «{old}» → «{new_name}»")


@router.message(Command("setpassword"))
async def cmd_setpassword(message: Message):
    uid = message.from_user.id
    ctx = _ctx(uid)
    if not ctx:
        await message.answer("❌ Вы не в комнате")
        return
    _, room = ctx
    if not _is_gm(uid, room):
        await message.answer("❌ Только ГМ")
        return

    parts = message.text.split(maxsplit=1)
    new_pwd = parts[1].strip() if len(parts) > 1 else ""
    if not new_pwd or new_pwd.lower() == "нет":
        room["password_hash"] = None
        await message.answer("🔓 Пароль снят")
    else:
        room["password_hash"] = hash_password(new_pwd)
        await message.answer("🔒 Пароль установлен")


@router.message(Command("kick"))
async def cmd_kick(message: Message):
    uid = message.from_user.id
    ctx = _ctx(uid)
    if not ctx:
        await message.answer("❌ Вы не в комнате")
        return
    rid, room = ctx
    if not _is_gm(uid, room):
        await message.answer("❌ Только ГМ")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("❌ /kick &lt;ник&gt;", parse_mode="HTML")
        return
    target_uid, target_nick = parse_nick_arg(parts[1], rid)
    if target_uid is None:
        await message.answer(f"❌ {target_nick}")
        return
    if target_uid == uid:
        await message.answer("❌ Нельзя кикнуть себя")
        return

    if target_uid in room["members"]:
        room["members"].remove(target_uid)
    room["local_identities"].pop(target_uid, None)
    user_room.pop(target_uid, None)
    await message.answer(f"👢 {html.escape(target_nick)} кикнут(а)", parse_mode="HTML")
    try:
        await message.bot.send_message(target_uid, f"❌ Вас кикнули из комнаты «{room['name']}»")
    except Exception:
        pass


# ---------- Ролевое форматирование ----------

_RE_OOC = re.compile(r"(?<!:)//(.+?)//", re.DOTALL)
_RE_ACTION = re.compile(r"\*(.+?)\*", re.DOTALL)
_RE_THOUGHT = re.compile(r'"(.*?)"', re.DOTALL)


def format_roleplay(text: str) -> str:
    """HTML. *bold*, "italic", //{bold italic}//."""
    s = html.escape(text)
    s = _RE_OOC.sub(lambda m: f"<b><i>{{{m.group(1)}}}</i></b>", s)
    s = _RE_ACTION.sub(lambda m: f"<b>{m.group(1)}</b>", s)
    s = _RE_THOUGHT.sub(lambda m: f"<i>«{m.group(1)}»</i>", s)
    return s


# ---------- Анонимная пересылка ----------

@router.message(
    F.text | F.photo | F.sticker | F.voice | F.video |
    F.video_note | F.animation | F.document | F.audio
)
async def handle_message(message: Message):
    uid = message.from_user.id
    ctx = _ctx(uid)
    if not ctx:
        return
    rid, room = ctx

    # команды и броски — не пересылаем
    if message.text:
        low = message.text.lstrip().lower()
        if low.startswith("/") or low.startswith("кинуть"):
            return

    name, symbol = get_identity(uid, rid)
    nick = format_nick(name, symbol)
    use_format = room["type"] in ("roleplay", "dnd")
    members = room["members"].copy()
    if len(members) == 1:
        await message.answer("⚠️ В комнате больше никого нет.")
        return

    for mid in members:
        if mid == uid:
            continue
        try:
            await _forward_message(message, mid, nick, use_format)
        except Exception:
            pass


async def _forward_message(source: Message, target_id: int, nick: str, use_format: bool):
    bot = source.bot

    def fmt(text: Optional[str]) -> str:
        if not text:
            return ""
        return format_roleplay(text) if use_format else html.escape(text)

    prefix = f"<b>{html.escape(nick)}</b>:"

    if source.text:
        await bot.send_message(target_id, f"{prefix}\n{fmt(source.text)}", parse_mode="HTML")
        return

    cap_body = fmt(source.caption or "")
    caption = f"{prefix}\n{cap_body}" if cap_body else prefix

    if source.photo:
        await bot.send_photo(target_id, source.photo[-1].file_id, caption=caption, parse_mode="HTML")
    elif source.video:
        await bot.send_video(target_id, source.video.file_id, caption=caption, parse_mode="HTML")
    elif source.voice:
        await bot.send_voice(target_id, source.voice.file_id, caption=caption, parse_mode="HTML")
    elif source.document:
        await bot.send_document(target_id, source.document.file_id, caption=caption, parse_mode="HTML")
    elif source.audio:
        await bot.send_audio(target_id, source.audio.file_id, caption=caption, parse_mode="HTML")
    elif source.sticker:
        await bot.send_message(target_id, f"{prefix} [стикер]", parse_mode="HTML")
        await bot.send_sticker(target_id, source.sticker.file_id)
    elif source.animation:
        await bot.send_message(target_id, f"{prefix} [гифка]", parse_mode="HTML")
        await bot.send_animation(target_id, source.animation.file_id)
    elif source.video_note:
        await bot.send_message(target_id, f"{prefix} [видеосообщение]", parse_mode="HTML")
        await bot.send_video_note(target_id, source.video_note.file_id)


# ---------- Шёпот ----------

_RE_SHH = re.compile(r"^/shh\s+(.+?)\.\s*(.+)$", re.DOTALL | re.IGNORECASE)


@router.message(Command("shh"))
async def cmd_shh(message: Message):
    uid = message.from_user.id
    ctx = _ctx(uid)
    if not ctx:
        await message.answer("❌ Вы не в комнате")
        return
    rid, room = ctx
    if room["type"] not in ("roleplay", "dnd"):
        await message.answer("❌ Шёпот доступен только в Roleplay и DND комнатах")
        return

    m = _RE_SHH.match(message.text or "")
    if not m:
        await message.answer("❌ /shh Ник1, Ник2. текст")
        return
    nicks_raw, body_raw = m.group(1), m.group(2).strip()

    nick_list = [n.strip() for n in nicks_raw.split(",") if n.strip()]
    if not nick_list:
        await message.answer("❌ Укажите хотя бы одного получателя")
        return

    targets, errors = [], []
    for n in nick_list:
        t_uid, nick_or_err = parse_nick_arg(n, rid)
        if t_uid is None:
            errors.append(f"{n}: {nick_or_err}")
        else:
            targets.append((t_uid, nick_or_err))

    if not targets:
        await message.answer("❌ Не удалось найти получателей:\n" + "\n".join(errors))
        return

    s_name, s_sym = get_identity(uid, rid)
    s_nick = format_nick(s_name, s_sym)
    body = format_roleplay(body_raw)

    recipient_ids = {t for t, _ in targets}
    gm_id = room.get("created_by")
    if gm_id and gm_id != uid and gm_id not in recipient_ids:
        targets.append((gm_id, "(ГМ)"))

    for t_uid, _ in targets:
        try:
            await message.bot.send_message(
                t_uid,
                f"🤫 <b>Шёпот от {html.escape(s_nick)}</b>:\n{body}",
                parse_mode="HTML",
            )
        except Exception:
            pass

    names = ", ".join(t[1] for t in targets)
    confirm = f"🤫 Отправлено: {html.escape(names)}"
    if errors:
        confirm += "\n⚠️ Не найдены: " + "; ".join(errors)
    await message.answer(confirm, parse_mode="HTML")