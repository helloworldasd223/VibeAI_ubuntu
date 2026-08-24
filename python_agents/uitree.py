#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
uitree.py — «СОНАР»: печатает видимые элементы интерфейса с координатами.

Зрение БЕЗ нейросети: использует дерево доступности (AT-SPI) — то самое
atk-bridge, из-за которого агент v2.1 пугался Gtk-Message. Теперь оно —
глаза системы. Память: почти ноль (обычный Python).

Установка (один раз):
    sudo apt install -y python3-pyatspi
    gsettings set org.gnome.desktop.interface toolkit-accessibility true
    (перезагрузить ВМ один раз, чтобы программы подхватили accessibility)

Запуск:
    python3 uitree.py              # весь экран
    python3 uitree.py Settings     # только окна, где в названии есть "Settings"
    python3 uitree.py Terminal
"""
import subprocess
import sys

try:
    import pyatspi
except ImportError:
    print("ОШИБКА: нет модуля pyatspi.")
    print("Лечение: sudo apt install -y python3-pyatspi")
    sys.exit(1)

LIMIT_ALL = 170          # максимум строк без фильтра
LIMIT_FILTERED = 400     # с фильтром по окну
MAX_VISIT = 6000         # защита от гигантских деревьев (Firefox и т.п.)

# Русские имена ролей — чтобы слабой модели было понятнее
RU = {
    "push button": "КНОПКА", "toggle button": "КНОПКА-ТУМБЛЕР",
    "check box": "ГАЛОЧКА", "radio button": "ПЕРЕКЛЮЧ",
    "menu item": "ПУНКТ-МЕНЮ", "check menu item": "ПУНКТ-МЕНЮ",
    "radio menu item": "ПУНКТ-МЕНЮ", "menu": "МЕНЮ",
    "combo box": "СПИСОК-ВЫБОР", "text": "ПОЛЕ-ВВОДА", "entry": "ПОЛЕ-ВВОДА",
    "password text": "ПОЛЕ-ПАРОЛЬ", "link": "ССЫЛКА", "page tab": "ВКЛАДКА",
    "list item": "ЭЛЕМЕНТ-СПИСКА", "tree item": "ЭЛЕМЕНТ-ДЕРЕВА",
    "table cell": "ЯЧЕЙКА", "slider": "ПОЛЗУНОК", "spin button": "СЧЁТЧИК",
    "label": "ТЕКСТ", "heading": "ЗАГОЛОВОК", "static": "ТЕКСТ",
    "icon": "ИКОНКА", "image": "КАРТИНКА",
}
NEED_NAME = {"label", "heading", "static", "icon", "image", "menu"}
WIN_ROLES = {"frame", "dialog", "alert", "window"}

state = {"out": [], "seen": set(), "budget": 0, "visited": 0}


def showing(acc):
    try:
        st = acc.getState()
        return st.contains(pyatspi.STATE_VISIBLE) and st.contains(pyatspi.STATE_SHOWING)
    except Exception:
        return False


def extents(acc):
    try:
        return acc.getExtents(pyatspi.DESKTOP_COORDS)
    except Exception:
        try:
            return acc.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
        except Exception:
            return None


def walk(acc, depth):
    if state["budget"] <= 0 or state["visited"] > MAX_VISIT or depth > 20:
        return
    state["visited"] += 1
    if not showing(acc):
        return
    try:
        role = acc.getRoleName()
    except Exception:
        return
    name = ""
    try:
        name = " ".join((acc.name or "").split())
    except Exception:
        pass
    ru = RU.get(role)
    if ru and (name or role not in NEED_NAME):
        e = extents(acc)
        if e and e.width > 0 and e.height > 0:
            cx, cy = e.x + e.width // 2, e.y + e.height // 2
            nm = name[:70]
            key = (role, nm, cx, cy)
            if key not in state["seen"]:
                state["seen"].add(key)
                if nm:
                    state["out"].append(f"{ru:15s} [{cx:4d},{cy:4d}]  «{nm}»")
                else:
                    state["out"].append(f"{ru:15s} [{cx:4d},{cy:4d}]")
                state["budget"] -= 1
    try:
        n = acc.childCount
    except Exception:
        return
    for i in range(min(n, 90)):
        try:
            child = acc.getChildAtIndex(i)
        except Exception:
            continue
        if child:
            walk(child, depth + 1)


def active_window():
    try:
        p = subprocess.run("xdotool getactivewindow getwindowname", shell=True,
                           capture_output=True, text=True, timeout=4)
        s = p.stdout.strip().splitlines()
        return s[0] if s else ""
    except Exception:
        return ""


def main():
    filt = " ".join(sys.argv[1:]).strip().lower()
    state["budget"] = LIMIT_FILTERED if filt else LIMIT_ALL

    try:
        desktop = pyatspi.Registry.getDesktop(0)
    except Exception as ex:
        print(f"ОШИБКА сонара: нет доступа к AT-SPI ({ex})")
        sys.exit(1)

    wins = []           # (app, winname, acc окна)
    apps_fallback = []  # приложения без видимых окон (gnome-shell и пр.)
    for i in range(desktop.childCount):
        try:
            app = desktop.getChildAtIndex(i)
        except Exception:
            continue
        try:
            appname = (app.name or "").strip() or "?"
        except Exception:
            appname = "?"
        found = False
        try:
            n = app.childCount
        except Exception:
            continue
        for j in range(min(n, 40)):
            try:
                c = app.getChildAtIndex(j)
                role = c.getRoleName()
            except Exception:
                continue
            if role in WIN_ROLES and showing(c):
                try:
                    wname = " ".join((c.name or "").split()) or appname
                except Exception:
                    wname = appname
                found = True
                wins.append((appname, wname, c))
        if not found:
            apps_fallback.append((appname, app))

    if filt:
        wins = [w for w in wins if filt in (w[0] + " " + w[1]).lower()]
        apps_fallback = []

    aw = active_window()
    print("АКТИВНОЕ ОКНО: «" + aw + "»" if aw else "АКТИВНОЕ ОКНО: (неизвестно)")
    if wins:
        print("ОКОН НА ЭКРАНЕ:", "; ".join(sorted({w[1][:40] for w in wins})[:8]))

    for appname, wname, acc in wins:
        if state["budget"] <= 0:
            break
        state["out"].append(f"--- ОКНО «{wname[:60]}» ({appname}) ---")
        walk(acc, 0)
    # неоконные приложения (верхняя панель gnome-shell и т.п.) — только без фильтра
    if not filt and state["budget"] > 40:
        for appname, app in apps_fallback[:6]:
            walk(app, 0)

    print("-" * 60)
    real_lines = [l for l in state["out"] if not l.startswith("--- ОКНО")]
    if len(real_lines) < 2:
        print("(сонар почти ничего не видит — Accessibility выключена?)")
        print("Включи один раз и перезагрузи ВМ:")
        print("  gsettings set org.gnome.desktop.interface toolkit-accessibility true")
    else:
        for l in state["out"]:
            print(l)


if __name__ == "__main__":
    main()
