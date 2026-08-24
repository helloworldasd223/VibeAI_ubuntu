#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent_v3_1.py — ИИ-агент v3.1 «КНОПКОДАВ»: первые руки в GUI.

Новое против v3.0:
- блок знаний CAPS: панели Настроек GNOME (gnome-control-center background |
  appearance | wifi | bluetooth | display | ...), обои через gsettings,
  скриншоты gnome-screenshot, запуск программ отсоединённо (nohup ... &)
- рецепты xdotool: mousemove X Y click 1, key Tab/Down/Return, type --delay,
  поиск/активация окон (search --name windowactivate --sync), getdisplaygeometry
- семантические клики dogtail/AT-SPI + включение toolkit-accessibility
- правило стратегии: сначала CLI/клавиатура/dogtail, координаты — в последнюю очередь
- ядро v3.0 сохранено: дневник, уроки, шум-фильтр, анти-зацикливатель,
  живой поток /free, переподключение к серверу, CONFIRM=1

Запуск:
    python3 agent_v3_1.py
    AGENT_MODEL=mybrain python3 agent_v3_1.py
    CONFIRM=1 python3 agent_v3_1.py

Команды: любая фраза = задача | /free [N] поток на N этапов | /memo | /exit
"""
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
HOME = os.path.expanduser("~")
DIARY = os.path.join(HOME, "agent_diary.md")
NOTES = os.path.join(HOME, "agent_notes.txt")
MAX_STEPS = 12          # шагов на этап (упорство!)
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
    r"^\s*WARNING:|QtWarning|sandbox|sandboxing|Skipping unsupported|"
    r"^\(process:\d+\):|^\*\*", re.IGNORECASE)

CAPS = """
Твои возможности (действуй через shell-команды):
- файлы: читать/писать в ~, ~/Desktop, /tmp
- системная информация: uname, nproc, free, df, ls, date, ps, pgrep
- GUI: запуск программ ОТСОЕДИНЁННО: (nohup <программа> >/dev/null 2>&1 &)
- проверка, что программа запустилась: pgrep -a <имя>
- НАСТРОЙКИ GNOME сразу на нужной панели: gnome-control-center background | appearance |
  wifi | bluetooth | display | network | power | sound | datetime | region | privacy | about
- обои: gsettings set org.gnome.desktop.background picture-uri file:///usr/share/backgrounds/<файл>
  (и picture-uri-dark тем же значением); список картинок: ls /usr/share/backgrounds
- скриншоты: gnome-screenshot -f /tmp/screen.png
- мышь/клавиатура (сеанс X11, проверка echo $XDG_SESSION_TYPE):
  xdotool mousemove X Y click 1     (клик левой в координаты)
  xdotool click 1                   (клик в текущую точку)
  xdotool key Tab / Down / Return / Escape   (клавиши: проход диалога клавиатурой)
  xdotool type --delay 40 "текст"   (печать текста)
  xdotool search --name "окно" windowactivate --sync  (активировать окно по имени)
  xdotool getdisplaygeometry        (разрешение экрана, пределы координат)
- включить доступность для семантических кликов (без sudo):
  gsettings set org.gnome.desktop.interface toolkit-accessibility true
- если установлен python3-dogtail: клики ПО ИМЕНИ кнопки через шину a11y:
  python3 -c 'import dogtail.tree as t, dogtail.predicate as p; t.root.application("gnome-control-center").child(name="...", roleName="push button").click()'
- интернет-текст: curl https://html.duckduckgo.com/html/?q=<запрос>
Не пытайся: ставить пакеты, sudo, перезагружать, удалять системное."""

BASE_SYSTEM = """Ты — автономный ИИ-агент внутри Ubuntu Linux. Управляешь машиной, отвечая СТРОГО
одним JSON-объектом без пояснений и без markdown:
{"command": "одна shell-команда"}  — следующий шаг
{"done": "итог по-русски"}        — когда задача выполнена и проверена

Правила:
- Одна простая команда за раз; после неё получишь реальный вывод в "OUTPUT:".
- Строки "Gtk-Message", "Not loading module" и прочие библиотечные ворчания — НЕ
  ошибки, а шум. Успех оценивай ТОЛЬКО по фактам (pgrep, ls, cat, gsettings get).
- Запустил программу -> проверь pgrep -a. Факт есть -> задача УСПЕШНА, сразу {"done": ...}.
- Нажатие кнопок: сначала пробуй ПРЯМУЮ команду (CLI, gsettings, аргументы программы);
  если только GUI — клавиатурную навигацию (xdotool key) или dogtail по имени кнопки;
  координатный клик (xdotool mousemove click) — последний вариант, только при известных
  координатах (пределы смотри xdotool getdisplaygeometry).
- Настоящая ошибка (No such file, command not found, отказ). НЕ СДАВАЙСЯ: придумай
  ДРУГОЙ подход. Сдаться можно только после 3 разных способов.
- НИКОГДА не повторяй успешную команду ради перестраховки.
- НИКОГДА не пиши OUTPUT сам.
- {"done": ...} — только когда вывод последней команды подтверждает результат."""

FREE_SYSTEM = """Ты — автономный ИИ-агент в режиме живого потока действий.
Придумай следующий этап:
- либо ЛОГИЧНОЕ ПРОДОЛЖЕНИЕ предыдущего (если там есть что развивать),
  либо новое направление, если предыдущее исчерпано
- маленький, безопасный, проверяемый терминалом (1-5 команд)
- НЕ повторяй уже сделанное (смотри дневник)
- возможности: файлы, факты о системе, обои, скриншоты, запуск программ,
  xdotool-фокусы (X11), поиск через html.duckduckgo.com
