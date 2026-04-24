"""
Party Game Bot — бот для разгара вечеринки.
Задания, дуэли, голосования, ачивки, номинации.
"""
import os
import io
import json
import base64
import random
import logging
from typing import Optional

from PIL import Image, ImageDraw, ImageFont, ImageFilter
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

from tasks import MAIN_TASKS, EASY_TASKS, DUEL_TASKS

# --- Конфиг ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID")) if os.environ.get("ADMIN_ID") else None
DATA_FILE = os.environ.get("DATA_FILE", "bot_data.json")

# --- Логгер ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Состояние бота ---
# state: idle | task_pending | task_voting | task_rating | duel_pending | duel_voting | finished
state = "idle"
participants = {}  # chat_id -> participant dict
used_main_tasks = []  # индексы уже выданных задач
used_easy_tasks = []
used_duel_tasks = []
current_task = None  # {executor_cid, mission, from_admin, phase, votes_yes, votes_no, ratings}
current_duel = None  # {p1_cid, p2_cid, mission, p1_done, p2_done, votes}
mission_history = []  # [{mission, executor, score}]
first_completed_done = False  # для ачивки «Первопроходец»

# --- Админ: временные состояния ---
# Когда админ выбирает человека/пишет задание
admin_state = {}  # admin_chat_id -> {"phase": "custom_target"|"custom_text"|"reg_names"|"reg_photo", ...}


# --- Сохранение/загрузка ---
def _save():
    """Сохранить данные на диск."""
    try:
        data = {
            "state": state,
            "used_main_tasks": used_main_tasks,
            "used_easy_tasks": used_easy_tasks,
            "used_duel_tasks": used_duel_tasks,
            "current_task": current_task,
            "current_duel": current_duel,
            "mission_history": mission_history,
            "first_completed_done": first_completed_done,
            "participants": {},
        }
        for cid, p in participants.items():
            entry = dict(p)
            if entry.get("photo"):
                entry["photo_b64"] = base64.b64encode(entry["photo"]).decode("ascii")
                entry.pop("photo")
            else:
                entry["photo_b64"] = None
            entry.pop("card", None)
            data["participants"][str(cid)] = entry

        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        logger.error(f"save err: {e}")


def _load():
    """Загрузить данные с диска."""
    global state, participants, used_main_tasks, used_easy_tasks, used_duel_tasks
    global current_task, current_duel, mission_history, first_completed_done
    if not os.path.exists(DATA_FILE):
        return
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        state = data.get("state", "idle")
        used_main_tasks = data.get("used_main_tasks", [])
        used_easy_tasks = data.get("used_easy_tasks", [])
        used_duel_tasks = data.get("used_duel_tasks", [])
        current_task = data.get("current_task")
        current_duel = data.get("current_duel")
        mission_history = data.get("mission_history", [])
        first_completed_done = data.get("first_completed_done", False)
        participants.clear()
        for cid_str, entry in data.get("participants", {}).items():
            e = dict(entry)
            b64 = e.pop("photo_b64", None)
            e["photo"] = base64.b64decode(b64) if b64 else None
            participants[int(cid_str)] = e
        logger.info(f"Загружено: state={state}, {len(participants)} участников")
    except Exception as e:
        logger.error(f"load err: {e}")


# --- Утилиты ---
def is_admin(uid: int) -> bool:
    return ADMIN_ID is not None and uid == ADMIN_ID


def new_participant(names: str, photo: Optional[bytes]) -> dict:
    """Создать запись участника."""
    return {
        "names": names,
        "photo": photo,
        "is_birthday": False,
        "score": 0,
        "penalty_rub": 0,
        "tasks_done": 0,
        "tasks_refused": 0,
        "ratings_given": [],     # какие оценки ставил этот человек
        "ratings_received": [],  # какие получал
        "streak": 0,             # серия выполненных подряд
        "roulette_used": False,
        "achievements": [],      # список полученных ачивок
        "duels_won": 0,
    }


def active_non_birthday() -> list:
    """Список chat_id активных не-именинников."""
    return [cid for cid, p in participants.items() if not p.get("is_birthday")]


def all_voters_except(executor_cid: int) -> list:
    """Все кто голосуют за задание (все зареганные кроме исполнителя)."""
    return [cid for cid in participants.keys() if cid != executor_cid]


async def grant_achievement(cid: int, ach: str, bot):
    """Дать ачивку участнику и уведомить."""
    p = participants.get(cid)
    if not p:
        return False
    if ach in p["achievements"]:
        return False
    p["achievements"].append(ach)
    _save()
    try:
        await bot.send_message(
            chat_id=cid,
            text=f"🎖 Новая ачивка:\n**{ach}**",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"ach notify err: {e}")
    return True


# --- Групповой чат для анонсов ---
group_chat_id = None  # set via /setgroup in group

def _save_group(cid):
    """Сохранить group chat id в отдельный файл."""
    try:
        with open("group_chat.txt", "w") as f:
            f.write(str(cid))
    except Exception as e:
        logger.error(f"save group err: {e}")


def _load_group():
    global group_chat_id
    if os.path.exists("group_chat.txt"):
        try:
            with open("group_chat.txt") as f:
                group_chat_id = int(f.read().strip())
        except Exception as e:
            logger.error(f"load group err: {e}")


async def announce(bot, text, photo=None):
    """Отправить анонс в групповой чат."""
    if not group_chat_id:
        return
    try:
        if photo:
            await bot.send_photo(chat_id=group_chat_id, photo=photo, caption=text, parse_mode="Markdown")
        else:
            await bot.send_message(chat_id=group_chat_id, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"announce err: {e}")


# --- Клавиатуры ---
def admin_kb():
    me_registered = ADMIN_ID and ADMIN_ID in participants
    rows = []

    # Основные действия
    rows.append([InlineKeyboardButton("🎲 Новое задание", callback_data="adm_new_task")])
    rows.append([
        InlineKeyboardButton("⚔️ Дуэль", callback_data="adm_duel"),
        InlineKeyboardButton("🎯 Кастомное", callback_data="adm_custom"),
    ])

    # Контекстная кнопка закрытия — только если есть активный этап
    if state == "task_voting" or state == "duel_voting":
        rows.append([InlineKeyboardButton("� Закрыть голосование", callback_data="adm_close_vote")])
    elif state == "task_rating":
        rows.append([InlineKeyboardButton("🔒 Закрыть оценки", callback_data="adm_close_rating")])

    # Инфо и настройки
    rows.append([
        InlineKeyboardButton("📈 Баллы", callback_data="adm_scores"),
        InlineKeyboardButton("👥 Участники", callback_data="adm_people"),
    ])
    rows.append([InlineKeyboardButton("⭐ Именинники", callback_data="adm_mark_birthday")])

    # Регистрация админа — только если ещё не зареган
    if not me_registered:
        rows.append([InlineKeyboardButton("📝 Я тоже играю (зарегаться)", callback_data="adm_reg")])

    # Финал
    rows.append([InlineKeyboardButton("🏁 Завершить вечеринку", callback_data="adm_finish")])

    return InlineKeyboardMarkup(rows)


