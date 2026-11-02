"""Hotkey Translator: выделите текст, нажмите горячую клавишу, получите перевод."""
import json
import platform
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import pyperclip
from pynput import keyboard

from providers import LANG_NAMES, REGISTRY

APP_NAME = "HotkeyTranslator"
IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"
FROZEN = getattr(sys, "frozen", False)
BASE_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
MOD = keyboard.Key.cmd if IS_MAC else keyboard.Key.ctrl

MODE_LABELS = {"popup": "Показать в окне", "replace": "Заменить текст"}
PROVIDER_FIELDS = {
    "google_cloud": ["api_key"],
    "deepl": ["api_key"],
    "libretranslate": ["url", "api_key"],
    "openai": ["base_url", "model", "api_key"],
    "anthropic": ["model", "api_key"],
}
FIELD_LABELS = {"api_key": "API-ключ", "url": "URL сервера", "base_url": "Base URL", "model": "Модель"}

DEFAULT_CONFIG = {
    "hotkeys": [
        {"keys": "<ctrl>+<alt>+t", "target": "ru", "provider": "google_free", "mode": "popup"},
        {"keys": "<ctrl>+<alt>+e", "target": "en", "provider": "google_free", "mode": "popup"},
        {"keys": "<ctrl>+<alt>+r", "target": "ru", "provider": "google_free", "mode": "replace"},
    ],
    "providers": {
        "google_cloud": {"api_key": "", "api_key_env": "GOOGLE_TRANSLATE_KEY"},
        "deepl": {"api_key": "", "api_key_env": "DEEPL_API_KEY"},
        "libretranslate": {"url": "https://libretranslate.com", "api_key": "", "api_key_env": "LIBRETRANSLATE_KEY"},
        "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini", "api_key": "", "api_key_env": "OPENAI_API_KEY"},
        "anthropic": {"model": "claude-haiku-4-5", "api_key": "", "api_key_env": "ANTHROPIC_API_KEY"},
    },
}

kb = keyboard.Controller()
ui_queue = queue.Queue()
busy = threading.Lock()
cfg = {}
listener = None
settings_win = None


# ---------------------------------------------------------------- конфиг ----
def load_config():
    result = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            hotkeys = user.get("hotkeys")
            if isinstance(hotkeys, dict):  # старый формат
                hotkeys = [{"keys": k, **v} for k, v in hotkeys.items()]
            if isinstance(hotkeys, list):
                result["hotkeys"] = [
                    {"keys": h.get("keys", ""), "target": h.get("target", "ru"),
                     "provider": h.get("provider", "google_free"), "mode": h.get("mode", "popup")}
                    for h in hotkeys
                ]
            for name, values in user.get("providers", {}).items():
                result["providers"].setdefault(name, {}).update(values)
        except Exception:
            pass
    return result


def save_config():
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def make_provider(name):
    if name not in REGISTRY:
        raise ValueError(f"Неизвестный провайдер '{name}'")
    return REGISTRY[name](cfg["providers"].get(name, {}))


# ----------------------------------------------------- перевод выделения ----
def safe_paste():
    try:
        return pyperclip.paste()
    except Exception:
        return ""


def press_combo(char):
    with kb.pressed(MOD):
        kb.press(char)
        kb.release(char)


def get_selection():
    """Копирует выделенный текст через Ctrl+C. Возвращает (текст, старый_буфер)."""
    old = safe_paste()
    pyperclip.copy("")
    for k in (keyboard.Key.alt, keyboard.Key.shift):  # отпускаем модификаторы хоткея
        kb.release(k)
    time.sleep(0.1)
    press_combo("c")
    text = ""
    for _ in range(15):
        time.sleep(0.05)
        text = safe_paste()
        if text:
            break
    return text, old


def run(hk):
    if not busy.acquire(blocking=False):
        return
    old = None
    try:
        text, old = get_selection()
        if not text.strip():
            pyperclip.copy(old or "")
            return
        provider = hk.get("provider", "google_free")
        target = hk.get("target", "ru")
        result = make_provider(provider).translate(text, target)
        if hk.get("mode") == "replace":
            pyperclip.copy(result)
            time.sleep(0.1)
            press_combo("v")
            time.sleep(0.3)
            pyperclip.copy(old or "")
        else:
            pyperclip.copy(old or "")
            ui_queue.put(("popup", result, f"{provider} → {target}"))
    except Exception as e:
        if old is not None:
            pyperclip.copy(old)
        ui_queue.put(("popup", f"Ошибка: {e}", "error"))
    finally:
        busy.release()


# -------------------------------------------------------- глобальные хоткеи ----
def stop_hotkeys():
    global listener
    if listener is not None:
        listener.stop()
        listener = None


