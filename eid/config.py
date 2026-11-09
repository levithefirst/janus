"""Configuration from environment variables (optionally loaded from a .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if value and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Config:
    paypal_mode: str
    paypal_client_id: str
    paypal_client_secret: str
    ai_mode: str
    model: str
    data_dir: Path
    base_url: str

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        env = os.environ.get
        paypal_mode = env("EID_PAYPAL") or ("sandbox" if env("PAYPAL_CLIENT_ID") else "mock")
        has_key = env("ANTHROPIC_API_KEY") or env("ANTHROPIC_AUTH_TOKEN")
        ai_mode = env("EID_AI") or ("claude" if has_key else "mock")
        if paypal_mode not in ("sandbox", "mock"):
            raise ValueError(f"EID_PAYPAL must be 'sandbox' or 'mock', got {paypal_mode!r}")
        if ai_mode not in ("claude", "mock"):
            raise ValueError(f"EID_AI must be 'claude' or 'mock', got {ai_mode!r}")
        return cls(
            paypal_mode=paypal_mode,
            paypal_client_id=env("PAYPAL_CLIENT_ID", ""),
            paypal_client_secret=env("PAYPAL_CLIENT_SECRET", ""),
            ai_mode=ai_mode,
            model=env("EID_MODEL", "claude-opus-5-5"),
            data_dir=Path(env("EID_DATA_DIR", "./data")),
            base_url=env("EID_BASE_URL", "http://localhost:8000").rstrip("/"),
        )
