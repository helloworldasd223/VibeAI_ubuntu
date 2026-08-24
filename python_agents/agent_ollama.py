#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent_ollama.py — ИИ-агент: локальная нейросеть (через Ollama) управляет компьютером.

Цикл: нейросеть придумывает shell-команду -> ПК выполняет -> вывод возвращается нейросети.
БЕЗ ЗАВИСИМОСТЕЙ: только python3 и работающий Ollama (localhost:11434).

Запуск:
    ollama pull qwen2.5:7b                     # один раз, мозг
    python3 agent_ollama.py "покажи версию ядра и дату"
    python3 agent_ollama.py "создай файл /tmp/note.txt с текущей датой и покажи его"

Модель под задачу (если 7B тяжело):  AGENT_MODEL=qwen2.5:3b python3 agent_ollama.py "..."
"""
import json, os, subprocess, sys, urllib.request

API = os.environ.get("OLLAMA_API", "http://localhost:11434/api/chat")
MODEL = os.environ.get("AGENT_MODEL", "qwen2.5:7b")
HOME = os.path.expanduser("~")
MAX_STEPS = 10
MAX_OUT = 1500   # сколько символов вывода отдавать модели (у локальных моделей маленький контекст)

# Политика безопасности: агент НЕ выполнит эти команды, даже если нейросеть их придумает
DENY = ["rm -rf /", "mkfs", "dd if=", "of=/dev/", "shutdown", "reboot", "poweroff", "halt",
        "passwd", "useradd", "userdel", "chmod -R /", "chown -R /", "> /dev/sd", "sudo ", ":(){"]

SYSTEM = """Ты — ИИ-агент, который управляет компьютером с Ubuntu Linux через терминал.
Ты отвечаешь СТРОГО одним JSON-объектом, без пояснений и без markdown:
{"command": "одна shell-команда для выполнения"}
или, когда задача полностью выполнена и проверена:
{"done": "итоговый ответ по-русски"}

Правила:
- Выполняй задачу по шагам, по одной простой команде за раз.
- После каждой команды ты получишь её РЕАЛЬНЫЙ вывод в сообщении "OUTPUT:".
- НИКОГДА не пиши "OUTPUT:" сам и не придумывай результат команд.
- Не используй сложные конструкции с кавычками; предпочитай простые короткие команды.
- Если OUTPUT содержит ошибку — не сдавайся, исправляй следующей командой.
- Запрещены опасные команды (удаление системы, перезагрузка, sudo и т.п.).
- Домашняя папка: ~ (домашний каталог пользователя).
- {"done": ...} разрешено ТОЛЬКО когда OUTPUT последней команды подтверждает результат
  (например, cat показал нужное содержимое файла).

Пример диалога:
TASK: покажи версию ядра
{"command": "uname -r"}
OUTPUT: 6.8.0-31-generic
{"done": "Версия ядра: 6.8.0-31-generic"}"""

def chat(messages):
    body = json.dumps({
        "model": MODEL,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": 250}
    }).encode()
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
    obj, _ = json.JSONDecoder().raw_decode(t[i:])  # берёт ПЕРВЫЙ объект, хвост игнорируем
    if not isinstance(obj, dict):
        raise ValueError("not a dict")
    return obj

def run(cmd):
    if any(b in cmd.lower() for b in DENY):
        return "ОШИБКА: команда заблокирована политикой безопасности агента."
    try:
        p = subprocess.run(cmd, shell=True, cwd=HOME, timeout=60,
                           capture_output=True, text=True)
        out = (p.stdout + p.stderr).strip() or "(команда выполнена, вывода нет)"
    except subprocess.TimeoutExpired:
        out = "ОШИБКА: команда не уложилась в 60 секунд."
    return out[:MAX_OUT]

def main():
    task = " ".join(sys.argv[1:]).strip()
    if not task:
        print('Использование: python3 agent_ollama.py "задача для агента"')
        sys.exit(1)
    print(f"МОЗГ: {MODEL} @ {API}")
    print(f"ЗАДАЧА: {task}\n" + "=" * 64)
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"TASK: {task}"}]
    bad = 0
    for step in range(1, MAX_STEPS + 1):
        raw = chat(messages)
        try:
            act = extract_json(raw)
        except Exception:
            bad += 1
            print(f"[шаг {step}] НЕВАЛИДНЫЙ ОТВЕТ: {raw[:150]}")
            if bad >= 3:
                print("[СТОП] модель 3 раза ответила не JSON. Совет: упростите задачу или возьмите модель крупнее.")
                return
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": 'Ошибка формата. Ответь СТРОГО одним JSON: {"command": "..."} или {"done": "..."}.'})
            continue
        if "done" in act:
            print(f"\n[ГОТОВО за {step} шаг(ов)] {act['done']}")
            print("(проверьте результат сами — словам модели не верим)")
            return
        cmd = str(act.get("command", "")).strip()
        print(f"[шаг {step}] АГЕНТ -> ПК: {cmd}")
        out = run(cmd)
        print(f"          ПК -> АГЕНТ: {out[:300]}")
        messages.append({"role": "assistant", "content": json.dumps(act, ensure_ascii=False)})
        messages.append({"role": "user", "content": f"OUTPUT: {out}"})
    print("\n[СТОП] лимит шагов. Попробуйте задачу поменьше.")

if __name__ == "__main__":
    main()
