"""Configurable Windows HUD with a global keyboard hook in its own process."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import multiprocessing as mp
import queue
import threading
import time
import tkinter as tk
from pathlib import Path

from PIL import Image, ImageOps, ImageSequence, ImageTk


KEY_CODES = {**{ord(ch): ch for ch in "WASDQERF1234"},
             0x10: "Shift", 0xA0: "Shift", 0xA1: "Shift",
             0x11: "Ctrl", 0xA2: "Ctrl", 0xA3: "Ctrl",
             0x20: "Space", 0x09: "Tab", 0x1B: "Esc",
             0x26: "Up", 0x28: "Down", 0x25: "Left", 0x27: "Right"}
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


class FpsOverlay:
    def __init__(self, stats, settings: dict):
        self._stats = stats
        self._settings = settings.copy()
        self._commands = mp.Queue()
        self._ready = mp.Event()
        self._errors = mp.Queue()
        self._stop_event = threading.Event()
        self._process = mp.Process(target=_run_overlay,
                                   args=(self._commands, self._settings, self._ready, self._errors),
                                   daemon=True, name="VeloxOverlay")
        self._thread = threading.Thread(target=self._feed_stats, daemon=True, name="VeloxOverlayStats")

    def start(self):
        self._process.start()
        if not self._ready.wait(8):
            raise RuntimeError("Оверлей не открылся за 8 секунд")
        if not self._errors.empty():
            raise RuntimeError(f"Оверлей не запустился: {self._errors.get()}")
        self._thread.start()

    def update(self, settings: dict):
        self._commands.put(("settings", settings.copy()))

    def stop(self):
        self._stop_event.set()
        self._commands.put(("stop", None))
        if self._thread.is_alive():
            self._thread.join(timeout=2)
        self._process.join(timeout=4)
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=2)

    def _feed_stats(self):
        while not self._stop_event.is_set() and self._process.is_alive():
            try:
                self._commands.put(("stats", self._stats()))
            except Exception:
                pass
            self._stop_event.wait(1)


def _run_overlay(commands, settings, ready, errors):
    _OverlayWorker(commands, settings, ready, errors).run()


class _OverlayWorker:
    def __init__(self, commands, settings, ready, errors):
        self._commands = commands
        self._settings = settings
        self._ready = ready
        self._errors = errors

    def run(self):
        hook = None
        root = None
        try:
            root = tk.Tk()
            root.title("Velox HUD")
            root.overrideredirect(True)
            root.attributes("-topmost", True)
            root.configure(bg="#010203")
            root.attributes("-transparentcolor", "#010203")

            card = tk.Frame(root, bg="#101821", highlightbackground="#314354",
                            highlightthickness=1, padx=9, pady=7)
            card.pack(padx=1, pady=1)
            header = tk.Frame(card, bg="#101821")
            header.pack(fill="x")
            avatar = tk.Label(header, bg="#101821")
            avatar.pack(side="left", padx=(0, 8))
            identity = tk.Frame(header, bg="#101821")
            identity.pack(side="left", fill="x", expand=True)
            nick = tk.Label(identity, bg="#101821", fg="#e9f4f1", font=("Segoe UI", 11, "bold"), anchor="w")
            nick.pack(fill="x")
            dpi = tk.Label(identity, bg="#101821", fg="#1ed8a0", font=("Consolas", 9), anchor="w")
            dpi.pack(fill="x")
            numbers = tk.Label(card, bg="#101821", fg="#cbd9e4", font=("Consolas", 10), anchor="w")
            numbers.pack(fill="x", pady=(5, 4))
            keys_row = tk.Frame(card, bg="#101821")
            keys_row.pack(fill="x")

            pressed: set[str] = set()
            key_labels: dict[str, tk.Label] = {}
            frames: list[tuple[ImageTk.PhotoImage, int]] = []
            frame_index = 0
            next_frame = 0.0

            def position():
                root.update_idletasks()
                width, height = root.winfo_reqwidth(), root.winfo_reqheight()
                offset_x = int(self._settings["offset_x"])
                offset_y = int(self._settings["offset_y"])
                x = offset_x if self._settings["position"] == "top_left" else root.winfo_screenwidth() - width - offset_x
                root.geometry(f"{width}x{height}+{max(0, x)}+{offset_y}")

            def load_image():
                nonlocal frames, frame_index, next_frame
                frames = []
                frame_index = 0
                next_frame = 0.0
                path = self._settings.get("image_path")
                if path and Path(path).is_file():
                    try:
                        with Image.open(path) as image:
                            for index, source in enumerate(ImageSequence.Iterator(image)):
                                if index >= 120:
                                    break
                                picture = ImageOps.fit(source.convert("RGBA"), (40, 40))
                                delay = max(40, min(1000, int(source.info.get("duration", 100))))
                                frames.append((ImageTk.PhotoImage(picture, master=root), delay))
                    except Exception:
                        frames = []
                if frames:
                    avatar.configure(image=frames[0][0], width=40, height=40)
                    next_frame = time.monotonic() + frames[0][1] / 1000
                else:
                    avatar.configure(image="", width=0, height=0)

            def configure():
                nick.configure(text=self._settings["nickname"] or "Player")
                dpi.configure(text=f"DPI {self._settings['dpi']}")
                for child in keys_row.winfo_children():
                    child.destroy()
                key_labels.clear()
                for key in self._settings["keys"]:
                    label = tk.Label(keys_row, text=key, bg="#263442", fg="#b7c7d2",
                                     font=("Consolas", 9, "bold"), padx=5, pady=3)
                    label.pack(side="left", padx=(0, 3))
                    key_labels[key] = label
                load_image()
                position()

            def hook_callback(code, wparam, lparam):
                if code >= 0:
                    try:
                        key = KEY_CODES.get(ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents.vkCode)
                        if key:
                            if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                                self._commands.put(("key_down", key))
                            elif wparam in (WM_KEYUP, WM_SYSKEYUP):
                                self._commands.put(("key_up", key))
                    except Exception:
                        pass
                return user32.CallNextHookEx(hook, code, wparam, lparam)

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
            self._hook_callback = callback_type(hook_callback)
            user32.SetWindowsHookExW.argtypes = [ctypes.c_int, callback_type, wintypes.HINSTANCE, wintypes.DWORD]
            user32.SetWindowsHookExW.restype = wintypes.HHOOK
            user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
            user32.CallNextHookEx.restype = ctypes.c_ssize_t
            user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
            user32.UnhookWindowsHookEx.restype = wintypes.BOOL
            kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE
            hook = user32.SetWindowsHookExW(13, self._hook_callback, kernel32.GetModuleHandleW(None), 0)
            if not hook:
                raise ctypes.WinError(ctypes.get_last_error())
            configure()

            def tick():
                nonlocal frame_index, next_frame
                try:
                    while True:
                        command, value = self._commands.get_nowait()
                        if command == "stop":
                            root.destroy()
                            return
                        if command == "settings":
                            self._settings = value
                            configure()
                        elif command == "stats":
                            try:
                                fmt = lambda item: str(round(item)) if isinstance(item, (int, float)) else "—"
                                fps = (value.get("fps") or {}).get("current")
                                numbers.configure(text=f"CPU {fmt(value['cpu']['load'])}%   GPU {fmt(value['gpu']['load'])}%   FPS {fmt(fps)}")
                            except Exception:
                                numbers.configure(text="CPU —%   GPU —%   FPS —")
                        elif command == "key_down":
                            pressed.add(value)
                        elif command == "key_up":
                            pressed.discard(value)
                except queue.Empty:
                    pass
                for key, label in key_labels.items():
                    active = key in pressed
                    label.configure(bg="#1ed8a0" if active else "#263442",
                                    fg="#061c1a" if active else "#b7c7d2")
                now = time.monotonic()
                if len(frames) > 1 and now >= next_frame:
                    frame_index = (frame_index + 1) % len(frames)
                    avatar.configure(image=frames[frame_index][0])
                    next_frame = now + frames[frame_index][1] / 1000
                root.after(50, tick)

            self._ready.set()
            tick()
            root.mainloop()
        except Exception as exc:
            self._errors.put(str(exc))
            self._ready.set()
        finally:
            if hook:
                try:
                    user32.UnhookWindowsHookEx(hook)
                except Exception:
                    pass
            if root:
                try:
                    root.destroy()
                except Exception:
                    pass
