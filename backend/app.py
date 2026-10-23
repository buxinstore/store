import base64
import hmac
import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

import cloudinary
import cloudinary.uploader
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, request, send_file
from flask_cors import CORS
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.exceptions import RequestEntityTooLarge

from models import Category, Order, OrderItem, PaymentAttempt, Product, ProductMedia, ProductExcelImport, StoreSetting, ShippingEstimate, ShippingEstimateHistory, ShippingRate, ShippingRateHistory, ShippingRefreshJob, ShippingReconciliation, ShippingExcelImport, db
from catalog_seed import seed_starter_catalog
from shipping_core import billable_weight, estimate_is_current, missing_provider_rate_pairs, shipping_charge, shipping_rate_is_current, safe_sources
from shipping_excel import build_template, parse_workbook, MAX_XLSX_BYTES
from product_excel import build_product_template, parse_product_workbook, safe_external_url, MAX_XLSX_BYTES as MAX_PRODUCT_XLSX_BYTES

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

ALLOWED_ORDER_STATUSES = {"pending", "processing", "shipped", "completed", "cancelled"}
PAYMENT_MODES = {"test", "live"}
PAYMENT_PROVIDERS = {"modem", "paypal"}
SHIPPING_MODES = {"manual_excel", "ai_research"}
ONLINE_PAYMENT_MESSAGE = "Online payments are not configured for the selected payment mode."
DEFAULT_SHIPPING_METHODS = [
    {"id": "dhl", "name": "DHL", "price": "4.99", "enabled": True},
    {"id": "fedex", "name": "FedEx", "price": "4.99", "enabled": True},
    {"id": "community", "name": "Community courier", "price": "4.99", "enabled": True},
]


def prepare_product(payload, existing=None):
    """Validate a create/update payload and return a detached canonical Product."""
    def value(key, default=None):
        if key in payload:
            return payload[key]
        return getattr(existing, key, default) if existing else default

    def boolean(key, default=False):
        raw = value(key, default)
        if isinstance(raw, bool):
            return raw
        normalized = str(raw).strip().lower()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0", ""}:
            return False
        raise ValueError(f"{key} must be true or false")

    try:
        name = str(value("name", "") or "").strip()
        slug = re.sub(r"[^a-z0-9]+", "-", str(value("slug", "") or name).strip().lower()).strip("-")[:200]
        price = Decimal(str(value("price", "")))
        stock_raw = value("stock", 0)
        stock = int(stock_raw)
        category_id = int(value("category_id", 0))
        weight_raw = value("weight_kg", None)
        weight = Decimal(str(weight_raw)) if weight_raw not in (None, "") else None
        compare_raw = value("compare_at_price", None)
        compare = Decimal(str(compare_raw)) if compare_raw not in (None, "") else None
        featured = boolean("is_featured", False)
        active_requested = boolean("is_active", True)
    except (TypeError, ValueError, InvalidOperation, OverflowError):
        return None, "Valid name, price, stock, and category are required"
    category = db.session.get(Category, category_id)
    if not name or len(name) > 180 or not slug:
        return None, "Product name is required and must be at most 180 characters"
    if not category:
        return None, "Select an existing product category"
    if not price.is_finite() or price < 0 or price > Decimal("99999999.99"):
        return None, "Selling price must be a valid non-negative USD amount"
    if compare is not None and (not compare.is_finite() or compare < price or compare > Decimal("99999999.99")):
        return None, "Original price must be a valid amount at least as high as the selling price"
    if stock < 0 or stock > 2147483647 or str(stock_raw).strip() != str(stock):
        return None, "Stock quantity must be a non-negative whole number in the supported range"
    if weight is not None and (not weight.is_finite() or weight <= 0 or weight > 1000):
        return None, "Product weight must be between 0 and 1000 kg"

    status = str(value("status", "active") or "active").strip().lower()
    if "status" not in payload and "is_active" in payload:
        status = "active" if payload["is_active"] else "inactive"
    if status not in {"draft", "active", "inactive", "archived"}:
        return None, "Product status must be Draft, Active, Inactive, or Archived"
    active = status == "active" if "status" in payload or not existing else active_requested
    if status == "active" and not active:
        status = "inactive"
    elif status == "inactive" and active_requested and "is_active" in payload and "status" not in payload:
        status = "active"
    image_url = value("image_url", None)
    if image_url:
        image_url = str(image_url).strip()
        if not (safe_external_url(image_url, "image") or (image_url.startswith("assets/") and ".." not in image_url.split("/"))):
            return None, "Product image must use HTTPS or a safe assets/ path"
    sku = str(value("sku", "") or "").strip().upper() or None
    if sku and len(sku) > 100:
        return None, "SKU must be at most 100 characters"
    if not existing and not sku:
        return None, "SKU is required"
    metadata = {}
    if existing:
        try:
            metadata = json.loads(existing.metadata_json or "{}")
            if not isinstance(metadata, dict):
                metadata = {}
        except (TypeError, ValueError):
            metadata = {}
    if "details" in payload:
        if not isinstance(payload["details"], dict):
            return None, "Product details must be an object"
        details = payload["details"]
        allowed_details = {"manufacturer_part_number", "model_number", "datasheet_url", "warranty_information", "country_of_origin", "track_inventory", "shipping_required", "weight_unit"}
        if set(details) - allowed_details:
            return None, "Product details include an unsupported field"
        clean_details = {}
        for key, raw in details.items():
            if key == "datasheet_url":
                url = str(raw or "").strip()
                if url and not safe_external_url(url, "image"):
                    return None, "Datasheet URL must be a safe HTTPS URL"
                clean_details[key] = url
            elif key in {"track_inventory", "shipping_required"}:
                if isinstance(raw, bool):
                    clean_details[key] = raw
                elif str(raw).strip().lower() in {"true", "yes", "1"}:
                    clean_details[key] = True
                elif str(raw).strip().lower() in {"false", "no", "0"}:
                    clean_details[key] = False
                else:
                    return None, f"{key.replace('_', ' ').capitalize()} must be true or false"
            else:
                clean_details[key] = str(raw or "").strip()[:1000]
        metadata.update(clean_details)
    if "tags" in payload:
        tags = payload["tags"]
        if not isinstance(tags, list) or len(tags) > 50:
            return None, "Tags must be a list with at most 50 entries"
        metadata["tags"] = [str(tag).strip()[:80] for tag in tags if str(tag).strip()]
    if "specifications" in payload:
        if not isinstance(payload["specifications"], dict) or len(payload["specifications"]) > 80:
            return None, "Specifications must be a key/value object with at most 80 entries"
        metadata["specifications"] = {str(key)[:100]: str(val)[:500] for key, val in payload["specifications"].items()}
    product = Product(
        name=name[:180], slug=slug, short_description=str(value("short_description", "") or "")[:500],
        description=str(value("description", "") or "")[:50000], price=price, compare_at_price=compare,
        image_url=image_url, cloudinary_public_id=value("cloudinary_public_id", None), sku=sku,
        brand=(str(value("brand", "") or "").strip()[:120] or None),
        subcategory=(str(value("subcategory", "") or "").strip()[:120] or None),
        stock=stock, weight_kg=weight, category_id=category.id, status=status, is_active=active,
        is_featured=featured, metadata_json=json.dumps(metadata, ensure_ascii=False),
    )
    return product, None


def replace_product_media(product, media_items):
    if not isinstance(media_items, list) or len(media_items) > 60:
        return "Product media must be a list of at most 60 items"
    normalized = []
    image_count = video_count = 0
    for index, item in enumerate(media_items):
        if not isinstance(item, dict):
            return "Each media item must be an object"
        kind = str(item.get("type", "image")).strip().lower()
        if kind not in {"image", "video"}:
            return "Media type must be Image or Video"
        image_count += kind == "image"
        video_count += kind == "video"
        url = safe_external_url(item.get("url"), kind)
        if not url:
            return "Media URL must use HTTPS; video URLs must be from YouTube or Vimeo"
        primary_raw = item.get("is_primary", item.get("primary", False))
        if isinstance(primary_raw, bool):
            primary = primary_raw
        elif str(primary_raw).strip().lower() in {"true", "yes", "1"}:
            primary = True
        elif str(primary_raw).strip().lower() in {"false", "no", "0", ""}:
            primary = False
        else:
            return "Primary image must be true or false"
        try:
            order = int(item.get("display_order", item.get("order", index)) or 0)
            if order < 0 or order > 10000:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            return "Media display order must be a non-negative whole number"
        normalized.append({"type": kind, "url": url, "title": str(item.get("title") or "")[:240],
                           "alt_text": str(item.get("alt_text") or item.get("title") or product.name)[:300],
                           "primary": primary and kind == "image", "order": order})
    if image_count > 50 or video_count > 10:
        return "A product can have up to 50 images and 10 videos"
    primary_images = [item for item in normalized if item["primary"]]
    for item in primary_images[1:]:
        item["primary"] = False
    if not primary_images:
        first = next((item for item in normalized if item["type"] == "image"), None)
        if first:
            first["primary"] = True
    for old in list(product.media):
        db.session.delete(old)
    product.media.clear()
    for item in normalized:
        product.media.append(ProductMedia(product=product, media_type=item["type"], url=item["url"], title=item["title"] or None,
                                          alt_text=item["alt_text"], is_primary=item["primary"], display_order=item["order"]))
    primary = next((item for item in normalized if item["type"] == "image" and item["primary"]), None)
    if primary:
        product.image_url = primary["url"]
    elif not any(item["type"] == "image" for item in normalized):
        product.image_url = None
    return None


def database_uri():
    uri = os.getenv("DATABASE_URL", "sqlite:///electrohub.db")
    if uri.startswith("postgres://"):
        return "postgresql+psycopg://" + uri[len("postgres://") :]
    if uri.startswith("postgresql://"):
        return "postgresql+psycopg://" + uri[len("postgresql://") :]
    return uri


def database_engine_options(uri):
    if uri.startswith(("postgres://", "postgresql://", "postgresql+psycopg://")):
        # Bound connection attempts so an unavailable database cannot hold
        # Gunicorn startup until Render's port scan times out. Neon poolers
        # reject lock_timeout as a startup option, so do not set it here.
        return {
            "connect_args": {
                "connect_timeout": 10,
            }
        }
    return {}


