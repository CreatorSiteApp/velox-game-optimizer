"""Import bundled Fortnite NIP profiles through the official Profile Inspector."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
import xml.etree.ElementTree as ET


TOOL_FILES = ("nvidiaProfileInspector.exe", "nvidiaProfileInspector.exe.config", "Reference.xml")
PRESET_FILES = {"potato": "Fortnite potato.nip", "default": "Fortnite Default.nip"}


def _settings(path: Path) -> dict[str, str]:
    root = ET.parse(path).getroot()
    for profile in root.findall("Profile"):
        if (profile.findtext("ProfileName") or "").casefold() == "fortnite":
            result = {}
            for setting in profile.findall("./Settings/ProfileSetting"):
                key = setting.findtext("SettingID")
                value = setting.findtext("SettingValue")
                if key and value is not None:
                    result[key] = value
            return result
    return {}


def _run(exe: Path, *args: str) -> None:
    result = subprocess.run([str(exe), *args], cwd=exe.parent, timeout=90,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
    if result.returncode != 0:
        raise RuntimeError(f"NVIDIA Profile Inspector завершился с кодом {result.returncode}")


def _export(exe: Path) -> Path:
    started = time.time_ns()
    _run(exe, "-exportCustomized")
    candidates = sorted(exe.parent.glob("CustomProfiles_*.nip"), key=lambda path: path.stat().st_mtime_ns,
                        reverse=True)
    if not candidates or candidates[0].stat().st_mtime_ns < started - 2_000_000_000:
        raise RuntimeError("NVIDIA Profile Inspector не создал резервный экспорт профилей")
    return candidates[0]


def import_fortnite_profile(preset: str, bundle: Path, data: Path) -> dict:
    try:
        if preset not in PRESET_FILES:
            raise ValueError("Неизвестный профиль NVIDIA")
        profile = bundle / "profiles" / PRESET_FILES[preset]
        expected = _settings(profile)
        if len(expected) < 1:
            raise ValueError("Файл .nip не содержит профиль Fortnite")
        tool_dir = data / "nvidiaProfileInspector"
        tool_dir.mkdir(parents=True, exist_ok=True)
        for name in TOOL_FILES:
            source = bundle / "third_party" / "nvidiaProfileInspector" / name
            target = tool_dir / name
            if not target.exists() or hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(source.read_bytes()).digest():
                shutil.copy2(source, target)
        exe = tool_dir / "nvidiaProfileInspector.exe"
        exported = _export(exe)
        backup_dir = data / "backups" / "nvidia_profile"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / (datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f") + ".nip")
        shutil.copy2(exported, backup)
        _run(exe, "-silentImport", "-mergeImport", str(profile))
        time.sleep(1.05)  # Profile Inspector names exports with one-second precision.
        actual = _settings(_export(exe))
        missing = [key for key, value in expected.items() if actual.get(key) != value]
        if missing:
            raise RuntimeError(f"Драйвер не подтвердил {len(missing)} параметров профиля Fortnite; резервная копия: {backup}")
        return {"ok": True, "message": "Профиль NVIDIA для Fortnite импортирован",
                "backup_path": str(backup), "settings_verified": len(expected)}
    except (OSError, ValueError, ET.ParseError, subprocess.TimeoutExpired, RuntimeError) as exc:
        return {"ok": False, "error": str(exc)}
