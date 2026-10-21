"""Dependency-free shipping rules that are safe to unit test."""
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from urllib.parse import urlsplit


def billable_weight(actual_kg):
    try:
        actual = Decimal(str(actual_kg))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Product weights are missing or invalid; shipping requires manual review")
    if not actual.is_finite() or actual <= 0 or actual > Decimal("1000"):
        raise ValueError("Shipment weight is outside the supported range; shipping requires manual review")
    return max(Decimal("0.5"), (actual / Decimal("0.5")).quantize(Decimal("1"), rounding=ROUND_CEILING) * Decimal("0.5"))


def estimate_is_current(row, now=None):
    now = now or datetime.now(timezone.utc)
    expires = row.expires_at
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return row.status == "available" and row.price_usd is not None and expires is not None and expires > now


def safe_sources(value):
    try:
        rows = json.loads(value or "[]")
        if not isinstance(rows, list):
            return []
        result = []
        for row in rows[:5]:
            if not isinstance(row, dict):
                continue
            parsed = urlsplit(str(row.get("url", "")))
            if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
                result.append({"url": parsed.geturl(), "title": str(row.get("title", ""))[:240]})
        return result
    except (TypeError, ValueError):
        return []
