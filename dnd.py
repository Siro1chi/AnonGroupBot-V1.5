"""DND: парсер бросков + все DND-хендлеры."""
from __future__ import annotations

import html
import os
import random
import re
from dataclasses import dataclass
from typing import Optional

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

import storage
from storage import (
    rooms, user_room, get_identity, format_nick, parse_nick_arg,
    ensure_sheet, mark_dirty, write_sheets_file, read_sheets_from_file,
    apply_sheets, sheets_file_exists, sheets_file_path,
)

router = Router(name="dnd")

MAX_HP_STAT = "Здоровье"
STAT_VALUE_MIN = -999
STAT_VALUE_MAX = 9999


class SaveFSM(StatesGroup):
    confirm = State()


# ============ BROLLS ============

ROLL_RE = re.compile(
    r"^Кинуть\s+(\d+)\s*[дdДD]\s*(\d+)\s*([+-]\s*\d+)?\s*([вн])?\s*$",
    re.IGNORECASE,
)


@dataclass
class RollSpec:
    count: int
    sides: int
    modifier: int = 0
    mode: str = "normal"


@dataclass
class RollResult:
    spec: RollSpec
    rolls: list[int]
    chosen: int
    total: int
    crit: Optional[str] = None


def parse_roll(text: str) -> tuple[Optional[RollSpec], str]:
    m = ROLL_RE.match(text.strip())
    if not m:
        return None, "Формат: Кинуть AдB[+C|-C][в|н]"
    count, sides = int(m.group(1)), int(m.group(2))
    mod_raw, mode_raw = m.group(3), (m.group(4) or "").lower()
    if not (1 <= count <= 20):
        return None, "Количество кубов: 1..20"
    if not (2 <= sides <= 100):
        return None, "Тип куба: 2..100"
    modifier = 0
    if mod_raw:
        modifier = int(mod_raw.replace(" ", ""))
        if not (-100 <= modifier <= 100):
            return None, "Модификатор: −100..+100"
    mode = {"": "normal", "в": "adv", "н": "dis"}[mode_raw]
    if mode != "normal" and (sides != 20 or count != 1):
        return None, "Преимущество/помеха — только для одиночного д20"
    return RollSpec(count=count, sides=sides, modifier=modifier, mode=mode), ""


def roll(spec: RollSpec) -> RollResult:
    if spec.mode == "normal":
        rolls = [random.randint(1, spec.sides) for _ in range(spec.count)]
        chosen = sum(rolls)
        crit = None
        if spec.sides == 20 and spec.count == 1:
            crit = "success" if rolls[0] == 20 else "fail" if rolls[0] == 1 else None
    else:
        a, b = random.randint(1, 20), random.randint(1, 20)
        rolls = [a, b]
        chosen = max(a, b) if spec.mode == "adv" else min(a, b)
        crit = "success" if chosen == 20 else "fail" if chosen == 1 else None
    return RollResult(spec=spec, rolls=rolls, chosen=chosen,
                      total=chosen + spec.modifier, crit=crit)


def format_roll(result: RollResult, nick: str) -> str:
    spec = result.spec
    head = f"🎲 <b>{html.escape(nick)}</b>"
    if spec.mode == "adv":
        head += " (преимущество)"
    elif spec.mode == "dis":
        head += " (помеха)"
    rolls_str = "[" + ", ".join(str(r) for r in result.rolls) + "]"
    if spec.mode == "normal":
        body = rolls_str
        if spec.modifier:
            body += f" {spec.modifier:+d}"
        body += f" = <b>{result.total}</b>"
    else:
        body = f"{rolls_str} → {result.chosen}"
        if spec.modifier:
            body += f" {spec.modifier:+d}"
        body += f" = <b>{result.total}</b>"
    line = f"{head}: {body}"
    if result.crit == "success":
        line = "🎯 <b>Крит!</b> " + line
    elif result.crit == "fail":
        line = "💀 <b>Провал!</b> " + line
    return line


# ============ HELPERS ============

