"""``bot setup``: guided first run. Asks for the API key IDs, saves the private keys as .pem
files, writes .env, then checks the connection. Nothing leaves the machine except the signed
requests to Kalshi. Keys are pasted into the terminal or given as a path to a saved file.

The paste is accepted however it arrives: with or without the BEGIN/END armor lines, with
damaged dashes, or collapsed onto one line. Leftover pasted lines are drained after a failure
so they never reach the shell as commands.
"""

from __future__ import annotations

import os
import re
import stat
import sys
import time
from pathlib import Path
from typing import Callable

from cryptography.hazmat.primitives import serialization

from .config import parse_dotenv

Input = Callable[[str], str]
Print = Callable[[str], None]
Pending = Callable[[], bool]

_HEADER = re.compile(r"-*\s*BEGIN\s+([A-Z ]*?PRIVATE KEY)\s*-*")
_FOOTER = re.compile(r"-*\s*END\s+([A-Z ]*?PRIVATE KEY)\s*-*")
_B64 = re.compile(r"^[A-Za-z0-9+/=]+$")
_KEY_ID = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
KEY_TYPES = ("RSA PRIVATE KEY", "PRIVATE KEY", "EC PRIVATE KEY")


def console_input_pending(timeout: float = 0.35) -> bool:
    """True if more typed/pasted input is already waiting (so a multi-line paste is read whole)."""
    try:
        if sys.platform == "win32":
            import msvcrt

            end = time.monotonic() + timeout
            while time.monotonic() < end:
                if msvcrt.kbhit():
                    return True
                time.sleep(0.02)
            return False
        import select

        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        return bool(ready)
    except Exception:
        return False


def drain(ask: Input, pending: Pending, limit: int = 500) -> int:
    """Discard pending lines (the rest of a bad paste) so they never reach the shell."""
    n = 0
    while n < limit and pending():
        try:
            ask("")
        except (EOFError, KeyboardInterrupt):
            break
        n += 1
    return n


def normalize_pem(text: str) -> str | None:
    """Rebuild a clean PEM from whatever was pasted. Returns None if no key material is found."""
    key_type: str | None = None
    body_parts: list[str] = []
    for raw in text.replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        h = _HEADER.search(line)
        if h:
            key_type = h.group(1).strip()
            line = line[h.end():]
        f = _FOOTER.search(line)  # searched after the header is removed so the offsets line up
        if f:
            line = line[: f.start()]
        line = re.sub(r"\s+", "", line)
        if line and _B64.match(line):
            body_parts.append(line)
    body = "".join(body_parts)
    if len(body) < 64:
        return None
    types = [key_type] if key_type else list(KEY_TYPES)
    for t in types:
        wrapped = "\n".join(body[i:i + 64] for i in range(0, len(body), 64))
        pem = f"-----BEGIN {t}-----\n{wrapped}\n-----END {t}-----\n"
        try:
            serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
            return pem
        except Exception:
            continue
    return None


def _collect_paste(first: str, ask: Input, pending: Pending) -> str:
    lines = [first]
    while True:
        if _FOOTER.search(lines[-1]):
            break
        if not pending():
            break
        try:
            line = ask("")
        except EOFError:
            break
        line = line.strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _read_pem(label: str, ask: Input, say: Print, pending: Pending) -> str:
    say(f"\n{label} private key. Paste the whole key (all of it, including the -----BEGIN and -----END lines),")
    say("or type the path to the file you saved it in. Then press Enter.")
    for attempt in range(3):
        try:
            first = ask("> ").strip()
        except EOFError:
            break
        if not first:
            say("Nothing entered; try again.")
            continue
        looks_like_key = bool(_HEADER.search(first) or _B64.match(first) and len(first) >= 32)
        if not looks_like_key:
            p = Path(first).expanduser()
            if p.exists():
                text = p.read_text(encoding="utf-8")
            else:
                drain(ask, pending)
                say(f"No file at {p}, and that does not look like a key. Try again.")
                continue
        else:
            text = _collect_paste(first, ask, pending)
        pem = normalize_pem(text)
        if pem is None:
            drain(ask, pending)
            say("That is not a complete, unencrypted RSA private key. Copy everything Kalshi shows, from the BEGIN line to the END line, and try again.")
            continue
        drain(ask, pending)
        return pem
    drain(ask, pending)
    raise SystemExit("Could not read a valid private key after 3 attempts. Run `uv run bot setup` again.")


def _read_key_id(prompt: str, ask: Input, say: Print, pending: Pending) -> str:
    for _ in range(3):
        value = ask(prompt).strip()
        drain(ask, pending)
        if _KEY_ID.match(value) and "BEGIN" not in value:
            return value
        say("That does not look like a key ID (Kalshi key IDs look like 8-4-4-4-12 groups of letters and digits). Try again.")
    raise SystemExit("Could not read a key ID after 3 attempts.")


def _write_secret(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def run_setup(root: Path, ask: Input = input, say: Print = print, with_live: bool | None = None,
              pending: Pending = console_input_pending) -> Path:
    env_path = root / ".env"
    existing = parse_dotenv(env_path.read_text(encoding="utf-8")) if env_path.exists() else {}
    say("kalshi-bot setup. Demo first; the production key is optional and only used with --live.")
    if existing:
        ans = ask(f"{env_path} already exists. Overwrite it? [y/N] ").strip().lower()
        if ans != "y":
            raise SystemExit("Keeping the existing .env. Nothing changed.")

    demo_id = _read_key_id("\nDemo API key ID (from https://demo.kalshi.co -> profile -> API keys): ", ask, say, pending)
    demo_pem = _read_pem("Demo", ask, say, pending)
    demo_file = root / "kalshi-demo.pem"
    _write_secret(demo_file, demo_pem)
    say(f"Saved {demo_file.name}")

    live_id = ""
    live_file = root / "kalshi-live.pem"
    if with_live is None:
        with_live = ask("\nAdd the production key now? You can do this later. [y/N] ").strip().lower() == "y"
    if with_live:
        live_id = _read_key_id("Production API key ID (from https://kalshi.com -> profile -> API keys): ", ask, say, pending)
        live_pem = _read_pem("Production", ask, say, pending)
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
