"""``bot setup``: guided first run. Asks for the API key IDs, saves the private keys as .pem
files, writes .env, then checks the connection. Nothing leaves the machine except the signed
requests to Kalshi. Keys are pasted into the terminal or given as a path to a saved file."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Callable

from cryptography.hazmat.primitives import serialization

from .config import parse_dotenv

Input = Callable[[str], str]
Print = Callable[[str], None]


def _read_pem(label: str, ask: Input, say: Print) -> str:
    say(f"\n{label} private key. Either paste the whole key (the lines from -----BEGIN to -----END),")
    say("or type the path to the file you saved it in, then press Enter.")
    for attempt in range(3):
        first = ask("> ").strip()
        if not first:
            say("Nothing entered; try again.")
            continue
        if first.startswith("-----BEGIN"):
            lines = [first]
            while not lines[-1].startswith("-----END"):
                line = ask("").rstrip("\r\n")
                if line.strip():
                    lines.append(line.strip())
            pem = "\n".join(lines) + "\n"
        else:
            p = Path(first).expanduser()
            if not p.exists():
                say(f"No file at {p}. Try again.")
                continue
            pem = p.read_text(encoding="utf-8")
        try:
            serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
        except Exception as e:
            say(f"That is not a valid unencrypted RSA private key ({e}). Try again.")
            continue
        return pem
    raise SystemExit("Could not read a valid private key after 3 attempts.")


def _write_secret(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def run_setup(root: Path, ask: Input = input, say: Print = print, with_live: bool | None = None) -> Path:
    env_path = root / ".env"
    existing = parse_dotenv(env_path.read_text(encoding="utf-8")) if env_path.exists() else {}
    say("kalshi-bot setup. Demo first; the production key is optional and only used with --live.")
    if existing:
        ans = ask(f"{env_path} already exists. Overwrite it? [y/N] ").strip().lower()
        if ans != "y":
            raise SystemExit("Keeping the existing .env. Nothing changed.")

    demo_id = ask("\nDemo API key ID (from https://demo.kalshi.co -> profile -> API keys): ").strip()
    if not demo_id:
        raise SystemExit("A demo key ID is required.")
    demo_pem = _read_pem("Demo", ask, say)
    demo_file = root / "kalshi-demo.pem"
    _write_secret(demo_file, demo_pem)
    say(f"Saved {demo_file.name}")

    live_id = ""
    live_file = root / "kalshi-live.pem"
    if with_live is None:
        with_live = ask("\nAdd the production key now? You can do this later. [y/N] ").strip().lower() == "y"
    if with_live:
        live_id = ask("Production API key ID (from https://kalshi.com -> profile -> API keys): ").strip()
        live_pem = _read_pem("Production", ask, say)
        if live_pem == demo_pem or live_id == demo_id:
            raise SystemExit("The production key must be a different key from the demo key.")
        _write_secret(live_file, live_pem)
        say(f"Saved {live_file.name}")

    lines = [
        "# Written by `bot setup`. Never commit this file.",
        "KALSHI_ENV=demo",
        f"KALSHI_API_KEY_ID={demo_id}",
        "KALSHI_PRIVATE_KEY_PATH=./kalshi-demo.pem",
        f"KALSHI_LIVE_API_KEY_ID={live_id}",
        "KALSHI_LIVE_PRIVATE_KEY_PATH=./kalshi-live.pem",
        "CONFIRM_LIVE=no",
        f"ALERT_WEBHOOK_URL={existing.get('ALERT_WEBHOOK_URL', '')}",
    ]
    _write_secret(env_path, "\n".join(lines) + "\n")
    say(f"Wrote {env_path}")
    return env_path
