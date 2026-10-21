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


class PaymentAttempt(db.Model):
    __tablename__ = "payment_attempts"

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.String(36), db.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True)
    provider = db.Column(db.String(30), nullable=False)
    provider_reference = db.Column(db.String(200), nullable=False, unique=True, index=True)
    mode = db.Column(db.String(10), nullable=False)
    status = db.Column(db.String(30), nullable=False, default="pending")
    frontend_url = db.Column(db.String(1000), nullable=False)
    order = db.relationship("Order", lazy="joined")
