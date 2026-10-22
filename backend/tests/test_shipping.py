import unittest
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from shipping_core import (billable_units, billable_weight, estimate_is_current,
    groq_retry_delay, groq_status_retryable, missing_provider_rate_pairs, official_provider_sources,
    ordered_destination_codes, rate_needs_research, safe_sources, shipping_charge,
    shipping_rate_is_current)
from datetime import datetime, timezone
from email.utils import format_datetime


def utcnow():
    return datetime.now(timezone.utc)


class BillableWeightTests(unittest.TestCase):
    def test_minimum_weight_and_half_kg_rounding(self):
        self.assertEqual(billable_weight("0.1"), Decimal("0.5"))
        self.assertEqual(billable_weight("0.5"), Decimal("0.5"))
        self.assertEqual(billable_weight("0.5001"), Decimal("1.0"))
        self.assertEqual(billable_weight("1.5"), Decimal("1.5"))
        self.assertEqual(billable_weight("1.5001"), Decimal("2.0"))

    def test_missing_nonpositive_or_implausible_weight_requires_review(self):
        for weight in (None, "0", "-1", "1000.1", "not-a-number"):
            with self.subTest(weight=weight), self.assertRaises(ValueError):
                billable_weight(weight)

    def test_billable_units_and_shipping_total(self):
        self.assertEqual(billable_units("0.2"), 1)
        self.assertEqual(billable_units("0.8"), 2)
        self.assertEqual(billable_units("1.5"), 3)
        self.assertEqual(shipping_charge("1.5", "4.00"), (Decimal("1.5"), 3, Decimal("12.00")))


class ProviderRateTests(unittest.TestCase):
    def test_provider_rates_are_separate_and_only_fresh_or_override_is_current(self):
        now = utcnow()
        dhl = SimpleNamespace(status="available", base_price_usd=Decimal("4.00"), expires_at=now + timedelta(days=1), manual_override=False)
        fedex = SimpleNamespace(status="manual_review", base_price_usd=None, expires_at=None, manual_override=False)
        stale = SimpleNamespace(status="available", base_price_usd=Decimal("8.00"), expires_at=now - timedelta(days=1), manual_override=False)
        manual = SimpleNamespace(status="available", base_price_usd=Decimal("6.00"), expires_at=now - timedelta(days=1), manual_override=True)
        self.assertTrue(shipping_rate_is_current(dhl, now))
        self.assertFalse(shipping_rate_is_current(fedex, now))
        self.assertFalse(shipping_rate_is_current(stale, now))
        self.assertTrue(shipping_rate_is_current(manual, now))

    def test_multiple_product_weights_sum_before_rounding(self):
        lines = [(Decimal("1.0"), 1), (Decimal("0.5"), 1), (Decimal("0.2"), 2)]
        total_weight = sum((weight * quantity for weight, quantity in lines), Decimal("0"))
        self.assertEqual(total_weight, Decimal("1.9"))
        self.assertEqual(shipping_charge(total_weight, "5.25"), (Decimal("2.0"), 4, Decimal("21.00")))

    def test_failed_and_unresearched_rates_are_revisited_without_daily_manual_review_loop(self):
        now = utcnow()
        never = SimpleNamespace(status="manual_review", researched_at=None, expires_at=None, manual_override=False)
        recently_reviewed = SimpleNamespace(status="manual_review", researched_at=now - timedelta(days=2), expires_at=None, manual_override=False)
        expired = SimpleNamespace(status="available", researched_at=now - timedelta(days=40), expires_at=now - timedelta(days=1), manual_override=False)
        override = SimpleNamespace(status="available", researched_at=None, expires_at=None, manual_override=True)
        self.assertTrue(rate_needs_research(never, now))
        self.assertFalse(rate_needs_research(recently_reviewed, now))
        self.assertTrue(rate_needs_research(expired, now))
        self.assertFalse(rate_needs_research(override, now))

    def test_groq_retry_after_and_permanent_error_policy(self):
        self.assertEqual(groq_retry_delay("4.5", 0), 4.5)
        self.assertEqual(groq_retry_delay(None, 2, jitter=0.25), 4.25)
        http_date = format_datetime(utcnow() + timedelta(seconds=30), usegmt=True)
        self.assertGreater(groq_retry_delay(http_date, 0), 0)
        self.assertTrue(groq_status_retryable(429))
        self.assertTrue(groq_status_retryable(503))
        self.assertFalse(groq_status_retryable(403))

    def test_failed_destinations_move_to_front_of_resumable_batch(self):
        self.assertEqual(ordered_destination_codes(["AA", "BB", "CC", "DD"], ["CC", "CC"], cursor=1), ["CC", "BB", "DD", "AA"])

    def test_country_provider_seed_is_complete_and_duplicate_free(self):
        countries = ["GM", "IN", "GM"]
        present = {("GM", "dhl"), ("IN", "fedex")}
        missing = missing_provider_rate_pairs(countries, present)
        self.assertEqual(len(missing), 4)
        self.assertEqual(len(set(missing)), 4)
        self.assertNotIn(("GM", "dhl"), missing)
        self.assertIn(("GM", "community"), missing)

    def test_only_official_carrier_citations_can_support_a_rate(self):
        citations = [{"url": "https://www.dhl.com/global-en/home.html", "title": "DHL"},
                     {"url": "https://www.fedex.com/en-us/shipping.html", "title": "FedEx"}]
        self.assertEqual(len(official_provider_sources("dhl", [citations[0]["url"], citations[1]["url"], "https://fake.example/rate"], citations)), 1)
        self.assertEqual(len(official_provider_sources("fedex", [citations[0]["url"], citations[1]["url"]], citations)), 1)
        self.assertEqual(official_provider_sources("community", [citations[0]["url"]], citations), [])


class EstimateValidationTests(unittest.TestCase):
    def test_expired_and_incomplete_estimates_are_not_current(self):
        self.assertFalse(estimate_is_current(SimpleNamespace(status="available", price_usd=Decimal("5"), expires_at=utcnow() - timedelta(seconds=1))))
        self.assertFalse(estimate_is_current(SimpleNamespace(status="manual_review", price_usd=None, expires_at=utcnow() + timedelta(days=1))))
        self.assertTrue(estimate_is_current(SimpleNamespace(status="available", price_usd=Decimal("5"), expires_at=utcnow() + timedelta(days=1))))

    def test_sources_reject_non_https_and_credentials(self):
        sources = safe_sources('[{"url":"http://example.com/a"},{"url":"https://user:pass@example.com/a"},{"url":"https://example.org/rate","title":"Rate"}]')
        self.assertEqual(sources, [{"url": "https://example.org/rate", "title": "Rate"}])


if __name__ == "__main__":
    unittest.main()
