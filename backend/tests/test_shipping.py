import unittest
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from shipping_core import billable_weight, estimate_is_current, safe_sources
from datetime import datetime, timezone


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
