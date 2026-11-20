"""Best-effort game-foreground contrast via the Windows display gamma ramp."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import threading


Ramp = (wintypes.WORD * 256) * 3


class GameContrast:
    def __init__(self):
        self.level = 100
        self.active = False
        self._original: Ramp | None = None
        self._applied_level = 100
        self._lock = threading.RLock()
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self._user32.GetDC.argtypes = [wintypes.HWND]
        self._user32.GetDC.restype = wintypes.HDC
        self._user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        self._user32.ReleaseDC.restype = ctypes.c_int
        self._gdi32.GetDeviceGammaRamp.argtypes = [wintypes.HDC, ctypes.POINTER(Ramp)]
        self._gdi32.GetDeviceGammaRamp.restype = wintypes.BOOL
        self._gdi32.SetDeviceGammaRamp.argtypes = [wintypes.HDC, ctypes.POINTER(Ramp)]
        self._gdi32.SetDeviceGammaRamp.restype = wintypes.BOOL

    def _read(self) -> Ramp:
        dc = self._user32.GetDC(None)
        if not dc:
            raise OSError("Не удалось получить контекст дисплея")
        try:
            ramp = Ramp()
            if not self._gdi32.GetDeviceGammaRamp(dc, ctypes.byref(ramp)):
                raise OSError("Драйвер не поддерживает чтение гамма-кривой")
            return ramp
        finally:
            self._user32.ReleaseDC(None, dc)

    def _write(self, ramp: Ramp) -> None:
        dc = self._user32.GetDC(None)
        if not dc:
            raise OSError("Не удалось получить контекст дисплея")
        try:
            if not self._gdi32.SetDeviceGammaRamp(dc, ctypes.byref(ramp)):
                raise OSError("Драйвер отклонил изменение контраста")
        finally:
            self._user32.ReleaseDC(None, dc)

    def supported(self) -> bool:
        try:
            self._read()
            return True
        except OSError:
            return False

    @staticmethod
    def _adjust(original: Ramp, level: int) -> Ramp:
        factor = level / 100
        result = Ramp()
        for channel in range(3):
            for index in range(256):
                value = original[channel][index] / 65535
                result[channel][index] = round(max(0, min(1, (value - .5) * factor + .5)) * 65535)
        return result

    def set_level(self, level: int) -> None:
        if not 100 <= level <= 130:
            raise ValueError("Контраст должен быть от 100 до 130")
        with self._lock:
            self.level = level

    def update(self, foreground_game: bool) -> None:
        with self._lock:
            if foreground_game and self.level > 100:
                if self._original is None:
                    self._original = self._read()
                if not self.active or self._applied_level != self.level:
                    self._write(self._adjust(self._original, self.level))
                    self.active = True
                    self._applied_level = self.level
            elif self.active:
                self.restore()

    def restore(self) -> None:
        with self._lock:
            if self.active and self._original is not None:
                self._write(self._original)
            self.active = False
            self._original = None
            self._applied_level = 100