def apply_hotkeys():
    global listener
    stop_hotkeys()
    mapping = {}
    for hk in cfg["hotkeys"]:
        if hk.get("keys"):
            mapping[hk["keys"]] = lambda hk=hk: threading.Thread(target=run, args=(hk,), daemon=True).start()
    try:
        listener = keyboard.GlobalHotKeys(mapping)
        listener.start()
    except Exception as e:
        listener = None
        ui_queue.put(("popup", f"Не удалось назначить горячие клавиши: {e}", "error"))


# ------------------------------------------------------ запись комбинации ----
MODS = {}
for _name, _label in (("ctrl", "<ctrl>"), ("alt", "<alt>"), ("shift", "<shift>"), ("cmd", "<cmd>")):
    for _suffix in ("", "_l", "_r"):
        if hasattr(keyboard.Key, _name + _suffix):
            MODS[getattr(keyboard.Key, _name + _suffix)] = _label
if hasattr(keyboard.Key, "alt_gr"):
    MODS[keyboard.Key.alt_gr] = "<alt>"
MOD_ORDER = ["<ctrl>", "<alt>", "<shift>", "<cmd>"]


def key_to_str(key):
    if key in MODS:
        return MODS[key]
    if isinstance(key, keyboard.Key):
        return f"<{key.name}>"
    vk = getattr(key, "vk", None)
    ch = getattr(key, "char", None)
    if IS_WIN and vk and (48 <= vk <= 57 or 65 <= vk <= 90):
        return chr(vk).lower()
    if ch and ch.isprintable() and ch not in " +":
        return ch.lower()
    return f"<{vk}>" if vk else None


def pretty(combo):
    names = {"<ctrl>": "Ctrl", "<alt>": "Alt", "<shift>": "Shift", "<cmd>": "Win" if IS_WIN else "Cmd"}
    return "+".join(names.get(p, p.strip("<>").upper()) for p in combo.split("+")) if combo else ""


class Capture:
    """Слушает клавиатуру, пока пользователь не нажмёт и не отпустит комбинацию."""

    def __init__(self):
        self.pressed = []
        self.result = None
        self.done = False
        self.listener = keyboard.Listener(on_press=self.on_press, on_release=self.on_release)
        self.listener.start()

    def combo(self):
        mods = [m for m in MOD_ORDER if m in self.pressed]
        return "+".join(mods + [p for p in self.pressed if p not in MOD_ORDER])

    def on_press(self, key):
        s = key_to_str(key)
        if s == "<esc>":
            self.done = True
            return False
        if s and s not in self.pressed:
            self.pressed.append(s)

    def on_release(self, key):
        if any(p not in MOD_ORDER for p in self.pressed):
            self.result = self.combo()
            self.done = True
            return False


# --------------------------------------------------------------- автозапуск ----
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def get_autostart():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except Exception:
        return False


def set_autostart(enabled):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, f'"{sys.executable}"')
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


# ------------------------------------------------------------------- окна ----
def lang_label(code):
    return f"{code} — {LANG_NAMES[code]}" if code in LANG_NAMES else code