def people_kb():
    """Подменю: список + удаление."""
    rows = []
    for cid, p in participants.items():
        star = " ⭐" if p.get("is_birthday") else ""
        rows.append([
            InlineKeyboardButton(f"{p['names']}{star} ({p['score']})", callback_data="noop"),
            InlineKeyboardButton("�", callback_data=f"del_{cid}"),
        ])
    rows.append([InlineKeyboardButton("🔄 Сбросить всё", callback_data="adm_reset")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="adm_back")])
    return InlineKeyboardMarkup(rows)


def yes_no_kb(prefix: str):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("👍 Да", callback_data=f"{prefix}_yes"),
        InlineKeyboardButton("👎 Нет", callback_data=f"{prefix}_no"),
    ]])


def rating_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(str(i), callback_data=f"rate_{i}") for i in range(1, 6)
    ]])


def executor_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Выполнил", callback_data="exec_done")],
        [InlineKeyboardButton("❌ Отказываюсь", callback_data="exec_refuse")],
    ])


def duel_executor_kb(role: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Готов / Выполнил", callback_data=f"duel_{role}_done")],
        [InlineKeyboardButton("❌ Отказываюсь", callback_data=f"duel_{role}_refuse")],
    ])


def duel_vote_kb(p1_names: str, p2_names: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🥇 {p1_names}", callback_data="dvote_p1")],
        [InlineKeyboardButton(f"🥇 {p2_names}", callback_data="dvote_p2")],
        [InlineKeyboardButton("🤝 Ничья", callback_data="dvote_tie")],
    ])


def roulette_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎰 Крутнуть рулетку", callback_data="roulette_spin")],
        [InlineKeyboardButton("🙅 Не, я выбываю", callback_data="roulette_skip")],
    ])


def roulette_done_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Выполнил", callback_data="roulette_done")],
        [InlineKeyboardButton("❌ Сдаюсь", callback_data="roulette_fail")],
    ])


# --- Генерация картинки победителя ---
def _find_font(size: int):
    paths = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for p in paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _circle_photo(photo_bytes: bytes, size: int) -> Image.Image:
    img = Image.open(io.BytesIO(photo_bytes)).convert("RGBA")
    # обрезаем по меньшей стороне в квадрат
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side)).resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, size, size), fill=255)
    result = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    result.paste(img, (0, 0), mask)
    return result


def _gradient_bg(w: int, h: int, color_top, color_bot) -> Image.Image:
    bg = Image.new("RGB", (w, h), color_top)
    top_r, top_g, top_b = color_top
    bot_r, bot_g, bot_b = color_bot
    for y in range(h):
        t = y / h
        r = int(top_r + (bot_r - top_r) * t)
        g = int(top_g + (bot_g - top_g) * t)
        b = int(top_b + (bot_b - top_b) * t)
        ImageDraw.Draw(bg).line([(0, y), (w, y)], fill=(r, g, b))
    return bg


