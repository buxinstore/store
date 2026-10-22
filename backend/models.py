import json
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import CheckConstraint, Index, Numeric, UniqueConstraint

db = SQLAlchemy()


def utcnow():
    return datetime.now(timezone.utc)


class Category(db.Model):
    __tablename__ = "categories"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    slug = db.Column(db.String(140), nullable=False, unique=True, index=True)
    description = db.Column(db.String(500), nullable=False, default="")
    image_url = db.Column(db.String(1000))
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    products = db.relationship("Product", back_populates="category", lazy="selectin")

    def to_dict(self, include_count=False):
        result = {
            "id": self.id,
            "name": self.name,
            "slug": self.slug,
            "description": self.description,
            "image_url": self.image_url,
            "is_active": self.is_active,
        }
        if include_count:
            result["product_count"] = sum(1 for p in self.products if p.is_active)
        return result


class Product(db.Model):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("price >= 0", name="ck_product_price_nonnegative"),
        CheckConstraint("stock >= 0", name="ck_product_stock_nonnegative"),
        Index("ix_products_active_category", "is_active", "category_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(180), nullable=False)
    slug = db.Column(db.String(200), nullable=False, unique=True, index=True)
    description = db.Column(db.Text, nullable=False, default="")
    price = db.Column(Numeric(10, 2), nullable=False)
    compare_at_price = db.Column(Numeric(10, 2))
    image_url = db.Column(db.String(1000))
    cloudinary_public_id = db.Column(db.String(300))
    sku = db.Column(db.String(100), unique=True)
    stock = db.Column(db.Integer, nullable=False, default=0)
    weight_kg = db.Column(Numeric(8, 3))
    rating = db.Column(Numeric(2, 1), nullable=False, default=Decimal("0.0"))
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    is_featured = db.Column(db.Boolean, nullable=False, default=False)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id", ondelete="RESTRICT"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    category = db.relationship("Category", back_populates="products")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "slug": self.slug,
            "description": self.description,
            "price": float(self.price),
            "compare_at_price": float(self.compare_at_price) if self.compare_at_price is not None else None,
            "image_url": self.image_url,
            "cloudinary_public_id": self.cloudinary_public_id,
            "sku": self.sku,
            "stock": self.stock,
            "weight_kg": float(self.weight_kg) if self.weight_kg is not None else None,
            "rating": float(self.rating),
            "is_active": self.is_active,
            "is_featured": self.is_featured,
            "category_id": self.category_id,
            "category_name": self.category.name if self.category else None,
            "category_slug": self.category.slug if self.category else None,
        }