Ответь СТРОГО JSON: {"goal": "короткое описание этапа"}"""

BANNER = f"""============================================================
 ИИ-АГЕНТ v3.1 «КНОПКОДАВ» | мозг: {MODEL}
 Тихий по умолчанию: пиши задачу — выполню.
 /free [N] — поток автономии | /memo дневник | /exit выход
============================================================"""


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
    """Сервер Ollama недоступен (даже после ретраев)."""


def chat(messages, temp=0.2, num=220):
    body = json.dumps({"model": MODEL, "messages": messages, "stream": False,
                       "options": {"temperature": temp, "num_predict": num}}).encode()
    req = urllib.request.Request(API, data=body, headers={"Content-Type": "application/json"})
    for attempt in range(1, NET_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                return json.load(r)["message"]["content"]
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            print(f"  [сеть] попытка {attempt}/{NET_RETRIES}: {e}. "
                  f"Сервер Ollama недоступен — жду {NET_WAIT}с...")
            if attempt == NET_RETRIES:
                raise NetDown(str(e))
            time.sleep(NET_WAIT)


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
        return "ОШИБКА: команда заблокирована политикой безопасности агента."
    try:
        p = subprocess.run(cmd, shell=True, cwd=HOME, timeout=60,
                           capture_output=True, text=True)
        out = (p.stdout + p.stderr).strip() or "(команда выполнена, вывода нет)"
    except subprocess.TimeoutExpired:
        out = "ОШИБКА: команда не уложилась в 60 секунд."
    return filter_noise(out)[:MAX_OUT]


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
            messages.append({"role": "user", "content": 'Ответь СТРОГО одним JSON: {"command": "..."} или {"done": "..."}.'})
            continue
        if "done" in act:
            print(f"[ГОТОВО за {step} шаг(ов)] {act['done']}")
            return True, str(act["done"])
        cmd = str(act.get("command", "")).strip()

        norm = " ".join(cmd.split())
        history[norm] = history.get(norm, 0) + 1
        if history[norm] >= 3:
            anti = ("ОШИБКА-ЗАЩИТА-ОТ-ЦИКЛА: команда '%s' уже выполнялась %d раз. "
                    "ПОВТОРЯТЬ ЗАПРЕЩЕНО. Проверь результат другой командой "
                    "(pgrep -a <имя>, ls, cat) и если факт есть — сразу {\"done\": ...}. "
                    "Если факта нет — придумай ДРУГОЙ способ достичь цели."
                    % (norm[:60], history[norm]))
            print(f"[шаг {step}] ⛔ анти-цикл: повтор команды заблокирован")
            messages.append({"role": "assistant", "content": json.dumps(act, ensure_ascii=False)})
            messages.append({"role": "user", "content": anti})
            continue

        if CONFIRM:
            ans = input(f"⚡ {cmd}\nВыполнить? [y/n] ").strip().lower()
            if ans != "y":
                messages.append({"role": "assistant", "content": json.dumps(act, ensure_ascii=False)})
                messages.append({"role": "user", "content": 'Пользователь отклонил команду. Предложи другой способ или завершись {"done": ...}.'})
                continue
        print(f"[шаг {step}] АГЕНТ -> ПК: {cmd}")
        out = run(cmd)
        last_out = out
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
    except NetDown:
        print("[память] сервер недоступен, урок пропущен")
    except Exception:
        pass


def remember(task, ok, outcome):
    append_diary(f"\n## {now()} — {'✅ УСПЕХ' if ok else '❌ ПРОВАЛ'}\n"
                 f"Задача: {task}\nИтог: {outcome[:400]}\n")


FREE_IDEAS = [
    "сделай скриншот рабочего стола в /tmp и проверь файл",
    "создай на Desktop файл sysinfo.txt с датой, ядром и свободной памятью",
    "смени обои на случайную картинку из /usr/share/backgrounds через gsettings",
    "найди через curl html.duckduckgo.com три факта про котиков и сохрани в файл",
    "покажи 5 самых больших файлов в домашней папке командой du и sort",
]


def gen_goal(prev):
    sys_msg = FREE_SYSTEM + "\n\nТвой дневник уроков:\n" + (notes() or "(пока пусто)")
    if prev:
        goal0, ok0, out0 = prev
        verdict = "успешно" if ok0 else "с ошибкой"
        user_msg = (f"Предыдущий этап был: «{goal0}» — завершился {verdict}: {out0[:200]}.\n"
                    "Если там логично есть продолжение — продолжи. Исчерпан — новое направление.")
    else:
        user_msg = "Это первый этап потока. Придумай стартовую цель."
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
    print(f"\n🌊 ПОТОК АВТОНОМИИ: {n} этапов подряд, без пауз. Стоп: Ctrl+C")
    prev = None
    try:
        for i in range(1, n + 1):
            try:
                goal = gen_goal(prev)
            except NetDown as e:
                print(f"\n[поток прерван] сервер Ollama недоступен ({e})")
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
            time.sleep(1)   # одна секунда передышки — и сразу дальше
    finally:
        print("\n🌊 ПОТОК завершён (автономия ВЫКЛ). Дневник пополнен — /memo.")


def main():
    print(BANNER)
    if not os.path.exists(os.path.join(HOME, "agent_v3.py")):
        pass
    while True:
        try:
            line = input("\nВы > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nДо встречи! Дневник: ~/agent_diary.md, уроки: ~/agent_notes.txt 🧠")
            break
        if not line:
            continue
        low = line.lower()
        if low in ("/exit", "/quit"):
            print("Выход. Память сохранена. 🧠")
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
