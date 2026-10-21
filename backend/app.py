import base64
import hmac
import hashlib
import json
import os
from datetime import datetime, timezone
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

from models import Category, Order, OrderItem, PaymentAttempt, Product, StoreSetting, db
from catalog_seed import seed_starter_catalog

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


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.getenv("SECRET_KEY", "local-development-secret-change-me"),
        SQLALCHEMY_DATABASE_URI=database_uri(),
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

    with app.app_context():
        db.create_all()
        # Additive, safe upgrades for databases created by earlier storefront versions.
        columns = {column["name"] for column in inspect(db.engine).get_columns("orders")}
        if "shipping_method" not in columns:
            with db.engine.begin() as connection:
                connection.execute(text("ALTER TABLE orders ADD COLUMN shipping_method VARCHAR(120) NOT NULL DEFAULT 'standard'"))
        if db.engine.dialect.name == "postgresql":
            with db.engine.begin() as connection:
                connection.execute(text("ALTER TABLE orders ALTER COLUMN address TYPE TEXT"))
                connection.execute(text("ALTER TABLE store_settings ALTER COLUMN value TYPE TEXT"))
        if Category.query.count() == 0 and Product.query.count() == 0:
            seed_starter_catalog(update_existing=False)

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

    def shipping_methods():
        setting = db.session.get(StoreSetting, "shipping_methods")
        if not setting:
            return DEFAULT_SHIPPING_METHODS
        try:
            methods = json.loads(setting.value)
        except (TypeError, ValueError):
            return DEFAULT_SHIPPING_METHODS
        return methods if isinstance(methods, list) else DEFAULT_SHIPPING_METHODS

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
            and bool(modem_webhook_secret)
            and os.getenv("PAYMENT_CURRENCY", "GMD").strip().upper() == "GMD",
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

    def storefront_base_url():
        origin = storefront_origin()
        if not origin:
            return ""
        configured = os.getenv("FRONTEND_URL", "").strip().rstrip("/")
        if configured:
            parsed = urlsplit(configured)
            configured_origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
            if configured_origin == origin and parsed.scheme in {"http", "https"}:
                return configured
        return origin

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
        selected_method = str(payload.get("shipping_method", "")).strip()
        method = next((item for item in shipping_methods() if item.get("id") == selected_method and item.get("enabled", True)), None)
        if not method:
            raise ValueError("Choose an available shipping method")
        try:
            shipping = Decimal(str(method.get("price", ""))).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            raise ValueError("The selected shipping method has an invalid price")
        if not shipping.is_finite() or shipping < 0 or shipping > Decimal("100000"):
            raise ValueError("The selected shipping method has an invalid price")
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
        order = Order(
            id=str(uuid4()), customer_name=str(payload["customer_name"]).strip()[:180],
            email=str(payload["email"]).strip()[:254], address=json.dumps({key: str(value).strip() for key, value in address.items()}, ensure_ascii=False),
            city=str(address["city"]).strip()[:120], postal_code=str(address["postal_code"]).strip()[:32], shipping_method=method["name"],
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
        return jsonify(mode=mode, providers=provider_status(mode), currency=os.getenv("PAYMENT_CURRENCY", "GMD").strip().upper(), shipping_methods=methods)

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
        frontend_url = storefront_base_url()
        if not frontend_url:
            return error("Storefront origin is not allowed. Configure FRONTEND_ORIGINS for the storefront URL.", 400)
        try:
            order = build_order(payload, provider)
            if provider == "modem" and order.total < Decimal("10.00"):
                db.session.rollback()
                return error("Modem Pay requires a minimum payment of GMD 10.00", 400)
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
        try:
            if provider == "modem":
                secret = payment_secret("modem", mode)
                callback_url = public_api_base_url() + "/api/payments/modem/webhook"
                result = provider_request("https://api.modempay.com/v1/payments", {
                    "data": {
                        "amount": float(Decimal(order.total)),
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
                token, api_base = paypal_access_token(mode)
                items = [{
                    "name": item.product_name[:127],
                    "quantity": str(item.quantity),
                    "unit_amount": {"currency_code": "GMD", "value": f"{Decimal(item.unit_price):.2f}"},
                } for item in order.items]
                purchase_unit = {
                    "reference_id": order.id,
                    "custom_id": order.id,
                    "amount": {
                        "currency_code": "GMD",
                        "value": f"{order.total:.2f}",
                        "breakdown": {
                            "item_total": {"currency_code": "GMD", "value": f"{order.subtotal:.2f}"},
                            **({"shipping": {"currency_code": "GMD", "value": f"{order.shipping:.2f}"}} if order.shipping else {}),
                        },
                    },
                    "items": items,
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
        return jsonify(order_id=order.id, payment_status=order.payment_status, status=order.status, total=float(order.total))

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
            if (payment.get("status") == "completed" and payment.get("currency") == "GMD"
                    and Decimal(str(payment.get("amount", 0))) == Decimal(order.total)):
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
            if capture.get("status") != "COMPLETED" or paid_amount != Decimal(order.total):
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
                    return error("Shipping prices must be between GMD 0 and GMD 100,000", 400)
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
        except (TypeError, ValueError, InvalidOperation):
            return error("Valid name, slug, price, stock, and category_id are required", 400)
        category = db.session.get(Category, category_id)
        if not name or not slug or price < 0 or stock < 0 or not category:
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
            if "category_id" in payload:
                category = db.session.get(Category, int(payload["category_id"]))
                if not category:
                    return error("Category not found", 404)
                product.category = category
        except (TypeError, ValueError, InvalidOperation):
            return error("Invalid price, stock, or category_id", 400)
        if product.price < 0 or product.stock < 0:
            return error("Price and stock cannot be negative", 400)
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
