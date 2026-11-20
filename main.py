"""
Velox — оптимизатор игр. Запуск интерфейса в окне pywebview + Python-бэкенд.

Установка:
    pip install -r requirements.txt

Запуск (рекомендуются права администратора для защищённых игровых файлов):
    python main.py
    python main.py --debug      # с DevTools

Структура:
    main.py
    web/index.html
    web/style.css
    web/app.js

Что делает бэкенд:
    get_system_stats      psutil + NVML (NVIDIA). Температура CPU в Windows без драйвера
                          недоступна — отдаётся None, интерфейс покажет «—».
                          FPS: см. _read_fps() — нужен внешний источник (PresentMon и т.п.).
    apply_vibrance        NVIDIA NvAPI (nvapi64.dll). На AMD/Intel вернёт понятную ошибку.
    apply_resolution      EnumDisplaySettingsW / ChangeDisplaySettingsW (ctypes).
    reset_resolution      возвращает режим, сохранённый при первом запуске.
    apply_game_preset     обновляет настройки игр + .bak перед первым изменением.
    toggle_fps_overlay    окно tkinter поверх оконных/безрамочных игр.
    turbo_boost           EmptyWorkingSet для всех процессов + высокий приоритет запущенной игре.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import glob
import json
import multiprocessing as mp
import os
import platform
import re
import shutil
import tempfile
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import psutil
import webview
from overlay import FpsOverlay
from nvidia_profile import import_fortnite_profile
from display_effects import GameContrast

IS_WIN = sys.platform == "win32"

try:
    import pynvml

    pynvml.nvmlInit()
    NVML = True
except Exception:
    pynvml = None
    NVML = False

if IS_WIN:
    import winreg
else:
    winreg = None

# PyInstaller кладёт файлы в _MEIPASS; пользовательские данные — в %APPDATA%\Velox
BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
WEB_INDEX = BASE / "web" / "index.html"
DATA = Path(os.environ.get("APPDATA", str(Path.home()))) / "Velox"
BACKUPS = DATA / "backups"
ORIGINAL_MODE = DATA / "original_display_mode.json"
OVERLAY_SETTINGS_FILE = DATA / "overlay_settings.json"
OVERLAY_IMAGE_FILE = DATA / "overlay_avatar"
CONTRAST_SETTINGS_FILE = DATA / "game_contrast.json"
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))

OVERLAY_KEYS = ("W", "A", "S", "D", "Q", "E", "R", "F", "1", "2", "3", "4",
                "Shift", "Ctrl", "Space", "Tab", "Esc", "Up", "Down", "Left", "Right")
OVERLAY_DEFAULTS = {"position": "top_left", "offset_x": 16, "offset_y": 16,
                    "dpi": 800, "nickname": "Player", "keys": ["Q", "W", "E", "Shift", "A", "S", "D", "Ctrl", "Space"],
                    "image_path": None}


class _PointL(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _DisplayUnion(ctypes.Structure):
    _fields_ = [("dmPosition", _PointL), ("dmDisplayOrientation", wintypes.DWORD),
                ("dmDisplayFixedOutput", wintypes.DWORD)]


class _PrinterUnion(ctypes.Structure):
    _fields_ = [("_reserved", ctypes.c_byte * 16)]


class _ModeUnion(ctypes.Union):
    _fields_ = [("display", _DisplayUnion), ("printer", _PrinterUnion)]


class DEVMODEW(ctypes.Structure):
    _anonymous_ = ("_mode",)
    _fields_ = [
        ("dmDeviceName", wintypes.WCHAR * 32), ("dmSpecVersion", wintypes.WORD),
        ("dmDriverVersion", wintypes.WORD), ("dmSize", wintypes.WORD),
        ("dmDriverExtra", wintypes.WORD), ("dmFields", wintypes.DWORD),
        ("_mode", _ModeUnion), ("dmColor", wintypes.SHORT),
        ("dmDuplex", wintypes.SHORT), ("dmYResolution", wintypes.SHORT),
        ("dmTTOption", wintypes.SHORT), ("dmCollate", wintypes.SHORT),
        ("dmFormName", wintypes.WCHAR * 32), ("dmLogPixels", wintypes.WORD),
        ("dmBitsPerPel", wintypes.DWORD), ("dmPelsWidth", wintypes.DWORD),
        ("dmPelsHeight", wintypes.DWORD), ("dmDisplayFlags", wintypes.DWORD),
        ("dmDisplayFrequency", wintypes.DWORD), ("dmICMMethod", wintypes.DWORD),
        ("dmICMIntent", wintypes.DWORD), ("dmMediaType", wintypes.DWORD),
        ("dmDitherType", wintypes.DWORD), ("dmReserved1", wintypes.DWORD),
        ("dmReserved2", wintypes.DWORD), ("dmPanningWidth", wintypes.DWORD),
        ("dmPanningHeight", wintypes.DWORD),
    ]


ENUM_CURRENT_SETTINGS = -1
CDS_UPDATEREGISTRY = 0x00000001
CDS_TEST = 0x00000002
DISP_CHANGE_SUCCESSFUL = 0
DM_PELSWIDTH = 0x00080000
DM_PELSHEIGHT = 0x00100000
DM_DISPLAYFREQUENCY = 0x00400000
DM_DISPLAYFIXEDOUTPUT = 0x20000000


def _enum_mode(index: int) -> DEVMODEW | None:
    if not IS_WIN:
        return None
    mode = DEVMODEW()
    mode.dmSize = ctypes.sizeof(DEVMODEW)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    fn = user32.EnumDisplaySettingsW
    fn.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(DEVMODEW)]
    fn.restype = wintypes.BOOL
    return mode if fn(None, index, ctypes.byref(mode)) else None


def _change_mode(mode: DEVMODEW, flags: int) -> int:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    fn = user32.ChangeDisplaySettingsW
    fn.argtypes = [ctypes.POINTER(DEVMODEW), wintypes.DWORD]
    fn.restype = wintypes.LONG
    return fn(ctypes.byref(mode), flags)


def _mode_data(mode: DEVMODEW) -> dict:
    return {"width": int(mode.dmPelsWidth), "height": int(mode.dmPelsHeight),
            "hz": int(mode.dmDisplayFrequency)}


# =====================================================================
#  Игры: процессы, файлы настроек, ключи пресетов
# =====================================================================
GAME_INFO = {
    "fortnite": {"name": "Fortnite", "exe": ["fortniteclient-win64-shipping.exe"], "fmt": "ini"},
    "cs2": {"name": "Counter-Strike 2", "exe": ["cs2.exe"], "fmt": "vdf"},
    "rust": {"name": "Rust", "exe": ["rustclient.exe"], "fmt": "cfg"},
    "valorant": {"name": "Valorant", "exe": ["valorant-win64-shipping.exe"], "fmt": "ini"},
}

# Ключи меняются только если уже есть в файле — неизвестные ключи игра не получит.
PRESET_KEYS = {
    "fortnite": {
        "esports": {
            "sg.ResolutionQuality": "100.000000", "sg.ViewDistanceQuality": "1", "sg.AntiAliasingQuality": "0",
            "sg.GlobalIlluminationQuality": "0", "sg.ReflectionQuality": "0", "sg.PostProcessQuality": "0",
            "sg.TextureQuality": "1", "sg.EffectsQuality": "0", "sg.FoliageQuality": "0", "sg.ShadingQuality": "0",
            "bUseVSync": "False", "bMotionBlur": "False",
        },
        "potato": {
            "sg.ResolutionQuality": "75.000000", "sg.ViewDistanceQuality": "0", "sg.AntiAliasingQuality": "0",
            "sg.GlobalIlluminationQuality": "0", "sg.ReflectionQuality": "0", "sg.PostProcessQuality": "0",
            "sg.TextureQuality": "0", "sg.EffectsQuality": "0", "sg.FoliageQuality": "0", "sg.ShadingQuality": "0",
            "bUseVSync": "False", "bMotionBlur": "False", "MeshQuality": "0",
        },
    },
    "cs2": {
        "esports": {
            "setting.shaderquality": "0", "setting.r_texturefilteringquality": "3", "setting.msaa_samples": "2",
            "setting.r_csgo_cmaa_enable": "0", "setting.videocfg_texture_detail": "1",
            "setting.videocfg_particle_detail": "0", "setting.videocfg_ao_detail": "0",
            "setting.videocfg_fsr_detail": "0", "setting.mat_vsync": "0",
        },
        "potato": {
            "setting.shaderquality": "0", "setting.r_texturefilteringquality": "0", "setting.msaa_samples": "0",
            "setting.r_csgo_cmaa_enable": "0", "setting.videocfg_texture_detail": "0",
            "setting.videocfg_particle_detail": "0", "setting.videocfg_ao_detail": "0",
            "setting.videocfg_dynamic_shadows": "0", "setting.videocfg_fsr_detail": "0", "setting.mat_vsync": "0",
        },
    },
    "rust": {
        "esports": {
            "graphics.drawdistance": "1500", "grass.quality": "0", "effects.ao": "False", "effects.motionblur": "False",
            "effects.bloom": "False", "effects.shafts": "False", "graphics.dof": "False", "particle.quality": "0",
            "water.quality": "0", "graphics.parallax": "0",
        },
        "potato": {
            "graphics.drawdistance": "1000", "grass.quality": "0", "grass.displace": "False", "effects.ao": "False",
            "effects.motionblur": "False", "effects.bloom": "False", "effects.shafts": "False", "graphics.dof": "False",
            "particle.quality": "0", "water.quality": "0", "graphics.parallax": "0", "tree.quality": "0",
            "terrain.quality": "0", "mesh.quality": "0", "graphics.af": "1",
        },
    },
    "valorant": {
        "esports": {
            "sg.ResolutionQuality": "100.000000", "sg.ViewDistanceQuality": "1", "sg.AntiAliasingQuality": "0",
            "sg.PostProcessQuality": "0", "sg.TextureQuality": "1", "sg.EffectsQuality": "0",
            "sg.FoliageQuality": "0", "sg.ShadingQuality": "1",
        },
        "potato": {
            "sg.ResolutionQuality": "100.000000", "sg.ViewDistanceQuality": "0", "sg.AntiAliasingQuality": "0",
            "sg.PostProcessQuality": "0", "sg.TextureQuality": "0", "sg.EffectsQuality": "0",
            "sg.FoliageQuality": "0", "sg.ShadingQuality": "0",
        },
    },
}

# (значения при включённом тумблере, значения при выключенном)
OPTION_KEYS = {
    "fortnite": {
        "shadows": ({"sg.ShadowQuality": "0"}, {"sg.ShadowQuality": "1"}),
        "lod": ({"sg.ViewDistanceQuality": "0", "sg.TextureQuality": "0", "sg.FoliageQuality": "0"}, {}),
        "latency": ({}, {}),
    },
    "cs2": {
        "shadows": ({"setting.videocfg_shadow_quality": "0"}, {"setting.videocfg_shadow_quality": "1"}),
        "lod": ({"setting.videocfg_texture_detail": "0", "setting.r_texturefilteringquality": "0"}, {}),
        "latency": ({"setting.r_low_latency": "1"}, {"setting.r_low_latency": "0"}),
    },
    "rust": {
        "shadows": ({"graphics.shadowmode": "0", "graphics.shadowlights": "0"}, {"graphics.shadowmode": "1"}),
        "lod": ({"graphics.lodbias": "0.25", "mesh.quality": "0", "tree.quality": "0"}, {"graphics.lodbias": "1"}),
        "latency": ({}, {}),
    },
    "valorant": {
        "shadows": ({"sg.ShadowQuality": "0"}, {"sg.ShadowQuality": "1"}),
        "lod": ({"sg.ViewDistanceQuality": "0", "sg.TextureQuality": "0"}, {}),
        "latency": ({}, {}),
    },
}

PRESET_TITLES = {"esports": "Киберспорт", "potato": "Графика картошки", "default": "Сброс к стандарту"}


def _reg_value(root, path, name):
    if not winreg:
        return None
    try:
        with winreg.OpenKey(root, path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def _steam_libraries() -> list[Path]:
    steam = _reg_value(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath") if winreg else None
    if not steam:
        return []
    libs = [Path(steam)]
    vdf = Path(steam) / "steamapps" / "libraryfolders.vdf"
    if vdf.exists():
        text = vdf.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r'"path"\s+"([^"]+)"', text):
            libs.append(Path(m.group(1).replace("\\\\", "\\")))
    uniq, seen = [], set()
    for p in libs:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def _steam_root() -> Path | None:
    steam = _reg_value(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath") if winreg else None
    return Path(steam) if steam else None


def find_game(game_id: str) -> Path | None:
    if game_id == "cs2":
        for lib in _steam_libraries():
            p = lib / "steamapps" / "common" / "Counter-Strike Global Offensive"
            if (p / "game" / "bin" / "win64" / "cs2.exe").exists():
                return p
    elif game_id == "rust":
        for lib in _steam_libraries():
            p = lib / "steamapps" / "common" / "Rust"
            if (p / "RustClient.exe").exists():
                return p
    elif game_id == "fortnite":
        manifests = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
        for item in manifests.glob("*.item"):
            try:
                data = json.loads(item.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                continue
            if str(data.get("DisplayName", "")).lower() == "fortnite" or data.get("AppName") == "Fortnite":
                loc = Path(data.get("InstallLocation", ""))
                if loc.exists():
                    return loc
    elif game_id == "valorant":
        for part in psutil.disk_partitions(all=False):
            p = Path(part.mountpoint) / "Riot Games" / "VALORANT" / "live"
            if (p / "VALORANT.exe").exists():
                return p
    return None


def config_files(game_id: str) -> list[Path]:
    files: list[Path] = []
    if game_id == "fortnite":
        files = [LOCALAPPDATA / "FortniteGame" / "Saved" / "Config" / "WindowsClient" / "GameUserSettings.ini"]
    elif game_id == "cs2":
        root = _steam_root()
        if root:
            files = [Path(p) for p in glob.glob(str(root / "userdata" / "*" / "730" / "local" / "cfg" / "cs2_video.txt"))]
    elif game_id == "rust":
        game = find_game("rust")
        if game:
            files = [game / "cfg" / "client.cfg"]
    elif game_id == "valorant":
        base = LOCALAPPDATA / "VALORANT" / "Saved" / "Config"
        files = [Path(p) for p in glob.glob(str(base / "*" / "Windows" / "GameUserSettings.ini"))]
        files.append(base / "Windows" / "GameUserSettings.ini")
    return [f for f in files if f.exists()]


def _read(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16"), "utf-16"
    try:
        return raw.decode("utf-8-sig"), "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
    except UnicodeDecodeError:
        return raw.decode("cp1251", errors="replace"), "cp1251"


def _write(path: Path, text: str, enc: str) -> None:
    if path.exists() and not os.access(path, os.W_OK):
        raise PermissionError(f"Файл только для чтения: {path}")
    path.write_text(text, encoding=enc, newline="")


def _atomic_write(path: Path, text: str, encoding: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not os.access(path, os.W_OK):
        raise PermissionError(f"Файл только для чтения: {path}")
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _backup_once(path: Path) -> Path:
    backup = Path(str(path) + ".bak")
    if not backup.exists():
        if path.exists():
            shutil.copy2(path, backup)
        else:
            backup.touch(exist_ok=False)
    return backup


def _cfg_set(text: str, key: str, value: str) -> str:
    newline = "\r\n" if "\r\n" in text else "\n"
    pattern = re.compile(rf"(?im)^([ \t]*{re.escape(key)}[ \t]+)[^\r\n]*")
    if pattern.search(text):
        return pattern.sub(lambda match: match.group(1) + value, text)
    return text + ("" if not text or text.endswith(("\n", "\r")) else newline) + f"{key} {value}{newline}"


def _preset_files(game_id: str, config_path: str | None = None) -> list[Path]:
    if game_id == "fortnite":
        return [LOCALAPPDATA / "FortniteGame" / "Saved" / "Config" / "WindowsClient" / "GameUserSettings.ini"]
    if game_id == "cs2":
        if config_path:
            selected = Path(config_path).expanduser().resolve()
            return [selected / "autoexec.cfg" if selected.is_dir() else selected]
        root = _steam_root()
        if root:
            folders = sorted((root / "userdata").glob("*/730/local/cfg"))
            if folders:
                return [folder / "autoexec.cfg" for folder in folders]
        game = find_game("cs2")
        return [game / "game" / "csgo" / "cfg" / "autoexec.cfg"] if game else []
    if game_id == "rust":
        game = find_game("rust")
        return [game / "cfg" / "client.cfg"] if game else []
    return config_files(game_id)


def patch_text(text: str, mapping: dict, fmt: str) -> tuple[str, int]:
    """Меняет значения существующих ключей. fmt: ini (k=v), vdf ("k" "v"), cfg (k "v")."""
    changed = 0
    for key, val in mapping.items():
        k = re.escape(key)
        if fmt == "ini":
            pat = re.compile(rf"^([ \t]*{k}[ \t]*=)[^\r\n]*", re.M)
            new = lambda m, v=val: m.group(1) + v
        elif fmt == "vdf":
            pat = re.compile(rf'^([ \t]*"{k}"[ \t]+)"[^"\r\n]*"', re.M)
            new = lambda m, v=val: m.group(1) + f'"{v}"'
        else:
            pat = re.compile(rf"^([ \t]*{k}[ \t]+)[^\r\n]*", re.M)
            new = lambda m, v=val: m.group(1) + f'"{v}"'
        text, n = pat.subn(new, text)
        changed += n
    return text, changed


def ini_set(text: str, section: str, key: str, value: str) -> str:
    """Ставит key=value в секции INI, создавая ключ или секцию при необходимости."""
    nl = "\r\n" if "\r\n" in text else "\n"
    sec = re.search(rf"^\[{re.escape(section)}\][ \t]*(?:\r?\n|\Z)", text, re.M)
    if not sec:
        sep = "" if not text or text.endswith("\n") else nl
        return f"{text}{sep}{nl}[{section}]{nl}{key}={value}{nl}"
    start = sec.end()
    nxt = re.search(r"^\[", text[start:], re.M)
    end = start + nxt.start() if nxt else len(text)
    body = text[start:end]
    pat = re.compile(rf"^([ \t]*{re.escape(key)}[ \t]*=)[^\r\n]*", re.M)
    if pat.search(body):
        body = pat.sub(lambda m: m.group(1) + value, body, count=1)
    else:
        prefix = "" if sec.group(0).endswith("\n") else nl
        body = f"{prefix}{key}={value}{nl}" + body
    return text[:start] + body + text[end:]


def _ini_upsert(text: str, key: str, value: str, fallback_section: str) -> str:
    """Update the section already owning key, or add it to its normal section."""
    match = re.search(rf"(?m)^[ \t]*{re.escape(key)}[ \t]*=", text)
    if match:
        sections = list(re.finditer(r"(?m)^\[([^\]\r\n]+)\]", text[:match.start()]))
        if sections:
            return ini_set(text, sections[-1].group(1), key, value)
    return ini_set(text, fallback_section, key, value)


FORTNITE_POTATO = {
    "bUseGPUCrashDebugging": "False", "sg.ResolutionQuality": "75.000000",
    "sg.ViewDistanceQuality": "0", "sg.AntiAliasingQuality": "0",
    "sg.ShadowQuality": "0", "sg.PostProcessQuality": "0",
    "sg.TextureQuality": "0", "sg.EffectsQuality": "0",
    "sg.FoliageQuality": "0", "sg.ShadingQuality": "0",
    "PreferredRHI": "dx11", "bPreferFeatureLevelES31": "True",
    "bShowGrass": "False", "bMotionBlur": "False",
}

CS2_PRESETS = {
    "potato": {"fps_max": "0", "fps_max_ui": "120", "cl_showfps": "1",
               "r_drawtracers_firstperson": "0", "cl_autohelp": "0",
               "gameinstructor_enable": "0"},
    "esports": {"fps_max": "0", "fps_max_ui": "120", "cl_showfps": "1",
                "cl_autohelp": "0", "gameinstructor_enable": "0"},
}

RUST_POTATO = {
    "grass.quality": "0", "grass.displace": "False",
    "graphics.shadowmode": "0", "graphics.shadowlights": "0",
    "particle.quality": "0", "graphics.drawdistance": "1000",
    "effects.ao": "False", "effects.motionblur": "False",
    "effects.bloom": "False", "tree.quality": "0",
}


# =====================================================================
#  NVIDIA Digital Vibrance через NvAPI
# =====================================================================
class _DVCInfo(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint), ("currentLevel", ctypes.c_int),
                ("minLevel", ctypes.c_int), ("maxLevel", ctypes.c_int)]


class NvVibrance:
    ID_INIT = 0x0150E828
    ID_ENUM_DISPLAY = 0x9ABDD40D
    ID_GET_DVC = 0x4085DE45
    ID_SET_DVC = 0x172409B4

    def __init__(self):
        self.ok = False
        self.error = "NVIDIA NvAPI не найден: насыщенность через драйвер работает только на видеокартах NVIDIA"
        if not IS_WIN:
            return
        try:
            dll = ctypes.CDLL("nvapi64.dll" if sys.maxsize > 2 ** 32 else "nvapi.dll")
            qi = dll.nvapi_QueryInterface
            qi.restype = ctypes.c_void_p
            qi.argtypes = [ctypes.c_uint]

            def fn(fid, *argtypes):
                addr = qi(fid)
                if not addr:
                    raise OSError(f"NvAPI function 0x{fid:08X} not found")
                return ctypes.CFUNCTYPE(ctypes.c_int, *argtypes)(addr)

            self._init = fn(self.ID_INIT)
            self._enum = fn(self.ID_ENUM_DISPLAY, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))
            self._get = fn(self.ID_GET_DVC, ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(_DVCInfo))
            self._set = fn(self.ID_SET_DVC, ctypes.c_void_p, ctypes.c_uint, ctypes.c_int)
            if self._init() != 0:
                raise OSError("NvAPI_Initialize failed")
            self.ok = True
        except Exception as e:  # нет драйвера NVIDIA
            self.error = f"{self.error} ({e})"

    def _displays(self):
        i = 0
        while i < 16:
            h = ctypes.c_void_p()
            if self._enum(i, ctypes.byref(h)) != 0:
                break
            yield h
            i += 1

    def set_percent(self, percent: int) -> int:
        """50% = стандарт драйвера (уровень 0), 100% = максимум."""
        if not self.ok:
            raise RuntimeError(self.error)
        if not 50 <= percent <= 100:
            raise ValueError("Насыщенность должна быть в диапазоне 50–100")
        done = 0
        for h in self._displays():
            info = _DVCInfo()
            info.version = ctypes.sizeof(_DVCInfo) | (1 << 16)
            if self._get(h, 0, ctypes.byref(info)) != 0:
                continue
            level = round((percent - 50) * 63 / 50)
            if not info.minLevel <= level <= info.maxLevel:
                continue
            if self._set(h, 0, level) == 0:
                done += 1
        if not done:
            raise RuntimeError("Драйвер NVIDIA не принял уровень насыщенности ни для одного монитора")
        return done


# =====================================================================
#  API для JavaScript (window.pywebview.api.*)
#  Атрибуты с подчёркиванием pywebview не пробрасывает в JS.
# =====================================================================
class Api:
    def __init__(self):
        self._window = None
        self._overlay = None
        self._overlay_settings = self._load_overlay_settings()
        self._lock = threading.Lock()
        self._resolution_timer = None
        self._resolution_generation = 0
        self._nv = NvVibrance()
        self._stretch = True
        self._native = self._load_original_mode()
        self._vib_auto = False
        self._vib_level = 50
        self._vib_active = False
        self._stop_event = threading.Event()
        self._contrast = GameContrast()
        try:
            saved = json.loads(CONTRAST_SETTINGS_FILE.read_text(encoding="utf-8"))
            self._contrast.set_level(int(saved.get("level", 100)))
        except (OSError, ValueError, TypeError):
            pass
        self._game_cache = (0.0, None)
        self._cpu_name = self._detect_cpu_name()
        self._gpu_name_fallback = self._detect_gpu_name()
        psutil.cpu_percent(None)  # первый вызов всегда 0 — «прогреваем»
        threading.Thread(target=self._vibrance_watcher, daemon=True).start()

    # ---------- служебное ----------
    @staticmethod
    def _detect_cpu_name() -> str:
        name = _reg_value(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
                          "ProcessorNameString") if winreg else None
        return (name or platform.processor() or "Процессор").strip()

    @staticmethod
    def _detect_gpu_name() -> str | None:
        if not winreg:
            return None
        base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        for i in range(8):
            name = _reg_value(winreg.HKEY_LOCAL_MACHINE, rf"{base}\{i:04d}", "DriverDesc")
            if name and "basic" not in name.lower():
                return name
        return None

    @staticmethod
    def _current_mode() -> dict | None:
        mode = _enum_mode(ENUM_CURRENT_SETTINGS)
        return _mode_data(mode) if mode else None

    @staticmethod
    def _load_original_mode() -> dict | None:
        """Persist the first observed desktop mode so Reset survives app restarts."""
        invalid_saved_mode = False
        if ORIGINAL_MODE.exists():
            try:
                data = json.loads(ORIGINAL_MODE.read_text(encoding="utf-8"))
                if all(isinstance(data.get(k), int) and data[k] > 0 for k in ("width", "height", "hz")):
                    return {k: data[k] for k in ("width", "height", "hz")}
            except (OSError, ValueError, TypeError):
                pass
            invalid_saved_mode = True
        data = Api._current_mode()
        if data:
            try:
                DATA.mkdir(parents=True, exist_ok=True)
                # A damaged file is replaced; normal startup never overwrites the baseline.
                with ORIGINAL_MODE.open("w" if invalid_saved_mode else "x", encoding="utf-8") as out:
                    json.dump(data, out)
            except FileExistsError:
                return Api._load_original_mode()
            except OSError:
                pass
        return data

    @staticmethod
    def _load_overlay_settings() -> dict:
        settings = {**OVERLAY_DEFAULTS, "keys": list(OVERLAY_DEFAULTS["keys"])}
        try:
            saved = json.loads(OVERLAY_SETTINGS_FILE.read_text(encoding="utf-8"))
            settings.update(Api._validate_overlay_settings(saved, settings))
            image_path = saved.get("image_path")
            if image_path and Path(image_path).is_file() and Path(image_path).parent == DATA:
                settings["image_path"] = image_path
        except (OSError, ValueError, TypeError, KeyError):
            pass
        return settings

    @staticmethod
    def _validate_overlay_settings(value: dict, current: dict) -> dict:
        if not isinstance(value, dict):
            raise ValueError("Некорректные настройки оверлея")
        position = value.get("position", current["position"])
        if position not in ("top_left", "top_right"):
            raise ValueError("Выберите левый или правый верхний угол")
        offset_x = int(value.get("offset_x", current["offset_x"]))
        offset_y = int(value.get("offset_y", current["offset_y"]))
        dpi = int(value.get("dpi", current["dpi"]))
        nickname = str(value.get("nickname", current["nickname"])).strip()
        keys = value.get("keys", current["keys"])
        if not 0 <= offset_x <= 500 or not 0 <= offset_y <= 500:
            raise ValueError("Отступы должны быть от 0 до 500 пикселей")
        if not 100 <= dpi <= 32000:
            raise ValueError("DPI должен быть от 100 до 32000")
        if not nickname or len(nickname) > 24 or any(ord(ch) < 32 for ch in nickname):
            raise ValueError("Ник должен содержать от 1 до 24 символов без переноса строки")
        if not isinstance(keys, list) or len(keys) > 12 or any(key not in OVERLAY_KEYS for key in keys):
            raise ValueError("Выберите не более 12 клавиш из списка")
        return {"position": position, "offset_x": offset_x, "offset_y": offset_y,
                "dpi": dpi, "nickname": nickname, "keys": list(dict.fromkeys(keys)),
                "image_path": current.get("image_path")}

    def _running_game(self):
        """(game_id, psutil.Process) запущенной игры, кэш на 3 секунды."""
        ts, cached = self._game_cache
        if time.time() - ts < 3:
            return cached
        found = None
        exe_map = {exe: gid for gid, info in GAME_INFO.items() for exe in info["exe"]}
        for p in psutil.process_iter(["name"]):
            name = (p.info.get("name") or "").lower()
            if name in exe_map:
                found = (exe_map[name], p)
                break
        self._game_cache = (time.time(), found)
        return found

    @staticmethod
    def _foreground_game() -> str | None:
        if not IS_WIN:
            return None
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.GetForegroundWindow.restype = wintypes.HWND
            user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            user32.GetWindowThreadProcessId.restype = wintypes.DWORD
            handle = user32.GetForegroundWindow()
            pid = wintypes.DWORD()
            if not handle or not user32.GetWindowThreadProcessId(handle, ctypes.byref(pid)):
                return None
            name = psutil.Process(pid.value).name().lower()
            return next((gid for gid, info in GAME_INFO.items() if name in info["exe"]), None)
        except (OSError, psutil.Error):
            return None

    def _read_fps(self, game_id: str):
        """
        Точка подключения счётчика FPS. Windows не отдаёт FPS чужой игры напрямую —
        нужен источник кадров: PresentMon (ETW), RTSS shared memory и т.п.
        Верните dict {current, avg, low1} или None.
        """
        return None

    def _vibrance_watcher(self):
        """Apply display effects while a supported game owns the foreground window."""
        while not self._stop_event.wait(.5):
            playing = self._foreground_game() is not None
            try:
                self._contrast.update(playing)
            except Exception:
                pass
            if self._vib_auto and self._nv.ok:
                try:
                    if playing and not self._vib_active:
                        self._nv.set_percent(self._vib_level)
                        self._vib_active = True
                    elif not playing and self._vib_active:
                        self._nv.set_percent(50)
                        self._vib_active = False
                except Exception:
                    pass

    def _on_closed(self):
        self._stop_event.set()
        try:
            self._contrast.restore()
        except Exception:
            pass
        if self._vib_auto and self._vib_active:
            try:
                self._nv.set_percent(50)
            except Exception:
                pass
        if self._overlay:
            try:
                self._overlay.stop()
            except Exception:
                pass
            self._overlay = None

    # ---------- методы из ТЗ ----------
    def get_system_stats(self):
        try:
            return self._collect_system_stats()
        except Exception as exc:
            return {"cpu": {"name": self._cpu_name, "load": None, "temp": None,
                            "clock_mhz": None, "cores": None, "threads": None},
                    "gpu": {"name": self._gpu_name_fallback or "Видеокарта", "load": None,
                            "temp": None, "clock_mhz": None, "vram_used": None, "vram_total": None},
                    "ram": {"used": None, "total": None}, "fps": None, "game": None,
                    "display": self._native, "error": str(exc)}

    def _collect_system_stats(self):
        vm = psutil.virtual_memory()
        freq = psutil.cpu_freq()
        cpu = {
            "name": self._cpu_name,
            "load": psutil.cpu_percent(None),
            "temp": None,
            "clock_mhz": round(freq.current) if freq else None,
            "cores": psutil.cpu_count(logical=False),
            "threads": psutil.cpu_count(logical=True),
        }
        gpu = {"name": self._gpu_name_fallback or "Видеокарта", "load": None, "temp": None,
               "clock_mhz": None, "vram_used": None, "vram_total": None}
        if NVML:
            try:
                h = pynvml.nvmlDeviceGetHandleByIndex(0)
                name = pynvml.nvmlDeviceGetName(h)
                mem = pynvml.nvmlDeviceGetMemoryInfo(h)
                gpu.update({
                    "name": name.decode() if isinstance(name, bytes) else name,
                    "load": pynvml.nvmlDeviceGetUtilizationRates(h).gpu,
                    "temp": pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU),
                    "clock_mhz": pynvml.nvmlDeviceGetClockInfo(h, pynvml.NVML_CLOCK_GRAPHICS),
                    "vram_used": round(mem.used / 1024 ** 3, 1),
                    "vram_total": round(mem.total / 1024 ** 3),
                })
            except Exception:
                pass

        fps = None
        running = self._running_game()
        if running:
            gid = running[0]
            data = self._read_fps(gid) or {}
            fps = {"current": data.get("current"), "avg": data.get("avg"), "low1": data.get("low1"),
                   "game": GAME_INFO[gid]["name"]}

        return {
            "cpu": cpu,
            "gpu": gpu,
            "ram": {"used": round(vm.used / 1024 ** 3, 1), "total": round(vm.total / 1024 ** 3)},
            "fps": fps,
            "game": GAME_INFO[running[0]]["name"] if running else None,
            "display": self._native or self._current_mode(),
        }

    def apply_vibrance(self, level):
        try:
            n = self._nv.set_percent(int(level))
            self._vib_active = int(level) != 50
            return {"ok": True, "message": f"Насыщенность {int(level)}% применена ({n} мон.)"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def apply_resolution(self, width, height, hz):
        try:
            if not IS_WIN:
                raise RuntimeError("Смена разрешения доступна только в Windows")
            w, h, f = int(width), int(height), int(hz)
            if not (640 <= w <= 16384 and 480 <= h <= 8640 and 24 <= f <= 1000):
                raise ValueError("Недопустимые размеры экрана или частота")
            with self._lock:
                mode = None
                for index in range(4096):
                    candidate = _enum_mode(index)
                    if candidate is None:
                        break
                    if _mode_data(candidate) == {"width": w, "height": h, "hz": f}:
                        mode = candidate
                        break
                if mode is None:
                    raise ValueError(f"Режим {w}×{h}, {f} Гц отсутствует в списке поддерживаемых")
                mode.dmFields = DM_PELSWIDTH | DM_PELSHEIGHT | DM_DISPLAYFREQUENCY
                mode.display.dmDisplayFixedOutput = 1 if self._stretch else 2
                mode.dmFields |= DM_DISPLAYFIXEDOUTPUT
                scaling_applied = True
                test = _change_mode(mode, CDS_TEST)
                if test != DISP_CHANGE_SUCCESSFUL:
                    # Some drivers reject fixed-output control but support the mode itself.
                    mode.dmFields &= ~DM_DISPLAYFIXEDOUTPUT
                    scaling_applied = False
                    test = _change_mode(mode, CDS_TEST)
                if test != DISP_CHANGE_SUCCESSFUL:
                    raise RuntimeError(f"Проверка режима Windows завершилась с кодом {test}")
                result = _change_mode(mode, CDS_UPDATEREGISTRY)
                if result != DISP_CHANGE_SUCCESSFUL:
                    raise RuntimeError(f"Windows отклонила смену режима (код {result})")
                self._resolution_generation += 1
                generation = self._resolution_generation
                if self._resolution_timer:
                    self._resolution_timer.cancel()
                if self._native and {"width": w, "height": h, "hz": f} != self._native:
                    self._resolution_timer = threading.Timer(18, self._resolution_timeout, args=(generation,))
                    self._resolution_timer.daemon = True
                    self._resolution_timer.start()
                else:
                    self._resolution_timer = None
            message = f"Разрешение {w}×{h}, {f} Гц применено"
            if not scaling_applied:
                message += "; управление растяжением драйвером не поддерживается"
            return {"ok": True, "message": message, "scaling_applied": scaling_applied}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def reset_resolution(self):
        if not self._native:
            return {"ok": False, "error": "Исходный режим экрана не сохранён"}
        result = self.apply_resolution(self._native["width"], self._native["height"], self._native["hz"])
        if result.get("ok"):
            self.confirm_resolution()
        return result

    def _resolution_timeout(self, generation):
        if generation == self._resolution_generation:
            self.reset_resolution()

    def confirm_resolution(self):
        with self._lock:
            self._resolution_generation += 1
            if self._resolution_timer:
                self._resolution_timer.cancel()
                self._resolution_timer = None
        return {"ok": True}

    def apply_game_preset(self, game_id, preset_name, config_path=None):
        try:
            if game_id not in GAME_INFO:
                raise ValueError(f"Неизвестная игра: {game_id}")
            if preset_name not in ("potato", "esports", "reset", "default"):
                raise ValueError(f"Неизвестный пресет: {preset_name}")
            running = self._running_game()
            if running and running[0] == game_id:
                raise RuntimeError(f"Закройте {GAME_INFO[game_id]['name']} перед изменением конфига")
            files = _preset_files(game_id, config_path)
            if not files:
                raise FileNotFoundError("Папка настроек игры не найдена")
            if game_id != "cs2" and any(not path.is_file() for path in files):
                raise FileNotFoundError("Файл настроек игры не найден. Запустите игру хотя бы раз")
            with self._lock:
                if preset_name in ("reset", "default"):
                    for extra in config_files(game_id):
                        if extra not in files and Path(str(extra) + ".bak").is_file():
                            files.append(extra)
                    missing = [str(path) for path in files if not Path(str(path) + ".bak").is_file()]
                    if missing:
                        raise FileNotFoundError("Резервная копия .bak не найдена: " + ", ".join(missing))
                    for path in files:
                        shutil.copy2(Path(str(path) + ".bak"), path)
                    return {"ok": True, "message": f"{GAME_INFO[game_id]['name']}: исходные настройки восстановлены"}

                for path in files:
                    if game_id == "cs2":
                        path.parent.mkdir(parents=True, exist_ok=True)
                    _backup_once(path)
                    text, enc = _read(path) if path.exists() else ("", "utf-8")
                    if game_id == "fortnite":
                        mapping = FORTNITE_POTATO if preset_name == "potato" else PRESET_KEYS[game_id]["esports"]
                        for key, value in mapping.items():
                            section = "ScalabilityGroups" if key.startswith("sg.") else (
                                "D3DRHIPreference" if key == "PreferredRHI" else
                                "/Script/FortniteGame.FortGameUserSettings")
                            text = _ini_upsert(text, key, value, section)
                    elif game_id == "cs2":
                        for key, value in CS2_PRESETS[preset_name].items():
                            text = _cfg_set(text, key, value)
                    elif game_id == "rust":
                        mapping = RUST_POTATO if preset_name == "potato" else PRESET_KEYS[game_id]["esports"]
                        for key, value in mapping.items():
                            text = _cfg_set(text, key, value)
                    else:
                        text, _ = patch_text(text, PRESET_KEYS[game_id][preset_name], "ini")
                    _atomic_write(path, text, enc)
            return {"ok": True, "message": f"{GAME_INFO[game_id]['name']}: пресет применён; .bak сохранён рядом с конфигом",
                    "paths": [str(path) for path in files]}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def toggle_fps_overlay(self, enabled):
        try:
            if bool(enabled) and not self._overlay:
                overlay = FpsOverlay(self.get_system_stats, self._overlay_settings)
                overlay.start()
                self._overlay = overlay
            elif not bool(enabled) and self._overlay:
                self._overlay.stop()
                self._overlay = None
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": f"Оверлей не запустился: {e}"}

    def apply_nvidia_profile(self, preset_name):
        try:
            if preset_name not in ("potato", "default"):
                raise ValueError("Доступны профили NVIDIA Potato и Default")
            if not self._nv.ok:
                raise RuntimeError(self._nv.error)
            if self._running_game() and self._running_game()[0] == "fortnite":
                raise RuntimeError("Закройте Fortnite перед импортом профиля NVIDIA")
            if not ctypes.windll.shell32.IsUserAnAdmin():
                raise PermissionError("Запустите Velox от имени администратора для изменения профиля драйвера")
            return import_fortnite_profile(preset_name, BASE, DATA)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_game_contrast(self):
        return {"ok": True, "level": self._contrast.level, "active": self._contrast.active,
                "supported": self._contrast.supported()}

    def set_game_contrast(self, level):
        try:
            new_level = int(level)
            if new_level > 100 and not self._contrast.supported():
                raise RuntimeError("Драйвер не поддерживает регулировку контраста через гамма-кривую")
            previous = self._contrast.level
            self._contrast.set_level(new_level)
            try:
                self._contrast.update(self._foreground_game() is not None)
                _atomic_write(CONTRAST_SETTINGS_FILE, json.dumps({"level": new_level}), "utf-8")
            except Exception:
                self._contrast.set_level(previous)
                raise
            return {"ok": True, "message": "Контраст будет действовать только при активном окне игры"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_overlay_settings(self):
        return {"ok": True, **self._overlay_settings}

    def set_overlay_settings(self, settings):
        try:
            updated = self._validate_overlay_settings(settings, self._overlay_settings)
            _atomic_write(OVERLAY_SETTINGS_FILE, json.dumps(updated, ensure_ascii=False, indent=2), "utf-8")
            self._overlay_settings = updated
            if self._overlay:
                self._overlay.update(updated)
            return {"ok": True, **updated}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def choose_overlay_image(self):
        try:
            if not self._window:
                raise RuntimeError("Окно программы ещё не открыто")
            chosen = self._window.create_file_dialog(
                webview.OPEN_DIALOG, file_types=("Изображения (*.png;*.jpg;*.jpeg;*.gif;*.webp)",))
            if not chosen:
                return {"ok": True, "cancelled": True}
            source = Path(chosen[0]).resolve()
            if source.suffix.lower() not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
                raise ValueError("Выберите PNG, JPG, GIF или WEBP")
            if source.stat().st_size > 10 * 1024 * 1024:
                raise ValueError("Изображение должно быть не больше 10 МБ")
            from PIL import Image
            with Image.open(source) as image:
                if image.width > 4096 or image.height > 4096:
                    raise ValueError("Изображение должно быть не больше 4096×4096 пикселей")
                image.verify()
            DATA.mkdir(parents=True, exist_ok=True)
            target = OVERLAY_IMAGE_FILE.with_suffix(source.suffix.lower())
            if source != target:
                shutil.copy2(source, target)
            self._overlay_settings["image_path"] = str(target)
            _atomic_write(OVERLAY_SETTINGS_FILE, json.dumps(self._overlay_settings, ensure_ascii=False, indent=2), "utf-8")
            if self._overlay:
                self._overlay.update(self._overlay_settings)
            return {"ok": True, "image_path": str(target)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def clear_overlay_image(self):
        try:
            self._overlay_settings["image_path"] = None
            _atomic_write(OVERLAY_SETTINGS_FILE, json.dumps(self._overlay_settings, ensure_ascii=False, indent=2), "utf-8")
            if self._overlay:
                self._overlay.update(self._overlay_settings)
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def minimize_window(self):
        try:
            if self._window:
                self._window.minimize()
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def close_window(self):
        try:
            self._on_closed()
            if self._window:
                self._window.destroy()
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ---------- дополнительные методы ----------
    def turbo_boost(self):
        try:
            if not IS_WIN:
                raise RuntimeError("Turbo Boost доступен только в Windows")
            before = psutil.virtual_memory().available
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            k32.CloseHandle.restype = wintypes.BOOL
            psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]
            psapi.EmptyWorkingSet.restype = wintypes.BOOL
            access = 0x0100 | 0x0400
            for proc in psutil.process_iter(["pid"]):
                handle = k32.OpenProcess(access, False, proc.info["pid"])
                if handle:
                    try:
                        psapi.EmptyWorkingSet(handle)
                    finally:
                        k32.CloseHandle(handle)
            freed_mb = max(0, psutil.virtual_memory().available - before) / 1024 ** 2
            self._game_cache = (0.0, None)
            running = self._running_game()
            prio = None
            if running:
                try:
                    running[1].nice(psutil.HIGH_PRIORITY_CLASS)
                    prio = running[1].name()
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    pass
            return {"ok": True, "freed_mb": round(freed_mb), "priority_process": prio}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_installed_games(self):
        result = {}
        for gid in GAME_INFO:
            path = find_game(gid)
            result[gid] = {"found": path is not None, "path": str(path) if path else None}
        return result

    def backup_game_config(self, game_id):
        try:
            if game_id not in GAME_INFO:
                raise ValueError("Неизвестная игра")
            files = list(dict.fromkeys(config_files(game_id) + _preset_files(game_id)))
            files = [path for path in files if path.is_file()]
            if not files:
                raise FileNotFoundError("Нечего сохранять: файл настроек игры не найден")
            dest = BACKUPS / game_id / datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
            dest.mkdir(parents=True, exist_ok=False)
            with (dest / "source.txt").open("w", encoding="utf-8") as log:
                for i, path in enumerate(files):
                    target = dest / f"{i}_{path.name}"
                    shutil.copy2(path, target)
                    log.write(f"{target.name} <- {path}\n")
            return {"ok": True, "path": str(dest)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def apply_game_options(self, game_id, options):
        try:
            files = config_files(game_id)
            if not files or game_id not in OPTION_KEYS:
                return {"ok": True}
            mapping = {}
            for opt, enabled in (options or {}).items():
                on, off = OPTION_KEYS[game_id].get(opt, ({}, {}))
                mapping.update(on if enabled else off)
            if not mapping:
                return {"ok": True}
            with self._lock:
                for f in files:
                    text, enc = _read(f)
                    changed, _ = patch_text(text, mapping, GAME_INFO[game_id]["fmt"])
                    if changed != text:
                        _backup_once(f)
                        _atomic_write(f, changed, enc)
        except Exception as e:
            return {"ok": False, "error": f"Не удалось записать тонкие настройки: {e}"}
        return {"ok": True}

    def set_vibrance_auto(self, enabled, level):
        self._vib_auto = bool(enabled)
        self._vib_level = int(level)
        if not self._nv.ok:
            return {"ok": False, "error": self._nv.error}
        return {"ok": True}

    def set_scaling_mode(self, stretch):
        self._stretch = bool(stretch)
        return {"ok": True}


def main():
    if not IS_WIN:
        raise SystemExit("Velox работает только в Windows")
    if not WEB_INDEX.is_file():
        raise SystemExit(f"Фронтенд не найден: {WEB_INDEX}")
    try:
        is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        is_admin = False
    if not is_admin:
        ctypes.windll.user32.MessageBoxW(
            None, "Velox запущен без прав администратора. Некоторые игровые конфиги могут быть недоступны для записи.",
            "Velox", 0x30)
    DATA.mkdir(parents=True, exist_ok=True)
    api = Api()
    window = webview.create_window(
        "Velox",
        url=str(WEB_INDEX),
        js_api=api,
        width=1200,
        height=750,
        min_size=(1000, 680),
        frameless=True,
        easy_drag=False,          # тянем только за заголовок (.pywebview-drag-region)
        background_color="#0B0D12",
    )
    api._window = window
    window.events.closed += api._on_closed
    webview.start(
        private_mode=False,       # иначе localStorage (настройки интерфейса) стирается при выходе
        storage_path=str(DATA / "webview"),
        debug="--debug" in sys.argv,
    )


if __name__ == "__main__":
    mp.freeze_support()
    main()
