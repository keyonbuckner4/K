import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Every fixture market is dated 2026-09-10 (helpers.NOW). RiskEngine.approve() defaults to the real
# clock when the executor calls it, so its 10-minute settlement window would start rejecting every
# fixture the moment the wall clock passes the fixtures' close times. Pin that default clock.
FIXTURE_NOW = datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _pin_risk_clock(monkeypatch):
    import kalshi_bot.risk as risk

    monkeypatch.setattr(risk, "now_utc", lambda: FIXTURE_NOW)
