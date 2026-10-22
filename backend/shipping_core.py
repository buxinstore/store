"""Dependency-free shipping rules that are safe to unit test."""
import json
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
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


def billable_units(actual_kg):
    """Number of 0.5 kg chargeable units; never uses dimensional weight."""
    return int(billable_weight(actual_kg) / Decimal("0.5"))


def shipping_charge(actual_kg, base_price_usd):
    """Return billable weight, units, and USD charge for a saved base rate."""
    price = Decimal(str(base_price_usd))
    if not price.is_finite() or price < 0:
        raise ValueError("A nonnegative saved shipping rate is required")
    weight = billable_weight(actual_kg)
    units = int(weight / Decimal("0.5"))
    return weight, units, (price * units).quantize(Decimal("0.01"))


def shipping_rate_is_current(row, now=None):
    now = now or datetime.now(timezone.utc)
    expires = row.expires_at
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return (row.status == "available" and getattr(row, "approved", True) and row.base_price_usd is not None
            and Decimal(str(row.base_price_usd)) >= 0
            and (row.manual_override or (expires is not None and expires > now)))


def rate_needs_research(row, now=None, freshness_days=30):
    """A recently reviewed manual-review route is not retried every day."""
    now = now or datetime.now(timezone.utc)
    if row is None or row.manual_override:
        return False
    researched = row.researched_at
    if researched is None:
        return True
    if researched.tzinfo is None:
        researched = researched.replace(tzinfo=timezone.utc)
    if row.status == "available":
        expires = row.expires_at
        if expires and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires is None or expires <= now
    if row.status == "stale":
        return True
    return researched <= now - timedelta(days=max(1, int(freshness_days)))


def groq_status_retryable(status_code):
    return status_code == 429 or status_code in {500, 502, 503, 504}


def groq_retry_delay(retry_after, attempt, jitter=0.5):
    """Honor Groq Retry-After; otherwise use capped exponential backoff + jitter."""
    try:
        if retry_after is not None:
            return max(0.0, float(retry_after))
    except (TypeError, ValueError):
        try:
            retry_time = parsedate_to_datetime(str(retry_after))
            if retry_time.tzinfo is None:
                retry_time = retry_time.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_time - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            pass
    return min(60.0, 2 ** max(0, int(attempt)) + max(0.0, float(jitter)))


def official_provider_sources(provider, candidate_urls, citations):
    """Intersect model-selected sources with API citations and provider domains."""
    domains = {"dhl": "dhl.com", "fedex": "fedex.com"}
    domain = domains.get(provider)
    cited = {str(source.get("url")): source for source in citations if isinstance(source, dict)}
    result = []
    for url in candidate_urls if isinstance(candidate_urls, list) else []:
        source = cited.get(str(url))
        host = (urlsplit(str(url)).hostname or "").lower()
        if domain and source and (host == domain or host.endswith("." + domain)):
            result.append(source)
    return result


def ordered_destination_codes(codes, retry_codes=(), cursor=0):
    """Return stable round-robin order with persisted failures at the front."""
    codes = list(dict.fromkeys(codes))
    if codes:
        cursor = int(cursor) % len(codes)
        codes = codes[cursor:] + codes[:cursor]
    retries = list(dict.fromkeys(code for code in retry_codes if code in codes))
    return retries + [code for code in codes if code not in set(retries)]


def missing_provider_rate_pairs(country_codes, present_pairs, providers=("dhl", "fedex", "community")):
    """Return a stable, duplicate-free list of absent country/provider rows."""
    present = set(present_pairs)
    return [(code, provider) for code in dict.fromkeys(country_codes) for provider in providers
            if (code, provider) not in present]


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
