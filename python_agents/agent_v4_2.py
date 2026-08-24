#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent_v4_2.py — ИИ-агент v4.2 «ОТКРЫВАШКА»: программы открываются надёжно.

Фикс v4.1: раньше "открой firefox" модель делала как хотела — snap-программа
просыпается 20-60 секунд, pgrep сразу после запуска пустой, модель паниковала
и кликала по доку (и промахивалась в соседний значок — привет, терминал).

Теперь запуск — детерминированное действие, а не импровизация:
  {"open": "firefox"}  — лаунчер САМ: проверяет что установлено, запускает,
                         ЖДЁТ до 45 секунд, подтверждает факт по pgrep,
                         активирует окно и докладывает только факты
  {"wait": 10}         — пауза 1-30 секунд (дождаться прогрузки окна/сайта)
Плюс правило: значки дока для запуска программ НЕ кликать — только {"open"}.

Всё остальное — наследие v4.1 «СОНАР»: scan/click без нейросети-зрения,
опциональные глаза (AGENT_VISION) с выгрузкой мозга,日记/уроки, /free поток.

Протокол: {"command":...} {"scan":""} {"click":[x,y]} {"open":"имя"}
          {"wait":N} {"see":"..."} (опция) {"done":"итог"}
Переменные: AGENT_MODEL, AGENT_VISION (по умолчанию ВЫКЛ), AGENT_UITREE,
            EYE_SWAP=0, SCAN=always, CONFIRM=1
Команды: задача | /free [N] | /memo | /exit
"""
import base64
import datetime
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = os.environ.get("OLLAMA_API", "http://localhost:11434/api/chat")
MODEL = os.environ.get("AGENT_MODEL", "mybrain")
VISION = os.environ.get("AGENT_VISION", "")       # "" = глаз нет (экономим RAM)
EYE_SWAP = os.environ.get("EYE_SWAP", "1") != "0"  # выгружать мозг на время взгляда
SCAN_ALWAYS = os.environ.get("SCAN", "") == "always"
HOME = os.path.expanduser("~")
UITREE = os.environ.get("AGENT_UITREE", os.path.join(HOME, "uitree.py"))
DIARY = os.path.join(HOME, "agent_diary.md")
NOTES = os.path.join(HOME, "agent_notes.txt")
EYE_SHOT = "/tmp/eye.png"
MAX_STEPS = 14
MAX_OUT = 1500
MAX_SCAN = 2600
FREE_BUDGET = 3
NET_RETRIES = 3
NET_WAIT = 5
CONFIRM = os.environ.get("CONFIRM", "") == "1"

DENY = ["rm -rf /", "rm -rf ~", "rm -rf *", "mkfs", "dd if=", "dd of=", "of=/dev/", ":(){",
        "shutdown", "reboot", "poweroff", "halt", "passwd", "useradd", "userdel",
        "chmod -r /", "chown -r /", "> /dev/sd", "sudo ", "su root", "apt ", "apt-get",
        "dpkg", "pip ", "pipx", "snap install", "crontab", "systemctl", "mknod",
        "nc ", "ncat ", "ssh ", "scp ", "kill -9 1", "killall gdm", "eval"]

NOISE = re.compile(
    r"Gtk-Message|Gtk-WARNING|Not loading module|atk-bridge|dbind|dconf|GLib-|"
    r"GLib-GObject|libva|vaapi|libEGL|Fontconfig|dbus-launch|Xlib.*extension|"
    r"^\s*WARNING:|QtWarning|sandboxing|Skipping unsupported|"
    r"^\(process:\d+\):|^\*\*", re.IGNORECASE)


def caps():
    eyes_line = ('- ГЛАЗА: {"see": "вопрос"} -> зрительная модель по скриншоту '
                 '(медленно: на время взгляда мозг выгружается из памяти)\n' if VISION else '')
    return ("""
