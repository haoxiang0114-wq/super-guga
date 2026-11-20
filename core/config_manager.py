# -*- coding: utf-8 -*-
"""配置管理 — QSettings 封装"""
from PyQt5.QtCore import QSettings


class ConfigManager:
    def __init__(self):
        # 独立存档命名空间（不与黑猫共用，避免继承低心情导致"常年哭泣"）
        self._s = QSettings("GugaPenguinPet", "GugaPenguin")

    @property
    def pet_scale(self):
        return max(.4,min(1.,self._s.value('appearance/scale',1.,type=float)))

    @pet_scale.setter
    def pet_scale(self,value):
        self._s.setValue('appearance/scale',max(.4,min(1.,float(value))))

    @property
    def avatar_mode(self) -> str:
        mode = self._s.value('appearance/mode', 'illustration', type=str)
        return mode if mode in ('3d', 'illustration') else 'illustration'

    @avatar_mode.setter
    def avatar_mode(self, mode: str):
        if mode not in ('3d', 'illustration'):
            raise ValueError('Unknown avatar mode')
        self._s.setValue('appearance/mode', mode)

    # ---- 窗口位置 ----
    def save_pos(self, x: int, y: int):
        self._s.setValue("pos/x", x)
        self._s.setValue("pos/y", y)

    def load_pos(self, fallback: tuple = (200, 200)) -> tuple:
        if not self._s.contains("pos/x") or not self._s.contains("pos/y"):
            return fallback
        try:
            x = self._s.value("pos/x", type=int)
            y = self._s.value("pos/y", type=int)
        except (TypeError, ValueError, OverflowError):
            return fallback
        return (x, y)

    # ---- 面板开关 ----
    @property
    def bongo_visible(self) -> bool:
        return self._s.value("ui/bongo", False, type=bool)

    @bongo_visible.setter
    def bongo_visible(self, v: bool):
        self._s.setValue("ui/bongo", v)

    # ---- 透明度 ----
    @property
    def opacity(self) -> float:
        return self._s.value("ui/opacity", 0.95, type=float)

    @opacity.setter
    def opacity(self, v: float):
        self._s.setValue("ui/opacity", v)

    # ---- 状态存档 ----
    def save_status(self, hunger: float, happy: float, energy: float):
        self._s.setValue("status/hunger", hunger)
        self._s.setValue("status/happy", happy)
        self._s.setValue("status/energy", energy)

    def load_status(self) -> dict:
        return {
            "hunger": self._s.value("status/hunger", 85.0, type=float),
            "happy": self._s.value("status/happy", 75.0, type=float),
            "energy": self._s.value("status/energy", 90.0, type=float),
        }

    # ---- 开机自启注册表 ----
    @property
    def auto_start(self) -> bool:
        import winreg
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0, winreg.KEY_READ
            )
            val, _ = winreg.QueryValueEx(key, "BlackCatPet")
            winreg.CloseKey(key)
            return val != ""
        except FileNotFoundError:
            return False

    @auto_start.setter
    def auto_start(self, enable: bool):
        import sys, os, winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE
        )
        if enable:
            exe = sys.executable if getattr(sys, 'frozen', False) else sys.argv[0]
            winreg.SetValueEx(key, "BlackCatPet", 0, winreg.REG_SZ, f'"{os.path.abspath(exe)}"')
        else:
            try:
                winreg.DeleteValue(key, "BlackCatPet")
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