def _dnd_ctx(user_id: int) -> Optional[tuple[str, dict]]:
    rid = user_room.get(user_id)
    if not rid:
        return None
    room = rooms.get(rid)
    if not room or room["type"] != "dnd" or user_id not in room["members"]:
        return None
    return rid, room


def _is_gm(user_id: int, room: dict) -> bool:
    return room.get("created_by") == user_id


def _split_nick_and_rest(text: str, rid: str) -> tuple[Optional[int], str, str]:
    """Try progressively shorter prefixes as nick. Возврат (uid, nick, rest) или (None, err, '')."""
    words = text.split()
    if not words:
        return None, "пустой аргумент", ""
    for i in range(len(words), 0, -1):
        prefix = " ".join(words[:i])
        rest = " ".join(words[i:]).strip()
        uid, nick_or_err = parse_nick_arg(prefix, rid)
        if uid is not None:
            return uid, nick_or_err, rest
    return None, "ник не найден", ""


def _hp_str(sheet: dict) -> str:
    cur = sheet.get("hp_current", 0)
    max_hp = sheet.get("stats", {}).get(MAX_HP_STAT)
    return f"{cur}/{max_hp}" if max_hp is not None else str(cur)


def _format_sheet(sheet: dict, nick: str) -> str:
    lines = [
        f"📋 <b>Анкета: {html.escape(nick)}</b>",
        "",
        f"❤️ ХП: <b>{_hp_str(sheet)}</b>",
        "",
    ]
    stats = sheet.get("stats", {})
    if stats:
        lines.append("📊 <b>Характеристики:</b>")
        for k, v in stats.items():
            lines.append(f"  • {html.escape(k)}: {v}")
        lines.append("")
    for field, label, icon in [
        ("items", "Предметы", "🎒"),
        ("statuses", "Статусы", "✨"),
    ]:
        lst = sheet.get(field, [])
        if lst:
            lines.append(f"{icon} <b>{label}:</b>")
            for item in lst:
                lines.append(f"  • {html.escape(item)}")
            lines.append("")
    return "\n".join(lines).rstrip()


async def _notify_room(bot, room: dict, text: str) -> None:
    for uid in room["members"]:
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
        except Exception:
            pass


# ============ ROLL ============

@router.message(F.text.lower().startswith("кинуть"))
async def handle_roll(message: Message):
    uid = message.from_user.id
    ctx = _dnd_ctx(uid)
    if not ctx:
        return
    rid, room = ctx
    spec, err = parse_roll(message.text)
    if not spec:
        await message.answer(f"❌ {err}")
        return
    name, symbol = get_identity(uid, rid)
    nick = format_nick(name, symbol)
    text = format_roll(roll(spec), nick)
    if len(room["members"]) == 1:
        await message.answer(text + "\n\n⚠️ В комнате никого больше нет", parse_mode="HTML")
        return
    for mid in room["members"]:
        try:
            await message.bot.send_message(mid, text, parse_mode="HTML")
        except Exception:
            pass


# ============ HP ============

@router.message(Command("hp"))
async def handle_hp(message: Message):
    uid = message.from_user.id
    ctx = _dnd_ctx(uid)
    if not ctx:
        await message.answer("❌ Только в DND-комнате")
        return
    rid, room = ctx
    parts = message.text.split(maxsplit=1)

    if len(parts) == 1:
        sheet = ensure_sheet(rid, uid)
        name, sym = get_identity(uid, rid)
        nick = format_nick(name, sym)
        await message.answer(f"❤️ {html.escape(nick)}: <b>{_hp_str(sheet)}</b>", parse_mode="HTML")
        return

    target_uid, target_nick = parse_nick_arg(parts[1].strip(), rid)
    if target_uid is None:
        await message.answer(f"❌ {target_nick}")
        return
    sheet = ensure_sheet(rid, target_uid)
    await message.answer(f"❤️ {html.escape(target_nick)}: <b>{_hp_str(sheet)}</b>", parse_mode="HTML")


@router.message(Command("dmg"))
async def handle_dmg(message: Message):
    await _hp_change(message, sign=-1)


