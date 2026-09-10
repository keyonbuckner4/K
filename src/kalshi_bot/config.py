"""Configuration loading.

Rules (from CLAUDE.md / BRIEF.md):
* Demo is the default everywhere. Live requires the ``--live`` flag AND ``CONFIRM_LIVE=yes``.
* Secrets come from ``.env`` only. Non-secret settings come from ``config/bot.toml``.
* Base URLs are never assumed from memory; they must be present in the TOML.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigError

DEMO = "demo"
LIVE = "live"


def find_root(start: Path | None = None) -> Path:
    """Walk up from ``start`` (or cwd) to the directory containing BRIEF.md; fall back to cwd."""
    here = (start or Path.cwd()).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "BRIEF.md").exists() and (candidate / "pyproject.toml").exists():
            return candidate
    return here


def parse_dotenv(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        out[key] = value
    return out


def load_dotenv(path: Path, environ: dict[str, str] | None = None) -> dict[str, str]:
    """Load ``.env`` into ``environ`` (default ``os.environ``) without overriding existing keys."""
    env = os.environ if environ is None else environ
    if not path.exists():
        return {}
    loaded = parse_dotenv(path.read_text(encoding="utf-8"))
    for k, v in loaded.items():
        env.setdefault(k, v)
    return loaded


def load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"missing config file {path}")
    with path.open("rb") as fh:
        return tomllib.load(fh)


def resolve_environment(live_flag: bool, environ: Mapping[str, str]) -> str:
    """Demo unless ``--live`` was passed AND CONFIRM_LIVE=yes. Any other combination is demo or an error."""
    confirm = environ.get("CONFIRM_LIVE", "no").strip().lower()
    if live_flag:
        if confirm != "yes":
            raise ConfigError("--live was passed but CONFIRM_LIVE is not 'yes'. Refusing to touch production.")
        return LIVE
    return DEMO


@dataclass(frozen=True)
class Settings:
    env: str
    root: Path
    rest_base_url: str
    ws_url: str
    api_key_id: str | None
    private_key_path: Path | None
    alert_webhook_url: str | None
    db_path: Path
    halt_path: Path
    toml: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_live(self) -> bool:
        return self.env == LIVE

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key_id) and self.private_key_path is not None and self.private_key_path.exists()

    def require_credentials(self) -> None:
        if not self.api_key_id:
            raise ConfigError("KALSHI_API_KEY_ID is not set (put it in .env)")
        if self.private_key_path is None or not self.private_key_path.exists():
            raise ConfigError(f"KALSHI_PRIVATE_KEY_PATH does not point to a file: {self.private_key_path}")

    def section(self, *keys: str, default: Any = None) -> Any:
        node: Any = self.toml
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node


def load_settings(root: Path | None = None, live: bool = False, environ: dict[str, str] | None = None,
                  config_path: Path | None = None) -> Settings:
    root = find_root(root)
    env_map = os.environ if environ is None else environ
    load_dotenv(root / ".env", env_map)
    toml = load_toml(config_path or root / "config" / "bot.toml")

    env = resolve_environment(live, env_map)
    kalshi_env = env_map.get("KALSHI_ENV", DEMO).strip().lower()
    if kalshi_env not in (DEMO, LIVE, "prod", "production"):
        raise ConfigError(f"KALSHI_ENV must be 'demo' or 'live', got {kalshi_env!r}")

    hosts = toml.get("hosts", {}).get(env)
    if not isinstance(hosts, dict) or not hosts.get("rest") or not hosts.get("ws"):
        raise ConfigError(f"config/bot.toml must define [hosts.{env}] with 'rest' and 'ws' URLs (no defaults are assumed)")
    rest = str(hosts["rest"]).rstrip("/")
    ws = str(hosts["ws"])
    if not rest.startswith("https://") or not ws.startswith("wss://"):
        raise ConfigError(f"[hosts.{env}] must use https:// and wss:// (got {rest!r}, {ws!r})")

    key_path_raw = env_map.get("KALSHI_PRIVATE_KEY_PATH", "").strip()
    key_path = (root / key_path_raw).resolve() if key_path_raw and not Path(key_path_raw).is_absolute() else (Path(key_path_raw) if key_path_raw else None)
    db_rel = toml.get("storage", {}).get("db_path", "data/bot.db")
    db_path = root / f"{db_rel}" if not Path(db_rel).is_absolute() else Path(db_rel)
    if env == LIVE:
        db_path = db_path.with_name(db_path.stem + ".live" + db_path.suffix)
    else:
        db_path = db_path.with_name(db_path.stem + ".demo" + db_path.suffix)

    return Settings(
        env=env,
        root=root,
        rest_base_url=rest,
        ws_url=ws,
        api_key_id=env_map.get("KALSHI_API_KEY_ID", "").strip() or None,
        private_key_path=key_path,
        alert_webhook_url=env_map.get("ALERT_WEBHOOK_URL", "").strip() or None,
        db_path=db_path,
        halt_path=root / "HALT",
        toml=toml,
    )