class Order(db.Model):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("subtotal >= 0 AND shipping >= 0 AND total >= 0", name="ck_order_amounts_nonnegative"),
        Index("ix_orders_created_status", "created_at", "status"),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    customer_name = db.Column(db.String(180), nullable=False)
    email = db.Column(db.String(254), nullable=False, index=True)
    address = db.Column(db.Text, nullable=False)
    city = db.Column(db.String(120), nullable=False)
    postal_code = db.Column(db.String(32), nullable=False)
    shipping_method = db.Column(db.String(120), nullable=False, default="standard")
    shipping_snapshot = db.Column(db.Text)
    shipping_acknowledged_at = db.Column(db.DateTime(timezone=True))
    shipping_reconciliation = db.relationship("ShippingReconciliation", back_populates="order", uselist=False, cascade="all, delete-orphan", lazy="joined")
    payment_method = db.Column(db.String(50), nullable=False)
    payment_status = db.Column(db.String(40), nullable=False, default="unpaid")
    status = db.Column(db.String(40), nullable=False, default="pending")
    subtotal = db.Column(Numeric(10, 2), nullable=False)
    shipping = db.Column(Numeric(10, 2), nullable=False, default=Decimal("0.00"))
    total = db.Column(Numeric(10, 2), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    items = db.relationship("OrderItem", back_populates="order", cascade="all, delete-orphan", lazy="selectin")

    def to_dict(self):
        try:
            shipping_address = json.loads(self.address)
            if not isinstance(shipping_address, dict):
                raise ValueError
        except (TypeError, ValueError):
            shipping_address = {"address": self.address, "city": self.city, "postal_code": self.postal_code}
        return {
            "id": self.id,
            "customer_name": self.customer_name,
            "email": self.email,
            "shipping_address": shipping_address,
            "shipping_method": getattr(self, "shipping_method", ""),
            "shipping_snapshot": json.loads(self.shipping_snapshot) if getattr(self, "shipping_snapshot", None) else None,
            "shipping_reconciliation": self.shipping_reconciliation.to_dict() if self.shipping_reconciliation else None,
            "payment_method": self.payment_method,
            "payment_status": self.payment_status,
            "status": self.status,
            "subtotal": float(self.subtotal),
            "shipping": float(self.shipping),
            "total": float(self.total),
            "created_at": self.created_at.isoformat(),
            "items": [item.to_dict() for item in self.items],
        }


class OrderItem(db.Model):
    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_item_quantity_positive"),
        CheckConstraint("unit_price >= 0", name="ck_order_item_price_nonnegative"),
    )

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.String(36), db.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id", ondelete="SET NULL"))
    product_name = db.Column(db.String(180), nullable=False)
    image_url = db.Column(db.String(1000))
    unit_price = db.Column(Numeric(10, 2), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    order = db.relationship("Order", back_populates="items")

    def to_dict(self):
        return {
            "product_id": self.product_id,
            "product_name": self.product_name,
            "image_url": self.image_url,
            "unit_price": float(self.unit_price),
            "quantity": self.quantity,
        }


class StoreSetting(db.Model):
    __tablename__ = "store_settings"

    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.Text, nullable=False)


class ShippingEstimate(db.Model):
    __tablename__ = "shipping_estimates"
    __table_args__ = (UniqueConstraint("country_code", "weight_kg", name="uq_shipping_country_weight"),)

    id = db.Column(db.Integer, primary_key=True)
    country_code = db.Column(db.String(2), nullable=False, index=True)
    country_name = db.Column(db.String(120), nullable=False)
    weight_kg = db.Column(Numeric(8, 3), nullable=False)
    price_usd = db.Column(Numeric(10, 2))
    status = db.Column(db.String(24), nullable=False, default="manual_review")
    confidence = db.Column(db.String(16))
    sources = db.Column(db.Text)
    fx_snapshot = db.Column(db.Text)
    notes = db.Column(db.Text)
    researched_at = db.Column(db.DateTime(timezone=True))
    expires_at = db.Column(db.DateTime(timezone=True))
    manual_override = db.Column(db.Boolean, nullable=False, default=False)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class ShippingEstimateHistory(db.Model):
    __tablename__ = "shipping_estimate_history"
    id = db.Column(db.Integer, primary_key=True)
    estimate_id = db.Column(db.Integer, nullable=False, index=True)
    snapshot = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)


class ShippingRate(db.Model):
    """Canonical USD base-rate and provenance record per destination/provider."""
    __tablename__ = "shipping_rates"
    __table_args__ = (UniqueConstraint("country_code", "provider", name="uq_shipping_rate_country_provider"),)

    id = db.Column(db.Integer, primary_key=True)
    country_code = db.Column(db.String(2), nullable=False, index=True)
    country_name = db.Column(db.String(120), nullable=False)
    origin = db.Column(db.String(120), nullable=False, default="Shenzhen, China")
    provider = db.Column(db.String(24), nullable=False)
    base_price_usd = db.Column(Numeric(10, 2))
    status = db.Column(db.String(24), nullable=False, default="manual_review")
    sources = db.Column(db.Text)
    notes = db.Column(db.Text)
    evidence = db.Column(db.Text)
    confidence = db.Column(db.String(16))
    researched_at = db.Column(db.DateTime(timezone=True))
    expires_at = db.Column(db.DateTime(timezone=True))
    manual_override = db.Column(db.Boolean, nullable=False, default=False)
    source_type = db.Column(db.String(24), nullable=False, default="ai_research")
    approved = db.Column(db.Boolean, nullable=False, default=False)
    rate_version = db.Column(db.Integer, nullable=False, default=1)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class ShippingRateHistory(db.Model):
    __tablename__ = "shipping_rate_history"
    id = db.Column(db.Integer, primary_key=True)
    rate_id = db.Column(db.Integer, nullable=False, index=True)
    snapshot = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)


