#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent_v2_1.py — ИИ-агент v2.1: дневник памяти + автономия + защита от зацикливания.

Отличия от v2:
- шум-фильтр вывода (Gtk-Message, "Not loading module", WARNING и прочий
  безобидный мусор не доходит до модели и не пугает её)
- анти-зацикливатель: 3-я попытка повторить ту же команду блокируется,
  модели приказывают проверить результат и завершить задачу
- системный промпт учит: судить об успехе по фактам (pgrep/ls/file), а не
  по страшным словам в выводе

Запуск / команды — как в v2:
    python3 agent_v2_1.py                (AGENT_MODEL=..., CONFIRM=1 — опционально)
    /free [N] автономия | /memo дневник | /exit выход
"""
import datetime
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.request

API = os.environ.get("OLLAMA_API", "http://localhost:11434/api/chat")
MODEL = os.environ.get("AGENT_MODEL", "mybrain")
HOME = os.path.expanduser("~")
DIARY = os.path.join(HOME, "agent_diary.md")
NOTES = os.path.join(HOME, "agent_notes.txt")
MAX_STEPS = 8
MAX_OUT = 1500
FREE_BUDGET = 3
CONFIRM = os.environ.get("CONFIRM", "") == "1"

# Стоп-лист опасных команд
DENY = ["rm -rf /", "rm -rf ~", "rm -rf *", "mkfs", "dd if=", "dd of=", "of=/dev/", ":(){",
        "shutdown", "reboot", "poweroff", "halt", "passwd", "useradd", "userdel",
        "chmod -r /", "chown -r /", "> /dev/sd", "sudo ", "su root", "apt ", "apt-get",
        "dpkg", "pip ", "pipx", "snap install", "crontab", "systemctl", "mknod",
        "nc ", "ncat ", "ssh ", "scp ", "kill -9 1", "killall gdm", "eval"]

# Шум ГРАФИЧЕСКИХ программ — не ошибки, модель их пугается: вырезаем из вывода
NOISE = re.compile(
    r"Gtk-Message|Gtk-WARNING|Not loading module|atk-bridge|dbind|dconf|GLib-|"
    r"GLib-GObject|libva|vaapi|libEGL|Fontconfig|dbus-launch|Xlib.*extension|"
    r"^\s*WARNING:|QtWarning|sandbox|sandboxing|Skipping unsupported|"
    r"^\(process:\d+\):|^\*\*", re.IGNORECASE)

CAPS = """
Твои возможности (действуй через shell-команды):
- файлы: читать/писать в ~, ~/Desktop, /tmp
- системная информация: uname, nproc, free, df, ls, date, ps, pgrep
- GUI: запуск программ ОТСОЕДИНЁННО: (nohup <программа> >/dev/null 2>&1 &) — тогда шум не мешает
- проверка, что программа запустилась: pgrep -a <имя> (например pgrep -a firefox)
- скриншоты: gnome-screenshot -f /tmp/screen.png
- обои: gsettings set org.gnome.desktop.background picture-uri file:///usr/share/backgrounds/<файл>
- мышь/клавиатура: xdotool (только X11; проверка: echo $XDG_SESSION_TYPE)
- интернет-текст: curl https://html.duckduckgo.com/html/?q=<запрос>
Не пытайся: ставить пакеты, sudo, перезагружать, удалять системное."""

BASE_SYSTEM = """Ты — автономный ИИ-агент внутри Ubuntu Linux. Управляешь машиной, отвечая СТРОГО
одним JSON-объектом без пояснений и без markdown:
{"command": "одна shell-команда"}  — следующий шаг
{"done": "итог по-русски"}        — когда задача выполнена и проверена

Правила:
- Одна простая команда за раз; после неё получишь реальный вывод в "OUTPUT:".
- Строки вида "Gtk-Message", "Not loading module", предупреждения библиотек — это НЕ
  ошибки, а шум. Успех оценивай ТОЛЬКО по фактам: pgrep нашёл процесс, ls нашёл файл,
  gsettings показала значение.
- Запуск программы: ОДНА попытка запуска, затем ОДНА проверка результата.
  Если факт подтвердился (процесс/файл есть) — задача УСПЕШНА, сразу {"done": ...}.
  НИКОГДА не повторяй одну и ту же успешную команду ради перестраховки.
- Если OUTPUT — настоящая ошибка (No such file, command not found, отказ в доступе) —
  исправляй следующей командой.
- НИКОГДА не пиши OUTPUT сам и не выдумывай результаты.
- {"done": ...} — только когда вывод последней команды-факта подтверждает результат."""

FREE_SYSTEM = """Ты — автономный ИИ-агент. Придумай ОДНУ следующую маленькую цель:
- безопасная, проверяемая терминалом, из твоих возможностей (1-4 команды)
- НЕ повторяй уже сделанное (смотри дневник ниже)
- можно: файлы, факты о системе, обои, скриншот, поиск в html.duckduckgo.com,
  запуск программ, xdotool-фокусы в X11