def edit_dialog(parent, hk):
    """Диалог одной горячей клавиши. Возвращает dict или None."""
    result = {}
    dlg = tk.Toplevel(parent)
    dlg.title("Горячая клавиша")
    dlg.transient(parent)
    dlg.resizable(False, False)
    pad = {"padx": 8, "pady": 5}

    raw = tk.StringVar(value=hk["keys"])
    shown = tk.StringVar(value=pretty(hk["keys"]))
    target = tk.StringVar(value=lang_label(hk["target"]))
    provider = tk.StringVar(value=hk["provider"])
    mode = tk.StringVar(value=MODE_LABELS.get(hk["mode"], MODE_LABELS["popup"]))

    ttk.Label(dlg, text="Клавиши").grid(row=0, column=0, sticky="w", **pad)
    ttk.Entry(dlg, textvariable=shown, state="readonly", width=24).grid(row=0, column=1, **pad)
    btn = ttk.Button(dlg, text="Записать")
    btn.grid(row=0, column=2, **pad)

    ttk.Label(dlg, text="Перевести на").grid(row=1, column=0, sticky="w", **pad)
    ttk.Combobox(dlg, textvariable=target, values=[lang_label(c) for c in LANG_NAMES], width=22).grid(row=1, column=1, columnspan=2, sticky="we", **pad)
    ttk.Label(dlg, text="Переводчик").grid(row=2, column=0, sticky="w", **pad)
    ttk.Combobox(dlg, textvariable=provider, values=list(REGISTRY), state="readonly", width=22).grid(row=2, column=1, columnspan=2, sticky="we", **pad)
    ttk.Label(dlg, text="Результат").grid(row=3, column=0, sticky="w", **pad)
    ttk.Combobox(dlg, textvariable=mode, values=list(MODE_LABELS.values()), state="readonly", width=22).grid(row=3, column=1, columnspan=2, sticky="we", **pad)
    ttk.Label(dlg, text="Esc во время записи — отмена", foreground="#888").grid(row=4, column=0, columnspan=3, sticky="w", **pad)

    def start_capture():
        btn.config(text="Нажмите комбинацию…", state="disabled")
        cap = Capture()

        def poll():
            if cap.done:
                if cap.result:
                    raw.set(cap.result)
                    shown.set(pretty(cap.result))
                btn.config(text="Записать", state="normal")
            else:
                if cap.pressed:
                    shown.set(pretty(cap.combo()))
                dlg.after(50, poll)

        poll()

    btn.config(command=start_capture)

    def ok():
        if not raw.get():
            messagebox.showwarning("Hotkey Translator", "Сначала запишите комбинацию клавиш.", parent=dlg)
            return
        inv = {v: k for k, v in MODE_LABELS.items()}
        result.update(keys=raw.get(), target=target.get().split()[0], provider=provider.get(), mode=inv[mode.get()])
        dlg.destroy()

    row = ttk.Frame(dlg)
    row.grid(row=5, column=0, columnspan=3, sticky="e", **pad)
    ttk.Button(row, text="OK", command=ok).pack(side="left", padx=4)
    ttk.Button(row, text="Отмена", command=dlg.destroy).pack(side="left")

    dlg.update_idletasks()
    try:
        dlg.grab_set()
    except tk.TclError:
        pass
    parent.wait_window(dlg)
    return result or None


def open_settings(root):
    global settings_win
    if settings_win is not None and settings_win.winfo_exists():
        settings_win.lift()
        settings_win.focus_force()
        return
    stop_hotkeys()  # пока окно открыто, хоткеи не срабатывают
    win = settings_win = tk.Toplevel(root)
    win.title("Hotkey Translator — настройки")
    win.geometry("660x430")
    hotkeys = [dict(h) for h in cfg["hotkeys"]]

    nb = ttk.Notebook(win)
    nb.pack(fill="both", expand=True, padx=8, pady=(8, 0))

    # --- вкладка «Горячие клавиши»
    tab1 = ttk.Frame(nb)
    nb.add(tab1, text="Горячие клавиши")
    tree = ttk.Treeview(tab1, columns=("keys", "target", "provider", "mode"), show="headings", height=11)
    for col, title, width in (("keys", "Клавиши", 150), ("target", "Язык", 60), ("provider", "Переводчик", 140), ("mode", "Результат", 160)):
        tree.heading(col, text=title)
        tree.column(col, width=width)
    tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
    side = ttk.Frame(tab1)
    side.pack(side="right", fill="y", padx=8, pady=8)

    def refresh():
        tree.delete(*tree.get_children())
        for i, h in enumerate(hotkeys):
            tree.insert("", "end", iid=str(i), values=(pretty(h["keys"]), h["target"], h["provider"], MODE_LABELS.get(h["mode"], "")))

    def selected():
        sel = tree.selection()
        return int(sel[0]) if sel else None

    def add():
        r = edit_dialog(win, {"keys": "", "target": "ru", "provider": "google_free", "mode": "popup"})
        if r:
            hotkeys.append(r)
            refresh()

    def edit(_event=None):
        i = selected()
        if i is not None:
            r = edit_dialog(win, hotkeys[i])
            if r:
                hotkeys[i] = r
                refresh()

    def delete():
        i = selected()
        if i is not None:
            hotkeys.pop(i)
            refresh()

    ttk.Button(side, text="Добавить", command=add).pack(fill="x", pady=2)
    ttk.Button(side, text="Изменить", command=edit).pack(fill="x", pady=2)
    ttk.Button(side, text="Удалить", command=delete).pack(fill="x", pady=2)
    tree.bind("<Double-1>", edit)
    refresh()

    # --- вкладка «Переводчики»
    tab2 = ttk.Frame(nb)
    nb.add(tab2, text="Переводчики и ключи")
    entries = {}
    ttk.Label(tab2, text="google_free работает без ключа. Остальным нужен ключ или адрес сервера.", foreground="#666").pack(anchor="w", padx=10, pady=(8, 2))
    for name, fields in PROVIDER_FIELDS.items():
        box = ttk.LabelFrame(tab2, text=name)
        box.pack(fill="x", padx=10, pady=3)
        for j, f in enumerate(fields):
            var = tk.StringVar(value=cfg["providers"].get(name, {}).get(f, ""))
            entries[(name, f)] = var
            ttk.Label(box, text=FIELD_LABELS[f], width=11).grid(row=j, column=0, sticky="w", padx=6, pady=2)
            ttk.Entry(box, textvariable=var, width=60, show="*" if f == "api_key" else "").grid(row=j, column=1, padx=6, pady=2, sticky="we")
        box.columnconfigure(1, weight=1)

    # --- низ окна
    bottom = ttk.Frame(win)
    bottom.pack(fill="x", padx=8, pady=8)
    auto = tk.BooleanVar(value=get_autostart() if IS_WIN and FROZEN else False)
    if IS_WIN and FROZEN:
        ttk.Checkbutton(bottom, text="Запускать вместе с Windows", variable=auto).pack(side="left")

    def close():
        global settings_win
        win.destroy()
        settings_win = None
        apply_hotkeys()

    def save():
        cfg["hotkeys"] = hotkeys
        for (name, f), var in entries.items():
            cfg["providers"].setdefault(name, {})[f] = var.get().strip()
        try:
            save_config()
            if IS_WIN and FROZEN:
                set_autostart(auto.get())
        except Exception as e:
            messagebox.showerror("Hotkey Translator", f"Не удалось сохранить: {e}", parent=win)
            return
        close()

    ttk.Button(bottom, text="Сохранить", command=save).pack(side="right", padx=(6, 0))
    ttk.Button(bottom, text="Отмена", command=close).pack(side="right")
    win.protocol("WM_DELETE_WINDOW", close)
    win.focus_force()


