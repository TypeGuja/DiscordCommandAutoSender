#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Discord Bump Bot – полностью автоматический режим.
Поддерживает:
  • двойной пробел после знаков пунктуации (настраиваемо);
  • корректное определение bump‑сообщения (защита от копирования кода);
  • планирование /up, /bump, /like;
  • меню, горячую клавишу, журнал.
"""

# ----------------------------------------------------------------------
# IMPORTS
# ----------------------------------------------------------------------
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
import unicodedata
from dataclasses import dataclass, asdict, field
from datetime import datetime
from threading import Lock
from typing import Any, Dict, List, Optional

import keyboard
import pyautogui
import pyperclip
import pygetwindow as gw

import platform  # ← новое

# ----------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------
SCHEDULE_FILE = "schedule.json"
RESPONSES_FILE = "responses.json"
LOG_FILE = "bot.log"                     # '' → отключить запись в файл
HOTKEY = "f12"                           # клавиша для вызова меню
MESSAGE_SCAN_RETRIES = 5                 # попыток копировать весь чат (Ctrl+A)
TARGET_CHANNEL_NAME = "⁠🍀└・up-like"      # частичное совпадение названия канала

# ---- копирование -------------------------------------------------------
COPY_METHOD = "context_menu"             # "context_menu" | "ctrl_a"
COPY_HOTKEY = "c"                        # клавиша в контекст‑меню (обычно «c»)
COPY_CONTEXT_OFFSET_RATIO = 0.12         # доля от высоты окна до точки клика

# ---- двойной пробел ----------------------------------------------------
DOUBLE_SPACE_ENABLED = True               # включить двойной пробел после .!? и в конце

# ----------------------------------------------------------------------
# GLOBAL STATE
# ----------------------------------------------------------------------
state_lock = Lock()
task_counter = 0

scheduled_tasks: List[Dict[str, Any]] = []   # отложенные однократные команды
bump_tasks: List[Dict[str, Any]] = []      # задачи автопарсинга (только в текущей сессии)

# ----------------------------------------------------------------------
# LOGGING HELPERS
# ----------------------------------------------------------------------
def _log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    print(line)
    if LOG_FILE:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def log_info(msg: str) -> None:            _log(msg, "INFO")
def log_success(msg: str) -> None:        _log(f"✅ {msg}", "SUCCESS")
def log_error(msg: str) -> None:           _log(f"❌ {msg}", "ERROR")
def log_warn(msg: str) -> None:            _log(f"⚠️ {msg}", "WARNING")
def log_debug(msg: str) -> None:           _log(f"🔍 {msg}", "DEBUG")
def log_status(msg: str) -> None:          _log(msg, "STATUS")


def _now_str() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ----------------------------------------------------------------------
# TIME UTILITIES
# ----------------------------------------------------------------------
def format_seconds(seconds: int) -> str:
    """Приводит количество секунд к виду «Xч Yм Zs»."""
    if seconds < 0:
        return "не определено"
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    parts = []
    if h:
        parts.append(f"{h}ч")
    if m:
        parts.append(f"{m}м")
    if s or not parts:
        parts.append(f"{s}s")
    return " ".join(parts)


def parse_duration_to_seconds(text: str) -> Optional[int]:
    """
    Превращает строку вида «2 часа 5 минут 30 секунд», «2h 5m», «02:05:30» и т.п.
    в количество секунд.
    """
    try:
        # Убираем всё после первой запятой (чаще всего timestamp)
        if "," in text:
            text = text.split(",", 1)[0]

        s = text.lower()
        s = re.sub(r"[;:\.\,\(\)\[\]«»]", " ", s)
        s = re.sub(r"\b(и|в|на|c|со|cо|с)\b", " ", s)
        s = re.sub(r"\s+", " ", s).strip()

        total = 0
        unit_map = {"ч": 3600, "h": 3600,
                    "м": 60,   "m": 60,
                    "с": 1,    "s": 1}

        # «число + слово», где слово начинается с ч/м/с (или h/m/s)
        for m in re.finditer(r"(\d+)\s*([a-zа-яё]+)", s):
            num = int(m.group(1))
            first = m.group(2)[0]
            if first in unit_map:
                total += unit_map[first] * num
            else:
                log_debug(f"Неизвестная единица: «{m.group(2)}»")

        # Если ничего не найдено – пробуем «чистые» числа (HH:MM:SS, MM:SS, SS)
        if total == 0:
            nums = list(map(int, re.findall(r"\d+", s)))
            if len(nums) >= 3:
                total = nums[0] * 3600 + nums[1] * 60 + nums[2]
            elif len(nums) == 2:
                total = nums[0] * 60 + nums[1]
            elif len(nums) == 1:
                total = nums[0]

        return total if total > 0 else None
    except Exception as e:
        log_error(f"Ошибка парсинга длительности: {e}")
        return None


# ----------------------------------------------------------------------
# WINDOW / CHANNEL HELPERS
# ----------------------------------------------------------------------
def _normalize_str(s: str) -> str:
    """Нормализует строку (удаляет пробелы, невидимые символы, нижний регистр)."""
    s = unicodedata.normalize("NFKC", s)
    filtered = "".join(ch for ch in s
                       if not (ch.isspace() or unicodedata.category(ch) in ("Cf", "Zs", "Zl", "Zp", "Cc")))
    return filtered.lower()


def _channel_is_target_from_title(title: str) -> bool:
    if not TARGET_CHANNEL_NAME:
        return True
    target = _normalize_str(TARGET_CHANNEL_NAME)
    return target in _normalize_str(title)


def find_discord_window() -> Optional[Any]:
    """Ищет открытое окно Discord."""
    try:
        for w in gw.getWindowsWithTitle("Discord"):
            if "Discord" in w.title:
                log_debug(f"Окно Discord найдено: {w.title}")
                return w
        return None
    except Exception as e:
        log_error(f"Ошибка поиска окна Discord: {e}")
        return None


def _channel_is_target() -> bool:
    """Проверяем, что активное окно относится к нужному каналу."""
    win = find_discord_window()
    if not win:
        log_debug("Окно Discord не найдено → считаем канал корректным")
        return True
    return _channel_is_target_from_title(win.title)


# ----------------------------------------------------------------------
# DOUBLE‑SPACE HELPERS
# ----------------------------------------------------------------------
def _apply_double_space(text: str) -> str:
    """
    Добавляет второй пробел после знаков пунктуации .!? и в конец строки.
    """
    # двойной пробел после . ! ?
    text = re.sub(r'([.!?])\s+', lambda m: f"{m.group(1)}  ", text)
    # гарантируем двойной пробел в конце
    if not text.endswith("  "):
        text = f"{text}  "
    return text


# ----------------------------------------------------------------------
# HELPER FUNCTIONS: раскладка и валидация команд
# ----------------------------------------------------------------------
def switch_to_english_layout() -> None:
    """
    Переключает раскладку клавиатуры на английскую (Windows).
    Если у вас используется другое сочетание переключения,
    замените строку `keyboard.send('alt+shift')` на нужное.
    """
    if platform.system() != "Windows":
        return
    # По умолчанию в Windows переключение – Alt+Shift.
    keyboard.send('alt+shift')
    time.sleep(0.2)


def _validate_command(cmd: str) -> bool:
    """
    Проверка команды перед записью в расписание.
    Возвращает True, если команда выглядит корректно:
      • не пустая;
      • не содержит символа '|';
      • если начинается с '/' или '!' – считается slash‑командой.
    """
    if not isinstance(cmd, str) or not cmd:
        return False
    if "|" in cmd:
        return False
    if cmd.startswith("/") or cmd.startswith("!"):
        return True
    # Допускаем любые обычные тексты (например, произвольные сообщения)
    return True


# ----------------------------------------------------------------------
# COPY HELPERS (контекст‑меню и fallback Ctrl+A)
# ----------------------------------------------------------------------
def _looks_like_real_bump(text: str) -> bool:
    """
    Проверка, что в тексте действительно есть bump‑сообщение.
    Требования:
      • строка начинается с эмодзи (например, :SDC:)
      • после эмодзи сразу /up, /bump или /like
      • в конце строки – timestamp HH:MM:SS
    """
    pattern = re.compile(
        r":\w+:\s*(/up|/bump|/like)\b.*\b\d{2}:\d{2}:\d{2}\b",
        re.IGNORECASE
    )
    return any(pattern.search(line) for line in text.splitlines())


def _copy_via_context_menu() -> Optional[str]:
    """Копирует последнее сообщение через контекст‑меню."""
    win = find_discord_window()
    if not win:
        log_error("Окно Discord не найдено")
        return None

    try:
        win.activate()
        time.sleep(0.3)

        # Прокручиваем в конец чата
        for _ in range(3):
            pyautogui.press('end')
            time.sleep(0.1)

        left, top, width, height = map(int, (win.left, win.top, win.width, win.height))
        # позиция чуть выше низа окна (настраивается коэффициентом)
        click_y = top + height - int(height * COPY_CONTEXT_OFFSET_RATIO)
        pyautogui.moveTo(left + width // 2, click_y, duration=0.2)
        log_debug(f"Клик в контекст‑меню: ({left + width // 2}, {click_y})")
        pyautogui.rightClick()
        time.sleep(0.2)

        pyautogui.press(COPY_HOTKEY)
        time.sleep(0.2)

        copied = pyperclip.paste()
        if not copied:
            log_warn("Контекст‑меню ничего не скопировало")
            return None

        log_debug("Скопировано (контекст‑меню) – первые 200 символов:")
        log_debug(copied[:200] + ("…" if len(copied) > 200 else ""))

        if _looks_like_real_bump(copied):
            return copied
        else:
            log_warn("Контекст‑меню скопировало не‑bump сообщение")
            return None
    except Exception as e:
        log_error(f"Контекст‑меню упало: {e}")
        return None


def _copy_via_ctrl_a() -> Optional[str]:
    """Копирует всё в окне Discord через Ctrl+A → Ctrl+C."""
    log_status("ПОИСК BUMP‑СООБЩЕНИЯ (Ctrl+A fallback)")
    win = find_discord_window()
    if win:
        try:
            win.activate()
            time.sleep(0.5)
        except Exception:
            pass

    original_clip = pyperclip.paste()

    try:
        for attempt in range(1, MESSAGE_SCAN_RETRIES + 1):
            log_info(f"Попытка {attempt}/{MESSAGE_SCAN_RETRIES}")

            if win:
                left, top, width, height = map(int, (win.left, win.top, win.width, win.height))
                pyautogui.moveTo(left + width // 2, top + height // 2, duration=0.1)
            else:
                w, h = pyautogui.size()
                pyautogui.moveTo(w // 2, h // 2, duration=0.1)

            pyautogui.click()
            time.sleep(0.2)

            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.15)
            pyautogui.hotkey("ctrl", "c")
            time.sleep(0.25)

            copied = pyperclip.paste()
            if copied and _looks_like_real_bump(copied):
                log_success("Bump‑сообщение найдено через Ctrl+A")
                return copied

            if copied:
                log_debug("Скопировано (первые 300):")
                log_debug(copied[:300] + ("…" if len(copied) > 300 else ""))

            time.sleep(0.5)

        log_error("Не удалось найти bump‑сообщение после всех попыток")
        return None
    finally:
        try:
            pyperclip.copy(original_clip)
        except Exception:
            pass


def get_last_bump_message() -> Optional[str]:
    """
    Пытаемся получить bump‑сообщение:
      1) через контекст‑меню (если выбран метод);
      2) fallback – Ctrl+A.
    Возвращает текст только если он прошёл проверку `_looks_like_real_bump`.
    """
    if COPY_METHOD == "context_menu":
        block = _copy_via_context_menu()
        if block:
            log_success("Bump‑сообщение найдено через контекст‑меню")
            return block
        else:
            log_warn("Контекст‑меню не дало валидного сообщения → переходим к Ctrl+A")

    return _copy_via_ctrl_a()


# ----------------------------------------------------------------------
# MESSAGE EXTRACTION & PARSING
# ----------------------------------------------------------------------
COMMAND_PATTERNS = [
    r"/up", r"/bump", r"/like",
    r"!\s*up", r"!\s*bump", r"!\s*like"
]

_COMMAND_REGEX = re.compile("|".join(COMMAND_PATTERNS), re.IGNORECASE)


def extract_latest_bump_message(full_text: str) -> Optional[str]:
    """
    Ищет в полном тексте последние строки, где:
      • есть одна из команд (/up, /bump, /like);
      • после команды есть хотя бы одна цифра.
    Возвращает до 5 последних подходящих строк, объединённых «\n».
    """
    if not full_text:
        return None

    lines = [ln.rstrip() for ln in full_text.splitlines() if ln.strip()]

    # команда + цифра после неё
    cmd_digit = re.compile(r"(?:/up|/bump|/like).*?\d", re.IGNORECASE)
    candidate = [ln for ln in lines if cmd_digit.search(ln)]

    if not candidate:
        log_debug("Не найдено строк с командами, за которыми идут цифры")
        return None

    return "\n".join(candidate[-5:])


def is_bump_message(text: str) -> bool:
    """Проверка: содержит /up|/bump|/like и цифру после команды."""
    return bool(re.search(r"(?:/up|/bump|/like).*?\d", text, re.IGNORECASE))


def _extract_time_from_line(line: str) -> Optional[int]:
    """
    Из строки с найденной командой вытаскивает количество секунд.
    """
    match = re.search(r"(?i)(/up|/bump|/like|!\s*up|!\s*bump|!\s*like)", line)
    if not match:
        return None

    after = line[match.end():].strip(" :‑–—,.;|#")
    if ',' in after:
        after = after.split(',', 1)[0]

    return parse_duration_to_seconds(after)


def parse_time_from_message(message_text: str) -> Dict[str, Any]:
    """
    Принимает полный текст сообщения (ответ /remaining и т.п.) и
    возвращает словарь:
        {
            "/up":   int|None,
            "/bump": int|None,
            "/like": int|None,
            "success": bool
        }
    """
    block = extract_latest_bump_message(message_text)
    if not block:
        log_error("Не найден блок с командами /up /bump /like")
        return {"/up": None, "/bump": None, "/like": None, "success": False}

    result: Dict[str, Optional[int]] = {"/up": None, "/bump": None, "/like": None}
    for cmd in ("/up", "/bump", "/like"):
        for line in block.splitlines():
            if re.search(rf"(?i){re.escape(cmd)}", line):
                secs = _extract_time_from_line(line)
                if secs is not None:
                    result[cmd] = secs
                    log_success(f"{cmd} → {format_seconds(secs)} (парсер)")
                else:
                    log_warn(f"Не удалось распарсить время из строки: «{line}»")
                break

    success = any(v is not None for v in result.values())
    result["success"] = success
    log_debug(f"Результат парсинга: {result}")
    return result  # type: ignore[return-value]


# ----------------------------------------------------------------------
# PERSISTENCE (schedule.json, responses.json)
# ----------------------------------------------------------------------
def load_schedule() -> None:
    global scheduled_tasks
    try:
        with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)

        # Фильтрация записей с некорректными командами
        filtered: List[Dict[str, Any]] = []
        for t in raw:
            if isinstance(t, dict):
                cmd = t.get("command", "")
                if _validate_command(cmd):
                    filtered.append(t)
                else:
                    log_warn(f"Удалена некорректная задача из расписания: {t}")
            else:
                log_warn(f"Неправильный формат записи в расписании (не dict): {t}")

        scheduled_tasks = filtered
        if len(raw) != len(filtered):
            log_warn("В расписании обнаружены и удалены некорректные задачи")
            save_schedule()

        log_success(f"Загружено {len(scheduled_tasks)} задач из {SCHEDULE_FILE}")
    except FileNotFoundError:
        scheduled_tasks = []
        log_info("Файл расписания не найден – стартуем с пустым списком")
    except Exception as e:
        scheduled_tasks = []
        log_error(f"Ошибка загрузки расписания: {e}")


def save_schedule() -> None:
    try:
        with open(SCHEDULE_FILE, "w", encoding="utf-8") as f:
            json.dump(scheduled_tasks, f, ensure_ascii=False, indent=2)
        log_debug("Расписание успешно сохранено")
    except Exception as e:
        log_error(f"Не удалось сохранить расписание: {e}")


def load_responses() -> None:
    global command_responses
    try:
        with open(RESPONSES_FILE, "r", encoding="utf-8") as f:
            command_responses = json.load(f)
        log_success(f"Загружено {len(command_responses)} пользовательских ответов")
    except FileNotFoundError:
        command_responses = {}
        log_info("Файл ответов не найден – стартуем без пользовательских шаблонов")
    except Exception as e:
        command_responses = {}
        log_error(f"Ошибка загрузки ответов: {e}")


def save_responses() -> None:
    try:
        with open(RESPONSES_FILE, "w", encoding="utf-8") as f:
            json.dump(command_responses, f, ensure_ascii=False, indent=2)
        log_debug("Ответы сохранены")
    except Exception as e:
        log_error(f"Не удалось сохранить ответы: {e}")


# ----------------------------------------------------------------------
# MESSAGE SENDING HELPERS (typewrite → clipboard fallback)
# ----------------------------------------------------------------------
def _send_via_typewrite(text: str) -> bool:
    """Пытается набрать строку через pyautogui.typewrite."""
    try:
        pyautogui.typewrite(text, interval=0.02)
        pyautogui.press("enter")
        return True
    except Exception as e:
        log_debug(f"typewrite failed: {e}")
        return False


def _send_via_clipboard(text: str, double_enter: bool = False) -> bool:
    """Отправка сообщения через буфер обмена – гарантированный способ."""
    original = pyperclip.paste()
    try:
        pyperclip.copy(text)
        time.sleep(0.1)
        pyautogui.hotkey("ctrl", "v")
        pyautogui.press("enter")
        if double_enter:
            pyautogui.press("enter")
        return True
    finally:
        try:
            pyperclip.copy(original)
        except Exception:
            pass


def send_message(text: str,
                 double_enter: bool = False,
                 double_space: bool = False) -> bool:
    """
    Пытается «ввести» `text` в активное окно Discord.
    1) Если сообщение начинается с «/» – используем clipboard‑fallback,
       чтобы гарантировать правильный слеш независимо от раскладки.
    2) Иначе – сначала пробуем typewrite, а при неудаче – тоже clipboard.
    `double_enter` → нажать Enter дважды.
    `double_space` → добавить двойной пробел после пунктуации и в конце.
    """
    log_info(f"Отправка сообщения: '{text[:40]}…'")

    # Применяем двойной пробел, если включено глобально или передано явно
    if DOUBLE_SPACE_ENABLED or double_space:
        text = _apply_double_space(text)

    # Если команда – отправляем через буфер обмена (гарантия корректного слеша)
    if text.startswith("/"):
        log_debug("Сообщение начинается с '/', используем clipboard‑fallback")
        return _send_via_clipboard(text, double_enter)

    # Пытаемся набрать обычным способом; если не получается – переходим к clipboard
    if _send_via_typewrite(text):
        log_success("Сообщение отправлено (typewrite)")
        return True

    log_debug("typewrite не удался → используем clipboard")
    return _send_via_clipboard(text, double_enter)


# ----------------------------------------------------------------------
# SCHEDULED TASK MANAGEMENT
# ----------------------------------------------------------------------
def _schedule_parsed_commands(task: Dict[str, Any]) -> None:
    """Получив времена из bump‑сообщения, планируем отдельные /up /bump /like."""
    now = time.time()
    added = 0

    for cmd in task["commands_to_schedule"]:
        secs = task["parsed_times"].get(cmd)
        if not secs:
            log_warn(f"Нет времени для {cmd} в распарсенных данных")
            continue

        exec_time = now + secs + 10           # 10‑сек «подушка» после кулдауна
        subtask = {
            "id": f"bump_{task['id']}_{cmd}",
            "time": exec_time,
            "command": cmd,
            "double_enter": task["double_enter"],
            "double_space": DOUBLE_SPACE_ENABLED,
            "source_task_id": task["id"],
            "status": "pending",
            "created_at": datetime.now().strftime("%H:%M:%S")
        }

        with state_lock:
            scheduled_tasks.append(subtask)
            task.setdefault("scheduled_subtasks", []).append(subtask)

        ts = datetime.fromtimestamp(exec_time).strftime("%H:%M:%S")
        left = format_seconds(int(exec_time - now))
        log_success(f"Запланировано {cmd} → {ts} (через {left})")
        added += 1

    if added:
        save_schedule()
        log_success(f"Всего запланировано {added} команд")
    else:
        log_error("Не удалось запланировать ни одной команды")


def execute_scheduled_tasks() -> None:
    """Выполняет задачи, время которых пришло."""
    now = time.time()
    completed: List[Dict[str, Any]] = []

    with state_lock:
        for task in scheduled_tasks:
            if task["status"] != "pending" or now < task["time"]:
                continue

            log_status(f"⚡ Выполнение: {task['command']}")
            if send_message(
                task["command"],
                task.get("double_enter", False),
                task.get("double_space", False)
            ):
                task["status"] = "executed"
                task["executed_at"] = now
                log_success(f"Команда {task['command']} выполнена")
            else:
                task["status"] = "error"
                log_error(f"Ошибка выполнения {task['command']}")
            completed.append(task)

        for t in completed:
            scheduled_tasks.remove(t)


def cleanup_old_tasks(max_age_seconds: int = 300) -> None:
    """Удаляет задачи, время которых уже прошло более `max_age_seconds` назад."""
    now = time.time()
    with state_lock:
        before = len(scheduled_tasks)
        scheduled_tasks[:] = [
            t for t in scheduled_tasks if t["time"] > now - max_age_seconds
        ]
        after = len(scheduled_tasks)
    if before != after:
        log_info(f"Удалено {before - after} устаревших задач")


# ----------------------------------------------------------------------
# BUMP‑TASK MANAGEMENT (автопарсинг)
# ----------------------------------------------------------------------
def add_bump_parse_task() -> None:
    """Создаёт задачу, автоматически парсит /remaining и планирует /up /bump /like."""
    log_status("Создание BUMP‑задачи")

    cmd = input(f"[{_now_str()}] Команда (по умолчанию /getbump): ").strip() or "/getbump"
    if not _validate_command(cmd):
        log_error("Команда некорректна (проверьте отсутствие символа '|').")
        return
    log_info(f"Команда: {cmd}")

    try:
        delay = int(input(f"[{_now_str()}] Задержка перед отправкой (сек, default 5): ").strip() or "5")
    except ValueError:
        delay = 5
    log_info(f"Задержка: {delay}s")

    print("""Какие команды планировать?