def create_app(test_config=None):
    app = Flask(__name__)
    db_uri = database_uri()
    app.config.update(
        SECRET_KEY=os.getenv("SECRET_KEY", "local-development-secret-change-me"),
        SQLALCHEMY_DATABASE_URI=db_uri,
        SQLALCHEMY_ENGINE_OPTIONS=database_engine_options(db_uri),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        # Product media may contain up to twelve 5 MB images in one request.
        MAX_CONTENT_LENGTH=int(os.getenv("MAX_CONTENT_LENGTH", 65 * 1024 * 1024)),
        ADMIN_TOKEN_TTL_SECONDS=8 * 60 * 60,
    )
    if test_config:
        app.config.update(test_config)

    db.init_app(app)
    configured_origins = [origin.strip() for origin in os.getenv("FRONTEND_ORIGINS", "*").split(",") if origin.strip()]
    origins = list(dict.fromkeys([*configured_origins, "https://buxinstore.github.io"]))
    CORS(
        app,
        resources={r"/api/*": {"origins": origins}},
        allow_headers=["Content-Type", "Authorization"],
        methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    )

    if os.getenv("CLOUDINARY_URL"):
        cloudinary.config(secure=True)

    app.logger.info("Initializing database schema and migrations")
    with app.app_context():
        db.create_all()
        # Maintain a database row for every supported country/provider pair;
        # unresearched rows stay explicitly in manual review with no price.
        from pycountry import countries
        present_rates = set(db.session.query(ShippingRate.country_code, ShippingRate.provider).all())
        country_names = {destination.alpha_2: destination.name for destination in countries}
        missing_pairs = missing_provider_rate_pairs(country_names, present_rates)
        for country_code, provider_id in missing_pairs:
            db.session.add(ShippingRate(country_code=country_code, country_name=country_names[country_code],
                provider=provider_id, status="manual_review"))
        db.session.commit()
        if not db.session.get(StoreSetting, "shipping_management_mode"):
            db.session.add(StoreSetting(key="shipping_management_mode", value="ai_research"))
            db.session.commit()
        # AI credentials now come only from the server environment.
        obsolete_groq_settings = [db.session.get(StoreSetting, key)
                                  for key in ("groq_api_key_encrypted", "groq_model")]
        obsolete_groq_settings = [setting for setting in obsolete_groq_settings if setting]
        if obsolete_groq_settings:
            for setting in obsolete_groq_settings:
                db.session.delete(setting)
            db.session.commit()
        else:
            # Release the read transaction before schema migrations. Neon pooler
            # sessions can terminate connections left idle inside a transaction.
            db.session.rollback()
        # Additive, safe upgrades for databases created by earlier storefront versions.
        columns = {column["name"] for column in inspect(db.engine).get_columns("orders")}
        if "shipping_method" not in columns:
            with db.engine.begin() as connection:
                connection.execute(text("ALTER TABLE orders ADD COLUMN shipping_method VARCHAR(120) NOT NULL DEFAULT 'standard'"))
        product_columns = {column["name"] for column in inspect(db.engine).get_columns("products")}
        if "weight_kg" not in product_columns:
            with db.engine.begin() as connection:
                connection.execute(text("ALTER TABLE products ADD COLUMN weight_kg NUMERIC(8, 3)"))
        product_migrations = {
            "short_description": "VARCHAR(500) NOT NULL DEFAULT ''",
            "brand": "VARCHAR(120)",
            "subcategory": "VARCHAR(120)",
            "status": "VARCHAR(16) NOT NULL DEFAULT 'active'",
            "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        for column, sql_type in product_migrations.items():
            if column not in product_columns:
                with db.engine.begin() as connection:
                    connection.execute(text(f"ALTER TABLE products ADD COLUMN {column} {sql_type}"))
        if "status" not in product_columns:
            with db.engine.begin() as connection:
                connection.execute(text("UPDATE products SET status = CASE WHEN is_active THEN 'active' ELSE 'inactive' END"))
        item_columns = {column["name"] for column in inspect(db.engine).get_columns("order_items")}
        if "inventory_tracked" not in item_columns:
            with db.engine.begin() as connection:
                connection.execute(text("ALTER TABLE order_items ADD COLUMN inventory_tracked BOOLEAN NOT NULL DEFAULT TRUE"))
        estimate_columns = {column["name"] for column in inspect(db.engine).get_columns("shipping_estimates")}
        if "fx_snapshot" not in estimate_columns:
            with db.engine.begin() as connection:
                connection.execute(text("ALTER TABLE shipping_estimates ADD COLUMN fx_snapshot TEXT"))
        order_columns = {column["name"] for column in inspect(db.engine).get_columns("orders")}
        if "shipping_snapshot" not in order_columns:
            with db.engine.begin() as connection:
                connection.execute(text("ALTER TABLE orders ADD COLUMN shipping_snapshot TEXT"))
        if "shipping_acknowledged_at" not in order_columns:
            with db.engine.begin() as connection:
                connection.execute(text("ALTER TABLE orders ADD COLUMN shipping_acknowledged_at TIMESTAMP WITH TIME ZONE"))
        rate_columns = {column["name"] for column in inspect(db.engine).get_columns("shipping_rates")}
        if "evidence" not in rate_columns:
            with db.engine.begin() as connection:
                connection.execute(text("ALTER TABLE shipping_rates ADD COLUMN evidence TEXT"))
        if "source_type" not in rate_columns:
            with db.engine.begin() as connection:
                connection.execute(text("ALTER TABLE shipping_rates ADD COLUMN source_type VARCHAR(24) NOT NULL DEFAULT 'ai_research'"))
        if "approved" not in rate_columns:
            with db.engine.begin() as connection:
                connection.execute(text("ALTER TABLE shipping_rates ADD COLUMN approved BOOLEAN NOT NULL DEFAULT FALSE"))
        with db.engine.begin() as connection:
            connection.execute(text("UPDATE shipping_rates SET approved = TRUE WHERE status = 'available' AND approved = FALSE"))
            connection.execute(text("UPDATE shipping_rates SET source_type = 'manual_admin' WHERE manual_override = TRUE AND source_type = 'ai_research'"))
        job_columns = {column["name"] for column in inspect(db.engine).get_columns("shipping_refresh_jobs")}
        job_migrations = {
            "tokens_used": "INTEGER NOT NULL DEFAULT 0",
            "rate_limit_remaining": "VARCHAR(40)",
            "current_destination": "VARCHAR(2)",
            "pending": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, sql_type in job_migrations.items():
            if column not in job_columns:
                with db.engine.begin() as connection:
                    connection.execute(text(f"ALTER TABLE shipping_refresh_jobs ADD COLUMN {column} {sql_type}"))
        if db.engine.dialect.name == "postgresql":
            with db.engine.begin() as connection:
                connection.execute(text("ALTER TABLE orders ALTER COLUMN address TYPE TEXT"))
                connection.execute(text("ALTER TABLE store_settings ALTER COLUMN value TYPE TEXT"))
        attempt_columns = {column["name"] for column in inspect(db.engine).get_columns("payment_attempts")}
        attempt_migrations = {
            "paypal_currency": "VARCHAR(3)",
            "paypal_amount": "NUMERIC(12, 2)",
            "paypal_exchange_rate": "NUMERIC(18, 8)",
            "paypal_rate_date": "VARCHAR(40)",
            "modem_currency": "VARCHAR(3)",
            "modem_amount": "NUMERIC(12, 2)",
            "modem_exchange_rate": "NUMERIC(18, 8)",
            "modem_rate_date": "VARCHAR(40)",
        }
        for column, sql_type in attempt_migrations.items():
            if column not in attempt_columns:
                with db.engine.begin() as connection:
                    connection.execute(text(f"ALTER TABLE payment_attempts ADD COLUMN {column} {sql_type}"))
        if Category.query.count() == 0 and Product.query.count() == 0:
            seed_starter_catalog(update_existing=False)
    app.logger.info("Database schema and migrations are ready")

    def admin_required(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            secret = app.config.get("SECRET_KEY")
            token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if not token:
                return jsonify(error="Admin login required"), 401
            try:
                URLSafeTimedSerializer(secret, salt="electrohub-admin").loads(
                    token, max_age=app.config["ADMIN_TOKEN_TTL_SECONDS"]
                )
            except SignatureExpired:
                return jsonify(error="Admin session expired"), 401
            except BadSignature:
                return jsonify(error="Invalid admin session"), 401
            return fn(*args, **kwargs)

        return wrapped

    def error(message, status):
        return jsonify(error=message), status

    def active_payment_mode():
        setting = db.session.get(StoreSetting, "payment_mode")
        return setting.value if setting and setting.value in PAYMENT_MODES else "test"

    def get_groq_key():
        return os.getenv("GROQ_API_KEY", "").strip(), "environment"

    def shipping_methods():
        setting = db.session.get(StoreSetting, "shipping_methods")
        if not setting:
            return DEFAULT_SHIPPING_METHODS
        try:
            methods = json.loads(setting.value)
        except (TypeError, ValueError):
            return DEFAULT_SHIPPING_METHODS
        return methods if isinstance(methods, list) else DEFAULT_SHIPPING_METHODS

    def usd_gmd_conversion():
        setting = db.session.get(StoreSetting, "usd_gmd_exchange_rate")
        cached = None
        if setting:
            try:
                cached = json.loads(setting.value)
                updated = datetime.fromisoformat(cached["updated_at"])
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - updated).total_seconds() < 86400:
                    return cached
            except (KeyError, TypeError, ValueError):
                cached = None

        req = Request("https://open.er-api.com/v6/latest/GMD", headers={"User-Agent": "ElectroHubStore/1.0"})
        try:
            with urlopen(req, timeout=8) as response:
                data = json.loads(response.read().decode())
            if not isinstance(data, dict):
                raise ValueError("Exchange rate response was invalid")
            rate = Decimal(str(data.get("rates", {}).get("USD", "0")))
            if data.get("result") != "success" or data.get("base_code") != "GMD" or rate <= 0:
                raise ValueError("Exchange rate response was invalid")
            rate_date = str(data.get("time_last_update_utc", ""))
            updated_at = datetime.now(timezone.utc).isoformat()
            converted = {"currency": "GMD", "usd_to_gmd": str(Decimal(1) / rate), "rate_date": rate_date, "updated_at": updated_at}
            setting = db.session.get(StoreSetting, "usd_gmd_exchange_rate")
            if setting:
                setting.value = json.dumps(converted)
            else:
                db.session.add(StoreSetting(key="usd_gmd_exchange_rate", value=json.dumps(converted)))
            db.session.commit()
            return converted
        except (HTTPError, URLError, TimeoutError, ValueError, InvalidOperation, json.JSONDecodeError):
            db.session.rollback()
            if cached:
                try:
                    updated = datetime.fromisoformat(cached["updated_at"])
                    if updated.tzinfo is None:
                        updated = updated.replace(tzinfo=timezone.utc)
                    if (datetime.now(timezone.utc) - updated).total_seconds() < 172800:
                        return cached
                except (KeyError, TypeError, ValueError):
                    pass
            app.logger.exception("Could not retrieve USD to GMD exchange rate")
            raise RuntimeError("Modern Pay currency conversion is temporarily unavailable")

    def payment_secret(provider, mode):
        if provider == "modem":
            return os.getenv("MODEM_LIVE_SECRET" if mode == "live" else "MODEM_TEST_SECRET", "").strip()
        if provider == "paypal":
            return os.getenv("PAYPAL_LIVE_SECRET" if mode == "live" else "PAYPAL_SANDBOX_SECRET", "").strip()
        return ""

    def provider_status(mode):
        live_enabled = os.getenv("ENABLE_LIVE_PAYMENTS", "false").strip().lower() == "true"
        modem_secret = payment_secret("modem", mode)
        modem_webhook_secret = os.getenv(
            "MODEM_WEBHOOK_SECRET" if mode == "live" else "MODEM_TEST_WEBHOOK_SECRET", ""
        ).strip()
        status = {
            "modem": modem_secret.startswith("sk_live_" if mode == "live" else "sk_test_")
            and bool(modem_webhook_secret),
            "paypal": bool(
                payment_secret("paypal", mode)
                and os.getenv("PAYPAL_LIVE_CLIENT_ID" if mode == "live" else "PAYPAL_SANDBOX_CLIENT_ID", "").strip()
            ),
        }
        if mode == "live" and not live_enabled:
            return {provider: False for provider in PAYMENT_PROVIDERS}
        return status

    def storefront_origin():
        origin = request.headers.get("Origin", "").rstrip("/")
        allowed_origins = [item.rstrip("/") for item in origins if item != "*"]
        if origin and origin in allowed_origins:
            return origin
        if not origin and allowed_origins:
            return allowed_origins[0]
        return ""

    def storefront_base_url(candidate_url=""):
        origin = storefront_origin()
        if not origin:
            return ""
        candidate_path = ""
        if candidate_url:
            candidate = urlsplit(str(candidate_url).strip())
            candidate_origin = f"{candidate.scheme}://{candidate.netloc}" if candidate.scheme and candidate.netloc else ""
            if (candidate_origin == origin and candidate.scheme in {"http", "https"}
                    and not candidate.query and not candidate.fragment and ".." not in candidate.path.split("/")):
                candidate_path = candidate.path.rstrip("/")
                if candidate_path.endswith("/index.html"):
                    candidate_path = candidate_path[:-len("/index.html")]
        configured = os.getenv("FRONTEND_URL", "").strip().rstrip("/")
        if configured:
            parsed = urlsplit(configured)
            configured_origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
            if configured_origin == origin and parsed.scheme in {"http", "https"}:
                configured_path = parsed.path.rstrip("/")
                if not configured_path and candidate_path:
                    return origin + candidate_path
                if not configured_path and origin == "https://buxinstore.github.io":
                    return origin + "/store"
                return configured
        if candidate_path:
            return origin + candidate_path
        return origin + "/store" if origin == "https://buxinstore.github.io" else origin

    def public_api_base_url():
        host = request.host
        local_hosts = {"localhost", "127.0.0.1", "[::1]"}
        hostname = urlsplit("//" + host).hostname
        scheme = "http" if hostname in local_hosts else "https"
        return f"{scheme}://{host}"

    def provider_request(url, data, headers, method="POST"):
        body = json.dumps(data).encode() if isinstance(data, dict) else data
        req = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(req, timeout=20) as response:
                return json.loads(response.read().decode())
        except (HTTPError, URLError, TimeoutError, ValueError):
            app.logger.exception("Payment provider request failed")
            raise RuntimeError("Payment provider could not start checkout")

    def paypal_access_token(mode):
        client_id = os.getenv("PAYPAL_LIVE_CLIENT_ID" if mode == "live" else "PAYPAL_SANDBOX_CLIENT_ID", "").strip()
        secret = payment_secret("paypal", mode)
        base = "https://api-m.paypal.com" if mode == "live" else "https://api-m.sandbox.paypal.com"
        auth = (client_id + ":" + secret).encode()
        req = Request(base + "/v1/oauth2/token", data=b"grant_type=client_credentials", headers={
            "Authorization": "Basic " + base64.b64encode(auth).decode(),
            "Content-Type": "application/x-www-form-urlencoded",
        })
        try:
            with urlopen(req, timeout=20) as response:
                token = json.loads(response.read().decode()).get("access_token")
        except (HTTPError, URLError, TimeoutError, ValueError):
            app.logger.exception("PayPal authentication failed")
            raise RuntimeError("PayPal could not authenticate")
        if not token:
            raise RuntimeError("PayPal did not return an access token")
        return token, base

    def build_order(payload, payment_method):
        address = payload.get("shipping_address")
        if not isinstance(address, dict):
            address = {"address": payload.get("address"), "city": payload.get("city"), "postal_code": payload.get("postal_code")}
        required = ("customer_name", "email")
        if any(not str(payload.get(field, "")).strip() for field in required) or any(
            not str(address.get(field, "")).strip() for field in ("address", "city", "region", "postal_code", "country", "phone")
        ):
            raise ValueError("Enter your name, email, phone, and complete delivery address")
        lines = payload.get("items")
        if not isinstance(lines, list) or not lines or len(lines) > 100:
            raise ValueError("Order must include between 1 and 100 items")
        requested = {}
        try:
            for line in lines:
                product_id = int(line.get("product_id", 0))
                quantity = int(line.get("quantity", 0))
                if quantity < 1 or quantity > 99:
                    raise ValueError("Each quantity must be between 1 and 99")
                requested[product_id] = requested.get(product_id, 0) + quantity
                if requested[product_id] > 99:
                    raise ValueError("Combined product quantity cannot exceed 99")
        except (AttributeError, TypeError, ValueError) as exc:
            if isinstance(exc, ValueError) and str(exc).startswith(("Each quantity", "Combined product")):
                raise
            raise ValueError("Each item must include a valid product_id and quantity") from exc

        order_lines = []
        subtotal = Decimal("0.00")
        for product_id, quantity in requested.items():
            product = Product.query.filter_by(id=product_id, is_active=True, status="active").with_for_update().first()
            if not product:
                raise LookupError(f"Product {product_id} was not found")
            if product.metadata_value("track_inventory", True) and product.stock < quantity:
                raise ArithmeticError(f"Insufficient stock for {product.name}")
            subtotal += Decimal(product.price) * quantity
            order_lines.append((product, quantity))
        country_code = str(address.get("country_code", address.get("country", ""))).strip().upper()
        if len(country_code) != 2 or payload.get("shipping_acknowledged") is not True:
            raise ValueError("Select your destination country and acknowledge the shipping estimate")
        try:
            actual_weight = sum((Decimal(product.weight_kg) * quantity for product, quantity in order_lines), Decimal("0"))
        except (TypeError, InvalidOperation):
            raise ValueError("A product is missing a valid shipping weight; contact support for a manual quote")
        provider = str(payload.get("shipping_provider", "")).strip().lower()
        if provider not in {"dhl", "fedex", "community"}:
            raise ValueError("Choose DHL, FedEx, or Community Courier")
        rate = ShippingRate.query.filter_by(country_code=country_code, provider=provider).with_for_update().first()
        if not rate or not shipping_rate_is_current(rate):
            raise ValueError("Shipping requires manual confirmation for this destination and provider; choose another provider or contact support")
        try:
            band, units, shipping = shipping_charge(actual_weight, rate.base_price_usd)
        except ValueError as exc:
            raise ValueError("Shipping requires manual confirmation for this destination and provider") from exc
        try:
            submitted_rate_id = int(payload.get("shipping_rate_id", 0))
        except (TypeError, ValueError):
            submitted_rate_id = 0
        if submitted_rate_id != rate.id:
            raise ValueError("Shipping rate changed; refresh the checkout quote and approve it again")
        snapshot = {"rate_id": rate.id, "rate_version": rate.rate_version, "country_code": country_code,
                    "country_name": rate.country_name, "origin": rate.origin, "provider": provider,
                    "product_weight_kg": str(actual_weight), "billable_weight_kg": str(band),
                    "base_price_per_0_5kg_usd": str(rate.base_price_usd), "billable_units": units,
                    "shipping_charge_usd": str(shipping), "currency": "USD", "rate_status": rate.status,
                    "manual_override": rate.manual_override, "sources": safe_sources(rate.sources),
                    "researched_at": rate.researched_at.isoformat() if rate.researched_at else None,
                    "calculated_at": datetime.now(timezone.utc).isoformat(),
                    "disclaimer": "Shipping amount calculated from the saved provider rate; any extra charge requires your approval."}
        order = Order(
            id=str(uuid4()), customer_name=str(payload["customer_name"]).strip()[:180],
            email=str(payload["email"]).strip()[:254], address=json.dumps({key: str(value).strip() for key, value in address.items()}, ensure_ascii=False),
            city=str(address["city"]).strip()[:120], postal_code=str(address["postal_code"]).strip()[:32], shipping_method=f"{provider.upper()} · {band} kg",
            shipping_snapshot=json.dumps(snapshot), shipping_acknowledged_at=datetime.now(timezone.utc),
            payment_method=payment_method, payment_status="unpaid", status="pending",
            subtotal=subtotal, shipping=shipping, total=subtotal + shipping,
        )
        for product, quantity in order_lines:
            order.items.append(OrderItem(
                product_id=product.id, product_name=product.name, image_url=product.image_url,
                unit_price=product.price, quantity=quantity,
                inventory_tracked=product.metadata_value("track_inventory", True),
            ))
            if product.metadata_value("track_inventory", True):
                product.stock -= quantity
        db.session.add(order)
        order.shipping_reconciliation = ShippingReconciliation(order_id=order.id, quoted_usd=shipping, status="awaiting_final_cost")
        return order

    def release_order_stock(order):
        for item in order.items:
            if item.product_id:
                product = db.session.get(Product, item.product_id)
                if product and getattr(item, "inventory_tracked", True):
                    product.stock += item.quantity
        order.status = "cancelled"
        order.payment_status = "failed"

    @app.get("/api/health")
    def health():
        try:
            db.session.execute(text("SELECT 1"))
            return jsonify(status="ok", database="connected", time=datetime.now(timezone.utc).isoformat())
        except SQLAlchemyError:
            db.session.rollback()
            app.logger.exception("Database health check failed")
            return jsonify(status="degraded", database="unavailable"), 503

    @app.get("/api/categories")
    def categories():
        rows = Category.query.filter_by(is_active=True).order_by(Category.name).all()
        return jsonify(items=[row.to_dict(include_count=True) for row in rows])

    @app.get("/api/products")
    def products():
        query = Product.query.join(Category).filter(Product.is_active.is_(True), Product.status == "active", Category.is_active.is_(True))
        category = request.args.get("category", "").strip()
        search = request.args.get("q", "").strip()
        if category:
            query = query.filter(Category.slug == category)
        if search:
            like = f"%{search[:100]}%"
            query = query.filter((Product.name.ilike(like)) | (Product.description.ilike(like)) | (Product.short_description.ilike(like)) | (Product.sku.ilike(like)) | (Product.brand.ilike(like)) | (Product.metadata_json.ilike(like)))
        featured = request.args.get("featured") == "true"
        if featured:
            query = query.filter(Product.is_featured.is_(True))
        try:
            limit = min(max(int(request.args.get("limit", 100)), 1), 200)
            page_no = max(1, int(request.args.get("page", 1)))
        except ValueError:
            limit, page_no = 100, 1
        count = query.count()
        rows = query.order_by(Product.is_featured.desc(), Product.name).offset((page_no - 1) * limit).limit(limit).all()
        return jsonify(items=[row.to_dict() for row in rows], count=count, page=page_no,
                       per_page=limit, pages=(count + limit - 1) // limit)

    @app.get("/api/products/<int:product_id>")
    def product_detail(product_id):
        row = Product.query.join(Category).filter(Product.id == product_id, Product.is_active.is_(True), Product.status == "active", Category.is_active.is_(True)).first()
        if not row:
            return error("Product not found", 404)
        return jsonify(item=row.to_dict())

    @app.get("/api/products/slug/<path:slug>")
    def product_detail_by_slug(slug):
        row = Product.query.join(Category).filter(Product.slug == slug, Product.is_active.is_(True),
                                                  Product.status == "active", Category.is_active.is_(True)).first()
        if not row:
            return error("Product not found", 404)
        return jsonify(item=row.to_dict())

    @app.get("/api/shipping/destinations")
    def shipping_destinations():
        from pycountry import countries
        return jsonify(items=[{"code": c.alpha_2, "name": c.name} for c in countries])

    @app.post("/api/shipping/estimate")
    def shipping_estimate():
        payload = request.get_json(silent=True) or {}
        country_code = str(payload.get("country_code", "")).strip().upper()
        provider = str(payload.get("provider", "")).strip().lower()
        from pycountry import countries
        country = countries.get(alpha_2=country_code)
        if not country:
            return error("Choose a supported destination country", 400)
        if provider not in {"dhl", "fedex", "community"}:
            return error("Choose DHL, FedEx, or Community Courier", 400)
        lines = payload.get("items")
        if not isinstance(lines, list) or not lines or len(lines) > 100:
            return error("Cart items are required to estimate shipping", 400)
        try:
            quantities = {}
            for item in lines:
                product_id, quantity = int(item["product_id"]), int(item["quantity"])
                if not 1 <= quantity <= 99:
                    raise ValueError
                quantities[product_id] = quantities.get(product_id, 0) + quantity
            products = {p.id: p for p in Product.query.filter(Product.id.in_(quantities), Product.is_active.is_(True), Product.status == "active").all()}
            if set(products) != set(quantities):
                return error("A cart item is no longer available", 409)
            actual = Decimal("0")
            for product_id, quantity in quantities.items():
                if products[product_id].weight_kg is None or Decimal(products[product_id].weight_kg) <= 0:
                    return jsonify(status="manual_review", reason=f"Shipping weight is missing for {products[product_id].name}; contact support for a quote"), 200
                actual += Decimal(products[product_id].weight_kg) * quantity
            band = billable_weight(actual)
        except (KeyError, TypeError, ValueError, InvalidOperation):
            return error("Invalid cart quantities or product weights", 400)
        row = ShippingRate.query.filter_by(country_code=country_code, provider=provider).first()
        if not row or not shipping_rate_is_current(row):
            return jsonify(status="manual_review", country_code=country_code, country_name=country.name,
                           provider=provider, actual_weight_kg=str(actual), billable_weight_kg=str(band),
                           reason="Shipping requires confirmation. No current approved rate is available; choose another provider or contact support."), 200
        billable, units, charge = shipping_charge(actual, row.base_price_usd)
        return jsonify(status="available", shipping_rate_id=row.id, rate_version=row.rate_version,
                       provider=provider, country_code=country_code, country_name=country.name,
                       actual_weight_kg=str(actual), billable_weight_kg=str(billable), billable_units=units,
                       base_price_per_0_5kg_usd=str(row.base_price_usd), price_usd=str(charge), currency="USD",
                       confidence=row.confidence, manual_override=row.manual_override, sources=safe_sources(row.sources), researched_at=row.researched_at.isoformat() if row.researched_at else None,
                       expires_at=row.expires_at.isoformat() if row.expires_at else None,
                       disclaimer="Calculated from the saved provider price per 0.5 kg. Additional charges require your approval.")

    @app.get("/api/admin/shipping")
    @admin_required
    def admin_shipping():
        from pycountry import countries
        rows = ShippingRate.query.order_by(ShippingRate.country_name, ShippingRate.provider).all()
        jobs = ShippingRefreshJob.query.order_by(ShippingRefreshJob.started_at.desc()).limit(20).all()
        rate_lookup = {(r.country_code, r.provider): r for r in rows}
        freshness_days = max(1, int(os.getenv("SHIPPING_ESTIMATE_MAX_AGE_DAYS", "30")))
        cutoff = datetime.now(timezone.utc) - timedelta(days=freshness_days)
        def reviewed(code, provider):
            rate = rate_lookup.get((code, provider))
            if rate and rate.status == "stale":
                return False
            stamp = rate.researched_at if rate else None
            if stamp and stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return bool(rate and ((rate.manual_override and rate.approved) or (stamp and stamp >= cutoff)))
        total_destinations = len(list(countries))
        reviewed_destinations = sum(1 for country in countries if all(reviewed(country.alpha_2, p) for p in ("dhl", "fedex")))
        retry_setting = db.session.get(StoreSetting, "shipping_retry_codes")
        try:
            failed_destinations = json.loads(retry_setting.value) if retry_setting else []
        except (TypeError, ValueError):
            failed_destinations = []
        latest_job = jobs[0] if jobs else None
        progress = {"total_destinations": total_destinations, "reviewed_destinations": reviewed_destinations,
                    "pending_destinations": max(0, total_destinations - reviewed_destinations),
                    "failed_destinations": failed_destinations,
                    "current_destination": latest_job.current_destination if latest_job and latest_job.status == "running" else None,
                    "last_success_at": latest_job.finished_at.isoformat() if latest_job and latest_job.succeeded and latest_job.finished_at else None}
        mode_setting = db.session.get(StoreSetting, "shipping_management_mode")
        shipping_mode = mode_setting.value if mode_setting and mode_setting.value in SHIPPING_MODES else "ai_research"
        return jsonify(mode=shipping_mode, progress=progress, community_provider_label=os.getenv("COMMUNITY_COURIER_LABEL", "Community Courier"), rates=[{"id": r.id, "country_code": r.country_code, "country_name": r.country_name,
                                   "origin": r.origin, "provider": r.provider,
                                   "base_price_usd": str(r.base_price_usd) if r.base_price_usd is not None else None,
                                   "rate_version": r.rate_version, "status": r.status, "confidence": r.confidence,
                                   "sources": safe_sources(r.sources), "evidence": r.evidence, "notes": r.notes,
                                   "researched_at": r.researched_at.isoformat() if r.researched_at else None,
                                   "expires_at": r.expires_at.isoformat() if r.expires_at else None,
                                   "manual_override": r.manual_override, "source_type": r.source_type,
                                   "approved": r.approved, "current": shipping_rate_is_current(r)} for r in rows],
                       jobs=[{"id": j.id, "status": j.status, "attempted": j.attempted, "succeeded": j.succeeded,
                              "failed": j.failed, "tokens_used": j.tokens_used,
                              "rate_limit_remaining": j.rate_limit_remaining, "current_destination": j.current_destination,
                              "pending": j.pending, "message": j.message, "started_at": j.started_at.isoformat(),
                              "finished_at": j.finished_at.isoformat() if j.finished_at else None} for j in jobs])

    @app.put("/api/admin/shipping/mode")
    @admin_required
    def admin_shipping_mode():
        payload = request.get_json(silent=True) or {}
        mode = str(payload.get("mode", "")).strip().lower()
        if mode not in SHIPPING_MODES:
            return error("Choose Manual Excel Mode or AI Research Mode", 400)
        setting = db.session.get(StoreSetting, "shipping_management_mode")
        if setting:
            setting.value = mode
        else:
            db.session.add(StoreSetting(key="shipping_management_mode", value=mode))
        if mode == "manual_excel":
            active_jobs = ShippingRefreshJob.query.filter_by(status="running").with_for_update().all()
            for job in active_jobs:
                job.status = "stopped_manual_mode"
                job.message = (job.message or "") + "Stopped because Manual Excel Mode was selected; any in-flight Groq result will be discarded.\n"
        db.session.commit()
        return jsonify(mode=mode, status="saved", message=(
            "AI research is disabled. Any in-flight Groq result will be discarded before saving." if mode == "manual_excel"
            else "AI research is enabled; the next scheduled run will resume eligible destinations."))

    @app.get("/api/admin/shipping/excel-template")
    @admin_required
    def admin_shipping_excel_template():
        from pycountry import countries
        workbook = build_template(list(countries))
        return send_file(workbook, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         as_attachment=True, download_name="buxin-shipping-rates-template.xlsx")

    @app.post("/api/admin/shipping/excel-preview")
    @admin_required
    def admin_shipping_excel_preview():
        mode_setting = db.session.get(StoreSetting, "shipping_management_mode")
        if not mode_setting or mode_setting.value != "manual_excel":
            return error("Switch to Manual Excel Mode before uploading shipping rates", 409)
        upload = request.files.get("file")
        if not upload or not upload.filename or not upload.filename.lower().endswith(".xlsx"):
            return error("Choose an .xlsx Excel file", 400)
        data = upload.stream.read(MAX_XLSX_BYTES + 1)
        try:
            from pycountry import countries
            parsed = parse_workbook(data, list(countries))
        except ValueError as exc:
            return error(str(exc), 400)
        rate_lookup = {(rate.country_code, rate.provider): rate for rate in ShippingRate.query.all()}
        create_count = update_count = blank_count = price_count = 0
        for row in parsed["rows"]:
            for provider in ("dhl", "fedex", "community"):
                value = row["prices"].get(provider)
                if value is None:
                    blank_count += 1
                else:
                    price_count += 1
                    if (row["country_code"], provider) in rate_lookup:
                        update_count += 1
                    else:
                        create_count += 1
        zero_count = len(parsed["zeros"])
        preview = {"recognized_rows": parsed["recognized_rows"], "valid_prices": price_count,
                   "prices_to_create": create_count, "existing_prices_to_update": update_count,
                   "blank_prices_unchanged": blank_count, "invalid_rows": parsed["invalid_rows"],
                   "errors": parsed["errors"], "zero_prices": zero_count,
                   "rates_requiring_review": price_count, "approval_required": True}
        import_id = None
        if not preview["invalid_rows"]:
            staged = ShippingExcelImport(id=str(uuid4()), payload=json.dumps(parsed["rows"]),
                                         preview=json.dumps(preview), created_at=datetime.now(timezone.utc))
            db.session.add(staged)
            db.session.commit()
            import_id = staged.id
        return jsonify(import_id=import_id, preview=preview), 201

    @app.post("/api/admin/shipping/excel-confirm")
    @admin_required
    def admin_shipping_excel_confirm():
        payload = request.get_json(silent=True) or {}
        if payload.get("confirm_import") is not True:
            return error("Explicitly confirm the reviewed Excel import", 400)
        mode_setting = db.session.get(StoreSetting, "shipping_management_mode")
        if not mode_setting or mode_setting.value != "manual_excel":
            return error("Switch to Manual Excel Mode before importing shipping rates", 409)
        staged = ShippingExcelImport.query.filter_by(id=str(payload.get("import_id", ""))).with_for_update().first()
        staged_created = staged.created_at if staged else None
        if staged_created and staged_created.tzinfo is None:
            staged_created = staged_created.replace(tzinfo=timezone.utc)
        if not staged or staged.consumed_at or staged_created < datetime.now(timezone.utc) - timedelta(minutes=30):
            return error("This preview expired or has already been imported. Upload the workbook again.", 409)
        preview, rows = json.loads(staged.preview), json.loads(staged.payload)
        if preview.get("invalid_rows"):
            return error("Fix every invalid or duplicate row and upload the workbook again.", 400)
        mode_setting = StoreSetting.query.filter_by(key="shipping_management_mode").populate_existing().with_for_update().first()
        if not mode_setting or mode_setting.value != "manual_excel":
            return error("Manual Excel Mode changed; upload the workbook again after selecting it.", 409)
        approve = payload.get("approve_prices") is True
        if preview.get("zero_prices") and payload.get("confirm_zero_prices") is not True:
            return error("Explicitly confirm the numeric zero prices, or correct them in the workbook.", 400)
        now = datetime.now(timezone.utc)
        try:
            changed = []
            for item in rows:
                for provider, raw_price in item["prices"].items():
                    price = Decimal(raw_price).quantize(Decimal("0.01"))
                    if not price.is_finite() or price < 0 or price > Decimal("100000"):
                        raise ValueError(f"Invalid price in workbook row {item['row']} for {provider}.")
                    rate = ShippingRate.query.filter_by(country_code=item["country_code"], provider=provider).with_for_update().first()
                    if rate is None:
                        rate = ShippingRate(country_code=item["country_code"], country_name=item["country_name"], provider=provider)
                        db.session.add(rate)
                        db.session.flush()
                    else:
                        db.session.add(ShippingRateHistory(rate_id=rate.id, snapshot=json.dumps({
                            "base_price_usd": str(rate.base_price_usd) if rate.base_price_usd is not None else None,
                            "status": rate.status, "sources": rate.sources, "evidence": rate.evidence,
                            "source_type": rate.source_type, "approved": rate.approved,
                            "researched_at": rate.researched_at.isoformat() if rate.researched_at else None,
                            "rate_version": rate.rate_version})))
                    rate.country_name, rate.origin, rate.base_price_usd = item["country_name"], item["origin"], price
                    rate.status = "available" if approve else "manual_review"
                    rate.source_type, rate.approved, rate.manual_override = "excel_import", approve, True
                    rate.confidence = "manual"
                    rate.sources, rate.evidence = None, None
                    rate.notes = ("Excel import " + now.isoformat() + ("; administrator approved for checkout. " if approve else "; awaiting administrator approval. ") + item["notes"])[:1000]
                    rate.researched_at = None
                    rate.expires_at = now + timedelta(days=365) if approve else None
                    rate.rate_version = (rate.rate_version or 0) + 1
                    changed.append({"country_code": item["country_code"], "provider": provider, "price_usd": str(price), "approved": approve})
            staged.consumed_at = now
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            if isinstance(exc, ValueError):
                return error(str(exc), 400)
            app.logger.exception("Shipping Excel import transaction failed")
            return error("Excel import was rolled back; no rates were changed", 500)
        return jsonify(imported_prices=len(changed), approved=approve, changes=changed,
                       message=f"Imported {len(changed)} rates; {'approved for checkout' if approve else 'left in manual review'}.")

    @app.get("/api/admin/shipping/<int:estimate_id>/history")
    @admin_required
    def admin_shipping_history(estimate_id):
        rate = db.session.get(ShippingRate, estimate_id)
        if not rate:
            return error("Shipping rate not found", 404)
        rows = ShippingRateHistory.query.filter_by(rate_id=estimate_id).order_by(ShippingRateHistory.created_at.desc()).limit(50).all()
        return jsonify(items=[{"snapshot": json.loads(row.snapshot), "created_at": row.created_at.isoformat()} for row in rows])

    @app.post("/api/admin/shipping/refresh")
    @admin_required
    def admin_shipping_refresh():
        return error("Shipping research runs automatically through the scheduled background job.", 409)

    @app.put("/api/admin/shipping/override")
    @admin_required
    def admin_shipping_override():
        payload = request.get_json(silent=True) or {}
        from pycountry import countries
        code = str(payload.get("country_code", "")).strip().upper()
        country = countries.get(alpha_2=code)
        try:
            price = Decimal(str(payload.get("price_usd"))).quantize(Decimal("0.01"))
            provider = str(payload.get("provider", "")).strip().lower()
            if not country or provider not in {"dhl", "fedex", "community"} or price <= 0 or price > Decimal("100000"):
                raise ValueError
        except (InvalidOperation, TypeError, ValueError):
            return error("Provide a valid country, provider, and positive USD base price per 0.5 kg", 400)
        source_url = str(payload.get("source_url", "")).strip()
        sources = safe_sources(json.dumps([{"url": source_url, "title": "Manually reviewed source"}])) if source_url else []
        if source_url and not sources:
            return error("Manual source must be an HTTPS URL", 400)
        row = ShippingRate.query.filter_by(country_code=code, provider=provider).first()
        if row:
            db.session.add(ShippingRateHistory(rate_id=row.id, snapshot=json.dumps({"base_price_usd": str(row.base_price_usd) if row.base_price_usd is not None else None, "status": row.status, "sources": row.sources, "evidence": row.evidence, "source_type": row.source_type, "approved": row.approved, "researched_at": row.researched_at.isoformat() if row.researched_at else None, "rate_version": row.rate_version})))
        else:
            row = ShippingRate(country_code=code, provider=provider)
            db.session.add(row)
        row.country_name, row.origin, row.base_price_usd, row.status = country.name, "Shenzhen, China", price, "available"
        row.confidence, row.sources, row.notes = "manual", json.dumps(sources), str(payload.get("notes", ""))[:1000]
        row.evidence = "Administrator-approved manual rate."
        row.source_type, row.approved = "manual_admin", True
        row.researched_at, row.expires_at, row.manual_override = datetime.now(timezone.utc), datetime.now(timezone.utc) + timedelta(days=365), True
        row.rate_version = (row.rate_version or 0) + 1
        db.session.commit()
        return jsonify(id=row.id, country_code=code, provider=provider, base_price_usd=str(price), rate_version=row.rate_version, status=row.status, manual_override=True)

    @app.post("/api/orders")
    def create_order():
        payload = request.get_json(silent=True) or {}
        method = str(payload.get("payment_method", "")).strip().lower()
        if method != "cash_on_delivery":
            return error(ONLINE_PAYMENT_MESSAGE, 503)
        try:
            order = build_order(payload, method)
            db.session.commit()
        except ValueError as exc:
            db.session.rollback()
            return error(str(exc), 400)
        except LookupError as exc:
            db.session.rollback()
            return error(str(exc), 404)
        except ArithmeticError as exc:
            db.session.rollback()
            return error(str(exc), 409)
        except SQLAlchemyError:
            db.session.rollback()
            app.logger.exception("Could not save order")
            return error("Could not save order", 500)
        return jsonify(order=order.to_dict()), 201

    @app.get("/api/payments/config")
    def public_payment_config():
        mode = active_payment_mode()
        methods = [item for item in shipping_methods() if item.get("enabled", True)]
        providers = provider_status(mode)
        conversion = None
        if providers.get("modem"):
            try:
                conversion = usd_gmd_conversion()
            except RuntimeError:
                providers["modem"] = False
        return jsonify(mode=mode, providers=providers, currency="USD", shipping_methods=methods,
                       community_provider_label=os.getenv("COMMUNITY_COURIER_LABEL", "Community Courier"), modem_conversion=conversion)

    @app.post("/api/payments/checkout")
    def create_payment_checkout():
        payload = request.get_json(silent=True) or {}
        provider = str(payload.get("provider", "")).strip().lower()
        if provider not in PAYMENT_PROVIDERS:
            return error("Choose Modem Pay or PayPal", 400)
        mode = active_payment_mode()
        if not provider_status(mode).get(provider):
            provider_name = "Modern Pay" if provider == "modem" else "PayPal"
            return error(f"{provider_name} needs valid {mode} credentials on the server", 503)
        frontend_url = storefront_base_url(payload.get("frontend_url", ""))
        if not frontend_url:
            return error("Storefront origin is not allowed. Configure FRONTEND_ORIGINS for the storefront URL.", 400)
        try:
            order = build_order(payload, provider)
            db.session.commit()
        except ValueError as exc:
            db.session.rollback()
            return error(str(exc), 400)
        except LookupError as exc:
            db.session.rollback()
            return error(str(exc), 404)
        except ArithmeticError as exc:
            db.session.rollback()
            return error(str(exc), 409)
        except SQLAlchemyError:
            db.session.rollback()
            app.logger.exception("Could not reserve inventory for payment")
            return error("Could not create order", 500)

        frontend_url = frontend_url.rstrip("/")
        success_url = frontend_url + "/?payment=success&order_id=" + order.id
        cancel_url = frontend_url + "/?payment=cancelled&order_id=" + order.id
        conversion = None
        modem_amount = None
        paypal_amount = None
        try:
            if provider == "modem":
                conversion = usd_gmd_conversion()
                modem_rate = Decimal(conversion["usd_to_gmd"])
                modem_amount = Decimal(order.total * modem_rate).quantize(Decimal("0.01"))
                if modem_amount < Decimal("10.00"):
                    raise RuntimeError("Modern Pay requires a converted payment of at least GMD 10.00")
                secret = payment_secret("modem", mode)
                callback_url = public_api_base_url() + "/api/payments/modem/webhook"
                result = provider_request("https://api.modempay.com/v1/payments", {
                    "data": {
                        "amount": float(modem_amount),
                        "currency": "GMD",
                        "title": "Store order " + order.id,
                        "description": "Store order payment",
                        "return_url": success_url,
                        "cancel_url": cancel_url,
                        "callback_url": callback_url,
                        "metadata": {"order_id": order.id},
                        "from_sdk": False,
                    }
                }, {"Authorization": "Bearer " + secret, "Content-Type": "application/json"})
                payment_data = result.get("data", {})
                provider_reference = payment_data.get("id") or payment_data.get("intent_secret")
                checkout_url = payment_data.get("payment_link")
            else:
                paypal_amount = Decimal(order.total).quantize(Decimal("0.01"))
                paypal_subtotal = Decimal(order.subtotal).quantize(Decimal("0.01"))
                paypal_shipping = paypal_amount - paypal_subtotal
                token, api_base = paypal_access_token(mode)
                purchase_unit = {
                    "reference_id": order.id,
                    "custom_id": order.id,
                    "amount": {
                        "currency_code": "USD",
                        "value": f"{paypal_amount:.2f}",
                        "breakdown": {
                            "item_total": {"currency_code": "USD", "value": f"{paypal_subtotal:.2f}"},
                            "shipping": {"currency_code": "USD", "value": f"{paypal_shipping:.2f}"},
                        },
                    },
                }
                result = provider_request(api_base + "/v2/checkout/orders", {
                    "intent": "CAPTURE", "purchase_units": [purchase_unit],
                    "application_context": {"return_url": f"{public_api_base_url()}/api/payments/paypal/return?order_id={order.id}",
                                            "cancel_url": f"{public_api_base_url()}/api/payments/paypal/cancel?order_id={order.id}"},
                }, {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
                provider_reference = result.get("id")
                checkout_url = next((link.get("href") for link in result.get("links", []) if link.get("rel") in {"approve", "payer-action"}), None)
            if not provider_reference or not checkout_url:
                raise RuntimeError("Payment provider returned an incomplete checkout response")
            shipping_snapshot = json.loads(order.shipping_snapshot or "{}")
            shipping_snapshot["payment_conversion"] = ({
                "currency": "GMD", "usd_to_gmd": str(conversion["usd_to_gmd"]),
                "rate_date": conversion["rate_date"], "order_total_usd": str(order.total),
                "provider_amount": str(modem_amount),
            } if provider == "modem" else {
                "currency": "USD", "usd_to_usd": "1", "order_total_usd": str(order.total),
                "provider_amount": str(paypal_amount),
            })
            order.shipping_snapshot = json.dumps(shipping_snapshot)
            db.session.add(PaymentAttempt(
                order_id=order.id, provider=provider, provider_reference=provider_reference,
                mode=mode, status="pending", frontend_url=frontend_url,
                paypal_currency="USD" if provider == "paypal" else None, paypal_amount=paypal_amount,
                modem_currency="GMD" if provider == "modem" else None, modem_amount=modem_amount,
                modem_exchange_rate=Decimal(conversion["usd_to_gmd"]) if provider == "modem" else None,
                modem_rate_date=conversion["rate_date"][:40] if provider == "modem" else None,
            ))
            db.session.commit()
            return jsonify(order_id=order.id, checkout_url=checkout_url, mode=mode), 201
        except Exception as exc:
            db.session.rollback()
            saved_order = db.session.get(Order, order.id)
            if saved_order and saved_order.payment_status != "paid":
                release_order_stock(saved_order)
                db.session.commit()
            if isinstance(exc, RuntimeError):
                return error(str(exc), 502)
            app.logger.exception("Could not start online payment")
            return error("Could not start online payment", 502)

    @app.get("/api/payments/orders/<order_id>")
    def payment_order_status(order_id):
        order = db.session.get(Order, order_id)
        if not order:
            return error("Order not found", 404)
        attempt = PaymentAttempt.query.filter_by(order_id=order.id, status="paid").first()
        return jsonify(order_id=order.id, payment_status=order.payment_status, status=order.status, total=float(order.total),
                       payment_currency=((attempt.paypal_currency or attempt.modem_currency) if attempt else "USD") or "USD",
                       payment_amount=float(attempt.paypal_amount if attempt.paypal_amount is not None else attempt.modem_amount) if attempt and (attempt.paypal_amount is not None or attempt.modem_amount is not None) else float(order.total))

    @app.post("/api/payments/modem/webhook")
    def modem_webhook():
        raw_body = request.get_data(cache=False)
        signature = request.headers.get("x-modem-signature", "")
        try:
            event = json.loads(raw_body)
            event_type = event["event"]
            payment = event["payload"]
            live_mode = not bool(payment.get("test_mode"))
        except (ValueError, KeyError, TypeError):
            return error("Invalid Modem Pay event", 400)
        signing_secret = os.getenv("MODEM_WEBHOOK_SECRET" if live_mode else "MODEM_TEST_WEBHOOK_SECRET", "").strip()
        canonical_body = json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        valid_signature = bool(signature and signing_secret) and any(
            hmac.compare_digest(
                hmac.new(signing_secret.encode(), body, hashlib.sha512).hexdigest(), signature
            )
            for body in {raw_body, canonical_body}
        )
        if not valid_signature:
            return error("Invalid Modem Pay signature", 400)
        reference = payment.get("payment_intent_id") or payment.get("id")
        metadata = payment.get("metadata") if isinstance(payment.get("metadata"), dict) else {}
        order_id = str(metadata.get("order_id", ""))
        attempt = PaymentAttempt.query.filter_by(provider="modem", order_id=order_id).first() if order_id else None
        if not attempt and reference:
            attempt = PaymentAttempt.query.filter_by(provider="modem", provider_reference=reference).first()
        if not attempt or (attempt.mode == "live") != live_mode:
            return error("Payment attempt not found", 404)
        order = attempt.order
        if event_type == "charge.succeeded":
            expected_amount = Decimal(attempt.modem_amount if attempt.modem_amount is not None else order.total)
            expected_currency = attempt.modem_currency or "GMD"
            if (payment.get("status") == "completed" and payment.get("currency") == expected_currency
                    and Decimal(str(payment.get("amount", 0))) == expected_amount):
                order.payment_status = "paid"
                order.status = "processing"
                attempt.status = "paid"
        elif event_type in {"charge.failed", "charge.expired", "charge.cancelled", "payment_intent.cancelled", "payment_intent.expired"} and attempt.status == "pending":
            release_order_stock(order)
            attempt.status = "failed"
        db.session.commit()
        return jsonify(received=True)

    @app.get("/api/payments/paypal/return")
    def paypal_return():
        provider_reference = request.args.get("token", "")
        order_id = request.args.get("order_id", "")
        attempt = PaymentAttempt.query.filter_by(provider="paypal", provider_reference=provider_reference, order_id=order_id).first()
        if not attempt:
            return error("PayPal order not found", 404)
        order = attempt.order
        if attempt.status == "paid":
            return redirect(attempt.frontend_url + "/?payment=success&order_id=" + order.id)
        try:
            token, api_base = paypal_access_token(attempt.mode)
            capture = provider_request(api_base + "/v2/checkout/orders/" + provider_reference + "/capture", {}, {
                "Authorization": "Bearer " + token, "Content-Type": "application/json",
            })
            captures = [c for unit in capture.get("purchase_units", []) for c in unit.get("payments", {}).get("captures", [])]
            paid_amount = sum((Decimal(c.get("amount", {}).get("value", "0")) for c in captures if c.get("status") == "COMPLETED"), Decimal("0.00"))
            expected_currency = attempt.paypal_currency or "USD"
            expected_amount = Decimal(attempt.paypal_amount if attempt.paypal_amount is not None else order.total)
            capture_currency = next((c.get("amount", {}).get("currency_code") for c in captures if c.get("status") == "COMPLETED"), None)
            if capture.get("status") != "COMPLETED" or paid_amount != expected_amount or capture_currency != expected_currency:
                raise RuntimeError("PayPal capture could not be verified")
            order.payment_status = "paid"
            order.status = "processing"
            attempt.status = "paid"
            db.session.commit()
            return redirect(attempt.frontend_url + "/?payment=success&order_id=" + order.id)
        except RuntimeError:
            app.logger.exception("PayPal payment capture failed")
            return redirect(attempt.frontend_url + "/?payment=processing&order_id=" + order.id)

    @app.get("/api/payments/paypal/cancel")
    def paypal_cancel():
        order_id = request.args.get("order_id", "")
        attempt = PaymentAttempt.query.filter_by(provider="paypal", order_id=order_id, status="pending").first()
        if attempt:
            release_order_stock(attempt.order)
            attempt.status = "cancelled"
            db.session.commit()
            return redirect(attempt.frontend_url + "/?payment=cancelled&order_id=" + order_id)
        return error("PayPal order not found", 404)

    @app.post("/api/admin/login")
    def admin_login():
        configured_user = os.getenv("ADMIN_USERNAME", "")
        configured_password = os.getenv("ADMIN_PASSWORD", "")
        payload = request.get_json(silent=True) or {}
        username = str(payload.get("username", ""))
        password = str(payload.get("password", ""))
        if not configured_user or not configured_password:
            return error("Admin credentials are not configured", 503)
        if not hmac.compare_digest(username, configured_user) or not hmac.compare_digest(password, configured_password):
            return error("Incorrect username or password", 401)
        token = URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="electrohub-admin").dumps(
            {"sub": configured_user}
        )
        return jsonify(token=token, expires_in=app.config["ADMIN_TOKEN_TTL_SECONDS"])

    @app.post("/api/admin/ai/test")
    @admin_required
    def test_admin_groq_chat():
        payload = request.get_json(silent=True) or {}
        submitted_messages = payload.get("messages")
        if submitted_messages is None:
            message = payload.get("message")
            submitted_messages = [{"role": "user", "content": message}] if isinstance(message, str) else None
        if not isinstance(submitted_messages, list) or not submitted_messages or len(submitted_messages) > 20:
            return error("Send between 1 and 20 chat messages", 400)
        messages = [{"role": "system", "content": "You are a helpful assistant."}]
        total_chars = 0
        for item in submitted_messages:
            if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
                return error("Chat messages must have a user or assistant role", 400)
            content = item.get("content")
            if not isinstance(content, str) or not content.strip() or len(content) > 2000:
                return error("Each chat message must contain 1 to 2,000 characters", 400)
            total_chars += len(content)
            messages.append({"role": item["role"], "content": content.strip()})
        if total_chars > 12000 or messages[-1]["role"] != "user":
            return error("Chat history is too long or must end with a user message", 400)
        api_key, _ = get_groq_key()
        if not api_key:
            return error("Set GROQ_API_KEY in the Render service environment variables first", 409)
        headers = {"Authorization": f"Bearer {api_key}"}

        def groq_failure(response, stage, model=None):
            try:
                provider_error = response.json()
            except requests.exceptions.JSONDecodeError:
                provider_error = None
            error_data = provider_error.get("error", {}) if isinstance(provider_error, dict) else {}
            detail = error_data.get("message", "") if isinstance(error_data, dict) else ""
            if not detail:
                detail = response.text
            detail = str(detail).replace(api_key, "[redacted]").strip()[:500]
            suffix = f": {detail}" if detail else ""
            app.logger.warning("Groq %s request failed with HTTP %s: %s", stage, response.status_code, detail or "no response detail")
            if response.status_code == 401:
                return error("Groq rejected the API key." + suffix, 502)
            if response.status_code == 403:
                if model is None:
                    return error("Groq denied the model-list request." + suffix, 502)
                return error(f"Groq denied access to the selected chat model {model}." + suffix, 502)
            if response.status_code == 429:
                return error("Groq rate limit or quota reached." + suffix, 503)
            return error(f"Groq {stage} failed (HTTP {response.status_code})" + suffix, 502)

        try:
            model_response = requests.get("https://api.groq.com/openai/v1/models", headers=headers, timeout=30)
            if not model_response.ok:
                return groq_failure(model_response, "model-list")
            model_result = model_response.json()
            all_models = [item["id"] for item in model_result["data"]]
            skip = ("whisper", "guard", "tts", "orpheus", "playai", "embed")
            chat_models = [item for item in all_models if not any(term in item.lower() for term in skip)]
            if not chat_models:
                return error("Groq returned no chat-capable models for this API key", 502)
            model = chat_models[0]
            chat_response = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers=headers,
                json={"model": model, "messages": messages},
                timeout=60,
            )
            if not chat_response.ok:
                return groq_failure(chat_response, "chat", model)
            result = chat_response.json()
            reply = result["choices"][0]["message"]["content"]
            if not isinstance(reply, str) or not reply.strip():
                raise ValueError("Empty chat response")
            return jsonify(reply=reply, model=model)
        except requests.RequestException:
            app.logger.exception("Could not connect to Groq")
            return error("Could not connect to Groq. Try again shortly.", 502)
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            app.logger.warning("Groq returned an invalid chat response")
            return error("Groq returned an invalid response. Check the selected model and try again.", 502)

    @app.get("/api/admin/payment-settings")
    @admin_required
    def admin_payment_settings():
        return jsonify(mode=active_payment_mode(), providers={mode: provider_status(mode) for mode in PAYMENT_MODES}, shipping_methods=shipping_methods())

    @app.put("/api/admin/payment-settings")
    @admin_required
    def update_admin_payment_settings():
        payload = request.get_json(silent=True) or {}
        if "shipping_methods" in payload:
            submitted = payload.get("shipping_methods")
            if not isinstance(submitted, list) or not submitted or len(submitted) > 10:
                return error("Add between 1 and 10 shipping methods", 400)
            methods, ids = [], set()
            for item in submitted:
                if not isinstance(item, dict):
                    return error("Each shipping method must have a name and price", 400)
                name = str(item.get("name", "")).strip()[:80]
                method_id = str(item.get("id", "")).strip().lower()[:40]
                try:
                    price = Decimal(str(item.get("price", ""))).quantize(Decimal("0.01"))
                except (InvalidOperation, ValueError):
                    return error("Shipping prices must be valid numbers", 400)
                if not name or not method_id or not method_id.replace("-", "").replace("_", "").isalnum() or method_id in ids:
                    return error("Shipping method names and IDs must be valid and unique", 400)
                if not price.is_finite() or price < 0 or price > Decimal("100000"):
                    return error("Shipping prices must be between USD 0 and USD 100,000", 400)
                ids.add(method_id)
                methods.append({"id": method_id, "name": name, "price": f"{price:.2f}", "enabled": bool(item.get("enabled", True))})
            setting = db.session.get(StoreSetting, "shipping_methods")
            if setting:
                setting.value = json.dumps(methods)
            else:
                db.session.add(StoreSetting(key="shipping_methods", value=json.dumps(methods)))
            db.session.commit()
            if "mode" not in payload:
                return jsonify(mode=active_payment_mode(), providers={mode: provider_status(mode) for mode in PAYMENT_MODES}, shipping_methods=methods)
        mode = str(payload.get("mode", "")).strip().lower()
        if mode not in PAYMENT_MODES:
            return error("Payment mode must be test or live", 400)
        available = provider_status(mode)
        if not any(available.values()):
            return error(f"No payment provider is configured for {mode} mode", 409)
        setting = db.session.get(StoreSetting, "payment_mode")
        if setting:
            setting.value = mode
        else:
            db.session.add(StoreSetting(key="payment_mode", value=mode))
        db.session.commit()
        return jsonify(mode=mode, providers={candidate: provider_status(candidate) for candidate in PAYMENT_MODES}, shipping_methods=shipping_methods())

    @app.get("/api/admin/orders")
    @admin_required
    def admin_orders():
        rows = Order.query.order_by(Order.created_at.desc()).limit(500).all()
        return jsonify(items=[row.to_dict() for row in rows], count=len(rows))

    @app.put("/api/admin/orders/<order_id>/shipping-reconciliation")
    @admin_required
    def set_shipping_reconciliation(order_id):
        order = db.session.get(Order, order_id)
        if not order:
            return error("Order not found", 404)
        payload = request.get_json(silent=True) or {}
        try:
            final_cost = Decimal(str(payload.get("final_usd"))).quantize(Decimal("0.01"))
            if not final_cost.is_finite() or final_cost < 0 or final_cost > Decimal("100000"):
                raise ValueError
        except (InvalidOperation, ValueError, TypeError):
            return error("Enter the carrier's final cost in USD", 400)
        reconciliation = order.shipping_reconciliation
        if not reconciliation:
            reconciliation = ShippingReconciliation(order_id=order.id, quoted_usd=order.shipping)
            db.session.add(reconciliation)
        difference = (final_cost - Decimal(reconciliation.quoted_usd)).quantize(Decimal("0.01"))
        reconciliation.final_usd = final_cost
        reconciliation.difference_usd = difference
        reconciliation.reason = str(payload.get("reason", ""))[:1000]
        approval_url = None
        if difference > 0:
            token = secrets.token_urlsafe(32)
            reconciliation.approval_token_hash = hashlib.sha256(token.encode()).hexdigest()
            reconciliation.approval_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
            reconciliation.customer_decision = None
            reconciliation.status = "awaiting_customer_approval"
            base = os.getenv("FRONTEND_URL", "https://buxinstore.github.io/store").rstrip("/")
            approval_url = base + "/?shipping_adjustment=" + token
        elif difference < 0:
            reconciliation.approval_token_hash = None
            reconciliation.approval_expires_at = None
            reconciliation.status = "refund_due"
        else:
            reconciliation.approval_token_hash = None
            reconciliation.approval_expires_at = None
            reconciliation.status = "resolved"
            reconciliation.resolved_at = datetime.now(timezone.utc)
        db.session.commit()
        return jsonify(reconciliation=reconciliation.to_dict(), customer_approval_url=approval_url,
                       instruction="Send the approval URL to the customer using your existing support channel. The app does not send email or take an additional payment/refund automatically.")

    @app.post("/api/admin/orders/<order_id>/shipping-reconciliation/resolve")
    @admin_required
    def resolve_shipping_reconciliation(order_id):
        order = db.session.get(Order, order_id)
        reconciliation = order.shipping_reconciliation if order else None
        if not reconciliation:
            return error("Shipping reconciliation not found", 404)
        payload = request.get_json(silent=True) or {}
        resolution = str(payload.get("resolution", ""))
        reference = str(payload.get("reference", "")).strip()[:200]
        if not reference:
            return error("Enter the external payment or refund reference", 400)
        if reconciliation.status == "customer_approved_additional_due" and resolution == "additional_paid":
            reconciliation.status = "resolved"
        elif reconciliation.status == "refund_due" and resolution == "refund_paid":
            reconciliation.status = "resolved"
            reconciliation.refund_reference = reference
        elif reconciliation.status == "resolved" and Decimal(reconciliation.difference_usd or 0) == 0:
            pass
        else:
            return error("Customer approval and the corresponding external payment/refund are required first", 409)
        reconciliation.resolved_at = datetime.now(timezone.utc)
        db.session.commit()
        return jsonify(reconciliation=reconciliation.to_dict())

    @app.get("/api/shipping-adjustments/<token>")
    def shipping_adjustment_details(token):
        digest = hashlib.sha256(token.encode()).hexdigest()
        reconciliation = ShippingReconciliation.query.filter_by(approval_token_hash=digest, status="awaiting_customer_approval").first()
        expires = reconciliation.approval_expires_at if reconciliation else None
        if expires and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if not reconciliation or not expires or expires <= datetime.now(timezone.utc):
            return error("This shipping adjustment link is invalid or already used", 404)
        return jsonify(order_id=reconciliation.order_id, customer_name=reconciliation.order.customer_name,
                       quoted_usd=str(reconciliation.quoted_usd), final_usd=str(reconciliation.final_usd),
                       additional_usd=str(reconciliation.difference_usd), reason=reconciliation.reason,
                       currency="USD", expires_in_days=30)

    @app.post("/api/shipping-adjustments/<token>")
    def decide_shipping_adjustment(token):
        digest = hashlib.sha256(token.encode()).hexdigest()
        reconciliation = ShippingReconciliation.query.filter_by(approval_token_hash=digest, status="awaiting_customer_approval").with_for_update().first()
        expires = reconciliation.approval_expires_at if reconciliation else None
        if expires and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if not reconciliation or not expires or expires <= datetime.now(timezone.utc):
            return error("This shipping adjustment link is invalid or already used", 404)
        decision = (request.get_json(silent=True) or {}).get("approve")
        if not isinstance(decision, bool):
            return error("Choose approve or decline", 400)
        reconciliation.customer_decision = "approved" if decision else "declined"
        reconciliation.customer_decided_at = datetime.now(timezone.utc)
        reconciliation.status = "customer_approved_additional_due" if decision else "manual_review_declined"
        reconciliation.approval_token_hash = None
        reconciliation.approval_expires_at = None
        db.session.commit()
        return jsonify(status=reconciliation.status, message="Your decision was recorded. The store will contact you about the next step.")

    @app.patch("/api/admin/orders/<order_id>")
    @admin_required
    def update_order(order_id):
        order = db.session.get(Order, order_id)
        if not order:
            return error("Order not found", 404)
        payload = request.get_json(silent=True) or {}
        status = str(payload.get("status", "")).lower()
        if status not in ALLOWED_ORDER_STATUSES:
            return error("Invalid order status", 400)
        if status == "shipped" and (not order.shipping_reconciliation or order.shipping_reconciliation.status != "resolved"):
            return error("Shipping cost must be reconciled and any customer-approved adjustment or refund completed before dispatch", 409)
        order.status = status
        db.session.commit()
        return jsonify(order=order.to_dict())

    @app.post("/api/admin/categories")
    @admin_required
    def create_category():
        payload = request.get_json(silent=True) or {}
        name = str(payload.get("name", "")).strip()
        slug = re.sub(r"[^a-z0-9]+", "-", str(payload.get("slug", "") or name).strip().lower()).strip("-")
        if not name or not slug:
            return error("Category name is required", 400)
        if Category.query.filter(db.func.lower(Category.name) == name.casefold()).first() or Category.query.filter_by(slug=slug).first():
            return error("A category with this name or URL already exists", 409)
        category = Category(name=name[:120], slug=slug[:140], description=str(payload.get("description", ""))[:500])
        db.session.add(category)
        try:
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            return error("Could not save this category; check its name and slug", 409)
        return jsonify(item=category.to_dict()), 201

    @app.get("/api/admin/categories")
    @admin_required
    def admin_categories():
        rows = Category.query.order_by(Category.name).all()
        return jsonify(items=[row.to_dict(include_count=True) for row in rows])

    @app.post("/api/admin/products")
    @admin_required
    def create_product():
        payload = request.get_json(silent=True) or {}
        product, problem = prepare_product(payload)
        if problem:
            return error(problem, 400)
        if Product.query.filter_by(slug=product.slug).first():
            return error("Product slug already exists", 409)
        if product.sku and Product.query.filter(db.func.lower(Product.sku) == product.sku.casefold()).first():
            return error("SKU already exists", 409)
        db.session.add(product)
        db.session.flush()
        if "media" in payload:
            problem = replace_product_media(product, payload["media"])
            if problem:
                db.session.rollback()
                return error(problem, 400)
        elif product.image_url:
            db.session.add(ProductMedia(product=product, media_type="image", url=product.image_url, is_primary=True, alt_text=product.name))
        try:
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            return error("Could not save product; check that its SKU and URL are unique", 409)
        return jsonify(item=product.to_dict()), 201

    @app.get("/api/admin/products")
    @admin_required
    def admin_products():
        query = Product.query.join(Category)
        search = request.args.get("q", "").strip()[:100]
        if search:
            pattern = f"%{search}%"
            query = query.filter(db.or_(Product.name.ilike(pattern), Product.sku.ilike(pattern), Product.brand.ilike(pattern), Category.name.ilike(pattern)))
        category = request.args.get("category", "").strip()
        if category:
            try:
                query = query.filter(Product.category_id == int(category))
            except ValueError:
                query = query.filter(Category.slug == category)
        status = request.args.get("status", "").strip().lower()
        if status in {"draft", "active", "inactive", "archived"}:
            query = query.filter(Product.status == status)
        availability = request.args.get("availability", "").strip().lower()
        if availability == "in_stock":
            query = query.filter(Product.stock > 0)
        elif availability == "out_of_stock":
            query = query.filter(Product.stock <= 0)
        sort = request.args.get("sort", "name")
        sort_columns = {"name": Product.name, "price": Product.price, "stock": Product.stock, "created": Product.created_at}
        sort_column = sort_columns.get(sort, Product.name)
        order = sort_column.desc() if request.args.get("direction") == "desc" else sort_column.asc()
        try:
            page_no = max(1, int(request.args.get("page", 1)))
            page_size = min(100, max(1, int(request.args.get("per_page", 1000))))
        except ValueError:
            page_no, page_size = 1, 1000
        count = query.count()
        rows = query.order_by(order, Product.id).offset((page_no - 1) * page_size).limit(page_size).all()
        return jsonify(items=[row.to_dict() for row in rows], count=count, page=page_no,
                       per_page=page_size, pages=(count + page_size - 1) // page_size)

    @app.get("/api/admin/products/excel-template")
    @admin_required
    def admin_products_excel_template():
        output = build_product_template()
        return send_file(output, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         as_attachment=True, download_name="buxin-product-import-template.xlsx")

    @app.post("/api/admin/products/excel-preview")
    @admin_required
    def admin_products_excel_preview():
        upload = request.files.get("file")
        if not upload or not upload.filename or not upload.filename.lower().endswith(".xlsx"):
            return error("Choose an .xlsx product workbook", 400)
        content = upload.stream.read(MAX_PRODUCT_XLSX_BYTES + 1)
        if len(content) > MAX_PRODUCT_XLSX_BYTES:
            return error("Product workbook is larger than 4 MB", 413)
        categories = Category.query.filter_by(is_active=True).order_by(Category.name).all()
        existing_products = Product.query.all()
        by_sku = {product.sku.casefold(): product for product in existing_products if product.sku}
        try:
            parsed = parse_product_workbook(content, categories, by_sku)
        except ValueError as exc:
            return error(str(exc), 400)
        by_slug = {product.slug: product for product in existing_products}
        workbook_slugs = set()
        for row in parsed["products"]:
            if row["slug"] in workbook_slugs:
                parsed["errors"].append({"row": row["row"], "sheet": "Products", "message": f"Duplicate slug '{row['slug']}' in workbook"})
            workbook_slugs.add(row["slug"])
            match = by_sku.get(row["sku"].casefold()) if row["sku"] else by_slug.get(row["slug"])
            occupied = by_slug.get(row["slug"])
            if occupied and occupied is not match:
                parsed["errors"].append({"row": row["row"], "sheet": "Products", "message": f"Slug '{row['slug']}' is already used by another product"})
            if row["sku"] and row["sku"].casefold() in by_sku and match is None:
                parsed["errors"].append({"row": row["row"], "sheet": "Products", "message": f"SKU '{row['sku']}' is already used"})
        invalid_rows = len({(item.get("sheet", "Products"), item["row"]) for item in parsed["errors"]})
        update_ids = set()
        for row in parsed["products"]:
            matched = by_sku.get(row["sku"].casefold()) if row["sku"] else by_slug.get(row["slug"])
            if matched:
                update_ids.add(matched.id)
        preview = {"rows_detected": parsed["rows_detected"], "new_products": max(0, len(parsed["products"]) - len(update_ids)),
                   "existing_products_to_update": len(update_ids), "invalid_rows": invalid_rows,
                   "errors": parsed["errors"], "media_records": len(parsed["media"]),
                   "media_needing_attention": sum(1 for item in parsed["errors"] if item.get("sheet") == "Images and Videos"),
                   "sku_updates": sum(1 for row in parsed["products"] if row["sku"] and row["sku"].casefold() in by_sku)}
        import_id = None
        if not invalid_rows:
            staged = ProductExcelImport(id=str(uuid4()), payload=json.dumps(parsed), preview=json.dumps(preview), created_at=datetime.now(timezone.utc))
            db.session.add(staged)
            db.session.commit()
            import_id = staged.id
        return jsonify(import_id=import_id, preview=preview), 201

    @app.post("/api/admin/products/excel-confirm")
    @admin_required
    def admin_products_excel_confirm():
        payload = request.get_json(silent=True) or {}
        if payload.get("confirm_import") is not True:
            return error("Explicitly confirm the reviewed product import", 400)
        staged = ProductExcelImport.query.filter_by(id=str(payload.get("import_id", ""))).with_for_update().first()
        if not staged or staged.consumed_at:
            return error("This product preview expired or was already imported. Upload the workbook again.", 409)
        created_at = staged.created_at.replace(tzinfo=timezone.utc) if staged.created_at.tzinfo is None else staged.created_at
        if created_at < datetime.now(timezone.utc) - timedelta(hours=1):
            return error("This product preview expired. Upload the workbook again.", 409)
        parsed = json.loads(staged.payload)
        preview = json.loads(staged.preview)
        if preview.get("invalid_rows"):
            return error("Correct every invalid row and upload the workbook again", 400)
        existing_products = Product.query.with_for_update().all()
        by_sku = {product.sku.casefold(): product for product in existing_products if product.sku}
        by_slug = {product.slug: product for product in existing_products}
        changes = {"created": [], "updated": [], "media_added": 0}
        try:
            for row in parsed["products"]:
                product = by_sku.get(row["sku"].casefold()) if row["sku"] else by_slug.get(row["slug"])
                is_new = product is None
                provided = set(row.get("provided", []))
                update_data = {"name": row["name"], "slug": row["slug"], "price": row["price"],
                               "stock": row["stock"], "category_id": row["category_id"],
                               "sku": row["sku"] or (product.sku if product else None)}
                if "Product Status" in provided or is_new:
                    update_data["status"] = row["status"]
                if "Featured" in provided or is_new:
                    update_data["is_featured"] = row["is_featured"]
                if row["details"]:
                    update_data["details"] = row["details"]
                if row["specifications"] is not None:
                    update_data["specifications"] = row["specifications"]
                if row["tags"] is not None:
                    update_data["tags"] = row["tags"]
                if "Product Image URL" in provided:
                    update_data["image_url"] = row["primary_image_url"] or None
                candidate, problem = prepare_product(update_data, existing=product)
                if problem:
                    raise ValueError(f"Row {row['row']}: {problem}")
                if is_new:
                    product = candidate
                    db.session.add(product)
                    db.session.flush()
                    by_slug[product.slug] = product
                    if product.sku:
                        by_sku[product.sku.casefold()] = product
                else:
                    for field in ("name", "slug", "short_description", "description", "price", "compare_at_price", "image_url", "sku", "brand", "subcategory", "status", "stock", "weight_kg", "is_active", "is_featured", "category_id", "metadata_json"):
                        setattr(product, field, getattr(candidate, field))
                    by_slug[product.slug] = product
                    if product.sku:
                        by_sku[product.sku.casefold()] = product
                if row["primary_image_url"]:
                    primary = next((item for item in product.media if item.media_type == "image" and item.url == row["primary_image_url"]), None)
                    for item in product.media:
                        if item.media_type == "image":
                            item.is_primary = item is primary
                    if not primary:
                        primary = ProductMedia(product=product, media_type="image", url=row["primary_image_url"], alt_text=product.name, is_primary=True, display_order=0)
                        db.session.add(primary)
                changes["created" if is_new else "updated"].append({"sku": product.sku, "name": product.name, "id": product.id})
            for media_row in parsed["media"]:
                product = by_sku.get(media_row["sku"].casefold())
                if not product:
                    raise ValueError(f"Media SKU '{media_row['sku']}' is no longer present; preview the workbook again")
                if any(item.media_type == media_row["type"] and item.url == media_row["url"] for item in product.media):
                    continue
                make_primary = media_row["primary"] or (media_row["type"] == "image" and not any(item.media_type == "image" and item.is_primary for item in product.media))
                if make_primary and media_row["type"] == "image":
                    for item in product.media:
                        if item.media_type == "image":
                            item.is_primary = False
                db.session.add(ProductMedia(product=product, media_type=media_row["type"], url=media_row["url"],
                                            title=media_row["title"] or None, alt_text=media_row["title"] or product.name,
                                            is_primary=make_primary, display_order=media_row["order"]))
                if make_primary and media_row["type"] == "image":
                    product.image_url = media_row["url"]
                changes["media_added"] += 1
            staged.consumed_at = datetime.now(timezone.utc)
            db.session.commit()
        except (ValueError, SQLAlchemyError) as exc:
            db.session.rollback()
            if isinstance(exc, SQLAlchemyError):
                app.logger.exception("Product Excel import failed atomically")
                return error("Import could not be saved; no product changes were applied", 409)
            return error(str(exc), 400)
        return jsonify(message=f"Imported {len(changes['created'])} new products and updated {len(changes['updated'])} existing products.", changes=changes)

    @app.patch("/api/admin/products/<int:product_id>")
    @admin_required
    def update_product(product_id):
        product = db.session.get(Product, product_id)
        if not product:
            return error("Product not found", 404)
        payload = request.get_json(silent=True) or {}
        candidate, problem = prepare_product(payload, existing=product)
        if problem:
            return error(problem, 400)
        if candidate.slug != product.slug and Product.query.filter(Product.slug == candidate.slug, Product.id != product.id).first():
            return error("Product slug already exists", 409)
        if candidate.sku and Product.query.filter(db.func.lower(Product.sku) == candidate.sku.casefold(), Product.id != product.id).first():
            return error("SKU already exists", 409)
        for field in ("name", "slug", "short_description", "description", "price", "compare_at_price", "image_url", "cloudinary_public_id", "sku", "brand", "subcategory", "status", "stock", "weight_kg", "is_active", "is_featured", "category_id", "metadata_json"):
            setattr(product, field, getattr(candidate, field))
        if "media" in payload:
            problem = replace_product_media(product, payload["media"])
            if problem:
                db.session.rollback()
                return error(problem, 400)
        elif "image_url" in payload and product.image_url:
            existing_primary = next((media for media in product.media if media.is_primary and media.media_type == "image"), None)
            if existing_primary:
                existing_primary.url = product.image_url
            else:
                db.session.add(ProductMedia(product=product, media_type="image", url=product.image_url, is_primary=True, alt_text=product.name))
        try:
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            return error("Could not update product; check that its SKU and URL are unique", 409)
        return jsonify(item=product.to_dict())

    @app.delete("/api/admin/products/<int:product_id>")
    @admin_required
    def archive_product(product_id):
        product = db.session.get(Product, product_id)
        if not product:
            return error("Product not found", 404)
        product.status = "archived"
        product.is_active = False
        try:
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            return error("Could not archive product", 409)
        return jsonify(item=product.to_dict(), message="Product archived; order history is preserved")

    @app.post("/api/admin/uploads")
    @admin_required
    def upload_image():
        images = request.files.getlist("images") or ([request.files["image"]] if "image" in request.files else [])
        if not images:
            return error("Choose at least one image file", 400)
        if len(images) > 12:
            return error("Upload up to 12 images at a time", 413)
        if not os.getenv("CLOUDINARY_URL"):
            return error("Cloudinary is not configured", 503)
        signatures = {"image/jpeg": (b"\xff\xd8\xff",), "image/png": (b"\x89PNG\r\n\x1a\n",),
                      "image/webp": (b"RIFF",), "image/gif": (b"GIF87a", b"GIF89a")}
        validated = []
        total_bytes = 0
        for image in images:
            content = image.stream.read(5 * 1024 * 1024 + 1)
            image.stream.seek(0)
            mime = image.mimetype.lower()
            valid_magic = any(content.startswith(signature) for signature in signatures.get(mime, ()))
            if not valid_magic or (mime == "image/webp" and content[8:12] != b"WEBP"):
                return error("Only verified JPEG, PNG, WebP, and GIF image files are accepted", 415)
            if len(content) > 5 * 1024 * 1024:
                return error("Each image must be 5 MB or smaller", 413)
            total_bytes += len(content)
            if total_bytes > 60 * 1024 * 1024:
                return error("Combined image uploads must be 60 MB or smaller", 413)
            validated.append(image)
        uploaded = []
        for image in validated:
            try:
                result = cloudinary.uploader.upload(image, folder="electrohub/products", resource_type="image",
                                                    allowed_formats=["jpg", "jpeg", "png", "webp", "gif"])
            except Exception:
                app.logger.exception("Cloudinary product image upload failed")
                return error("Image upload failed", 502)
            uploaded.append({"image_url": result["secure_url"], "public_id": result["public_id"]})
        return jsonify(images=uploaded, **uploaded[0]), 201

    @app.errorhandler(RequestEntityTooLarge)
    def request_too_large(_exc):
        return error("Uploaded file is larger than the configured upload limit", 413)

    @app.errorhandler(404)
    def not_found(_exc):
        if request.path.startswith("/api/"):
            return error("API route not found", 404)
        return "Not Found", 404

    @app.errorhandler(500)
    def internal_error(_exc):
        db.session.rollback()
        return jsonify(error="Internal server error"), 500

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=os.getenv("FLASK_DEBUG") == "1")
