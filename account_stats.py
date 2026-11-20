"""Explicitly linked accounts and documented Steam Web API statistics.

Never infer game ownership from a Steam login or a ban expiry from ban counts.
The optional Steam Web API key is kept in memory for this process only.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlencode
from urllib.request import Request, urlopen

import psutil


GAME_IDS = ("fortnite", "cs2", "rust", "valorant")
STEAM_APPS = {"cs2": 730, "rust": 252490}
STEAM_ID = re.compile(r"^7656119\d{10}$")
KEY = re.compile(r"^[0-9a-fA-F]{32}$")
MAX_ACCOUNTS = 30


class LocalPlaytime:
    """Count game process uptime while Velox itself is running, not lifetime playtime."""
    def __init__(self, data_dir: Path, executables: dict[str, list[str]]):
        self.file = data_dir / "local_playtime.json"
        self.executables = {g: {exe.lower() for exe in names}
                            for g, names in executables.items()}
        saved = _read_json(self.file)
        raw_seconds = saved.get("seconds", {})
        if not isinstance(raw_seconds, dict):
            raw_seconds = {}
        self.seconds = {}
        for game in self.executables:
            try:
                self.seconds[game] = max(0.0, float(raw_seconds.get(game, 0)))
            except (ValueError, TypeError):
                self.seconds[game] = 0.0
        self.since = saved.get("since") or datetime.now(timezone.utc).isoformat()
        self._lock = threading.RLock()
        self._last = None
        self._previous: set[str] = set()

    def tick(self, now: float | None = None, running: set[str] | None = None):
        now = time.monotonic() if now is None else now
        if running is None:
            names = set()
            try:
                for proc in psutil.process_iter(["name"]):
                    try:
                        name = proc.info.get("name")
                        if name:
                            names.add(name.lower())
                    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                        continue
            except (psutil.Error, OSError):
                pass
            running = {g for g, exes in self.executables.items() if names & exes}
        with self._lock:
            if self._last is not None:
                delta = max(0.0, min(now - self._last, 30.0))
                for game in running & self._previous:
                    self.seconds[game] += delta
            self._last = now
            self._previous = running

    def run(self, stop_event):
        while not stop_event.is_set():
            try:
                self.tick()
                self.save()
            except Exception:
                pass
            if stop_event.wait(10):
                break

    def save(self):
        with self._lock:
            payload = {"since": self.since, "seconds": self.seconds}
        self.file.parent.mkdir(parents=True, exist_ok=True)
        temp = self.file.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temp.replace(self.file)

    def overview(self) -> dict:
        with self._lock:
            return {"since": self.since,
                    "hours": {g: round(seconds / 3600, 2) for g, seconds in self.seconds.items()}}


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def local_steam_accounts(steam_root: Path | None) -> list[dict]:
    """Read only account IDs and display names from Steam's remembered login list."""
    if not steam_root:
        return []
    try:
        text = (steam_root / "config" / "loginusers.vdf").read_text(
            encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    result = []
    for match in re.finditer(r'"(7656119\d{10})"\s*\{([^{}]*)\}', text):
        name = re.search(r'"PersonaName"\s+"([^"\r\n]{0,120})"', match.group(2), re.I)
        result.append({"steam_id": match.group(1),
                       "name": name.group(1) if name else match.group(1)})
    return result[:MAX_ACCOUNTS]


def _steam_ref(ref: str) -> tuple[str, str]:
    value = ref.strip()
    if STEAM_ID.fullmatch(value):
        return "id", value
    url = urlparse(value)
    if url.scheme != "https" or url.hostname not in ("steamcommunity.com", "www.steamcommunity.com"):
        raise ValueError("Укажите Steam ID64 или ссылку https://steamcommunity.com/profiles/...")
    segments = [part for part in url.path.split("/") if part]
    if len(segments) != 2 or segments[0] not in ("profiles", "id"):
        raise ValueError("Нужна ссылка на профиль Steam")
    if segments[0] == "profiles" and STEAM_ID.fullmatch(segments[1]):
        return "id", segments[1]
    if segments[0] == "id" and re.fullmatch(r"[A-Za-z0-9_-]{2,64}", segments[1]):
        return "vanity", segments[1]
    raise ValueError("Ссылка Steam содержит некорректный идентификатор")


def _safe_link(ref: str) -> str:
    value = ref.strip()
    if not value:
        return ""
    url = urlparse(value)
    if url.scheme != "https" or not url.hostname or url.username or url.password or len(value) > 240:
        raise ValueError("Ссылка должна начинаться с https:// и не содержать пароль")
    return value


class AccountStats:
    def __init__(self, data_dir: Path, steam_root: Path | None):
        self.file = data_dir / "game_accounts.json"
        self.steam_root = steam_root
        self._lock = threading.RLock()
        self._key = None
        self._cache: dict[str, dict] = {}
        self._accounts = _read_json(self.file).get("accounts", [])
        if not isinstance(self._accounts, list):
            self._accounts = []
        self._accounts = [a for a in self._accounts if isinstance(a, dict)
                          and a.get("game_id") in GAME_IDS][:MAX_ACCOUNTS]

    def set_steam_key(self, value: str) -> dict:
        value = str(value or "").strip()
        if value and not KEY.fullmatch(value):
            return {"ok": False, "error": "Steam API-ключ должен содержать 32 шестнадцатеричных символа"}
        with self._lock:
            self._key = value or None
            self._cache.clear()
        return {"ok": True, "configured": bool(value),
                "message": "Ключ действует только до закрытия Velox и не записывается на диск"}

    def _save(self):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        temp = self.file.with_suffix(".json.tmp")
        temp.write_text(json.dumps({"accounts": self._accounts}, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        temp.replace(self.file)

    def add(self, game_id: str, name: str, ref: str) -> dict:
        try:
            if game_id not in GAME_IDS:
                raise ValueError("Неизвестная игра")
            name = str(name or "").strip()
            ref = str(ref or "").strip()
            if not name or len(name) > 60:
                raise ValueError("Введите название аккаунта до 60 символов")
            if game_id in STEAM_APPS:
                kind, value = _steam_ref(ref)
                ref = value if kind == "id" else f"https://steamcommunity.com/id/{value}/"
            else:
                ref = _safe_link(ref)
            with self._lock:
                if len(self._accounts) >= MAX_ACCOUNTS:
                    raise ValueError("Можно добавить не больше 30 аккаунтов")
                if any(a.get("game_id") == game_id and a.get("ref") == ref and ref
                       for a in self._accounts):
                    raise ValueError("Этот аккаунт уже добавлен для игры")
                import uuid
                account = {"id": uuid.uuid4().hex, "game_id": game_id,
                           "name": name, "ref": ref}
                self._accounts.append(account)
                self._save()
            return {"ok": True, "account": account}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def remove(self, account_id: str) -> dict:
        try:
            with self._lock:
                before = len(self._accounts)
                self._accounts = [a for a in self._accounts if a.get("id") != account_id]
                if len(self._accounts) == before:
                    raise ValueError("Аккаунт не найден")
                self._cache.pop(account_id, None)
                self._save()
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def overview(self) -> dict:
        with self._lock:
            accounts = [{**a, "stats": self._cache.get(a.get("id", ""))} for a in self._accounts]
            has_key = bool(self._key)
        return {"ok": True, "accounts": accounts,
                "counts": {g: sum(a["game_id"] == g for a in accounts) for g in GAME_IDS},
                "steam_detected": local_steam_accounts(self.steam_root),
                "steam_key_ready": has_key}

    def _get(self, interface: str, method: str, version: int, params: dict) -> dict:
        query = urlencode({"key": self._key, **params})
        url = f"https://api.steampowered.com/{interface}/{method}/v{version}/?{query}"
        request = Request(url, headers={"User-Agent": "VeloxGameOptimizer/1.2"})
        try:
            with urlopen(request, timeout=8) as response:
                if response.status != 200:
                    raise RuntimeError(f"Steam API: HTTP {response.status}")
                return json.load(response)
        except HTTPError as exc:
            raise RuntimeError(f"Steam API: HTTP {exc.code}; проверьте ключ и доступность профиля") from None
        except (URLError, TimeoutError, OSError):
            raise RuntimeError("Steam API недоступен; проверьте подключение к интернету") from None

    def _resolve(self, ref: str) -> str:
        kind, value = _steam_ref(ref)
        if kind == "id":
            return value
        result = self._get("ISteamUser", "ResolveVanityURL", 1, {"vanityurl": value})
        answer = result.get("response", {})
        if answer.get("success") != 1 or not STEAM_ID.fullmatch(str(answer.get("steamid", ""))):
            raise RuntimeError("Профиль Steam по этой ссылке не найден")
        return str(answer["steamid"])

    def _fetch_steam(self, account: dict) -> dict:
        steam_id = self._resolve(account["ref"])
        app_id = STEAM_APPS[account["game_id"]]
        games = self._get("IPlayerService", "GetOwnedGames", 1,
                          {"steamid": steam_id, "include_played_free_games": 1,
                           "appids_filter[0]": app_id, "include_appinfo": 1})
        entries = games.get("response", {}).get("games", [])
        game = next((g for g in entries if g.get("appid") == app_id), None)
        bans = self._get("ISteamUser", "GetPlayerBans", 1, {"steamids": steam_id})
        ban = next(iter(bans.get("players", [])), None)
        summary = self._get("ISteamUser", "GetPlayerSummaries", 2, {"steamids": steam_id})
        player = next(iter(summary.get("response", {}).get("players", [])), {})
        achievements = None
        if game:
            try:
                result = self._get("ISteamUserStats", "GetPlayerAchievements", 1,
                                   {"steamid": steam_id, "appid": app_id})
                items = result.get("playerstats", {}).get("achievements", [])
                if result.get("playerstats", {}).get("success") and isinstance(items, list):
                    achievements = {"unlocked": sum(bool(a.get("achieved")) for a in items),
                                    "total": len(items)}
            except (RuntimeError, ValueError, KeyError, TypeError):
                pass
        return {"source": "Steam Web API", "steam_id": steam_id,
                "persona": player.get("personaname"),
                "profile_url": player.get("profileurl") or f"https://steamcommunity.com/profiles/{steam_id}/",
                "hours_total": round(game["playtime_forever"] / 60, 1) if game and "playtime_forever" in game else None,
                "hours_2weeks": round(game["playtime_2weeks"] / 60, 1) if game and "playtime_2weeks" in game else None,
                "hours_note": "Не видно в Steam или нет доступа к игровым данным" if not game else None,
                "achievements": achievements,
                "steam_bans": {"vac": int(ban.get("NumberOfVACBans", 0)),
                               "game": int(ban.get("NumberOfGameBans", 0)),
                               "community": bool(ban.get("CommunityBanned", False)),
                               "days_since_last": ban.get("DaysSinceLastBan")} if ban else None,
                "ban_note": "Баны Steam относятся ко всему аккаунту, не обязательно к этой игре. Срок окончания API не отдаёт.",
                "ban_expires_at": None, "fetched_at": int(time.time())}

    def refresh(self) -> dict:
        with self._lock:
            accounts = [a.copy() for a in self._accounts if a.get("game_id") in STEAM_APPS]
            has_key = bool(self._key)
        if not has_key:
            return {"ok": False, "error": "Для часов и банов Steam введите собственный Steam Web API-ключ"}
        for account in accounts:
            try:
                stats = self._fetch_steam(account)
            except Exception as exc:
                stats = {"error": str(exc), "fetched_at": int(time.time())}
            with self._lock:
                self._cache[account["id"]] = stats
        return self.overview()