@router.message(Command("heal"))
async def handle_heal(message: Message):
    await _hp_change(message, sign=+1)


async def _hp_change(message: Message, sign: int):
    uid = message.from_user.id
    ctx = _dnd_ctx(uid)
    if not ctx:
        await message.answer("❌ Только в DND-комнате")
        return
    rid, room = ctx
    if not _is_gm(uid, room):
        await message.answer("❌ Только ГМ")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        cmd = "dmg" if sign < 0 else "heal"
        await message.answer(f"❌ /{cmd} &lt;ник&gt; &lt;число&gt;", parse_mode="HTML")
        return

    target_uid, target_nick, rest = _split_nick_and_rest(parts[1], rid)
    if target_uid is None:
        await message.answer(f"❌ {target_nick}")
        return
    if not rest:
        await message.answer("❌ Укажите число: /dmg ник 8")
        return
    try:
        amount = int(rest.strip())
    except ValueError:
        await message.answer("❌ Число после ника")
        return
    if amount <= 0:
        await message.answer("❌ Число должно быть положительным")
        return

    sheet = ensure_sheet(rid, target_uid)
    sheet["hp_current"] = sheet.get("hp_current", 0) + sign * amount
    mark_dirty(rid)

    action = "получил(а) урон" if sign < 0 else "полечился(ась)"
    notify = (
        f"Игрок {html.escape(target_nick)} {action} {amount}. "
        f"Хп: <b>{_hp_str(sheet)}</b>"
    )
    await _notify_room(message.bot, room, notify)


# ============ CHAR ============

@router.message(Command("char"))
async def handle_char(message: Message):
    uid = message.from_user.id
    ctx = _dnd_ctx(uid)
    if not ctx:
        await message.answer("❌ Только в DND-комнате")
        return
    rid, room = ctx
    if not _is_gm(uid, room):
        await message.answer("❌ Только ГМ")
        return

    parts = message.text.split(maxsplit=3)
    if len(parts) < 4:
        await message.answer(
            "❌ /char Стат add|edit|del ник [значение]\n"
            "Например: /char Сила edit Леголас 15"
        )
        return

    stat, action, rest = parts[1].strip(), parts[2].strip().lower(), parts[3].strip()
    if action not in ("add", "edit", "del"):
        await message.answer("❌ Действие: add/edit/del")
        return

    value = None
    if action in ("add", "edit"):
        sub = rest.rsplit(maxsplit=1)
        if len(sub) < 2:
            await message.answer("❌ Укажите значение в конце")
            return
        try:
            value = int(sub[1])
        except ValueError:
            await message.answer("❌ Значение должно быть числом")
            return
        if not (STAT_VALUE_MIN <= value <= STAT_VALUE_MAX):
            await message.answer(f"❌ Значение: {STAT_VALUE_MIN}..{STAT_VALUE_MAX}")
            return
        nick_text = sub[0]
    else:
        nick_text = rest

    target_uid, target_nick = parse_nick_arg(nick_text, rid)
    if target_uid is None:
        await message.answer(f"❌ {target_nick}")
        return

    sheet = ensure_sheet(rid, target_uid)
    stats = sheet["stats"]

    if action == "add":
        if stat in stats:
            await message.answer(f"❌ Стат «{html.escape(stat)}» уже есть у {html.escape(target_nick)}", parse_mode="HTML")
            return
        stats[stat] = value
        if stat == MAX_HP_STAT and not sheet.get("hp_was_init"):
            sheet["hp_current"] = value
            sheet["hp_was_init"] = True
        mark_dirty(rid)
        await message.answer(f"✅ {html.escape(target_nick)}: «{html.escape(stat)}» = {value}", parse_mode="HTML")

    elif action == "edit":
        if stat not in stats:
            await message.answer(f"❌ Стат «{html.escape(stat)}» не найден у {html.escape(target_nick)}", parse_mode="HTML")
            return
        stats[stat] = value
        if stat == MAX_HP_STAT:
            if not sheet.get("hp_was_init"):
                sheet["hp_current"] = value
                sheet["hp_was_init"] = True
            elif sheet["hp_current"] > value:
                sheet["hp_current"] = value
        mark_dirty(rid)
        await message.answer(f"✅ {html.escape(target_nick)}: «{html.escape(stat)}» → {value}", parse_mode="HTML")

    else:  # del
        if stat not in stats:
            await message.answer(f"❌ Стат «{html.escape(stat)}» не найден у {html.escape(target_nick)}", parse_mode="HTML")
            return
        del stats[stat]
        mark_dirty(rid)
        await message.answer(f"✅ {html.escape(target_nick)}: удалён стат «{html.escape(stat)}»", parse_mode="HTML")


