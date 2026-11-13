"""Слой данных: комнаты (в памяти), анкеты, автосейв листов."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

SAVE_INTERVAL = 300  # 5 минут

# ---------- Глобальное состояние (только память) ----------

global_names: dict[int, dict] = {}       # user_id -> {"name", "symbol"}
rooms: dict[str, dict] = {}              # room_id -> комната
user_room: dict[int, str] = {}           # user_id -> room_id


# ---------- Утилиты ----------

def hash_password(password: str) -> str:
    # scrypt с солью: формат "<salt_hex>$<hash_hex>"
    salt = os.urandom(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return f"{salt.hex()}${h.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, hash_hex = stored.split("$")
        salt = bytes.fromhex(salt_hex)
        h = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
        return h.hex() == hash_hex
    except Exception:
        return False


def new_room_id() -> str:
    return uuid.uuid4().hex[:12]


# ---------- Комнаты ----------

def make_room(room_type: str, name: str, password: Optional[str], created_by: int) -> dict:
    return {
        "type": room_type,                 # "regular" | "roleplay" | "dnd"
        "name": name,
        "password_hash": hash_password(password) if password else None,
        "created_by": created_by,
        "members": [],                     # user_id
        "local_identities": {},            # user_id -> {"name","symbol"}
        "sheets": {},                      # user_id -> DND-анкета (dict)
        "cards": {},                       # user_id -> RP-карточка (dict)
        "dirty": False,                    # были изменения анкет с последнего сейва
    }


def find_room_by_name(name: str) -> Optional[tuple[str, dict]]:
    target = name.strip().lower()
    for rid, room in rooms.items():
        if room["name"].strip().lower() == target:
            return rid, room
    return None


def mark_dirty(room_id: str) -> None:
    if room_id in rooms:
        rooms[room_id]["dirty"] = True


# ---------- Идентичности ----------

def has_global_name(user_id: int) -> bool:
    return user_id in global_names


def get_identity(user_id: int, room_id: Optional[str] = None) -> tuple[str, Optional[str]]:
    if room_id and room_id in rooms:
        local = rooms[room_id]["local_identities"].get(user_id)
        if local:
            return local["name"], local.get("symbol")
    g = global_names.get(user_id)
    if g:
        return g["name"], g.get("symbol")
    return "Безымянный", None


def format_nick(name: str, symbol: Optional[str]) -> str:
    return f"{symbol} {name}" if symbol else name


def is_nick_allowed(room_id: str, name: str, symbol: Optional[str],
                    exclude_user: Optional[int] = None) -> tuple[bool, str]:
    room = rooms.get(room_id)
    if not room:
        return False, "комната не найдена"
    for uid in room["members"]:
        if uid == exclude_user:
            continue
        n, s = get_identity(uid, room_id)
        if n != name:
            continue
        if s is None:
            return False, f"Ник «{name}» уже занят (без символа). Выберите другой или добавьте символ."
        if s == symbol:
            return False, f"Пара «{name}» с символом {symbol} уже занята."
    return True, ""


def parse_nick_arg(text: str, room_id: str) -> tuple[Optional[int], str]:
    text = text.strip()
    if not text:
        return None, "ник не указан"
    room = rooms.get(room_id)
    if not room:
        return None, "комната не найдена"

    cands = []
    for uid in room["members"]:
        n, s = get_identity(uid, room_id)
        cands.append((uid, n, s, format_nick(n, s)))

    exact = [c for c in cands if c[3] == text]
    if len(exact) == 1:
        return exact[0][0], exact[0][3]
    if len(exact) > 1:
        return None, "несколько игроков с таким ником, уточните символ"

    by_name = [c for c in cands if c[1] == text]
    if len(by_name) == 1:
        return by_name[0][0], by_name[0][3]
    if len(by_name) > 1:
        return None, "несколько игроков с таким именем, укажите символ"

    return None, f"игрок «{text}» не найден"


# ---------- DND-анкета по умолчанию ----------

BASE_STATS = ["Сила", "Ловкость", "Телосложение", "Интеллект", "Мудрость", "Харизма"]


def new_sheet() -> dict:
    return {
        "stats": {s: 0 for s in BASE_STATS},   # "Макс ХП" — обычный стат
        "items": [],
        "statuses": [],
        "hp_current": 0,
    }


def ensure_sheet(room_id: str, user_id: int) -> dict:
    room = rooms.get(room_id)
    if not room:
        return new_sheet()
    if user_id not in room["sheets"]:
        room["sheets"][user_id] = new_sheet()
        mark_dirty(room_id)
    return room["sheets"][user_id]


# ---------- Save / Load листов ----------

def _safe_filename(room_name: str) -> str:
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in room_name)
    return f"{safe or 'room'}_sheets.json"


def sheets_file_path(room_name: str) -> str:
    return _safe_filename(room_name)


def sheets_file_exists(room_name: str) -> bool:
    return os.path.exists(_safe_filename(room_name))


def write_sheets_file(room_id: str) -> tuple[bool, str]:
    room = rooms.get(room_id)
    if not room:
        return False, "комната не найдена"
    out = {}
    for uid, sheet in room["sheets"].items():
        n, s = get_identity(uid, room_id)
        out[format_nick(n, s)] = sheet
    fname = _safe_filename(room["name"])
    try:
        with open(fname, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return False, f"ошибка записи: {e}"
    room["dirty"] = False
    return True, fname


def read_sheets_from_file(filename: str) -> tuple[Optional[dict], str]:
    if not os.path.exists(filename):
        return None, f"файл «{filename}» не найден"
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f), ""
    except Exception as e:
        return None, f"ошибка чтения: {e}"


def apply_sheets(room_id: str, data: dict) -> tuple[int, list[str]]:
    """Перезапись по никам. Возвращает (сколько загружено, ники которых не нашли)."""
    room = rooms.get(room_id)
    if not room:
        return 0, list(data.keys())

    nick_to_uid = {}
    for uid in room["members"]:
        n, s = get_identity(uid, room_id)
        nick_to_uid[format_nick(n, s)] = uid

    loaded, missing = 0, []
    for nick, sheet in data.items():
        uid = nick_to_uid.get(nick)
        if uid is None:
            missing.append(nick)
            continue
        room["sheets"][uid] = sheet
        loaded += 1

    if loaded:
        mark_dirty(room_id)
    return loaded, missing


# ---------- Фоновый автосейв ----------

async def autosave_loop() -> None:
    """Каждые 5 минут сохраняем только «грязные» комнаты с участниками."""
    while True:
        await asyncio.sleep(SAVE_INTERVAL)
        try:
            for rid, room in list(rooms.items()):
                if not room["dirty"] or not room["members"]:
                    continue
                ok, info = write_sheets_file(rid)
                if ok:
                    logger.info(f"autosave: комната «{room['name']}» → {info}")
                else:
                    logger.error(f"autosave: {info}")
        except Exception as e:
            logger.exception(f"autosave_loop: {e}")

# ---------- RP-карточки ----------

def new_card() -> dict:
    return {
        "name": "",
        "age": "",
        "text": "",
        "photo_file_id": None
    }

def ensure_card(room_id: str, user_id: int) -> dict:
    room = rooms.get(room_id)
    if not room:
        return new_card()
    if user_id not in room["cards"]:
        room["cards"][user_id] = new_card()
        mark_dirty(room_id)
    return room["cards"][user_id]