Твои возможности:
- shell: файлы в ~, ~/Desktop, /tmp; ps, pgrep, ls, df, free, date
- ЗАПУСК ПРОГРАММ: действие {"open": "firefox"} — само запустит, дождётся (до 45 с) и скажет ФАКТ; nohup — только если open программу не знает
- Настройки GNOME на панели: gnome-control-center background | appearance | wifi | bluetooth | display | about
- обои: gsettings set org.gnome.desktop.background picture-uri file:///usr/share/backgrounds/<файл>
- СОНАР: {"scan": ""} -> список ВСЕХ видимых элементов экрана с ТОЧНЫМИ координатами; {"scan": "Settings"} — только окно со словом в названии
- КЛИКИ: {"click": [x, y]} -> клик в координаты из сонара (сеанс X11!)
- ПАУЗА: {"wait": 10} -> подождать 1-30 сек, пока окно/сайт прогружается
""" + eyes_line +
"""- xdotool: mousemove/key/type/windowactivate; getdisplaygeometry = разрешение экрана
- интернет-текст: curl https://html.duckduckgo.com/html/?q=<запрос>
Не пытайся: ставить пакеты, sudo, перезагружать.""")


def base_system():
    eyes_action = '{"see": "вопрос к глазам"}\n' if VISION else ''
    eyes_rule = ('- Содержимое картинок/фото на экране: {"see": "что на изображении"}.\n' if VISION else '')
    return (
        "Ты — автономный ИИ-агент внутри Ubuntu Linux с органом зрения «СОНАР». За шаг отвечаешь СТРОГО\n"
        "одним JSON-объектом (без пояснений, без markdown), выбрав ОДНО действие:\n"
        '{"command": "одна shell-команда"}\n'
        '{"scan": ""}  или  {"scan": "слово из названия окна"}\n'
        '{"click": [x, y]}\n'
        '{"open": "имя программы: firefox, файлы, настройки..."}\n'
        '{"wait": 10}\n'
        + eyes_action +
        '{"done": "итог по-русски"}\n'
        "\nПравила:\n"
        "- Одна простая команда/действие за раз. После действия получишь результат в \"OUTPUT:\".\n"
        "- \"Gtk-Message\" и библиотечные ворчания — шум, не ошибки. Успех — по фактам (pgrep, ls, cat).\n"
        "- Чтобы увидеть интерфейс — используй СОНАР, это бесплатно: {\"scan\": \"\"}.\n"
        "- Клик по кнопке: 1) {\"scan\": \"\"} 2) найди элемент в списке, возьми его координаты [x, y] "
        "3) {\"click\": [x, y]} 4) снова {\"scan\": \"\"} — проверь, что изменилось.\n"
        "- Координаты из сонара ТОЧНЫЕ — доверяй им, не выдумывай свои.\n"
        "- Открыть программу — ТОЛЬКО {\"open\": ...}; кликать значки панели ЗАПРЕЩЕНО (легко промахнуться в соседний значок).\n"
        "- Программы открываются 10-60 секунд: open сам дождётся и скажет факт. Потом можно {\"scan\": \"\"}.\n"
        + eyes_rule +
        "- Запустил программу -> pgrep -a -> факт есть -> {\"done\": ...}. Не повторяй команды.\n"
        "- Настоящая ошибка -> другой подход, сдаться лишь после 3 разных способов.\n"
        "- НИКОГДА не пиши OUTPUT сам. done — только при подтверждении факта."
    )


FREE_SYSTEM = """Ты — ИИ-агент в живом потоке действий. Придумай следующий этап:
- продолжение предыдущего (если развивается) или новое направление
- маленький, безопасный, проверяемый (1-5 действий), не повторяй дневник
- можно: файлы, обои, скриншоты, запуск программ, СОНАР+КЛИКИ по экрану (X11),
  поиск через html.duckduckgo.com