# ============ ITEMS / STATUSES ============

async def _list_edit_common(message: Message, field: str, label: str):
    uid = message.from_user.id
    ctx = _dnd_ctx(uid)
    if not ctx:
        await message.answer("❌ Только в DND-комнате")
        return
    rid, room = ctx
    if not _is_gm(uid, room):
        await message.answer("❌ Только ГМ")
        return

    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(
            f"❌ Формат:\n"
            f"/{label} add &lt;ник&gt; &lt;название&gt;\n"
            f"/{label} rem &lt;ник&gt; &lt;название&gt;\n"
            f"/{label} edit &lt;ник&gt; &lt;старое&gt; -&gt; &lt;новое&gt;",
            parse_mode="HTML",
        )
        return

    action, rest = parts[1].strip().lower(), parts[2].strip()
    if action not in ("add", "rem", "edit"):
        await message.answer("❌ Действие: add/rem/edit")
        return

    if action == "edit":
        if " -> " not in rest:
            await message.answer("❌ Разделитель « -> » для edit")
            return
        left, new_val = rest.split(" -> ", 1)
        new_val = new_val.strip()
        if not new_val:
            await message.answer("❌ Новое значение пустое")
            return
        target_uid, target_nick, old_val = _split_nick_and_rest(left.strip(), rid)
        if target_uid is None:
            await message.answer(f"❌ {target_nick}")
            return
        if not old_val:
            await message.answer("❌ Укажите старое значение")
            return
        sheet = ensure_sheet(rid, target_uid)
        lst = sheet[field]
        if old_val not in lst:
            await message.answer(f"❌ «{html.escape(old_val)}» не найдено у {html.escape(target_nick)}", parse_mode="HTML")
            return
        if new_val in lst and new_val != old_val:
            await message.answer(f"❌ «{html.escape(new_val)}» уже есть у {html.escape(target_nick)}", parse_mode="HTML")
            return
        lst[lst.index(old_val)] = new_val
        mark_dirty(rid)
        await message.answer(f"✅ {html.escape(target_nick)}: «{html.escape(old_val)}» → «{html.escape(new_val)}»", parse_mode="HTML")
        return

    target_uid, target_nick, name_val = _split_nick_and_rest(rest, rid)
    if target_uid is None:
        await message.answer(f"❌ {target_nick}")
        return
    if not name_val:
        await message.answer("❌ Укажите название")
        return

    sheet = ensure_sheet(rid, target_uid)
    lst = sheet[field]

    if action == "add":
        if name_val in lst:
            await message.answer(f"❌ «{html.escape(name_val)}» уже есть у {html.escape(target_nick)}", parse_mode="HTML")
            return
        lst.append(name_val)
        mark_dirty(rid)
        await message.answer(f"✅ {html.escape(target_nick)}: + «{html.escape(name_val)}»", parse_mode="HTML")
    else:  # rem
        if name_val not in lst:
            await message.answer(f"❌ «{html.escape(name_val)}» не найдено у {html.escape(target_nick)}", parse_mode="HTML")
            return
        lst.remove(name_val)
        mark_dirty(rid)
        await message.answer(f"✅ {html.escape(target_nick)}: − «{html.escape(name_val)}»", parse_mode="HTML")


@router.message(Command("item"))
async def handle_item(message: Message):
    await _list_edit_common(message, "items", "item")


@router.message(Command("stt"))
async def handle_stt(message: Message):
    await _list_edit_common(message, "statuses", "stt")