Ответь СТРОГО JSON: {"goal": "короткое описание цели"}"""

BANNER = f"""============================================================
 ИИ-АГЕНТ v2.1 | мозг: {MODEL}
 Тихий по умолчанию: пиши задачу — выполню.
 /free [N] автономия | /memo дневник | /exit выход
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


def chat(messages, temp=0.2, num=220):
    body = json.dumps({"model": MODEL, "messages": messages, "stream": False,
                       "options": {"temperature": temp, "num_predict": num}}).encode()
    req = urllib.request.Request(API, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)["message"]["content"]


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
    """Убираем безобидные строки-пугала, чтобы мелкая модель не паниковала."""
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
    history = {}  # команда -> сколько раз уже выполнялась в ЭТОЙ задаче
    for step in range(1, MAX_STEPS + 1):
        raw = chat(messages)
        try:
            act = extract_json(raw)
        except Exception:
            bad += 1
            print(f"[шаг {step}] НЕВАЛИДНЫЙ ОТВЕТ: {raw[:150]}")
            if bad >= 3:
                return False, "модель 3 раза ответила не в формате"
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": 'Ответь СТРОГО одним JSON: {"command": "..."} или {"done": "..."}.'})
            continue
        if "done" in act:
            print(f"[ГОТОВО за {step} шаг(ов)] {act['done']}")
            return True, str(act["done"])
        cmd = str(act.get("command", "")).strip()

        # --- АНТИ-ЗАЦИКЛИВАТЕЛЬ ---
        norm = " ".join(cmd.split())
        history[norm] = history.get(norm, 0) + 1
        if history[norm] >= 3:
            anti = ("ОШИБКА-ЗАЩИТА-ОТ-ЦИКЛА: команда '%s' уже выполнялась %d раз. "
                    "ПОВТОРЯТЬ ЗАПРЕЩЕНО. Проверь результат другой командой "
                    "(pgrep -a <имя>, ls, cat, gsettings get) и если факт подтверждается "
                    "— сразу {\"done\": ...}." % (norm[:60], history[norm]))
            print(f"[шаг {step}] ⛔ анти-цикл: повтор команды заблокирован")
            messages.append({"role": "assistant", "content": json.dumps(act, ensure_ascii=False)})
            messages.append({"role": "user", "content": anti})
            continue
        # --------------------------

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
                                                 "Одной строкой до 12 слов: что важно запомнить на будущее? "
                                                 "Если нечего — ответь ровно '-'.")}],
                    temp=0.2, num=60)
        line = line.strip().strip('"').splitlines()[0] if line.strip() else "-"
        if line and line != "-":
            add_note(line)
            print(f"[память +] {line}")
    except Exception as e:
        print(f"[память] пропуск: {e}")


def remember(task, ok, outcome):
    append_diary(f"\n## {now()} — {'✅ УСПЕХ' if ok else '❌ ПРОВАЛ'}\n"
                 f"Задача: {task}\nИтог: {outcome[:400]}\n")


FREE_IDEAS = [
    "сделай скриншот рабочего стола командой gnome-screenshot в /tmp и проверь файл",
    "создай на Desktop файл sysinfo.txt с датой, версией ядра и свободной памятью",
    "смени обои на случайную картинку из /usr/share/backgrounds через gsettings",
    "найди через curl html.duckduckgo.com три факта про котиков и сохрани в файл",
    "покажи 5 самых больших файлов в домашней папке командой du и sort",
    "создай файл quote.txt на Desktop с датой и мотивирующей фразой",
]


def gen_goal():
    sys_msg = FREE_SYSTEM + "\n\nТвой дневник уроков:\n" + (notes() or "(пока пусто)")
    for _ in range(2):
        try:
            raw = chat([{"role": "system", "content": sys_msg},
                        {"role": "user", "content": "Придумай следующую цель."}], temp=0.9, num=80)
            goal = str(extract_json(raw).get("goal", "")).strip()
            if goal:
                return goal
        except Exception:
            pass
    return random.choice(FREE_IDEAS)


def free_mode(n):
    print(f"\n🕊 АВТОНОМИЯ ВКЛ: {n} целей. Экстренный стоп: Ctrl+C")
    for i in range(1, n + 1):
        goal = gen_goal()
        print(f"\n=== автономная цель {i}/{n}: {goal} ===")
        ok, out = stepper(goal)
        remember(goal, ok, out)
        reflect(goal, ok, out)
        time.sleep(1)
    print("\n🕊 АВТОНОМИЯ ВЫКЛ (автоматически). Дневник пополнен — /memo посмотреть.")


def main():
    print(BANNER)
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
            print(t if t else "Дневник пока пуст — сделайте первую задачу.")
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
