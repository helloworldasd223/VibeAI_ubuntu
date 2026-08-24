#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent_v4.py — ИИ-агент v4.0 «ГЛАЗА»: видит экран, кликает по координатам.

Два мозга:
  - текстовый (AGENT_MODEL, напр. mybrain) — планирует и командует
  - зрительный (AGENT_VISION, напр. eyes = qwen2.5vl:3b) — отвечает на
    вопросы по скриншоту и даёт координаты элементов

Новый протокол действий (модель выбирает ОДНО действие за шаг):
  {"command": "shell-команда"}        — как раньше
  {"see": "вопрос к экрану"}          — скриншот -> глаза -> текстовый ответ
  {"click": [x, y]}                   — клик мышью в координаты экрана
  {"done": "итог"}                    — задача выполнена

Переменные окружения:
    AGENT_MODEL=mybrain     текстовый мозг (по умолчанию mybrain)
    AGENT_VISION=eyes       зрительный мозг (по умолчанию eyes)
    EYE=always              после КАЖДОГО действия автоматически смотреть экран
    CONFIRM=1               спрашивать разрешение на каждое действие

Требования: сеанс X11 (echo $XDG_SESSION_TYPE), gnome-screenshot, xdotool,
8+ ГБ RAM в ВМ. Команды: задача | /free [N] | /memo | /exit
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
VISION = os.environ.get("AGENT_VISION", "eyes")
EYE_ALWAYS = os.environ.get("EYE", "") == "always"
HOME = os.path.expanduser("~")
DIARY = os.path.join(HOME, "agent_diary.md")
NOTES = os.path.join(HOME, "agent_notes.txt")
EYE_SHOT = "/tmp/eye.png"
MAX_STEPS = 14
MAX_OUT = 1500
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

CAPS = """
Твои возможности:
- shell: файлы в ~, ~/Desktop, /tmp; ps, pgrep, ls, df, free, date
- запуск программ: (nohup <prog> >/dev/null 2>&1 &)  | проверка: pgrep -a <имя>
- Настройки GNOME на панели: gnome-control-center background | appearance | wifi | bluetooth | display | about
- обои: gsettings set org.gnome.desktop.background picture-uri file:///usr/share/backgrounds/<файл>
- ГЛАЗА: действие {"see": "вопрос"} -> делает скриншот и зрительная модель отвечает
- КЛИКИ: действие {"click": [x, y]} -> клик мышью в координаты (сеанс X11!)
- xdotool: mousemove/key/type/windowactivate; getdisplaygeometry = разрешение экрана
- интернет-текст: curl https://html.duckduckgo.com/html/?q=<запрос>
Не пытайся: ставить пакеты, sudo, перезагружать."""

BASE_SYSTEM = """Ты — автономный ИИ-агент внутри Ubuntu Linux с ГЛАЗАМИ. За шаг отвечаешь СТРОГО
одним JSON-объектом (без пояснений, без markdown), выбрав ОДНО действие:
{"command": "одна shell-команда"}
{"see": "вопрос к глазам о том, что на экране (по-русски, со словом КООРДИНАТЫ если нужны точки)"}
{"click": [x, y]}
{"done": "итог по-русски"}

Правила:
- Одна простая команда/действие за раз. После действия получишь результат в "OUTPUT:".
- "Gtk-Message" и библиотечные ворчания — шум, не ошибки. Успех — по фактам (pgrep, ls, cat).
- Работа с экраном: 1) {"see": "что на экране, где находится <элемент> — дай КООРДИНАТЫ x,y центра"}
  2) {"click": [x, y]}  3) {"see": "что изменилось после клика"}.
- Глаза бывают неточны на ±50 пикселей: если промазал — {"see"} уточни и кликни снова.
- Запустил программу -> pgrep -a -> факт есть -> {"done": ...}. Не повторяй команды.
- Настоящая ошибка -> другой подход, сдаться лишь после 3 разных способов.
- НИКОГДА не пиши OUTPUT сам. done — только при подтверждении факта."""

FREE_SYSTEM = """Ты — ИИ-агент в живом потоке действий. Придумай следующий этап:
- продолжение предыдущего (если развивается) или новое направление
- маленький, безопасный, проверяемый (1-5 действий), не повторяй дневник
- можно: файлы, обои, скриншоты, запуск программ, ГЛАЗА+КЛИКИ по экрану (X11),
  поиск через html.duckduckgo.com
Ответь СТРОГО JSON: {"goal": "коротко"}"""

