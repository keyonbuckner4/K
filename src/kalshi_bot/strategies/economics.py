"""Economics: CPI, Fed, claims markets priced from consensus distributions the operator supplies in
``config/econ_views.toml``. Low frequency. Refuses to price any event without a view."""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Any

from ..intent import Intent
from ..pricing import prob_yes_normal, to_decimal_prob
from .base import ScanContext, Strategy

log = logging.getLogger(__name__)


def load_views(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    out: dict[str, dict[str, Any]] = {}
    for v in data.get("views", []):
        if "event_ticker" in v and "mean" in v and "sd" in v:
            out[str(v["event_ticker"])] = {"mean": float(v["mean"]), "sd": float(v["sd"]), "note": v.get("note", "")}
    return out


class EconomicsStrategy(Strategy):
    name = "economics"

    def __init__(self, cfg: dict[str, Any], storage, root: Path | None = None):
        super().__init__(cfg, storage)
        p = Path(str(self.cfg.get("views_path", "config/econ_views.toml")))
        self.views_path = p if p.is_absolute() else (root or Path.cwd()) / p
        self.views = load_views(self.views_path)
        self._series = sorted({t.split("-", 1)[0] for t in self.views})

    def series(self) -> list[str]:
        return self._series

    async def scan(self, ctx: ScanContext) -> list[Intent]:
        intents: list[Intent] = []
        self.views = load_views(self.views_path)
        for event in ctx.events:
            view = self.views.get(event.event_ticker)
            if view is None:
                self.reject("model", "no consensus view configured for this event", event_ticker=event.event_ticker)
                continue
            for m in event.markets:
                if not m.is_open():
                    continue
                p = prob_yes_normal(m, view["mean"], view["sd"], integer_settlement=False)
                if p is None:
                    self.reject("model", f"unpriceable strike_type {m.strike_type!r}", m)
                    continue
                reason = f"consensus N({view['mean']}, {view['sd']}) {view.get('note', '')}".strip()
                it = self.directional_candidate(m, ctx.book(m.ticker), to_decimal_prob(p), ctx, reason)
                if it:
                    intents.append(it)
        return intents
