import json
import os
import re
from datetime import datetime, timezone
from cryptography.fernet import Fernet, InvalidToken


def env_value(key: str) -> str:
    value = os.getenv(key, "").strip()
    if not value:
        raise RuntimeError(f"Environment variable {key} is required")
    return value


def cipher_box() -> Fernet:
    return Fernet(env_value("FERNET_KEY").encode())


def encrypt_cookies(value: str) -> str:
    return cipher_box().encrypt(value.encode()).decode()


def decrypt_cookies(value: str) -> str:
    try:
        return cipher_box().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        raise ValueError("COOKIE_DECRYPTION_FAILED") from None


def parse_settings(raw: str | None) -> dict:
    defaults = {
        "skip_admins": False,
        "excluded_levels": [],
        "only_older": False,
        "content_mode": "all",
        "notifications_paused": False,
        "blacklist": [],
    }
    try:
        incoming = json.loads(raw or "{}")
        if isinstance(incoming, dict):
            defaults.update(incoming)
    except (json.JSONDecodeError, TypeError):
        pass
    return defaults


def serialize_settings(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_text(value: str | None, limit: int = 3500) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    return value[:limit]


def parse_positive_int(value: str, maximum: int = 10080) -> int | None:
    try:
        result = int(value.strip())
    except (ValueError, AttributeError):
        return None
    return result if 1 <= result <= maximum else None


def safe_nickname(value: str) -> str:
    return clean_text(value, 100).lstrip("@")
