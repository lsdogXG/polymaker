from __future__ import annotations

from app.config import Settings

_SETTINGS: Settings | None = None


def set_settings(settings: Settings) -> None:
    global _SETTINGS
    _SETTINGS = settings


def get_settings() -> Settings:
    if _SETTINGS is None:
        raise RuntimeError("Strategy settings not initialized")
    return _SETTINGS
