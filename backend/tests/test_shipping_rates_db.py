import json
import unittest
from datetime import timedelta
from decimal import Decimal

from flask import Flask

from models import Order, ShippingRate, ShippingRateHistory, ShippingRefreshJob, StoreSetting, db, utcnow
from shipping import refresh_destinations, save_provider_rate
from shipping_core import shipping_charge, shipping_rate_is_current


class ShippingRateDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(SQLALCHEMY_DATABASE_URI="sqlite://", SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()
        db.engines.clear()

    def test_country_provider_lookup_and_missing_or_stale_rates(self):
        now = utcnow()
        dhl = ShippingRate(country_code="GM", country_name="Gambia", provider="dhl", base_price_usd=Decimal("4.00"), status="available", approved=True, expires_at=now + timedelta(days=2))
        fedex = ShippingRate(country_code="GM", country_name="Gambia", provider="fedex", status="manual_review")
        stale = ShippingRate(country_code="US", country_name="United States", provider="dhl", base_price_usd=Decimal("8.00"), status="available", expires_at=now - timedelta(days=1))
        db.session.add_all([dhl, fedex, stale]); db.session.commit()
        found = ShippingRate.query.filter_by(country_code="GM", provider="dhl").one()
        self.assertTrue(shipping_rate_is_current(found, now))
        self.assertFalse(shipping_rate_is_current(ShippingRate.query.filter_by(country_code="GM", provider="fedex").one(), now))
        self.assertFalse(shipping_rate_is_current(ShippingRate.query.filter_by(country_code="US", provider="dhl").one(), now))
        self.assertEqual(shipping_charge("0.8", found.base_price_usd)[2], Decimal("8.00"))

    def test_research_updates_version_and_history_but_never_replaces_manual_override(self):
        rate = ShippingRate(country_code="GM", country_name="Gambia", provider="dhl", base_price_usd=Decimal("4.00"), status="available", rate_version=3)
        db.session.add(rate); db.session.commit()
        saved = save_provider_rate("GM", "Gambia", "dhl", {"amount": Decimal("6.25"), "sources": [{"url": "https://carrier.example/rates", "title": "Carrier"}], "confidence": "high", "notes": "verified"})
        db.session.commit()
        self.assertTrue(saved)
        self.assertEqual(rate.base_price_usd, Decimal("6.25"))
        self.assertEqual(rate.rate_version, 4)
        self.assertEqual(ShippingRateHistory.query.filter_by(rate_id=rate.id).count(), 1)
        rate.manual_override = True
        rate.base_price_usd = Decimal("7.00")
        db.session.commit()
        self.assertFalse(save_provider_rate("GM", "Gambia", "dhl", {"amount": Decimal("2.00"), "sources": [{"url": "https://carrier.example/new"}]}))
        db.session.commit()
        self.assertEqual(rate.base_price_usd, Decimal("7.00"))

    def test_manual_excel_mode_blocks_scheduled_ai_jobs(self):
        db.session.add(StoreSetting(key="shipping_management_mode", value="manual_excel"))
        db.session.commit()
        job = refresh_destinations(maximum=1)
        self.assertEqual(job.status, "paused_manual_mode")
        self.assertEqual(job.attempted, 0)

    def test_inflight_groq_result_is_discarded_after_manual_mode_switch(self):
        db.session.add_all([StoreSetting(key="shipping_management_mode", value="ai_research"),
                            StoreSetting(key="shipping_refresh_cursor", value="0")])
        db.session.commit()

        def switch_to_manual(_country):
            setting = db.session.get(StoreSetting, "shipping_management_mode")
            setting.value = "manual_excel"
            db.session.commit()
            return {"tokens_used": 1, "remaining_tokens": "100", "providers": {
                "dhl": {"amount": Decimal("4.00"), "sources": [{"url": "https://www.dhl.com/rates"}], "confidence": "high"},
                "fedex": {"amount": Decimal("5.00"), "sources": [{"url": "https://www.fedex.com/rates"}], "confidence": "high"},
            }}

        from unittest.mock import patch
        with patch("shipping.groq_research_destination", side_effect=switch_to_manual):
            job = refresh_destinations(maximum=1)
        self.assertEqual(job.status, "stopped_manual_mode")
        self.assertEqual(job.succeeded, 0)
        self.assertEqual(ShippingRate.query.count(), 0)

    def test_excel_approved_rate_is_preserved_by_ai_research(self):
        rate = ShippingRate(country_code="GM", country_name="Gambia", provider="dhl", base_price_usd=Decimal("0.00"),
            status="available", source_type="excel_import", manual_override=True, approved=True)
        db.session.add(rate)
        db.session.commit()
        self.assertFalse(save_provider_rate("GM", "Gambia", "dhl", {
            "amount": Decimal("9.99"), "sources": [{"url": "https://www.dhl.com/new-rate"}], "confidence": "high"}))
        db.session.commit()
        self.assertEqual(rate.base_price_usd, Decimal("0.00"))
        self.assertEqual(rate.source_type, "excel_import")

    def test_order_shipping_snapshot_survives_later_rate_changes(self):
        rate = ShippingRate(country_code="GM", country_name="Gambia", provider="dhl", base_price_usd=Decimal("4.00"), status="available", rate_version=2)
        snapshot = {"rate_id": 1, "rate_version": 2, "country_code": "GM", "provider": "dhl", "product_weight_kg": "1.5", "billable_weight_kg": "1.5", "base_price_per_0_5kg_usd": "4.00", "billable_units": 3, "shipping_charge_usd": "12.00", "currency": "USD"}
        order = Order(id="snapshot-test", customer_name="Test Customer", email="test@example.com", address="{}", city="Banjul", postal_code="0000", shipping_method="DHL · 1.5 kg", shipping_snapshot=json.dumps(snapshot), payment_method="paypal", subtotal=Decimal("20.00"), shipping=Decimal("12.00"), total=Decimal("32.00"))
        db.session.add_all([rate, order]); db.session.commit()
        rate.base_price_usd = Decimal("9.00"); rate.rate_version = 3; db.session.commit()
        stored = db.session.get(Order, "snapshot-test")
        self.assertEqual(json.loads(stored.shipping_snapshot), snapshot)
        self.assertEqual(stored.shipping, Decimal("12.00"))


if __name__ == "__main__":
    unittest.main()