1 – /up, /bump, /like
2 – только /up
3 – только /bump
4 – только /like
5 – /up и /bump""")
    choice = input(f"[{_now_str()}] Выбор (1‑5): ").strip()
    mapping = {
        "1": ["/up", "/bump", "/like"],
        "2": ["/up"],
        "3": ["/bump"],
        "4": ["/like"],
        "5": ["/up", "/bump"],
    }
    commands_to_schedule = mapping.get(choice, ["/up", "/bump", "/like"])
    log_info(f"Будут запланированы: {', '.join(commands_to_schedule)}")

    double_enter = input(f"[{_now_str()}] Двойной Enter? (y/n, default n): ").lower() == "y"

    global task_counter
    with state_lock:
        task_id = task_counter
        task_counter += 1

    task = {
        "id": task_id,
        "command": cmd,
        "start_time": time.time() + delay,
        "commands_to_schedule": commands_to_schedule,
        "double_enter": double_enter,
        "double_space": DOUBLE_SPACE_ENABLED,
        "status": "waiting",
        "parsed_times": {},                # заполнится после парсинга
        "scheduled_subtasks": [],        # ссылки на подзадачи
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    bump_tasks.append(task)
    log_success(f"BUMP‑задача #{task_id} создана, старт через {delay}s")


def execute_bump_tasks() -> None:
    """Цикл, обслуживающий все активные BUMP‑задачи."""
    now = time.time()
    for task in bump_tasks[:]:
        status = task["status"]

        if status == "waiting" and now >= task["start_time"]:
            log_status(f"🚀 Запуск BUMP‑задачи #{task['id']}")
            task["status"] = "sending"

        elif status == "sending":
            if send_message(task["command"], task["double_enter"], task.get("double_space", False)):
                task["status"] = "waiting_response"
                task["response_deadline"] = now + 5
                log_info("⏳ Ожидаем ответ от bump‑бота...")
            else:
                task["status"] = "failed"
                log_error("Не удалось отправить стартовую команду")

        elif status == "waiting_response" and now >= task.get("response_deadline", 0):
            task["status"] = "reading"
            log_info("🔍 Переходим к чтению сообщения...")

        elif status == "reading":
            msg = get_last_bump_message()
            if msg:
                task["message"] = msg
                task["status"] = "parsing"
                log_success("Сообщение получено и скопировано")
            else:
                task["status"] = "failed"
                log_error("Не удалось скопировать bump‑сообщение")

        elif status == "parsing":
            parsed = parse_time_from_message(task.get("message", ""))
            if parsed.get("success"):
                task["parsed_times"] = parsed
                task["status"] = "scheduling"
                log_success("Парсинг прошёл успешно")
            else:
                task["status"] = "failed"
                log_error("Парсинг не удался")

        elif status == "scheduling":
            _schedule_parsed_commands(task)
            task["status"] = "completed"
            log_success(f"BUMP‑задача #{task['id']} завершена")

        elif status in ("failed", "completed"):
            bump_tasks.remove(task)


# ----------------------------------------------------------------------
# ONE‑TIME TASK (ручное планирование)
# ----------------------------------------------------------------------
def add_one_time_task() -> None:
    """Позволяет добавить произвольную команду, которая выполнится через N секунд."""
    cmd = input(f"[{_now_str()}] Введите команду: ").strip()
    if not _validate_command(cmd):
        log_error("Команда неприемлема (проверьте отсутствие символа '|', пустую строку и т.п.)")
        return
    try:
        delay = int(input(f"[{_now_str()}] Через сколько секунд выполнить? ").strip())
    except ValueError:
        log_error("Введите корректное число")
        return
    double_enter = input(f"[{_now_str()}] Двойной Enter? (y/n): ").lower() == "y"

    with state_lock:
        scheduled_tasks.append({
            "id": f"manual_{int(time.time())}",
            "time": time.time() + delay,
            "command": cmd,
            "double_enter": double_enter,
            "double_space": DOUBLE_SPACE_ENABLED,
            "status": "pending",
            "created_at": datetime.now().strftime("%H:%M:%S")
        })
        save_schedule()

    exec_ts = datetime.fromtimestamp(time.time() + delay).strftime("%H:%M:%S")
    log_success(f"Команда «{cmd}» запланирована на {exec_ts}")


# ----------------------------------------------------------------------
# MENU & UI
# ----------------------------------------------------------------------
def show_schedule() -> None:
    """Выводит текущий список отложенных задач."""
    log_status("ТЕКУЩЕЕ РАСПИСАНИЕ")
    with state_lock:
        if not scheduled_tasks:
            log_info("Нет запланированных задач")
            return

        now = time.time()
        for i, t in enumerate(scheduled_tasks, 1):
            left = max(0, int(t["time"] - now))
            ts = datetime.fromtimestamp(t["time"]).strftime("%H:%M:%S")
            log_info(f"{i}. {ts} (через {format_seconds(left)}): {t['command']}")


def show_bump_tasks() -> None:
    """Отображает список активных BUMP‑задач."""
    log_status("АКТИВНЫЕ BUMP‑ЗАДАЧИ")
    if not bump_tasks:
        log_info("Нет активных задач")
        return

    STATUS_MAP = {
        "waiting": "⏳ Ожидание старта",
        "sending": "📤 Отправка команды",
        "waiting_response": "⏳ Ожидание ответа",
        "reading": "🔍 Чтение сообщения",
        "parsing": "🔎 Парсинг",
        "scheduling": "📅 Планирование подзадач",
        "completed": "✅ Завершена",
        "failed": "❌ Ошибка",
    }

    for task in bump_tasks:
        print("\n" + "─" * 30)
        log_info(f"Задача #{task['id']}")
        log_info(f"Статус: {STATUS_MAP.get(task['status'], task['status'])}")
        log_info(f"Команда: {task['command']}")
        log_info(f"Создана: {task['created_at']}")
        if task.get("parsed_times"):
            log_info("Распарсенные времена:")
            for cmd in ("/up", "/bump", "/like"):
                secs = task["parsed_times"].get(cmd)
                if secs:
                    log_info(f"  {cmd}: {format_seconds(secs)}")
        if task.get("scheduled_subtasks"):
            log_info(f"Подзадач запланировано: {len(task['scheduled_subtasks'])}")


def test_parser() -> None:
    """Тестовый разбор заранее подготовленного сообщения."""
    log_status("ТЕСТ ПАРСИНГА")
    test_msg = """Времени до