# ============ SHEET ============

@router.message(Command("sheet"))
async def handle_sheet(message: Message):
    uid = message.from_user.id
    ctx = _dnd_ctx(uid)
    if not ctx:
        return
    rid, room = ctx
    
    parts = message.text.split(maxsplit=1)
    if len(parts) == 1:
        target_uid = uid
        sheet = ensure_sheet(rid, uid)
        name, sym = get_identity(uid, rid)
        target_nick = format_nick(name, sym)
    else:
        target_uid, target_nick = parse_nick_arg(parts[1].strip(), rid)
        if target_uid is None:
            await message.answer(f"❌ {target_nick}")
            return
        sheet = ensure_sheet(rid, target_uid)

    await message.answer(_format_sheet(sheet, target_nick), parse_mode="HTML")


# ============ SAVE / LOAD ============

@router.message(Command("save"))
async def handle_save(message: Message, state: FSMContext):
    parts = message.text.split()
    if len(parts) < 2 or parts[1].lower() != "sheets":
        await message.answer("❌ /save sheets")
        return
    uid = message.from_user.id
    ctx = _dnd_ctx(uid)
    if not ctx:
        await message.answer("❌ Только в DND-комнате")
        return
    rid, room = ctx
    if not _is_gm(uid, room):
        await message.answer("❌ Только ГМ")
        return
    if not room["sheets"]:
        await message.answer("❌ В комнате нет анкет")
        return

    if sheets_file_exists(room["name"]):
        fname = sheets_file_path(room["name"])
        data, _err = read_sheets_from_file(fname)
        nicks = list(data.keys()) if data else ["<не прочитано>"]
        await state.set_state(SaveFSM.confirm)
        await state.update_data(rid=rid)
        await message.answer(
            f"⚠️ Файл «{fname}» уже существует.\n"
            f"В нём анкеты:\n" + "\n".join(f"  • {n}" for n in nicks) +
            "\n\nПерезаписать? да/нет"
        )
        return

    ok, info = write_sheets_file(rid)
    await message.answer(f"✅ Сохранено в «{info}»" if ok else f"❌ {info}")


@router.message(SaveFSM.confirm, F.text.lower().in_({"да", "yes", "д", "y"}))
async def confirm_save_yes(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    ok, info = write_sheets_file(data["rid"])
    await message.answer(f"✅ Перезаписано в «{info}»" if ok else f"❌ {info}")


@router.message(SaveFSM.confirm, F.text.lower().in_({"нет", "no", "н", "n"}))
async def confirm_save_no(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.")


@router.message(SaveFSM.confirm, ~F.text.startswith("/"))
async def confirm_save_other(message: Message):
    await message.answer("Ответьте «да» или «нет».")


@router.message(Command("load"))
async def handle_load(message: Message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3 or parts[1].lower() != "sheets":
        await message.answer("❌ /load sheets &lt;имя_файла&gt;", parse_mode="HTML")
        return
    fname_arg = parts[2].strip()

    uid = message.from_user.id
    ctx = _dnd_ctx(uid)
    if not ctx:
        await message.answer("❌ Только в DND-комнате")
        return
    rid, room = ctx
    if not _is_gm(uid, room):
        await message.answer("❌ Только ГМ")
        return

    candidates = [fname_arg]
    if not fname_arg.endswith(".json"):
        candidates.extend([fname_arg + ".json", fname_arg + "_sheets.json"])
    fname = next((c for c in candidates if os.path.exists(c)), None)
    if not fname:
        await message.answer(f"❌ Файл не найден. Пробовали: {', '.join(candidates)}")
        return

    data, err = read_sheets_from_file(fname)
    if data is None:
        await message.answer(f"❌ {err}")
        return

    loaded, missing = apply_sheets(rid, data)
    msg = f"✅ Загружено {loaded} анкет из «{fname}»"
    if missing:
        msg += "\n⚠️ Не найдены ники:\n" + "\n".join(f"  • {n}" for n in missing)
    await message.answer(msg)
