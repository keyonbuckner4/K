"""The HALT-file kill switch.

``halt_active`` is called from exactly one place in the codebase: ``RiskEngine.approve``.
Every order passes through that method, so touching the file stops all order placement.
The CLI commands ``bot halt`` and ``bot resume`` create and remove the file.
"""

from __future__ import annotations

from pathlib import Path

HALT_FILENAME = "HALT"


def halt_active(halt_path: Path) -> bool:
    return Path(halt_path).exists()


def engage(halt_path: Path, reason: str = "manual") -> None:
    Path(halt_path).write_text(f"{reason}\n", encoding="utf-8")


def release(halt_path: Path) -> bool:
    p = Path(halt_path)
    if p.exists():
        p.unlink()
        return True
    return False