def generate_winner_card(photo_bytes: Optional[bytes], names: str, score: int) -> bytes:
    W, H = 900, 1200
    # Золотой градиент
    bg = _gradient_bg(W, H, (255, 200, 60), (200, 120, 20))
    draw = ImageDraw.Draw(bg)

    # Фото в круге
    photo_size = 500
    photo_y = 280
    if photo_bytes:
        try:
            circle = _circle_photo(photo_bytes, photo_size)
            # Тень-обводка
            ring = Image.new("RGBA", (photo_size + 40, photo_size + 40), (0, 0, 0, 0))
            ImageDraw.Draw(ring).ellipse(
                (0, 0, photo_size + 40, photo_size + 40), fill=(255, 255, 255, 255)
            )
            bg.paste(ring, ((W - photo_size - 40) // 2, photo_y - 20), ring)
            bg.paste(circle, ((W - photo_size) // 2, photo_y), circle)
        except Exception as e:
            logger.error(f"winner photo err: {e}")

    # Корона 👑 большая над фото
    try:
        crown_font = _find_font(180)
        crown = "👑"
        tw = draw.textlength(crown, font=crown_font)
        draw.text(((W - tw) // 2, 100), crown, font=crown_font, embedded_color=True)
    except Exception:
        pass

    # "ПОБЕДИТЕЛЬ"
    title_font = _find_font(72)
    title = "ПОБЕДИТЕЛЬ"
    tw = draw.textlength(title, font=title_font)
    # тень
    draw.text(((W - tw) // 2 + 3, 820 + 3), title, font=title_font, fill=(60, 30, 0))
    draw.text(((W - tw) // 2, 820), title, font=title_font, fill=(255, 255, 255))

    # Имя
    name_font = _find_font(60)
    tw = draw.textlength(names, font=name_font)
    draw.text(((W - tw) // 2 + 2, 920 + 2), names, font=name_font, fill=(60, 30, 0))
    draw.text(((W - tw) // 2, 920), names, font=name_font, fill=(255, 255, 255))

    # Баллы
    sc_font = _find_font(80)
    sc_text = f"{score} баллов"
    tw = draw.textlength(sc_text, font=sc_font)
    draw.text(((W - tw) // 2 + 2, 1030 + 2), sc_text, font=sc_font, fill=(60, 30, 0))
    draw.text(((W - tw) // 2, 1030), sc_text, font=sc_font, fill=(255, 240, 150))

    buf = io.BytesIO()
    bg.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()


# =====================================================
#  ХЭНДЛЕРЫ
# =====================================================

# --- Состояния регистрации ---
REG = {}  # chat_id -> {"phase": "names"|"photo", "names": ...}


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка /start — в ЛС регистрация, в группе другая команда."""
    global ADMIN_ID
    chat = update.effective_chat
    user = update.effective_user

    # В групповом чате /start ничего не делает (зарегистрацию в ЛС)
    if chat.type in ("group", "supergroup"):
        await update.message.reply_text(
            "👋 Привет! Напиши мне в личку /start чтобы зарегаться.\n\n"
            "Админ: используй /setgroup в этом чате чтобы я мог отправлять сюда объявления."
        )
        return

    # Первый юзер = админ, если ADMIN_ID ещё не задан
    if ADMIN_ID is None:
        ADMIN_ID = user.id
        await update.message.reply_text(
            f"👑 Привет, {user.first_name}! Ты теперь админ вечеринки.\n"
            f"Твой ID: `{user.id}`",
            parse_mode="Markdown",
            reply_markup=admin_kb(),
        )
        return

    if is_admin(user.id):
        await update.message.reply_text("👑 Админ-панель:", reply_markup=admin_kb())
        return

    cid = chat.id
    if cid in participants:
        p = participants[cid]
        await update.message.reply_text(
            f"✅ Ты уже зареган как: **{p['names']}**",
            parse_mode="Markdown",
        )
        return

    REG[cid] = {"phase": "names"}
    await update.message.reply_text(
        "🎉 Привет! Это бот для игры на вечеринке.\n\n"
        "Напиши имя (или имена пары), например:\n"
        "• `Макс`\n"
        "• `Макс и Катя`",
        parse_mode="Markdown",
    )


async def cmd_setgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Регистрация группового чата для анонсов."""
    global group_chat_id
    if update.effective_chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Эту команду надо писать в групповом чате.")
        return
    if ADMIN_ID is not None and not is_admin(update.effective_user.id):
        await update.message.reply_text("Только админ может установить группу.")
        return
    group_chat_id = update.effective_chat.id
    _save_group(group_chat_id)
    await update.message.reply_text(
        f"✅ Группа зарегистрирована! Сюда буду кидать объявления о заданиях и результатах."
    )


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update.effective_user.id):
        await update.message.reply_text("👑 Админ-панель:", reply_markup=admin_kb())


# --- Приём сообщений (текст/фото) ---
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Универсальный обработчик — маршрутизирует в регистрацию, админ-ввод."""
    chat = update.effective_chat
    if chat.type != "private":
        return  # групповой чат обрабатывается отдельно
    uid = update.effective_user.id
    cid = chat.id

    # Админский ввод (кастомные задания, регистрация админа, отметка именинников)
    if is_admin(uid) and cid in admin_state:
        await _handle_admin_input(update, context)
        return

    # Регистрация обычного юзера
    if cid in REG:
        await _handle_registration(update, context)
        return


async def _handle_registration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    st = REG[cid]
    msg = update.message

    if st["phase"] == "names":
        if not msg.text:
            await msg.reply_text("Напиши имя текстом.")
            return
        names = msg.text.strip()
        if len(names) < 2 or len(names) > 80:
            await msg.reply_text("Имя слишком короткое или длинное. Попробуй ещё раз.")
            return
        st["names"] = names
        st["phase"] = "photo"
        await msg.reply_text(
            f"👍 **{names}**!\n\nТеперь пришли фото (для карточки победителя в финале).\n"
            f"Или напиши `скип` — без фото.",
            parse_mode="Markdown",
        )
        return

    if st["phase"] == "photo":
        photo_bytes = None
        if msg.photo:
            photo_file = await msg.photo[-1].get_file()
            buf = io.BytesIO()
            await photo_file.download_to_memory(buf)
            photo_bytes = buf.getvalue()
        elif msg.text and msg.text.strip().lower() in ("скип", "skip", "нет", "-"):
            photo_bytes = None
        else:
            await msg.reply_text("Пришли фото или напиши `скип`.", parse_mode="Markdown")
            return

        participants[cid] = new_participant(st["names"], photo_bytes)
        REG.pop(cid, None)
        _save()
        status = "с фото ✨" if photo_bytes else "без фото"
        await msg.reply_text(
            f"✅ Зарегали тебя: **{st['names']}** ({status})\n\n"
            f"Жди когда начнётся игра! 🎉",
            parse_mode="Markdown",
        )


# --- Админский ввод (имена/фото/кастомное задание) ---
async def _handle_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    msg = update.message
    st = admin_state.get(uid)
    if not st:
        return

    phase = st.get("phase")

    if phase == "reg_names":
        if not msg.text:
            return
        names = msg.text.strip()
        if len(names) < 2 or len(names) > 80:
            await msg.reply_text("Коротко или длинно. Попробуй ещё.")
            return
        st["names"] = names
        st["phase"] = "reg_photo"
        await msg.reply_text(
            f"👍 **{names}**! Теперь фото (или `скип`).",
            parse_mode="Markdown",
        )
        return

    if phase == "reg_photo":
        photo_bytes = None
        if msg.photo:
            f = await msg.photo[-1].get_file()
            buf = io.BytesIO()
            await f.download_to_memory(buf)
            photo_bytes = buf.getvalue()
        elif msg.text and msg.text.strip().lower() in ("скип", "skip", "нет", "-"):
            photo_bytes = None
        else:
            await msg.reply_text("Фото или `скип`.", parse_mode="Markdown")
            return
        st["photo"] = photo_bytes
        st["phase"] = "reg_bday"
        await msg.reply_text(
            f"Почти готово! Ты **именинник**?\n\n"
            f"_(Именинники не получают заданий — только голосуют и оценивают)_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🎂 Да, я именинник!", callback_data="adm_reg_bday_yes"),
                InlineKeyboardButton("🙂 Нет, я играю", callback_data="adm_reg_bday_no"),
            ]]),
        )
        return

    if phase == "custom_text":
        if not msg.text:
            await msg.reply_text("Напиши текст задания.")
            return
        target_cid = st["target_cid"]
        mission = msg.text.strip()
        admin_state.pop(uid, None)
        await _start_task(context.bot, target_cid, mission, from_admin=True)
        await msg.reply_text(
            f"✅ Задание отправлено {participants[target_cid]['names']}!",
            reply_markup=admin_kb(),
        )
        return


# =====================================================
#  ЛОГИКА ЗАДАНИЙ
# =====================================================

async def _start_task(bot, executor_cid: int, mission: str, from_admin: bool = False):
    """Начать задание — отправить исполнителю в ЛС, анонсировать в группу."""
    global state, current_task
    state = "task_pending"
    current_task = {
        "executor_cid": executor_cid,
        "mission": mission,
        "from_admin": from_admin,
        "phase": "waiting_executor",
        "votes_yes": [],
        "votes_no": [],
        "ratings": {},
    }
    _save()

    names = participants[executor_cid]["names"]

    # Исполнителю в ЛС
    try:
        await bot.send_message(
            chat_id=executor_cid,
            text=(
                f"🎲 **Тебе выпало задание!**\n\n"
                f"_{mission}_\n\n"
                f"Выполнишь или сольёшься?"
            ),
            parse_mode="Markdown",
            reply_markup=executor_kb(),
        )
    except Exception as e:
        logger.error(f"send task err: {e}")

    # В группу — анонс (без имён голосующих)
    prefix = "🎯 Секретное задание от админа!" if from_admin else "🎲 Новое задание!"
    await announce(
        bot,
        f"{prefix}\n\n"
        f"🎤 Выпало: **{names}**\n"
        f"📜 _{mission}_\n\n"
        f"⏳ Ждём реакции...",
    )


async def _pick_random_task():
    """Выбрать случайное неиспользованное основное задание."""
    available = [i for i in range(len(MAIN_TASKS)) if i not in used_main_tasks]
    if not available:
        # все использованы — сбрасываем
        used_main_tasks.clear()
        available = list(range(len(MAIN_TASKS)))
    idx = random.choice(available)
    used_main_tasks.append(idx)
    return MAIN_TASKS[idx]


async def _pick_random_easy():
    available = [i for i in range(len(EASY_TASKS)) if i not in used_easy_tasks]
    if not available:
        used_easy_tasks.clear()
        available = list(range(len(EASY_TASKS)))
    idx = random.choice(available)
    used_easy_tasks.append(idx)
    return EASY_TASKS[idx]


async def _pick_random_duel():
    available = [i for i in range(len(DUEL_TASKS)) if i not in used_duel_tasks]
    if not available:
        used_duel_tasks.clear()
        available = list(range(len(DUEL_TASKS)))
    idx = random.choice(available)
    used_duel_tasks.append(idx)
    return DUEL_TASKS[idx]


async def _ask_voting(bot):
    """Разослать голосование «правда выполнил?» всем кроме исполнителя."""
    global state
    state = "task_voting"
    current_task["phase"] = "voting"
    _save()

    executor_cid = current_task["executor_cid"]
    names = participants[executor_cid]["names"]
    mission = current_task["mission"]

    for cid in all_voters_except(executor_cid):
        try:
            await bot.send_message(
                chat_id=cid,
                text=(
                    f"🗳 **Голосование**\n\n"
                    f"**{names}** говорит что выполнил(а):\n"
                    f"_{mission}_\n\n"
                    f"Правда?"
                ),
                parse_mode="Markdown",
                reply_markup=yes_no_kb("vote"),
            )
        except Exception as e:
            logger.error(f"voting msg err for {cid}: {e}")

    await announce(bot, f"🗳 **{names}** говорит что выполнил(а)!\nИдёт голосование...")


async def _ask_rating(bot):
    """Разослать оценку всем кроме исполнителя."""
    global state
    state = "task_rating"
    current_task["phase"] = "rating"
    _save()

    executor_cid = current_task["executor_cid"]
    names = participants[executor_cid]["names"]
    mission = current_task["mission"]

    for cid in all_voters_except(executor_cid):
        try:
            await bot.send_message(
                chat_id=cid,
                text=(
                    f"⭐ **Оцени выступление**\n\n"
                    f"**{names}** — _{mission}_\n\n"
                    f"От 1 (кринж) до 5 (огонь):"
                ),
                parse_mode="Markdown",
                reply_markup=rating_kb(),
            )
        except Exception as e:
            logger.error(f"rating msg err for {cid}: {e}")

    await announce(bot, f"⭐ Голосование прошло! Все ставят оценку **{names}**...")


async def _close_task_vote(bot):
    """Админ закрыл голосование. Подвести итог."""
    if not current_task or current_task["phase"] != "voting":
        return
    yes = len(current_task["votes_yes"])
    no = len(current_task["votes_no"])
    executor_cid = current_task["executor_cid"]
    names = participants[executor_cid]["names"]

    if no > yes:
        # Не засчитано
        global state
        state = "idle"
        _save()
        try:
            await bot.send_message(
                chat_id=executor_cid,
                text=f"❌ Народ проголосовал что ты не выполнил. Задание не засчитано. Баллы не начисляются.",
            )
        except Exception:
            pass
        await announce(bot, f"❌ Народ не поверил! Задание **{names}** не засчитано.\n(Да: {yes}, Нет: {no})")
        await broadcast_live_table(bot)
        _reset_current_task()
    else:
        # Засчитано — переходим к оценкам
        await announce(bot, f"✅ Народ поверил! (Да: {yes}, Нет: {no})")
        await _ask_rating(bot)


async def _close_task_rating(bot):
    """Админ закрыл оценки. Подвести итог, начислить баллы."""
    global state, first_completed_done
    if not current_task or current_task["phase"] != "rating":
        return
    executor_cid = current_task["executor_cid"]
    names = participants[executor_cid]["names"]
    mission = current_task["mission"]
    ratings = list(current_task["ratings"].values())

    if not ratings:
        # никто не оценил — ставим 3 по умолчанию от админа, либо 0
        total = 0
    else:
        total = sum(ratings)

    p = participants[executor_cid]
    p["score"] += total
    p["tasks_done"] += 1
    p["streak"] += 1
    p["ratings_received"].append(sum(ratings) / len(ratings) if ratings else 0)

    # сохранить оценки от каждого в ratings_given
    for voter_cid, r in current_task["ratings"].items():
        participants[voter_cid]["ratings_given"].append(r)

    mission_history.append({
        "mission": mission,
        "executor": names,
        "score": total,
    })

    _save()

    # Ачивки
    if not first_completed_done:
        first_completed_done = True
        await grant_achievement(executor_cid, "🥇 Первопроходец", bot)
    if p["streak"] >= 3:
        await grant_achievement(executor_cid, "💪 Железный", bot)
    if p["tasks_done"] >= 5:
        await grant_achievement(executor_cid, "🎭 Актёр", bot)
    if ratings and sum(ratings) / len(ratings) >= 5.0:
        await grant_achievement(executor_cid, "⭐ Пятёрочник", bot)

    state = "idle"
    _save()

    avg = (sum(ratings) / len(ratings)) if ratings else 0
    try:
        await bot.send_message(
            chat_id=executor_cid,
            text=f"🎉 Задание засчитано!\n\n+{total} баллов\nСредняя оценка: {avg:.1f}\nВсего у тебя: {p['score']} баллов.",
        )
    except Exception:
        pass

    await announce(
        bot,
        f"✅ **{names}** выполнил(а) задание!\n"
        f"📊 Сумма оценок: **+{total}**\n"
        f"⭐ Средняя: {avg:.1f}\n"
        f"Итого у {names}: **{p['score']}** баллов"
    )
    await broadcast_live_table(bot)
    _reset_current_task()


def _reset_current_task():
    global current_task
    current_task = None
    _save()


async def _handle_refusal(bot, executor_cid: int):
    """Обработка отказа: штраф, предложение рулетки."""
    global state
    p = participants[executor_cid]
    names = p["names"]
    mission = current_task["mission"] if current_task else "?"
    penalty = random.randint(10, 200)
    p["penalty_rub"] += penalty
    p["score"] -= 3
    p["tasks_refused"] += 1
    p["streak"] = 0
    _save()

    state = "idle"
    _save()

    # Сохраняем контекст для рулетки
    roulette_available = not p["roulette_used"]

    try:
        txt = (
            f"❌ Ты отказался(ась) от задания.\n\n"
            f"💸 Штраф в общак: **{penalty} ₽**\n"
            f"📉 −3 балла (итого: {p['score']})\n"
        )
        if roulette_available:
            txt += "\n🎰 Есть шанс: крутни рулетку — получишь лёгкое задание. Выполнишь — штраф и минус баллы спишутся!"
            await bot.send_message(chat_id=executor_cid, text=txt, parse_mode="Markdown",
                                   reply_markup=roulette_kb())
        else:
            await bot.send_message(chat_id=executor_cid, text=txt, parse_mode="Markdown")
    except Exception:
        pass

    await announce(
        bot,
        f"💸 **{names}** слил(а)сь!\n"
        f"Штраф {penalty}₽ в общак, −3 балла.\n"
        f"_Задание было:_ _{mission}_"
    )
    await broadcast_live_table(bot)
    _reset_current_task()


# =====================================================
#  ДУЭЛЬ
# =====================================================

async def _start_duel(bot):
    """Запустить дуэль — два случайных не-именинника."""
    global state, current_duel
    candidates = active_non_birthday()
    if len(candidates) < 2:
        return False
    p1_cid, p2_cid = random.sample(candidates, 2)
    mission = await _pick_random_duel()

    state = "duel_pending"
    current_duel = {
        "p1_cid": p1_cid,
        "p2_cid": p2_cid,
        "mission": mission,
        "p1_done": None,  # None | "done" | "refuse"
        "p2_done": None,
        "votes": {},  # cid -> "p1"|"p2"|"tie"
    }
    _save()

    p1_names = participants[p1_cid]["names"]
    p2_names = participants[p2_cid]["names"]

    for role, cid, opponent in [("p1", p1_cid, p2_names), ("p2", p2_cid, p1_names)]:
        try:
            await bot.send_message(
                chat_id=cid,
                text=(
                    f"⚔️ **ДУЭЛЬ!**\n\n"
                    f"Твой соперник — **{opponent}**\n\n"
                    f"📜 _{mission}_\n\n"
                    f"Выполняй и жми «Готов»!"
                ),
                parse_mode="Markdown",
                reply_markup=duel_executor_kb(role),
            )
        except Exception as e:
            logger.error(f"duel msg err: {e}")

    await announce(
        bot,
        f"⚔️ **ДУЭЛЬ!**\n\n"
        f"**{p1_names}** 🆚 **{p2_names}**\n\n"
        f"📜 _{mission}_"
    )
    return True


async def _check_duel_ready(bot):
    """После ответа обоих — либо голосование либо закрытие."""
    if not current_duel:
        return
    p1_r = current_duel["p1_done"]
    p2_r = current_duel["p2_done"]
    if p1_r is None or p2_r is None:
        return  # ждём

    p1_cid = current_duel["p1_cid"]
    p2_cid = current_duel["p2_cid"]
    p1_names = participants[p1_cid]["names"]
    p2_names = participants[p2_cid]["names"]

    global state
    # Оба отказались
    if p1_r == "refuse" and p2_r == "refuse":
        for cid, names in [(p1_cid, p1_names), (p2_cid, p2_names)]:
            pen = random.randint(10, 200)
            participants[cid]["penalty_rub"] += pen
            participants[cid]["score"] -= 3
            participants[cid]["tasks_refused"] += 1
            participants[cid]["streak"] = 0
            try:
                await bot.send_message(cid, f"❌ Дуэль провалена. Штраф {pen}₽, −3 балла.")
            except Exception:
                pass
        await announce(bot, f"💩 Оба слились! И {p1_names}, и {p2_names} получили штрафы.")
        await broadcast_live_table(bot)
        state = "idle"
        _reset_duel()
        return

    # Один отказался — второй автопобедитель
    if p1_r == "refuse" or p2_r == "refuse":
        loser_cid = p1_cid if p1_r == "refuse" else p2_cid
        winner_cid = p2_cid if p1_r == "refuse" else p1_cid
        pen = random.randint(10, 200)
        participants[loser_cid]["penalty_rub"] += pen
        participants[loser_cid]["score"] -= 3
        participants[loser_cid]["tasks_refused"] += 1
        participants[loser_cid]["streak"] = 0
        participants[winner_cid]["score"] += 10
        participants[winner_cid]["duels_won"] += 1
        await grant_achievement(winner_cid, "🗡 Дуэлянт", bot)
        _save()
        await announce(
            bot,
            f"⚔️ Дуэль закончилась!\n"
            f"💸 {participants[loser_cid]['names']} слил(а)сь — штраф {pen}₽\n"
            f"🏆 Автопобеда **{participants[winner_cid]['names']}** (+10)"
        )
        await broadcast_live_table(bot)
        state = "idle"
        _reset_duel()
        return

    # Оба готовы — запускаем голосование
    state = "duel_voting"
    _save()

    for cid in participants.keys():
        if cid == p1_cid or cid == p2_cid:
            continue
        try:
            await bot.send_message(
                chat_id=cid,
                text=(
                    f"⚔️ Кто круче выполнил дуэль?\n\n"
                    f"**{p1_names}** 🆚 **{p2_names}**\n\n"
                    f"_{current_duel['mission']}_"
                ),
                parse_mode="Markdown",
                reply_markup=duel_vote_kb(p1_names, p2_names),
            )
        except Exception as e:
            logger.error(f"duel vote send err: {e}")

    await announce(bot, f"✅ Оба выполнили! Идёт голосование за лучшего 🗳")


async def _close_duel_vote(bot):
    """Админ закрыл голосование дуэли — подсчитать победителя."""
    global state
    if not current_duel:
        return
    votes = current_duel["votes"]
    p1_votes = sum(1 for v in votes.values() if v == "p1")
    p2_votes = sum(1 for v in votes.values() if v == "p2")
    ties = sum(1 for v in votes.values() if v == "tie")

    p1_cid = current_duel["p1_cid"]
    p2_cid = current_duel["p2_cid"]
    p1_names = participants[p1_cid]["names"]
    p2_names = participants[p2_cid]["names"]

    if p1_votes > p2_votes:
        winner, loser = p1_cid, p2_cid
        w_name, l_name = p1_names, p2_names
    elif p2_votes > p1_votes:
        winner, loser = p2_cid, p1_cid
        w_name, l_name = p2_names, p1_names
    else:
        winner = None

    if winner is not None:
        participants[winner]["score"] += 10
        participants[winner]["duels_won"] += 1
        participants[winner]["tasks_done"] += 1
        participants[winner]["streak"] += 1
        participants[loser]["score"] += 2
        participants[loser]["tasks_done"] += 1
        participants[loser]["streak"] += 1
        await grant_achievement(winner, "🗡 Дуэлянт", bot)
        _save()
        await announce(
            bot,
            f"⚔️ **Победитель дуэли: {w_name}** 🏆\n"
            f"(+10 баллов)\n\n"
            f"{l_name}: +2 балла за участие\n"
            f"Голоса: {w_name} {max(p1_votes, p2_votes)} — {l_name} {min(p1_votes, p2_votes)}, ничья: {ties}"
        )
    else:
        # Ничья
        for cid in (p1_cid, p2_cid):
            participants[cid]["score"] += 5
            participants[cid]["tasks_done"] += 1
            participants[cid]["streak"] += 1
        _save()
        await announce(
            bot,
            f"🤝 **Ничья!** {p1_names} и {p2_names} получают по +5.\n"
            f"Голоса: {p1_names} {p1_votes} — {p2_names} {p2_votes}, ничья: {ties}"
        )

    await broadcast_live_table(bot)
    state = "idle"
    _reset_duel()


def _reset_duel():
    global current_duel
    current_duel = None
    _save()


# =====================================================
#  CALLBACK HANDLER (нажатия кнопок)
# =====================================================

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Единая точка обработки inline-кнопок."""
    q = update.callback_query
    await q.answer()
    data = q.data
    uid = q.from_user.id
    cid = q.message.chat_id
    bot = context.bot

    if data == "noop":
        return

    # === ФИНАЛ РЕГИСТРАЦИИ АДМИНА: ИМЕНИННИК? ===
    if data.startswith("adm_reg_bday_") and is_admin(uid):
        st = admin_state.get(uid)
        if not st or st.get("phase") != "reg_bday":
            await q.edit_message_text("⏱ Регистрация уже завершена.", reply_markup=admin_kb())
            return
        is_bday = data == "adm_reg_bday_yes"
        p = new_participant(st["names"], st.get("photo"))
        p["is_birthday"] = is_bday
        participants[uid] = p
        admin_state.pop(uid, None)
        _save()
        tag = "🎂 именинник" if is_bday else "🙂 игрок"
        await q.edit_message_text(
            f"✅ Зареган: **{st['names']}** ({tag})",
            parse_mode="Markdown",
            reply_markup=admin_kb(),
        )
        return

    # === АДМИНСКИЕ ===
    if data.startswith("adm_") and is_admin(uid):
        await _on_admin_cb(q, context, data)
        return

    if data.startswith("del_") and is_admin(uid):
        await _on_delete_cb(q, context, data)
        return

    if data.startswith("mark_") and is_admin(uid):
        await _on_mark_cb(q, context, data)
        return

    if data.startswith("custom_") and is_admin(uid):
        await _on_custom_cb(q, context, data)
        return

    # === ИСПОЛНИТЕЛЬ ЗАДАНИЯ ===
    if data == "exec_done":
        if not current_task or current_task["executor_cid"] != cid:
            await q.edit_message_text("⏱ Это задание больше не актуально.")
            return
        if current_task["phase"] != "waiting_executor":
            return
        await q.edit_message_text("👍 Жди голосования от остальных.")
        await _ask_voting(bot)
        return

    if data == "exec_refuse":
        if not current_task or current_task["executor_cid"] != cid:
            await q.edit_message_text("⏱ Это задание больше не актуально.")
            return
        if current_task["phase"] != "waiting_executor":
            return
        await q.edit_message_text("😢 Принято, отказ.")
        await _handle_refusal(bot, cid)
        return

    # === РУЛЕТКА ===
    if data == "roulette_spin":
        p = participants.get(cid)
        if not p or p["roulette_used"]:
            await q.edit_message_text("Рулетка уже использована.")
            return
        p["roulette_used"] = True
        _save()
        easy = await _pick_random_easy()
        # Сохраняем в user_data инфу про рулетку
        context.user_data["roulette_mission"] = easy
        await q.edit_message_text(
            f"🎰 Твоё лёгкое задание:\n\n_{easy}_\n\nСделал?",
            parse_mode="Markdown",
            reply_markup=roulette_done_kb(),
        )
        return

    if data == "roulette_skip":
        await q.edit_message_text("Ок, штраф остаётся. Удачи в следующем!")
        return

    if data == "roulette_done":
        p = participants.get(cid)
        if not p:
            return
        # Возвращаем штраф (последний добавленный) и баллы, +1 бонус
        # Для простоты: возвращаем 3 балла, бонус +1, штраф обнуляем
        # Но penalty_rub — суммарный. Вычтем последний штраф — не знаем.
        # Упростим: просто восстановим −3 → +3 и +1 бонус; рубли не возвращаем (в общаге остаются)
        p["score"] += 4  # −3 (вернули) + 1 бонус = +4
        await grant_achievement(cid, "🎰 Феникс", bot)
        _save()
        await q.edit_message_text(
            f"🎉 Молодец! Баллы за отказ возвращены + бонус.\n"
            f"Итого у тебя: {p['score']} баллов."
        )
        await announce(bot, f"🎰 **{p['names']}** отыгрался(ась) через рулетку! Уважение 🔥")
        await broadcast_live_table(bot)
        return

    if data == "roulette_fail":
        p = participants.get(cid)
        if not p:
            return
        pen = random.randint(10, 200)
        p["penalty_rub"] += pen
        _save()
        await q.edit_message_text(
            f"😔 Ну бывает. Дополнительный штраф: **{pen}₽**", parse_mode="Markdown"
        )
        await announce(bot, f"💸 **{p['names']}** не справился(ась) с рулеткой — ещё {pen}₽ в общак.")
        await broadcast_live_table(bot)
        return

    # === ГОЛОСОВАНИЕ ЗА ЗАДАНИЕ ===
    if data in ("vote_yes", "vote_no"):
        if not current_task or current_task["phase"] != "voting":
            await q.edit_message_text("⏱ Голосование уже закрыто.")
            return
        if cid == current_task["executor_cid"]:
            await q.answer("Ты же и есть исполнитель!", show_alert=True)
            return
        # Убираем из обоих списков сначала
        if cid in current_task["votes_yes"]:
            current_task["votes_yes"].remove(cid)
        if cid in current_task["votes_no"]:
            current_task["votes_no"].remove(cid)
        if data == "vote_yes":
            current_task["votes_yes"].append(cid)
        else:
            current_task["votes_no"].append(cid)
        _save()
        total_voters = len(all_voters_except(current_task["executor_cid"]))
        voted = len(current_task["votes_yes"]) + len(current_task["votes_no"])
        await q.edit_message_text(f"✅ Твой голос: {'Да' if data == 'vote_yes' else 'Нет'}\n\nПроголосовало: {voted}/{total_voters}")

        # Если проголосовали все — автозакрытие
        if voted >= total_voters:
            await _close_task_vote(bot)
        return

    # === ОЦЕНКА ===
    if data.startswith("rate_"):
        if not current_task or current_task["phase"] != "rating":
            await q.edit_message_text("⏱ Оценки уже закрыты.")
            return
        if cid == current_task["executor_cid"]:
            return
        try:
            rating = int(data.split("_")[1])
        except Exception:
            return
        current_task["ratings"][cid] = rating
        _save()
        total_voters = len(all_voters_except(current_task["executor_cid"]))
        voted = len(current_task["ratings"])
        await q.edit_message_text(f"⭐ Поставил: {rating}\n\nОценили: {voted}/{total_voters}")

        if voted >= total_voters:
            await _close_task_rating(bot)
        return

    # === ДУЭЛЬ: ответ исполнителей ===
    if data.startswith("duel_p") and ("_done" in data or "_refuse" in data):
        if not current_duel:
            await q.edit_message_text("⏱ Дуэль больше не активна.")
            return
        role = data.split("_")[1]  # p1 или p2
        action = data.split("_")[2]  # done/refuse
        my_cid = current_duel[f"{role}_cid"]
        if cid != my_cid:
            await q.answer("Это не ваша дуэль.", show_alert=True)
            return
        current_duel[f"{role}_done"] = "done" if action == "done" else "refuse"
        _save()
        await q.edit_message_text(f"{'👍 Готов!' if action == 'done' else '😢 Отказ принят.'}")
        await _check_duel_ready(bot)
        return

    # === ДУЭЛЬ: голосование ===
    if data.startswith("dvote_"):
        if not current_duel or state != "duel_voting":
            await q.edit_message_text("⏱ Голосование закрыто.")
            return
        if cid == current_duel["p1_cid"] or cid == current_duel["p2_cid"]:
            await q.answer("Вы участник дуэли — не голосуете.", show_alert=True)
            return
        choice = data.split("_")[1]  # p1|p2|tie
        current_duel["votes"][cid] = choice
        _save()
        total = len(participants) - 2
        voted = len(current_duel["votes"])
        await q.edit_message_text(f"✅ Голос учтён.\n\nПроголосовало: {voted}/{total}")
        if voted >= total:
            await _close_duel_vote(bot)
        return


# --- Обработчики отдельных админских callback'ов ---

async def _on_admin_cb(q, context, data):
    global state, current_task, current_duel, used_main_tasks, used_easy_tasks, used_duel_tasks
    global mission_history, first_completed_done
    bot = context.bot
    uid = q.from_user.id

    if data == "adm_reg":
        admin_state[uid] = {"phase": "reg_names"}
        await q.edit_message_text("📝 Напиши имя (или имена пары).")
        return

    if data == "adm_mark_birthday":
        if not participants:
            await q.edit_message_text("Никого нет.", reply_markup=admin_kb())
            return
        rows = []
        for cid, p in participants.items():
            mark = "⭐" if p.get("is_birthday") else "  "
            rows.append([InlineKeyboardButton(f"{mark} {p['names']}", callback_data=f"mark_{cid}")])
        rows.append([InlineKeyboardButton("◀️ Назад", callback_data="adm_back")])
        await q.edit_message_text(
            "⭐ Тыкай на именинников (повторно = убрать):",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    if data == "adm_people":
        if not participants:
            await q.edit_message_text("� Пока никого нет.", reply_markup=admin_kb())
            return
        await q.edit_message_text(
            f"👥 **Участники ({len(participants)}):**\nТыкни 🗑 чтобы удалить.",
            parse_mode="Markdown",
            reply_markup=people_kb(),
        )
        return

    if data == "adm_back":
        await q.edit_message_text("👑 Админ-панель:", reply_markup=admin_kb())
        return

    if data == "adm_new_task":
        candidates = active_non_birthday()
        if len(candidates) < 1:
            await q.edit_message_text("Нет кандидатов (все именинники или никого).", reply_markup=admin_kb())
            return
        if state != "idle":
            await q.edit_message_text(
                f"⏱ Сейчас уже идёт процесс: {state}. Заверши текущее!",
                reply_markup=admin_kb()
            )
            return
        executor_cid = random.choice(candidates)
        mission = await _pick_random_task()
        await _start_task(bot, executor_cid, mission)
        await q.edit_message_text(
            f"✅ Задание отправлено **{participants[executor_cid]['names']}**!",
            parse_mode="Markdown",
            reply_markup=admin_kb()
        )
        return

    if data == "adm_duel":
        if state != "idle":
            await q.edit_message_text(f"⏱ Сейчас идёт: {state}", reply_markup=admin_kb())
            return
        ok = await _start_duel(bot)
        if not ok:
            await q.edit_message_text("❌ Нужно минимум 2 не-именинника.", reply_markup=admin_kb())
        else:
            await q.edit_message_text("⚔️ Дуэль запущена!", reply_markup=admin_kb())
        return

    if data == "adm_custom":
        if state != "idle":
            await q.edit_message_text(f"⏱ Сейчас идёт: {state}", reply_markup=admin_kb())
            return
        if not participants:
            await q.edit_message_text("Никого нет.", reply_markup=admin_kb())
            return
        rows = []
        for cid, p in participants.items():
            if p.get("is_birthday"):
                continue
            rows.append([InlineKeyboardButton(p["names"], callback_data=f"custom_{cid}")])
        rows.append([InlineKeyboardButton("◀️ Назад", callback_data="adm_back")])
        await q.edit_message_text("🎯 Кому кастомное задание?", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data == "adm_close_vote":
        if state == "task_voting":
            await _close_task_vote(bot)
            await q.edit_message_text("🔒 Голосование закрыто.", reply_markup=admin_kb())
        elif state == "duel_voting":
            await _close_duel_vote(bot)
            await q.edit_message_text("🔒 Голосование дуэли закрыто.", reply_markup=admin_kb())
        else:
            await q.edit_message_text(f"Нет активного голосования (state={state}).", reply_markup=admin_kb())
        return

    if data == "adm_close_rating":
        if state == "task_rating":
            await _close_task_rating(bot)
            await q.edit_message_text("🔒 Оценки закрыты.", reply_markup=admin_kb())
        else:
            await q.edit_message_text(f"Нет активных оценок (state={state}).", reply_markup=admin_kb())
        return

    if data == "adm_scores":
        await q.edit_message_text(_format_scoreboard(), parse_mode="Markdown", reply_markup=admin_kb())
        return

    if data == "adm_finish":
        # Показать предупреждение
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Да, завершаем", callback_data="adm_finish_confirm")],
            [InlineKeyboardButton("❌ Нет", callback_data="adm_back")],
        ])
        await q.edit_message_text("🏁 Завершить вечеринку? Бот объявит победителя.", reply_markup=kb)
        return

    if data == "adm_finish_confirm":
        await _finish_party(bot)
        await q.edit_message_text("🏁 Готово!", reply_markup=admin_kb())
        return

    if data == "adm_reset":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Сбросить ВСЁ", callback_data="adm_reset_confirm")],
            [InlineKeyboardButton("❌ Нет", callback_data="adm_back")],
        ])
        await q.edit_message_text("🔄 Сбросить всё?", reply_markup=kb)
        return

    if data == "adm_reset_confirm":
        participants.clear()
        state = "idle"
        current_task = None
        current_duel = None
        used_main_tasks = []
        used_easy_tasks = []
        used_duel_tasks = []
        mission_history = []
        first_completed_done = False
        _save()
        await q.edit_message_text("🔄 Всё сброшено.", reply_markup=admin_kb())
        return


async def _on_delete_cb(q, context, data):
    try:
        target = int(data.split("_", 1)[1])
    except Exception:
        return
    if target in participants:
        name = participants[target]["names"]
        del participants[target]
        _save()
        if participants:
            await q.edit_message_text(
                f"✅ {name} удалён(а).\n\n👥 **Участники ({len(participants)}):**",
                parse_mode="Markdown",
                reply_markup=people_kb(),
            )
        else:
            await q.edit_message_text(f"✅ {name} удалён(а). Больше никого.", reply_markup=admin_kb())
    else:
        await q.edit_message_text("Не найден.", reply_markup=admin_kb())


async def _on_mark_cb(q, context, data):
    try:
        target = int(data.split("_", 1)[1])
    except Exception:
        return
    if target in participants:
        participants[target]["is_birthday"] = not participants[target].get("is_birthday", False)
        _save()
        # перерисуем список
        rows = []
        for cid, p in participants.items():
            mark = "⭐" if p.get("is_birthday") else "  "
            rows.append([InlineKeyboardButton(f"{mark} {p['names']}", callback_data=f"mark_{cid}")])
        rows.append([InlineKeyboardButton("◀️ Назад", callback_data="adm_back")])
        await q.edit_message_text(
            "⭐ Тыкай на именинников (повторно = убрать):",
            reply_markup=InlineKeyboardMarkup(rows),
        )


async def _on_custom_cb(q, context, data):
    try:
        target = int(data.split("_", 1)[1])
    except Exception:
        return
    if target not in participants:
        return
    uid = q.from_user.id
    admin_state[uid] = {"phase": "custom_text", "target_cid": target}
    await q.edit_message_text(
        f"📝 Напиши задание для **{participants[target]['names']}**:",
        parse_mode="Markdown",
    )


# =====================================================
#  ФИНАЛ + ТАБЛИЦЫ
# =====================================================

def _format_scoreboard() -> str:
    """Текущие баллы."""
    if not participants:
        return "Никого нет."
    items = [(cid, p) for cid, p in participants.items() if not p.get("is_birthday")]
    items.sort(key=lambda x: -x[1]["score"])
    lines = ["📈 **Текущие баллы:**\n"]
    for i, (cid, p) in enumerate(items, 1):
        lines.append(f"{i}. {p['names']} — **{p['score']}** (✅{p['tasks_done']} ❌{p['tasks_refused']})")
    # именинники отдельно
    bdays = [p for p in participants.values() if p.get("is_birthday")]
    if bdays:
        lines.append("\n⭐ Именинники: " + ", ".join(p["names"] for p in bdays))
    # общак
    total_rub = sum(p["penalty_rub"] for p in participants.values())
    lines.append(f"\n💰 Общак: **{total_rub} ₽**")
    return "\n".join(lines)


def _format_live_table() -> str:
    """Компактная лайв-таблица для общего чата после каждого события."""
    items = [(cid, p) for cid, p in participants.items() if not p.get("is_birthday")]
    if not items:
        return ""
    items.sort(key=lambda x: -x[1]["score"])
    lines = ["📊 **Турнирная таблица:**"]
    for i, (cid, p) in enumerate(items, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
        lines.append(f"{medal} {p['names']} — **{p['score']}**")
    total_rub = sum(p["penalty_rub"] for p in participants.values())
    if total_rub > 0:
        lines.append(f"\n💰 Общак: **{total_rub} ₽**")
    return "\n".join(lines)


async def broadcast_live_table(bot):
    """Отправить лайв-таблицу в групповой чат."""
    text = _format_live_table()
    if text:
        await announce(bot, text)


async def _finish_party(bot):
    """Завершение — победитель + номинации + золотое задание."""
    global state
    state = "finished"
    _save()

    if not participants:
        await announce(bot, "Некого награждать. Никого нет.")
        return

    non_bdays = [(cid, p) for cid, p in participants.items() if not p.get("is_birthday")]
    if not non_bdays:
        await announce(bot, "Все именинники 😊 Нет кого награждать в соревновании.")
        return

    non_bdays.sort(key=lambda x: -x[1]["score"])
    winner_cid, winner_p = non_bdays[0]

    # Картинка победителя
    try:
        card = generate_winner_card(winner_p.get("photo"), winner_p["names"], winner_p["score"])
        await announce(
            bot,
            f"🎉🎉🎉 **ПОБЕДИТЕЛЬ ВЕЧЕРА: {winner_p['names']}**\n\n"
            f"👑 С результатом **{winner_p['score']} баллов!**\n\n"
            f"Поздравляем!",
            photo=card,
        )
    except Exception as e:
        logger.error(f"winner card err: {e}")
        await announce(
            bot,
            f"🎉 Победитель: **{winner_p['names']}** — {winner_p['score']} баллов!"
        )

    # Таблица мест
    lines = ["🏆 **Итоговая таблица:**\n"]
    for i, (cid, p) in enumerate(non_bdays, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
        lines.append(f"{medal} **{p['names']}** — {p['score']} баллов")
    await announce(bot, "\n".join(lines))

    # Номинации
    await _announce_nominations(bot, non_bdays)

    # Золотое задание
    if mission_history:
        top = max(mission_history, key=lambda m: m["score"])
        await announce(
            bot,
            f"⭐ **Золотое задание вечера:**\n\n"
            f"_{top['mission']}_\n\n"
            f"🎤 Исполнил(а): **{top['executor']}** — {top['score']} баллов!"
        )

    # Общак
    total_rub = sum(p["penalty_rub"] for p in participants.values())
    await announce(bot, f"💰 **В общак собрано: {total_rub} ₽**")

    # Именинники
    bdays = [p for p in participants.values() if p.get("is_birthday")]
    if bdays:
        names_str = ", ".join(p["names"] for p in bdays)
        await announce(bot, f"🎂 С Днём Рождения, **{names_str}**! 🎉")

    # Ачивки каждому в ЛС
    for cid, p in participants.items():
        if p["achievements"]:
            ach_text = "\n".join(p["achievements"])
            try:
                await bot.send_message(
                    chat_id=cid,
                    text=f"🎖 **Твои ачивки за вечер:**\n\n{ach_text}",
                    parse_mode="Markdown",
                )
            except Exception:
                pass


async def _announce_nominations(bot, non_bdays):
    """6 номинаций."""
    lines = ["🏆 **Номинации вечера:**\n"]

    # Главный герой
    lines.append(f"🏆 **Главный герой:** {non_bdays[0][1]['names']} — {non_bdays[0][1]['score']} баллов")

    # Слизень — больше всего отказов
    slizen = max(non_bdays, key=lambda x: x[1]["tasks_refused"])
    if slizen[1]["tasks_refused"] > 0:
        lines.append(f"💸 **Главный слизень:** {slizen[1]['names']} — {slizen[1]['tasks_refused']} отказов")

    # Чемпион по заданиям
    champ = max(non_bdays, key=lambda x: x[1]["tasks_done"])
    if champ[1]["tasks_done"] > 0:
        lines.append(f"🎭 **Чемпион по заданиям:** {champ[1]['names']} — {champ[1]['tasks_done']} выполнено")

    # Самый строгий судья (низкая средняя поставленная)
    judges = [(cid, p) for cid, p in participants.items() if p["ratings_given"]]
    if judges:
        strict = min(judges, key=lambda x: sum(x[1]["ratings_given"]) / len(x[1]["ratings_given"]))
        avg = sum(strict[1]["ratings_given"]) / len(strict[1]["ratings_given"])
        lines.append(f"😈 **Самый строгий судья:** {strict[1]['names']} — средняя {avg:.1f}")

        # Добряк
        nice = max(judges, key=lambda x: sum(x[1]["ratings_given"]) / len(x[1]["ratings_given"]))
        avg2 = sum(nice[1]["ratings_given"]) / len(nice[1]["ratings_given"])
        lines.append(f"🎁 **Добряк:** {nice[1]['names']} — средняя {avg2:.1f}")

    # Король дуэлей
    duelers = [(cid, p) for cid, p in non_bdays if p["duels_won"] > 0]
    if duelers:
        king = max(duelers, key=lambda x: x[1]["duels_won"])
        lines.append(f"🗡 **Король дуэлей:** {king[1]['names']} — побед: {king[1]['duels_won']}")

    # Душа партии — больше всего пятёрок получил (ratings_received, но там средние, нужно считать иначе)
    # Посчитаем сколько раз получал 5 — в текущих данных мы не храним. Пропускаем или упрощаем:
    # Считаем по ratings_received[средние], кто чаще выше 4.5
    top_rated = [(cid, p) for cid, p in non_bdays if p["ratings_received"]]
    if top_rated:
        dusha = max(top_rated, key=lambda x: sum(r >= 4.5 for r in x[1]["ratings_received"]))
        count5 = sum(r >= 4.5 for r in dusha[1]["ratings_received"])
        if count5 > 0:
            lines.append(f"👑 **Душа партии:** {dusha[1]['names']} — {count5} топовых выступлений")
            # ачивка
            await grant_achievement(dusha[0], "👑 Душа партии", bot)

    # Анти-ачивка слизню
    if slizen[1]["tasks_refused"] > 0:
        await grant_achievement(slizen[0], "💸 Слизень", bot)

    await announce(bot, "\n".join(lines))


# =====================================================
#  MAIN
# =====================================================

def main():
    if not BOT_TOKEN:
        print("❌ Задай BOT_TOKEN в переменных окружения!")
        return

    _load()
    _load_group()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("setgroup", cmd_setgroup))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO) & ~filters.COMMAND, on_message
    ))

    print("🎉 Party Game Bot запущен!")
    print(f"   Админ ID: {ADMIN_ID}")
    print(f"   Group chat: {group_chat_id}")
    print(f"   Участников: {len(participants)}")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
