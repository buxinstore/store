import base64
import hmac
import hashlib
import json
import os
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
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, request
from flask_cors import CORS
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.exceptions import RequestEntityTooLarge

from models import Category, Order, OrderItem, PaymentAttempt, Product, StoreSetting, ShippingEstimate, ShippingEstimateHistory, ShippingRefreshJob, ShippingReconciliation, db
from catalog_seed import seed_starter_catalog
from shipping import refresh_destinations
from shipping_core import billable_weight, estimate_is_current, safe_sources

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

ALLOWED_ORDER_STATUSES = {"pending", "processing", "shipped", "completed", "cancelled"}
PAYMENT_MODES = {"test", "live"}
PAYMENT_PROVIDERS = {"modem", "paypal"}
ONLINE_PAYMENT_MESSAGE = "Online payments are not configured for the selected payment mode."
DEFAULT_SHIPPING_METHODS = [
    {"id": "dhl", "name": "DHL", "price": "4.99", "enabled": True},
    {"id": "fedex", "name": "FedEx", "price": "4.99", "enabled": True},
    {"id": "community", "name": "Community courier", "price": "4.99", "enabled": True},
]


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
        MAX_CONTENT_LENGTH=int(os.getenv("MAX_CONTENT_LENGTH", 5 * 1024 * 1024)),
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
        # AI credentials now come only from the server environment.
        obsolete_groq_settings = [db.session.get(StoreSetting, key)
                                  for key in ("groq_api_key_encrypted", "groq_model")]
        obsolete_groq_settings = [setting for setting in obsolete_groq_settings if setting]
        if obsolete_groq_settings:
            for setting in obsolete_groq_settings:
                db.session.delete(setting)
            db.session.commit()
        # Additive, safe upgrades for databases created by earlier storefront versions.
        columns = {column["name"] for column in inspect(db.engine).get_columns("orders")}
        if "shipping_method" not in columns:
            with db.engine.begin() as connection:
                connection.execute(text("ALTER TABLE orders ADD COLUMN shipping_method VARCHAR(120) NOT NULL DEFAULT 'standard'"))
        product_columns = {column["name"] for column in inspect(db.engine).get_columns("products")}
        if "weight_kg" not in product_columns:
            with db.engine.begin() as connection:
                connection.execute(text("ALTER TABLE products ADD COLUMN weight_kg NUMERIC(8, 3)"))
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
        estimate = None
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
            product = Product.query.filter_by(id=product_id, is_active=True).with_for_update().first()
            if not product:
                raise LookupError(f"Product {product_id} was not found")
            if product.stock < quantity:
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
        band = billable_weight(actual_weight)
        try:
            estimate_id = int(payload.get("shipping_estimate_id", 0))
        except (TypeError, ValueError):
            estimate_id = 0
        estimate = db.session.get(ShippingEstimate, estimate_id)
        if not estimate or estimate.country_code != country_code or estimate.weight_kg != band or not estimate_is_current(estimate):
            raise ValueError("A current shipping estimate is not available for this destination and weight; contact support for a manual quote")
        shipping = Decimal(estimate.price_usd).quantize(Decimal("0.01"))
        snapshot = {"estimate_id": estimate.id, "country_code": country_code, "country_name": estimate.country_name,
                    "actual_weight_kg": str(actual_weight), "billable_weight_kg": str(band), "currency": "USD",
                    "price_usd": str(shipping), "sources": safe_sources(estimate.sources),
                    "fx_snapshot": json.loads(estimate.fx_snapshot) if estimate.fx_snapshot else None,
                    "researched_at": estimate.researched_at.isoformat() if estimate.researched_at else None,
                    "expires_at": estimate.expires_at.isoformat() if estimate.expires_at else None,
                    "disclaimer": "Indicative shipping estimate; final carrier charges can vary."}
        order = Order(
            id=str(uuid4()), customer_name=str(payload["customer_name"]).strip()[:180],
            email=str(payload["email"]).strip()[:254], address=json.dumps({key: str(value).strip() for key, value in address.items()}, ensure_ascii=False),
            city=str(address["city"]).strip()[:120], postal_code=str(address["postal_code"]).strip()[:32], shipping_method=f"Indicative estimate · {band} kg",
            shipping_snapshot=json.dumps(snapshot), shipping_acknowledged_at=datetime.now(timezone.utc),
            payment_method=payment_method, payment_status="unpaid", status="pending",
            subtotal=subtotal, shipping=shipping, total=subtotal + shipping,
        )
        for product, quantity in order_lines:
            order.items.append(OrderItem(
                product_id=product.id, product_name=product.name, image_url=product.image_url,
                unit_price=product.price, quantity=quantity,
            ))
            product.stock -= quantity
        db.session.add(order)
        order.shipping_reconciliation = ShippingReconciliation(order_id=order.id, quoted_usd=shipping, status="awaiting_final_cost")
        return order

    def release_order_stock(order):
        for item in order.items:
            if item.product_id:
                product = db.session.get(Product, item.product_id)
                if product:
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
        query = Product.query.join(Category).filter(Product.is_active.is_(True), Category.is_active.is_(True))
        category = request.args.get("category", "").strip()
        search = request.args.get("q", "").strip()
        if category:
            query = query.filter(Category.slug == category)
        if search:
            like = f"%{search[:100]}%"
            query = query.filter((Product.name.ilike(like)) | (Product.description.ilike(like)))
        featured = request.args.get("featured") == "true"
        if featured:
            query = query.filter(Product.is_featured.is_(True))
        try:
            limit = min(max(int(request.args.get("limit", 100)), 1), 200)
        except ValueError:
            limit = 100
        rows = query.order_by(Product.is_featured.desc(), Product.name).limit(limit).all()
        return jsonify(items=[row.to_dict() for row in rows], count=len(rows))

    @app.get("/api/products/<int:product_id>")
    def product_detail(product_id):
        row = Product.query.filter_by(id=product_id, is_active=True).first()
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
        from pycountry import countries
        country = countries.get(alpha_2=country_code)
        if not country:
            return error("Choose a supported destination country", 400)
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
            products = {p.id: p for p in Product.query.filter(Product.id.in_(quantities), Product.is_active.is_(True)).all()}
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
        row = ShippingEstimate.query.filter_by(country_code=country_code, weight_kg=band).first()
        if not row or not estimate_is_current(row):
            return jsonify(status="manual_review", country_code=country_code, country_name=country.name,
                           actual_weight_kg=str(actual), billable_weight_kg=str(band),
                           reason="No current source-backed estimate is available; contact support for a manual quote"), 200
        return jsonify(status="available", estimate_id=row.id, country_code=country_code, country_name=country.name,
                       actual_weight_kg=str(actual), billable_weight_kg=str(band), price_usd=str(row.price_usd), currency="USD",
                       confidence=row.confidence, sources=safe_sources(row.sources), researched_at=row.researched_at.isoformat() if row.researched_at else None,
                       expires_at=row.expires_at.isoformat() if row.expires_at else None,
                       disclaimer="Indicative estimate only. Carrier charges can vary; no additional amount is charged without your approval.")

    @app.get("/api/admin/shipping")
    @admin_required
    def admin_shipping():
        rows = ShippingEstimate.query.order_by(ShippingEstimate.country_name, ShippingEstimate.weight_kg).limit(5000).all()
        jobs = ShippingRefreshJob.query.order_by(ShippingRefreshJob.started_at.desc()).limit(20).all()
        return jsonify(estimates=[{"id": r.id, "country_code": r.country_code, "country_name": r.country_name,
                                   "weight_kg": str(r.weight_kg), "price_usd": str(r.price_usd) if r.price_usd is not None else None,
                                   "status": r.status, "confidence": r.confidence, "sources": safe_sources(r.sources),
                                   "fx_snapshot": json.loads(r.fx_snapshot) if r.fx_snapshot else None,
                                   "notes": r.notes, "researched_at": r.researched_at.isoformat() if r.researched_at else None,
                                   "expires_at": r.expires_at.isoformat() if r.expires_at else None, "manual_override": r.manual_override,
                                   "current": estimate_is_current(r)} for r in rows],
                       jobs=[{"id": j.id, "status": j.status, "attempted": j.attempted, "succeeded": j.succeeded,
                              "failed": j.failed, "message": j.message, "started_at": j.started_at.isoformat(),
                              "finished_at": j.finished_at.isoformat() if j.finished_at else None} for j in jobs])

    @app.get("/api/admin/shipping/<int:estimate_id>/history")
    @admin_required
    def admin_shipping_history(estimate_id):
        if not db.session.get(ShippingEstimate, estimate_id):
            return error("Shipping estimate not found", 404)
        rows = ShippingEstimateHistory.query.filter_by(estimate_id=estimate_id).order_by(ShippingEstimateHistory.created_at.desc()).limit(50).all()
        return jsonify(items=[{"snapshot": json.loads(row.snapshot), "created_at": row.created_at.isoformat()} for row in rows])

    @app.post("/api/admin/shipping/refresh")
    @admin_required
    def admin_shipping_refresh():
        payload = request.get_json(silent=True) or {}
        code = str(payload.get("country_code", "")).strip().upper()
        if code:
            from pycountry import countries
            if not countries.get(alpha_2=code):
                return error("Unsupported country code", 400)
        try:
            job = refresh_destinations([code] if code else None, maximum=1 if code else None)
            return jsonify(job_id=job.id, status=job.status, attempted=job.attempted,
                           succeeded=job.succeeded, failed=job.failed, message=job.message), 200
        except Exception as exc:
            db.session.rollback()
            app.logger.exception("Shipping estimate refresh failed")
            return error("Shipping estimate refresh failed: " + str(exc)[:240], 502)

    @app.put("/api/admin/shipping/override")
    @admin_required
    def admin_shipping_override():
        payload = request.get_json(silent=True) or {}
        from pycountry import countries
        code = str(payload.get("country_code", "")).strip().upper()
        country = countries.get(alpha_2=code)
        try:
            band = Decimal(str(payload.get("weight_kg")))
            price = Decimal(str(payload.get("price_usd"))).quantize(Decimal("0.01"))
            if not country or band < Decimal("0.5") or band % Decimal("0.5") or price < 0 or price > Decimal("100000"):
                raise ValueError
        except (InvalidOperation, TypeError, ValueError):
            return error("Provide a valid country, 0.5 kg weight band, and USD price", 400)
        source_url = str(payload.get("source_url", "")).strip()
        sources = safe_sources(json.dumps([{"url": source_url, "title": "Manually reviewed source"}])) if source_url else []
        if source_url and not sources:
            return error("Manual source must be an HTTPS URL", 400)
        row = ShippingEstimate.query.filter_by(country_code=code, weight_kg=band).first()
        if row:
            db.session.add(ShippingEstimateHistory(estimate_id=row.id, snapshot=json.dumps({"price_usd": str(row.price_usd) if row.price_usd is not None else None, "status": row.status, "sources": row.sources, "researched_at": row.researched_at.isoformat() if row.researched_at else None})))
        else:
            row = ShippingEstimate(country_code=code, weight_kg=band)
            db.session.add(row)
        row.country_name, row.price_usd, row.status = country.name, price, "available"
        row.confidence, row.sources, row.fx_snapshot, row.notes = "manual", json.dumps(sources), json.dumps({"currency":"USD","rate":"1","date":datetime.now(timezone.utc).date().isoformat()}), str(payload.get("notes", ""))[:1000]
        row.researched_at, row.expires_at, row.manual_override = datetime.now(timezone.utc), datetime.now(timezone.utc) + timedelta(days=365), True
        db.session.commit()
        return jsonify(id=row.id, country_code=code, weight_kg=str(band), price_usd=str(price), status=row.status, manual_override=True)

    @app.post("/api/orders")
    def create_order():
        payload = request.get_json(silent=True) or {}
        method = str(payload.get("payment_method", "")).strip().lower()
        if method != "cash_on_delivery":
            return error(ONLINE_PAYMENT_MESSAGE, 503)
        required = ("customer_name", "email", "address", "city", "postal_code")
        if any(not str(payload.get(field, "")).strip() for field in required):
            return error("Customer name, email, and shipping address are required", 400)
        lines = payload.get("items")
        if not isinstance(lines, list) or not lines:
            return error("Order must include at least one item", 400)
        if len(lines) > 100:
            return error("Order contains too many line items", 400)

        order_lines = []
        subtotal = Decimal("0.00")
        requested = {}
        try:
            for line in lines:
                product_id = int(line.get("product_id", 0))
                quantity = int(line.get("quantity", 0))
                if quantity < 1 or quantity > 99:
                    return error("Each quantity must be between 1 and 99", 400)
                requested[product_id] = requested.get(product_id, 0) + quantity
                if requested[product_id] > 99:
                    return error("Combined product quantity cannot exceed 99", 400)
            for product_id, quantity in requested.items():
                product = Product.query.filter_by(id=product_id, is_active=True).with_for_update().first()
                if not product:
                    return error(f"Product {product_id} was not found", 404)
                if product.stock < quantity:
                    return error(f"Insufficient stock for {product.name}", 409)
                subtotal += Decimal(product.price) * quantity
                order_lines.append((product, quantity))
        except (AttributeError, TypeError, ValueError):
            return error("Each item must include a valid product_id and quantity", 400)

        shipping = Decimal("0.00") if subtotal >= Decimal("50.00") else Decimal("4.99")
        order = Order(
            id=str(uuid4()),
            customer_name=str(payload["customer_name"]).strip()[:180],
            email=str(payload["email"]).strip()[:254],
            address=str(payload["address"]).strip()[:300],
            city=str(payload["city"]).strip()[:120],
            postal_code=str(payload["postal_code"]).strip()[:32],
            payment_method=method,
            payment_status="unpaid",
            status="pending",
            subtotal=subtotal,
            shipping=shipping,
            total=subtotal + shipping,
        )
        for product, quantity in order_lines:
            order.items.append(
                OrderItem(
                    product_id=product.id,
                    product_name=product.name,
                    image_url=product.image_url,
                    unit_price=product.price,
                    quantity=quantity,
                )
            )
            product.stock -= quantity
        db.session.add(order)
        try:
            db.session.commit()
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
        return jsonify(mode=mode, providers=providers, currency="USD", shipping_methods=methods, modem_conversion=conversion)

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
        try:
            with urlopen(Request("https://api.groq.com/openai/v1/models", headers=headers), timeout=30) as response:
                model_result = json.loads(response.read().decode("utf-8"))
            all_models = [item["id"] for item in model_result["data"]]
            skip = ("whisper", "guard", "tts", "orpheus", "playai", "embed")
            chat_models = [item for item in all_models if not any(term in item.lower() for term in skip)]
            if not chat_models:
                return error("Groq returned no chat-capable models for this API key", 502)
            model = chat_models[0]
            body = json.dumps({"model": model, "messages": messages}).encode("utf-8")
            req = Request("https://api.groq.com/openai/v1/chat/completions", data=body,
                          headers={**headers, "Content-Type": "application/json"})
            with urlopen(req, timeout=30) as response:
                result = json.loads(response.read().decode("utf-8"))
            reply = result["choices"][0]["message"]["content"]
            if not isinstance(reply, str) or not reply.strip():
                raise ValueError("Empty chat response")
            return jsonify(reply=reply, model=model)
        except HTTPError as exc:
            app.logger.warning("Groq %s request failed with HTTP %s", "model-list" if "model" not in locals() else "chat", exc.code)
            try:
                provider_error = json.loads(exc.read().decode("utf-8", errors="replace"))
                error_data = provider_error.get("error", {}) if isinstance(provider_error, dict) else {}
                detail = error_data.get("message", "") if isinstance(error_data, dict) else ""
                detail = str(detail).replace(api_key, "[redacted]").strip()[:500]
            except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
                detail = ""
            suffix = f": {detail}" if detail else ""
            if exc.code == 401:
                return error("Groq rejected the API key. Check that it is valid and active." + suffix, 502)
            if exc.code == 403:
                if "model" not in locals():
                    return error("Groq denied the model-list request. Check that Render's GROQ_API_KEY is valid and has access to list models." + suffix, 502)
                return error(f"Groq denied access to the selected chat model {model}." + suffix, 502)
            if exc.code == 429:
                return error("Groq rate limit or quota reached. Try again later." + suffix, 503)
            stage = "model discovery" if "model" not in locals() else "chat request"
            return error(f"Groq {stage} failed (HTTP {exc.code})" + suffix, 502)
        except (URLError, TimeoutError):
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
        slug = str(payload.get("slug", "")).strip().lower()
        if not name or not slug:
            return error("Category name and slug are required", 400)
        if Category.query.filter_by(slug=slug).first():
            return error("Category slug already exists", 409)
        category = Category(name=name[:120], slug=slug[:140], description=str(payload.get("description", ""))[:500])
        db.session.add(category)
        db.session.commit()
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
        try:
            name = str(payload.get("name", "")).strip()
            slug = str(payload.get("slug", "")).strip().lower()
            price = Decimal(str(payload.get("price", "")))
            stock = int(payload.get("stock", 0))
            category_id = int(payload.get("category_id", 0))
            weight_kg = Decimal(str(payload["weight_kg"])) if payload.get("weight_kg") not in (None, "") else None
        except (TypeError, ValueError, InvalidOperation):
            return error("Valid name, slug, price, stock, and category_id are required", 400)
        category = db.session.get(Category, category_id)
        if not name or not slug or price < 0 or stock < 0 or not category or (weight_kg is not None and (weight_kg <= 0 or weight_kg > 1000)):
            return error("Product details or category are invalid", 400)
        if Product.query.filter_by(slug=slug).first():
            return error("Product slug already exists", 409)
        product = Product(
            name=name[:180],
            slug=slug[:200],
            description=str(payload.get("description", "")),
            price=price,
            compare_at_price=payload.get("compare_at_price"),
            image_url=payload.get("image_url"),
            cloudinary_public_id=payload.get("cloudinary_public_id"),
            sku=payload.get("sku"),
            stock=stock,
            weight_kg=weight_kg,
            category=category,
            is_featured=bool(payload.get("is_featured", False)),
        )
        db.session.add(product)
        db.session.commit()
        return jsonify(item=product.to_dict()), 201

    @app.get("/api/admin/products")
    @admin_required
    def admin_products():
        rows = Product.query.join(Category).order_by(Product.name).all()
        return jsonify(items=[row.to_dict() for row in rows], count=len(rows))

    @app.patch("/api/admin/products/<int:product_id>")
    @admin_required
    def update_product(product_id):
        product = db.session.get(Product, product_id)
        if not product:
            return error("Product not found", 404)
        payload = request.get_json(silent=True) or {}
        for key in ("name", "description", "sku", "image_url", "cloudinary_public_id"):
            if key in payload:
                setattr(product, key, payload[key])
        for key in ("is_active", "is_featured"):
            if key in payload:
                setattr(product, key, bool(payload[key]))
        try:
            if "price" in payload:
                product.price = Decimal(str(payload["price"]))
            if "compare_at_price" in payload:
                product.compare_at_price = Decimal(str(payload["compare_at_price"])) if payload["compare_at_price"] is not None else None
            if "stock" in payload:
                product.stock = int(payload["stock"])
            if "weight_kg" in payload:
                product.weight_kg = Decimal(str(payload["weight_kg"])) if payload["weight_kg"] not in (None, "") else None
            if "category_id" in payload:
                category = db.session.get(Category, int(payload["category_id"]))
                if not category:
                    return error("Category not found", 404)
                product.category = category
        except (TypeError, ValueError, InvalidOperation):
            return error("Invalid price, stock, weight, or category_id", 400)
        if product.price < 0 or product.stock < 0:
            return error("Price and stock cannot be negative", 400)
        if product.weight_kg is not None and (product.weight_kg <= 0 or product.weight_kg > 1000):
            return error("Product weight must be between 0 and 1000 kg", 400)
        db.session.commit()
        return jsonify(item=product.to_dict())

    @app.post("/api/admin/uploads")
    @admin_required
    def upload_image():
        image = request.files.get("image")
        if image is None:
            return error("Choose an image file", 400)
        if image.mimetype not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
            return error("Supported formats are JPEG, PNG, WebP, and GIF", 415)
        if not os.getenv("CLOUDINARY_URL"):
            return error("Cloudinary is not configured", 503)
        try:
            result = cloudinary.uploader.upload(
                image,
                folder="electrohub/products",
                resource_type="image",
                allowed_formats=["jpg", "jpeg", "png", "webp", "gif"],
            )
        except Exception:
            app.logger.exception("Cloudinary upload failed")
            return error("Image upload failed", 502)
        return jsonify(image_url=result["secure_url"], public_id=result["public_id"]), 201

    @app.errorhandler(RequestEntityTooLarge)
    def request_too_large(_exc):
        return error("Image is larger than the configured upload limit", 413)

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