Ответь СТРОГО JSON: {"goal": "коротко"}"""

BANNER = (f"============================================================\n"
          f" ИИ-АГЕНТ v4.2 «ОТКРЫВАШКА» | мозг: {MODEL} | зрение: {VISION or 'ВЫКЛ (экономим RAM)'}"
          f"{' | АВТОСКАН' if SCAN_ALWAYS else ''}\n"
          f" Тихий по умолчанию. /free [N] поток | /memo дневник | /exit выход\n"
          f"============================================================")

_vision_state = {"ok": None}


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_file(path, tail=None):
    try:
        with open(path, encoding="utf-8") as f:
            data = f.read()
    except (FileNotFoundError, OSError):
        return ""
    return data[-tail:] if tail else data


def append_diary(text):
    try:
        with open(DIARY, "a", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass


def notes():
    return read_file(NOTES, 1800).strip()


def add_note(line):
    line = " ".join(line.strip().split())
    if not line or line.startswith("-"):
        return
    lines = read_file(NOTES).splitlines()[-14:]
    lines.append(f"- {line}")
    try:
        with open(NOTES, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


class NetDown(Exception):
    pass


def _post(model, payload, timeout=600):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(API, data=body, headers={"Content-Type": "application/json"})
    for attempt in range(1, NET_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            print(f"  [сеть] попытка {attempt}/{NET_RETRIES}: {e}. Жду {NET_WAIT}с...")
            if attempt == NET_RETRIES:
                raise NetDown(str(e))
            time.sleep(NET_WAIT)


def chat(messages, temp=0.2, num=220):
    r = _post(MODEL, {"model": MODEL, "messages": messages, "stream": False,
                      "options": {"temperature": temp, "num_predict": num}})
    return r["message"]["content"]


def vision_chat(question, image_path):
    with open(image_path, "rb") as f:
        img64 = base64.b64encode(f.read()).decode()
    # keep_alive=0 — выгрузить глаза сразу после взгляда (бережём 4 ГБ)
    r = _post(VISION, {"model": VISION, "stream": False, "keep_alive": 0,
                       "messages": [{"role": "user", "content": question, "images": [img64]}],
                       "options": {"temperature": 0.1, "num_predict": 250}},
              timeout=900)
    return r["message"]["content"]


def extract_json(text):
    t = text.strip().strip("`")
    if t.startswith("json"):
        t = t[4:].strip()
    i = t.find("{")
    if i == -1:
        raise ValueError("no json")
    obj, _ = json.JSONDecoder().raw_decode(t[i:])
    if not isinstance(obj, dict):
        raise ValueError("not a dict")
    return obj


def filter_noise(text):
    lines = [ln for ln in text.splitlines() if not NOISE.search(ln)]
    clean = "\n".join(lines).strip()
    removed = len(text.splitlines()) - len(lines)
    if removed and not clean:
        clean = f"(команда выполнена; {removed} строк безобидных системных сообщений)"
    elif removed:
        clean += f"\n(…и {removed} строк безобидных системных сообщений)"
    return clean


def run(cmd):
    if any(b in cmd.lower() for b in DENY):
        return "ОШИБКА: команда заблокирована политикой безопасности."
    try:
        p = subprocess.run(cmd, shell=True, cwd=HOME, timeout=60,
                           capture_output=True, text=True)
        out = (p.stdout + p.stderr).strip() or "(команда выполнена, вывода нет)"
    except subprocess.TimeoutExpired:
        out = "ОШИБКА: команда не уложилась в 60 секунд."
    return filter_noise(out)[:MAX_OUT]


def run_scan(query):
    """Сонар: список видимых элементов экрана с координатами (без нейросети!)."""
    if not os.path.exists(UITREE):
        return (f"ОШИБКА: нет файла сонара {UITREE}. Перенеси uitree.py в домашнюю папку "
                "(тогда появится зрение без нейросети).")
    q = " ".join((query or "").split())[:40]
    cmd = f'python3 "{UITREE}"' + (f' "{q}"' if q else "")
    try:
        p = subprocess.run(cmd, shell=True, cwd=HOME, timeout=25,
                           capture_output=True, text=True)
        out = (p.stdout or "").strip()
        if p.returncode != 0 and p.stderr:
            out += "\n" + p.stderr.strip()[:300]
        out = out or "(сонар: пусто)"
    except subprocess.TimeoutExpired:
        return ("ОШИБКА: сонар завис (>25 с). Попробуй {\"scan\": \"слово\"} "
                "с названием конкретного окна.")
    if len(out) > MAX_SCAN:
        out = out[:MAX_SCAN] + "\n...(обрезано — уточни scan словом из названия окна)"
    return out



OPEN_APPS = {
    "firefox": ("firefox", "firefox"), "браузер": ("firefox", "firefox"),
    "файрфокс": ("firefox", "firefox"),
    "терминал": ("gnome-terminal", "gnome-terminal"), "консоль": ("gnome-terminal", "gnome-terminal"),
    "cmd": ("gnome-terminal", "gnome-terminal"),
    "файлы": ("nautilus", "nautilus"), "проводник": ("nautilus", "nautilus"), "files": ("nautilus", "nautilus"),
    "настройки": ("gnome-control-center", "gnome-control-center"), "settings": ("gnome-control-center", "gnome-control-center"),
    "блокнот": ("gnome-text-editor", "gnome-text-editor"), "текст": ("gnome-text-editor", "gnome-text-editor"),
    "editor": ("gnome-text-editor", "gnome-text-editor"),
    "калькулятор": ("gnome-calculator", "gnome-calculator"),
    "магазин": ("snap-store", "snap-store"),
    "хром": ("google-chrome", "chrome"), "chrome": ("google-chrome", "chrome"),
}


def _running(pattern):
    try:
        p = subprocess.run(f"pgrep -a {pattern}", shell=True, timeout=8,
                           capture_output=True, text=True)
        return [l.strip() for l in p.stdout.splitlines() if l.strip()][:3]
    except Exception:
        return []


def do_open(name):
    """Детерминированный запуск программы: запустить -> ждать -> факт."""
    raw = " ".join(str(name or "").split()).lower()[:30]
    if not raw:
        return 'ОШИБКА: формат {"open": "имя программы"}, напр. {"open": "firefox"}'
    cmd, pat = OPEN_APPS.get(raw, (raw, raw))
    if not re.fullmatch(r"[a-z0-9_.+\-]+", cmd):
        return f'ОШИБКА: «{raw}» не похоже на имя программы (нужно одно слово).'
    have = subprocess.run(f"command -v {cmd}", shell=True, timeout=8,
                          capture_output=True, text=True).stdout.strip()
    if not have:
        return (f"ОШИБКА: программа «{cmd}» НЕ УСТАНОВЛЕНА (это факт). "
                f"Проверь альтернативы командой: ls /usr/share/applications | grep -i {pat[:8]} ; "
                f"или работай без неё.")
    procs = _running(pat)
    if not procs:
        try:
            subprocess.Popen(f"setsid nohup {cmd} >/dev/null 2>&1 &", shell=True)
        except Exception as e:
            return f"ОШИБКА запуска: {e}"
        print(f"          [open] запускаю {cmd}, жду до 45 с (snap-программы просыпаются долго)...")
        t0 = time.time()
        while time.time() - t0 < 45:
            time.sleep(4)
            procs = _running(pat)
            if procs:
                break
    if not procs:
        return (f"ОШИБКА: «{cmd}» не появился в процессах за 45 с. Проверь вручную: "
                f"выполни shell-команду «{cmd}» и прочитай её ошибку.")
    win = ""
    t1 = time.time()
    while time.time() - t1 < 20:
        try:
            p = subprocess.run(f'xdotool search --onlyvisible --name "{pat}"', shell=True,
                               timeout=8, capture_output=True, text=True)
            ids = p.stdout.split()
        except Exception:
            ids = []
        if ids:
            subprocess.run(f"xdotool windowactivate {ids[0]}", shell=True, timeout=8,
                           capture_output=True)
            win = ids[0]
            break
        time.sleep(4)
    pid_info = procs[0][:60]
    if win:
        return (f"ФАКТ: «{cmd}» запущен и окно АКТИВНО (id {win}; {pid_info}). "
                'Можно {"scan": "окно"} или работать клавиатурой.')
    return (f"ФАКТ: процесс «{cmd}» есть ({pid_info}), но видимого окна пока нет. "
            'Сделай {"wait": 10} и потом {"scan": ""}.')


def do_wait(val):
    try:
        n = int(float(str(val).strip()[:4]))
    except Exception:
        n = 5
    n = max(1, min(n, 30))
    print(f"          [пауза] жду {n} с...")
    time.sleep(n)
    return ('прошло ' + str(n) + ' с. Проверь факты: {"scan": ""} или pgrep -a <имя>.')


def geometry():
    try:
        p = subprocess.run("xdotool getdisplaygeometry", shell=True, timeout=10,
                           capture_output=True, text=True)
        parts = p.stdout.split()
        return int(parts[0]), int(parts[1])
    except Exception:
        return None, None


def take_shot():
    try:
        subprocess.run(f"gnome-screenshot -f {EYE_SHOT}", shell=True, timeout=30,
                       capture_output=True)
    except Exception:
        pass
    if os.path.exists(EYE_SHOT) and os.path.getsize(EYE_SHOT) > 1000:
        return EYE_SHOT
    return None


def unload_model(name):
    try:
        subprocess.run(f"ollama stop {name}", shell=True, timeout=30, capture_output=True)
    except Exception:
        pass


def look(question):
    """Настоящие глаза (опционально): скриншот -> маленькая зрительная модель."""
    if not VISION:
        return ('{"see"} не настроен — и это нормально при 4 ГБ RAM. '
                'Используй СОНАР: {"scan": ""} — он бесплатный и точный.')
    if _vision_state["ok"] is False:
        return ("ОШИБКА ГЛАЗ: зрительная модель недоступна. Работай сонаром: {\"scan\": \"\"}.")
    shot = take_shot()
    if not shot:
        _vision_state["ok"] = False
        return ("ОШИБКА ГЛАЗ: скриншот не получился (часто это Wayland — "
                "перелогинься в 'Ubuntu on Xorg'). Используй сонар.")
    w, h = geometry()
    geo = f"Разрешение экрана {w}x{h}." if w else "Разрешение неизвестно."
    q = (f"{geo} Вопрос: {question}\n"
         "Отвечай кратко по-русски. Если просят координаты — дай примерные x,y центра "
         "элемента в пикселях исходного изображения.")
    if EYE_SWAP:
        print("          [глаза] выгружаю мозг, чтобы влезли глаза...")
        unload_model(MODEL)
    print(f"          [глаза] смотрю на экран (вопрос: {question[:80]})...")
    try:
        ans = vision_chat(q, shot)
        _vision_state["ok"] = True
        return "ОТВЕТ ГЛАЗ: " + ans.strip()
    except NetDown as e:
        _vision_state["ok"] = False
        return f"ОШИБКА ГЛАЗ: сервер недоступен ({e}). Работай сонаром."
    except Exception as e:
        _vision_state["ok"] = False
        return (f"ОШИБКА ГЛАЗ: {e}. Возможно, модель '{VISION}' не установлена "
                "(ollama list). Работай сонаром.")


def do_click(x, y):
    w, h = geometry()
    try:
        xi, yi = int(x), int(y)
    except Exception:
        return "ОШИБКА: координаты должны быть числами."
    if w and (xi < 0 or yi < 0 or xi > w or yi > h):
        return f"ОШИБКА: клик ({xi},{yi}) за пределами экрана {w}x{h} — сделай scan и возьми координаты из сонара."
    try:
        subprocess.run(f"xdotool mousemove {xi} {yi} click 1", shell=True, timeout=10,
                       capture_output=True)
        return f"клик выполнен в ({xi},{yi})"
    except Exception as e:
        return f"ОШИБКА клика: {e}. Сеанс X11? (echo $XDG_SESSION_TYPE)"


def parse_click(val):
    if isinstance(val, (list, tuple)) and len(val) == 2:
        return val
    m = re.findall(r"-?\d+", str(val))
    if len(m) >= 2:
        return int(m[0]), int(m[1])
    return None


def format_hint():
    h = ('Ответь СТРОГО одним JSON-объектом: {"command": "..."} / {"scan": ""} / '
         '{"click": [x, y]} / {"open": "имя"} / {"wait": 10}')
    if VISION:
        h += ' / {"see": "..."}'
    return h + ' / {"done": "..."}.'


def system_prompt():
    n = notes()
    mem = ("\nТвой дневник уроков (ты писала их сама — пользуйся):\n" + n) if n else ""
    return base_system() + mem + caps()


def stepper(task):
    print(f"\nЗАДАЧА: {task}\n" + "-" * 60)
    messages = [{"role": "system", "content": system_prompt()},
                {"role": "user", "content": f"TASK: {task}"}]
    bad, last_out = 0, ""
    history = {}
    for step in range(1, MAX_STEPS + 1):
        try:
            raw = chat(messages)
        except NetDown as e:
            return False, f"сервер Ollama недоступен ({e}); запустите 'ollama serve'"
        try:
            act = extract_json(raw)
        except Exception:
            bad += 1
            print(f"[шаг {step}] НЕВАЛИДНЫЙ ОТВЕТ: {raw[:150]}")
            if bad >= 5:
                return False, "модель 5 раз ответила не в формате"
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": format_hint()})
            continue
        if "done" in act:
            print(f"[ГОТОВО за {step} шаг(ов)] {act['done']}")
            return True, str(act["done"])

        act_key = next((k for k in ("see", "click", "scan", "open", "wait") if k in act), "command")
        act_repr = json.dumps({act_key: act.get(act_key)}, ensure_ascii=False)
        norm = " ".join(act_repr.split())
        history[norm] = history.get(norm, 0) + 1
        if history[norm] >= 3:
            anti = ("ОШИБКА-ЗАЩИТА-ОТ-ЦИКЛА: действие %s уже делалось %d раз. "
                    "ПОВТОР ЗАПРЕЩЁН. Проверь факты (pgrep -a, ls, или {\"scan\": \"окно\"} "
                    "по конкретному окну) и либо done, либо ДРУГОЙ способ."
                    % (norm[:60], history[norm]))
            print(f"[шаг {step}] ⛔ анти-цикл")
            messages.append({"role": "assistant", "content": json.dumps(act, ensure_ascii=False)})
            messages.append({"role": "user", "content": anti})
            continue

        if CONFIRM:
            ans = input(f"⚡ {act_repr}\nВыполнить? [y/n] ").strip().lower()
            if ans != "y":
                messages.append({"role": "assistant", "content": json.dumps(act, ensure_ascii=False)})
                messages.append({"role": "user", "content": 'Пользователь отклонил. Другой способ или {"done": ...}.'})
                continue

        print(f"[шаг {step}] АГЕНТ -> ПК: {act_repr}")
        if act_key == "see":
            out = look(str(act.get("see", "что на экране? опиши кратко")))
        elif act_key == "click":
            xy = parse_click(act.get("click"))
            out = do_click(*xy) if xy else 'ОШИБКА: формат клика {"click": [x, y]}'
        elif act_key == "scan":
            out = run_scan(str(act.get("scan", "")))
        elif act_key == "open":
            out = do_open(act.get("open"))
        elif act_key == "wait":
            out = do_wait(act.get("wait"))
        else:
            out = run(str(act.get("command", "")).strip())
        last_out = out

        if SCAN_ALWAYS and act_key in ("command", "click", "open"):
            try:
                auto = run_scan("")
                out += "\nАВТОСКАН:\n" + auto[:1200]
            except Exception:
                pass

        print(f"          ПК -> АГЕНТ: {out[:300]}")
        messages.append({"role": "assistant", "content": json.dumps(act, ensure_ascii=False)})
        messages.append({"role": "user", "content": f"OUTPUT: {out}"})
    return False, f"исчерпан лимит шагов; последний вывод: {last_out[:200]}"


def reflect(task, ok, outcome):
    try:
        verdict = "успех" if ok else "неудача"
        line = chat([{"role": "system", "content": "Ты сжато формулируешь выводы."},
                     {"role": "user", "content": (f"Задача: {task}\nИтог ({verdict}): {outcome[:300]}\n"
                                                 "Одной строкой до 12 слов: что важно запомнить? "
                                                 "Если нечего — ответь ровно '-'.")}],
                    temp=0.2, num=60)
        line = line.strip().strip('"').splitlines()[0] if line.strip() else "-"
        if line and line != "-":
            add_note(line)
            print(f"[память +] {line}")
    except Exception:
        pass


def remember(task, ok, outcome):
    append_diary(f"\n## {now()} — {'✅ УСПЕХ' if ok else '❌ ПРОВАЛ'}\n"
                 f"Задача: {task}\nИтог: {outcome[:400]}\n")


FREE_IDEAS = [
    'открой проводник через {"open": "файлы"}, посмотри сонаром {"scan": "файлы"} и запиши список элементов в ~/Desktop/sonar.txt',
    "смени обои командой gsettings на случайный файл из /usr/share/backgrounds и проверь gsettings get",
    "открой Настройки (nohup gnome-control-center background ...), найди сонаром кнопку/рисунок и кликни",
    "найди через curl html.duckduckgo.com три факта и сохрани в файл",
]


def gen_goal(prev):
    sys_msg = FREE_SYSTEM + "\n\nТвой дневник уроков:\n" + (notes() or "(пока пусто)")
    if prev:
        goal0, ok0, out0 = prev
        verdict = "успешно" if ok0 else "с ошибкой"
        user_msg = (f"Предыдущий этап: «{goal0}» — {verdict}: {out0[:200]}.\n"
                    "Продолжи логично или новое направление.")
    else:
        user_msg = "Первый этап потока. Придумай стартовую цель."
    for _ in range(2):
        try:
            raw = chat([{"role": "system", "content": sys_msg},
                        {"role": "user", "content": user_msg}], temp=0.85, num=80)
            goal = str(extract_json(raw).get("goal", "")).strip()
            if goal:
                return goal
        except NetDown:
            raise
        except Exception:
            pass
    ideas = [i for i in FREE_IDEAS if not prev or i != prev[0]]
    return random.choice(ideas)


def free_mode(n):
    print(f"\n🌊 ПОТОК АВТОНОМИИ: {n} этапов подряд. Стоп: Ctrl+C")
    prev = None
    try:
        for i in range(1, n + 1):
            try:
                goal = gen_goal(prev)
            except NetDown as e:
                print(f"\n[поток прерван] сервер недоступен ({e})")
                break
            print(f"\n=== этап {i}/{n}: {goal} ===")
            try:
                ok, out = stepper(goal)
            except KeyboardInterrupt:
                print("\n[поток] остановлен пользователем.")
                break
            remember(goal, ok, out)
            reflect(goal, ok, out)
            prev = (goal, ok, out)
            time.sleep(1)
    finally:
        print("\n🌊 ПОТОК завершён (автономия ВЫКЛ). /memo — дневник.")


def startup_check():
    try:
        with urllib.request.urlopen(API.replace("/api/chat", "/api/tags"), timeout=5) as r:
            names = [m.get("name", "") for m in json.load(r).get("models", [])]
        print(f"Модели на сервере: {', '.join(names) if names else '(пусто)'}")
        if VISION and not any(VISION in n for n in names):
            print(f"⚠ Зрительная модель '{VISION}' не найдена — see отключён, но сонар работает!")
    except Exception:
        print(f"⚠ Сервер Ollama не отвечает ({API}) — запустите 'ollama serve'.")

    if not os.path.exists(UITREE):
        print(f"⚠ Сонар НЕ найден: {UITREE}. Перенеси uitree.py в домашнюю папку!")
    else:
        try:
            p = subprocess.run('python3 -c "import pyatspi; print(1)"', shell=True,
                               timeout=10, capture_output=True)
            if p.returncode == 0:
                print("Сонар: ОК (pyatspi на месте) 📡")
            else:
                print("⚠ pyatspi не установлен: sudo apt install -y python3-pyatspi")
        except Exception:
            print("⚠ не удалось проверить pyatspi")

    st = os.environ.get("XDG_SESSION_TYPE", "")
    if st and st != "x11":
        print(f"⚠ Сеанс {st}: клики xdotool могут не работать — "
              "перелогинься через шестерёнку в 'Ubuntu on Xorg'.")


def main():
    print(BANNER)
    startup_check()
    while True:
        try:
            line = input("\nВы > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nПока! Дневник: ~/agent_diary.md, уроки: ~/agent_notes.txt 🧠📡")
            break
        if not line:
            continue
        low = line.lower()
        if low in ("/exit", "/quit"):
            print("Выход. Память сохранена. 🧠📡")
            break
        if low.startswith("/memo"):
            t = read_file(DIARY, 3000)
            print(t if t else "Дневник пока пуст.")
            continue
        if low.startswith("/free"):
            parts = low.split()
            n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else FREE_BUDGET
            free_mode(n)
            continue
        ok, out = stepper(line)
        remember(line, ok, out)
        reflect(line, ok, out)


if __name__ == "__main__":
    main()