BANNER = f"""============================================================
 ИИ-АГЕНТ v4.0 «ГЛАЗА» | мозг: {MODEL} | зрение: {VISION}{' (АВТОГЛАЗ)' if EYE_ALWAYS else ''}
 Тихий по умолчанию. /free [N] поток | /memo дневник | /exit выход
============================================================"""

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
    r = _post(VISION, {"model": VISION, "stream": False,
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


def filter_noise_args():
    return NOISE


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


def geometry():
    try:
        p = subprocess.run("xdotool getdisplaygeometry", shell=True, timeout=10,
                           capture_output=True, text=True)
        parts = p.stdout.split()
        w, h = int(parts[0]), int(parts[1])
        return w, h
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


def look(question):
    """Делает скриншот и спрашивает зрительную модель."""
    if _vision_state["ok"] is False:
        return ("ОШИБКА ГЛАЗ: зрительная модель недоступна. "
                "Проверь: ollama list (модель eyes), ollama serve, сеанс X11. "
                "Продолжай без глаз или исправь.")
    shot = take_shot()
    if not shot:
        _vision_state["ok"] = False
        return ("ОШИБКА ГЛАЗ: скриншот не получился (gnome-screenshot не сработал — "
                "часто это Wayland. Перелогинься в 'Ubuntu on Xorg').")
    w, h = geometry()
    geo = f"Разрешение экрана {w}x{h}." if w else "Разрешение неизвестно."
    q = (f"{geo} Вопрос: {question}\n"
         "Отвечай кратко по-русски. Если просят координаты — дай примерные x,y центра "
         "элемента в пикселях исходного изображения.")
    print(f"          [глаза] смотрю на экран (вопрос: {question[:80]})...")
    try:
        ans = vision_chat(q, shot)
        _vision_state["ok"] = True
        return "ОТВЕТ ГЛАЗ: " + ans.strip()
    except NetDown as e:
        _vision_state["ok"] = False
        return f"ОШИБКА ГЛАЗ: сервер/модель зрения недоступны ({e}). Действуй без глаз."
    except Exception as e:
        _vision_state["ok"] = False
        return (f"ОШИБКА ГЛАЗ: {e}. Возможно, модель '{VISION}' не установлена "
                f"(ollama list). Действуй без глаз или поправь.")


def do_click(x, y):
    w, h = geometry()
    try:
        xi, yi = int(x), int(y)
    except Exception:
        return "ОШИБКА: координаты должны быть числами."
    if w and (xi < 0 or yi < 0 or xi > w or yi > h):
        return f"ОШИБКА: клик ({xi},{yi}) за пределами экрана {w}x{h} — уточни через глаза."
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


def system_prompt():
    n = notes()
    mem = ("\nТвой дневник уроков (ты писала их сама — пользуйся):\n" + n) if n else ""
    return BASE_SYSTEM + mem + CAPS


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
            messages.append({"role": "user", "content": 'Ответь СТРОГО одним JSON: {"command": "..."} / {"see": "..."} / {"click": [x, y]} / {"done": "..."}.'})
            continue
        if "done" in act:
            print(f"[ГОТОВО за {step} шаг(ов)] {act['done']}")
            return True, str(act["done"])

        act_key = "see" if "see" in act else ("click" if "click" in act else "command")
        act_repr = json.dumps({act_key: act.get(act_key)}, ensure_ascii=False)
        norm = " ".join(act_repr.split())
        history[norm] = history.get(norm, 0) + 1
        if history[norm] >= 3:
            anti = ("ОШИБКА-ЗАЩИТА-ОТ-ЦИКЛА: действие %s уже делалось %d раз. "
                    "ПОВТОР ЗАПРЕЩЁН. Проверь факты (pgrep -a, ls, или посмотри снова "
                    "{"see": "где сейчас курсор/кнопка? КООРДИНАТЫ"}) и либо done, либо ДРУГОЙ способ."
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
            out = do_click(*xy) if xy else "ОШИБКА: формат клика {\"click\": [x, y]}"
        else:
            out = run(str(act.get("command", "")).strip())
        last_out = out

        if EYE_ALWAYS and act_key != "see" and _vision_state["ok"] is not False:
            try:
                auto = look(f"Что изменилось на экране для задачи '{task[:60]}'? Кратко, КООРДИНАТЫ важных элементов если есть.")
                out += "\n" + auto
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
    "посмотри глазами {"see": "что на экране"} и запиши результат в файл на Desktop",
    "сделай скриншот в /tmp и проверь файл",
    "смени обои на случайную из /usr/share/backgrounds и посмотри глазами результат",
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
    """Проверка моделей на сервере; глазам — предупреждение, если их нет."""
    try:
        with urllib.request.urlopen(API.replace("/api/chat", "/api/tags"), timeout=5) as r:
            names = [m.get("name", "") for m in json.load(r).get("models", [])]
        print(f"Модели на сервере: {', '.join(names) if names else '(пусто)'}")
        if not any(VISION in n for n in names):
            print(f"⚠ Зрительная модель '{VISION}' НЕ найдена — глаза отключены, "
                  f"v4 будет вести себя как v3.1. Как поставить — см. инструкцию.")
        return True
    except Exception:
        print(f"⚠ Сервер Ollama не отвечает ({API}) — запустите 'ollama serve'.")
        return False


def main():
    print(BANNER)
    startup_check()
    while True:
        try:
            line = input("\nВы > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nПока! Дневник: ~/agent_diary.md, уроки: ~/agent_notes.txt 🧠👁")
            break
        if not line:
            continue
        low = line.lower()
        if low in ("/exit", "/quit"):
            print("Выход. Память сохранена. 🧠👁")
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