:SDC: /up: 25 минут и 15 секунд, 17:24:25
:ServerMonitoring: /bump: 2 часа 36 минут и 35 секунд, 19:35:44
:DSMonitoring: /like: 3 часа 39 минут и 12 секунд, 20:38:22

Сообщения будут высылаться в канал: ⁠🍀└・up-like"""
    log_info("-" * 50)
    log_info(test_msg)
    log_info("-" * 50)

    res = parse_time_from_message(test_msg)
    if res.get("success"):
        log_success("Тест пройден")
        for cmd in ("/up", "/bump", "/like"):
            if res.get(cmd):
                log_info(f"{cmd}: {format_seconds(res[cmd])}")
    else:
        log_error("Тест НЕ пройден")


def show_logs() -> None:
    """Печатает последние 10 строк из лог‑файла."""
    log_status("ПОСЛЕДНИЕ 10 ЗАПИСЕЙ ЛОГА")
    if not LOG_FILE or not os.path.exists(LOG_FILE):
        log_warn("Лог‑файл не найден")
        return
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in lines[-10:]:
            log_info(line.rstrip())
    except Exception as e:
        log_error(f"Не удалось прочитать лог: {e}")


def cleanup_old_schedule() -> None:
    """Удаляет устаревшее (старше 5 минут) задачи из расписания."""
    cleanup_old_tasks()
    save_schedule()
    log_success("Устаревшие задачи удалены")


def show_menu() -> None:
    """Отображает главное меню и переадресует ввод."""
    log_status("ГЛАВОЕ МЕНЮ")
    options = [
        "1. 📅 Показать расписание",
        "2. ➕ Добавить разовую команду",
        "3. 🔄 Добавить BUMP‑задачу (автопарсинг)",
        "4. 📊 Показать активные BUMP‑задачи",
        "5. 🔍 Тестировать парсер",
        "6. 📋 Показать последние записи лога",
        "7. 🧹 Очистить устаревшее расписание",
        "8. 🚪 Выход"
    ]
    for opt in options:
        log_info(opt)

    choice = input(f"[{_now_str()}] Выбор: ").strip()
    if choice == "1":
        show_schedule()
    elif choice == "2":
        add_one_time_task()
    elif choice == "3":
        add_bump_parse_task()
    elif choice == "4":
        show_bump_tasks()
    elif choice == "5":
        test_parser()
    elif choice == "6":
        show_logs()
    elif choice == "7":
        cleanup_old_schedule()
    elif choice == "8":
        log_success("Выход...")
        save_schedule()
        save_responses()
        sys.exit(0)
    else:
        log_warn("Неверный пункт меню")


# ----------------------------------------------------------------------
# MAIN LOOP
# ----------------------------------------------------------------------
def main_loop() -> None:
    log_status("БОТ ЗАПУЩЕН")
    log_info(f"Нажмите {HOTKEY.upper()} для вызова меню")
    last_cleanup = time.time()

    try:
        while True:
            # Открываем меню по горячей клавише
            if keyboard.is_pressed(HOTKEY):
                log_status("Открываю меню")
                show_menu()
                # небольшая задержка, чтобы не «залипнуть» на клавише
                time.sleep(0.5)

            execute_scheduled_tasks()
            execute_bump_tasks()

            if time.time() - last_cleanup > 60:
                cleanup_old_tasks()
                last_cleanup = time.time()

            time.sleep(0.1)
    except KeyboardInterrupt:
        log_status("Остановка пользователем")
    except Exception as e:
        log_error(f"Критическая ошибка: {e}\n{traceback.format_exc()}")
    finally:
        save_schedule()
        save_responses()
        log_success("Работа завершена")


# ----------------------------------------------------------------------
# ENTRY POINT
# ----------------------------------------------------------------------
def main() -> None:
    print("\n" + "=" * 60)
    print("🤖 DISCORD BUMP BOT (автоматический режим) – полная переработка")
    print("=" * 60)

    pyautogui.PAUSE = 0.05          # ускоряем набор текста
    pyautogui.FAILSAFE = False      # отключаем «выход» движением мыши в угол

    load_schedule()
    load_responses()

    log_status("ИНСТРУКЦИЯ")
    log_info("1. Откройте Discord и перейдите в канал с bump‑ботом.")
    log_info(f"2. Нажмите {HOTKEY.upper()} для вызова меню.")
    log_info("3. Для автопарсинга выберите пункт «Добавить BUMP‑задачу».")
    log_info("4. Для ручного планирования – «Добавить разовую команду».")
    log_info("ВАЖНО: Окно Discord должно быть открыто и находиться на переднем плане!")
    input(f"\n[{_now_str()}] Нажмите Enter, чтобы запустить…")

    main_loop()


if __name__ == "__main__":
    main()