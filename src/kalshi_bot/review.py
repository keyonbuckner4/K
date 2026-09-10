"""Weekly calibration review. It proposes configuration changes as a diff; it never applies one."""

from __future__ import annotations

import time
from typing import Any

from .backtest import run_backtest
from .storage import Storage


def weekly_review(storage: Storage, days: float = 7.0) -> dict[str, Any]:
    rep = run_backtest(storage, since_days=days)
    since = time.time() - days * 86400
    decisions = storage.decisions(since=since, limit=100000)
    per: dict[str, dict[str, int]] = {}
    for d in decisions:
        s = per.setdefault(d["strategy"], {"decisions": 0, "accepted": 0, "gate_rejects": 0, "risk_rejects": 0})
        s["decisions"] += 1
        if d["accepted"]:
            s["accepted"] += 1
        if d["stage"] == "gate" and not d["accepted"]:
            s["gate_rejects"] += 1
        if d["stage"] == "risk" and not d["accepted"]:
            s["risk_rejects"] += 1
    gaps = storage.arb_gaps(since=since, limit=100000)
    proposals: list[str] = []
    over = [c for c in rep.calibration if c["n"] >= 10 and c["mean_p"] - c["realized"] > 0.10]
    under = [c for c in rep.calibration if c["n"] >= 10 and c["realized"] - c["mean_p"] > 0.10]
    if over:
        proposals.append("Model is overconfident in buckets " + ", ".join(c["bucket"] for c in over)
                         + ". Proposed diff: [strategies.weather] sigma_f_day0 += 0.5 (widen the forecast error).")
    if under:
        proposals.append("Model is underconfident in buckets " + ", ".join(c["bucket"] for c in under)
                         + ". Proposed diff: [strategies.weather] sigma_f_day0 -= 0.3, only after the overconfidence check above is clean.")
    if rep.brier_model is not None and rep.brier_market is not None and rep.brier_model >= rep.brier_market:
        proposals.append("Brier(model) >= Brier(market): proposed diff: keep every model strategy in mode = \"observe\"; do not enable trade.")
    tradeable_gaps = [g for g in gaps if float(g["net_edge_cents"]) >= 5]
    if gaps and not tradeable_gaps:
        proposals.append(f"{len(gaps)} ladder gaps detected, none >= 5c net after fees. Proposed: no change; keep observing (this is the expected state on liquid ladders).")
    if not proposals:
        proposals.append("No change proposed.")
    return {"window_days": days, "backtest": rep.to_dict(), "per_strategy": per, "arb_gaps": len(gaps), "tradeable_gaps": len(tradeable_gaps),
            "proposals": proposals, "note": "Proposals only. Apply by editing config/bot.toml yourself."}