class Popup:
    def __init__(self, root):
        self.root = root
        self.win = None

    def show(self, text, title):
        self.close()
        w = self.win = tk.Toplevel(self.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.configure(bg="#444", padx=1, pady=1)
        w.geometry(f"+{self.root.winfo_pointerx() + 10}+{self.root.winfo_pointery() + 10}")
        frame = tk.Frame(w, bg="#1e1e1e")
        frame.pack()
        tk.Label(frame, text=title, bg="#1e1e1e", fg="#888", font=("Segoe UI", 8)).pack(anchor="w", padx=8, pady=(6, 0))
        lines = len(text) // 55 + text.count("\n") + 1
        box = tk.Text(frame, wrap="word", width=60, height=min(15, max(2, lines)), bg="#1e1e1e", fg="#eee",
                      relief="flat", font=("Segoe UI", 11), padx=8, pady=4)
        box.insert("1.0", text)
        box.configure(state="disabled")
        box.pack()
        row = tk.Frame(frame, bg="#1e1e1e")
        row.pack(fill="x", padx=8, pady=(0, 6))
        tk.Button(row, text="Копировать", command=lambda: (pyperclip.copy(text), self.close())).pack(side="left")
        tk.Button(row, text="Закрыть (Esc)", command=self.close).pack(side="right")
        w.bind("<Escape>", lambda e: self.close())
        w.focus_force()

    def close(self):
        if self.win is not None:
            self.win.destroy()
            self.win = None


# --------------------------------------------------------------------- трей ----
def start_tray():
    try:
        import pystray
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (64, 64), "#2f6fed")
        d = ImageDraw.Draw(img)
        d.rectangle((14, 12, 50, 22), fill="white")
        d.rectangle((27, 22, 37, 52), fill="white")
        menu = pystray.Menu(
            pystray.MenuItem("Настройки", lambda: ui_queue.put(("settings",)), default=True),
            pystray.MenuItem("Выход", lambda: ui_queue.put(("quit",))),
        )
        icon = pystray.Icon(APP_NAME, img, "Hotkey Translator", menu)
        icon.run_detached()
        return icon
    except Exception:
        return None


def main():
    global cfg
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    first_run = not CONFIG_PATH.exists()
    cfg = load_config()
    if first_run:
        save_config()

    root = tk.Tk()
    root.withdraw()
    popup = Popup(root)
    icon = start_tray()
    apply_hotkeys()

    def poll():
        try:
            while True:
                msg = ui_queue.get_nowait()
                if msg[0] == "popup":
                    popup.show(msg[1], msg[2])
                elif msg[0] == "settings":
                    open_settings(root)
                elif msg[0] == "quit":
                    stop_hotkeys()
                    if icon:
                        icon.stop()
                    root.quit()
                    return
        except queue.Empty:
            pass
        root.after(100, poll)

    root.after(100, poll)
    if first_run or icon is None:
        root.after(300, lambda: open_settings(root))
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