class ShippingExcelImport(db.Model):
    __tablename__ = "shipping_excel_imports"
    id = db.Column(db.String(36), primary_key=True)
    payload = db.Column(db.Text, nullable=False)
    preview = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    consumed_at = db.Column(db.DateTime(timezone=True))


class ShippingRefreshJob(db.Model):
    __tablename__ = "shipping_refresh_jobs"
    id = db.Column(db.Integer, primary_key=True)
    status = db.Column(db.String(20), nullable=False, default="running")
    attempted = db.Column(db.Integer, nullable=False, default=0)
    succeeded = db.Column(db.Integer, nullable=False, default=0)
    failed = db.Column(db.Integer, nullable=False, default=0)
    tokens_used = db.Column(db.Integer, nullable=False, default=0)
    rate_limit_remaining = db.Column(db.String(40))
    current_destination = db.Column(db.String(2))
    pending = db.Column(db.Integer, nullable=False, default=0)
    message = db.Column(db.Text)
    started_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    finished_at = db.Column(db.DateTime(timezone=True))


class ShippingRefreshLock(db.Model):
    __tablename__ = "shipping_refresh_lock"
    id = db.Column(db.Integer, primary_key=True, default=1)
    locked_until = db.Column(db.DateTime(timezone=True), nullable=False)


class ShippingReconciliation(db.Model):
    __tablename__ = "shipping_reconciliations"
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.String(36), db.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    status = db.Column(db.String(40), nullable=False, default="awaiting_final_cost")
    quoted_usd = db.Column(Numeric(10, 2), nullable=False)
    final_usd = db.Column(Numeric(10, 2))
    difference_usd = db.Column(Numeric(10, 2))
    reason = db.Column(db.Text)
    approval_token_hash = db.Column(db.String(64))
    approval_expires_at = db.Column(db.DateTime(timezone=True))
    customer_decision = db.Column(db.String(16))
    customer_decided_at = db.Column(db.DateTime(timezone=True))
    refund_reference = db.Column(db.String(200))
    resolved_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    order = db.relationship("Order", back_populates="shipping_reconciliation")

    def to_dict(self):
        return {"status": self.status, "quoted_usd": float(self.quoted_usd),
                "final_usd": float(self.final_usd) if self.final_usd is not None else None,
                "difference_usd": float(self.difference_usd) if self.difference_usd is not None else None,
                "reason": self.reason, "customer_decision": self.customer_decision,
                "customer_decided_at": self.customer_decided_at.isoformat() if self.customer_decided_at else None,
                "refund_reference": self.refund_reference,
                "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None}


class PaymentAttempt(db.Model):
    __tablename__ = "payment_attempts"

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.String(36), db.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True)
    provider = db.Column(db.String(30), nullable=False)
    provider_reference = db.Column(db.String(200), nullable=False, unique=True, index=True)
    mode = db.Column(db.String(10), nullable=False)
    status = db.Column(db.String(30), nullable=False, default="pending")
    frontend_url = db.Column(db.String(1000), nullable=False)
    paypal_currency = db.Column(db.String(3))
    paypal_amount = db.Column(Numeric(12, 2))
    paypal_exchange_rate = db.Column(Numeric(18, 8))
    paypal_rate_date = db.Column(db.String(40))
    modem_currency = db.Column(db.String(3))
    modem_amount = db.Column(Numeric(12, 2))
    modem_exchange_rate = db.Column(Numeric(18, 8))
    modem_rate_date = db.Column(db.String(40))
    order = db.relationship("Order", lazy="joined")
