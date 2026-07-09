"""Manual backend-action overrides for remake reporting.

Source file maintained by Lars in iCloud Desktop:
Remake_Backend_Actions_for_Lars.html

The committed JSON lets GitHub Actions/Fly regenerate the report without
needing access to Lars's local iCloud folder.
"""
from __future__ import annotations

import json
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parents[1]
ACTIONS_PATH = ROOT / "data" / "remake_backend_actions.json"


def _load() -> dict:
    if not ACTIONS_PATH.exists():
        return {"cancelled": [], "admin_changes": [], "not_remake": [], "missing_in_backend": []}
    with ACTIONS_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


ACTIONS = _load()
CANCELLED = {str(r.get("order")) for r in ACTIONS.get("cancelled", []) if r.get("order")}
NOT_REMAKE = {str(r.get("order")) for r in ACTIONS.get("not_remake", []) if r.get("order")}
EXCLUDED_REMAKE_ORDERS = CANCELLED | NOT_REMAKE

# order -> list[{qty, to_admin, ...}]
ADMIN_CHANGES = defaultdict(list)
for row in ACTIONS.get("admin_changes", []):
    order = str(row.get("order") or "").strip()
    if not order:
        continue
    try:
        qty = int(float(str(row.get("qty") or 0).replace(",", "")))
    except Exception:
        qty = 0
    ADMIN_CHANGES[order].append({**row, "order": order, "qty": qty})


def is_excluded_remake(order_no) -> bool:
    return str(order_no) in EXCLUDED_REMAKE_ORDERS


def admin_action_note(order_no) -> str:
    order = str(order_no)
    if order in CANCELLED:
        row = next((r for r in ACTIONS.get("cancelled", []) if str(r.get("order")) == order), {})
        return "Backend action: cancel/remove from remake KPI" + (f" — {row.get('reason')}" if row.get("reason") else "")
    if order in NOT_REMAKE:
        row = next((r for r in ACTIONS.get("not_remake", []) if str(r.get("order")) == order), {})
        return "Backend action: not a remake/exclude from remake KPI" + (f" — {row.get('reason')}" if row.get("reason") else "")
    if order in ADMIN_CHANGES:
        bits = [f"{r.get('qty', 0)} pcs → {r.get('to_admin', '')}" for r in ADMIN_CHANGES[order]]
        return "Backend action: change admin credit — " + "; ".join(bits)
    return ""


def remake_admin_allocations(order_no, original_admin, total_qty):
    """Return [(admin_name, qty, key_suffix)] for remake numerator attribution.

    Excluded orders return []. Admin-change rows move the listed qty to the
    target admin(s). If listed qty is less than total_qty, the residual stays
    with the original admin (example: #20451 has 2 pcs moved and 1 pc stays).
    """
    order = str(order_no)
    try:
        total = int(float(total_qty or 0))
    except Exception:
        total = 0
    if order in EXCLUDED_REMAKE_ORDERS:
        return []
    changes = ADMIN_CHANGES.get(order) or []
    if not changes:
        return [(original_admin or "(unknown)", total, order)]
    out = []
    moved = 0
    for idx, row in enumerate(changes):
        qty = max(0, int(row.get("qty") or 0))
        if qty <= 0:
            continue
        moved += qty
        out.append((row.get("to_admin") or original_admin or "(unknown)", qty, f"{order}:{idx}"))
    residual = max(0, total - moved)
    if residual:
        out.append((original_admin or "(unknown)", residual, f"{order}:residual"))
    return out
