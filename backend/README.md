# ElectroHub Flask API

Flask REST API for the storefront. PostgreSQL is the production database (Neon), Cloudinary stores product images, and Render runs the API. Local development falls back to SQLite so the app can start before a Neon database is provisioned.

## Local setup

1. Create a Neon project and copy its pooled PostgreSQL connection string. Keep the `sslmode=require` query parameter.
2. Create a Cloudinary product environment and copy its `CLOUDINARY_URL` from the dashboard.
3. Copy the repository-root `.env.example` to the repository-root `.env`, then fill in `DATABASE_URL`, `CLOUDINARY_URL`, `SECRET_KEY`, `ADMIN_USERNAME` and `ADMIN_PASSWORD`, `FRONTEND_ORIGINS`, and provider credentials. The backend loads this root `.env` even when started from `backend/`.
4. From this folder, install and run:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   python app.py
   ```

5. Upload the reference storefront crops and seed the starter catalog:

   ```powershell
   python upload_assets.py
   python seed_demo.py
   ```

   `upload_assets.py` creates `assets_manifest.json` with public image URLs. It contains no Cloudinary secret and can be regenerated if needed.

The API health check is `http://127.0.0.1:5000/api/health`.

To try the storefront against the local API, serve the project root in a second terminal (avoid opening `index.html` as a `file://` URL so browser CORS behaves like production):

```powershell
py -m http.server 5500
```

Open `http://127.0.0.1:5500`. Keep `FRONTEND_ORIGINS` set to the local origins listed in `.env.example`.
For a local API connection, temporarily set `window.ELECTROHUB_API_URL` in the root `config.js` to `http://127.0.0.1:5000`; the GitHub Pages workflow replaces the placeholder using the repository variable during deployment.

## Render deployment

The repository-root `render.yaml` describes the API web service. Deploy it as a Render Blueprint, then set the prompted values in the Render dashboard:

- `DATABASE_URL`: Neon pooled connection URI.
- `CLOUDINARY_URL`: Cloudinary server-side credential URI.
- `SECRET_KEY`: long, random value used to sign admin sessions.
- `ADMIN_USERNAME` and `ADMIN_PASSWORD`: private admin credentials.
- `FRONTEND_ORIGINS`: your GitHub Pages origin, such as `https://your-user.github.io` (origin only; no repository path).
- `FRONTEND_URL`: the full storefront base URL, including the GitHub Pages repository path when applicable (`https://buxinstore.github.io/store` for this repository).
- `MODEM_PUBLIC_KEY` for the Modem Pay dashboard public key. The current server-to-server integration does not use it.
- `MODEM_TEST_SECRET` and `MODEM_TEST_WEBHOOK_SECRET` for Modem Pay test mode; set the webhook endpoint to `/api/payments/modem/webhook`.
- `MODEM_LIVE_SECRET` and `MODEM_WEBHOOK_SECRET` for Modem Pay live mode. Set `PAYMENT_CURRENCY=USD`; storefront prices and order totals use USD, while Modem Pay checkout converts the final amount to GMD.
- `PAYPAL_SANDBOX_CLIENT_ID` and `PAYPAL_SANDBOX_SECRET` for PayPal sandbox; `PAYPAL_LIVE_CLIENT_ID` and `PAYPAL_LIVE_SECRET` for production.
- `ENABLE_LIVE_PAYMENTS`: leave `false` while testing. Set to `true` only when you are ready to enable live credentials; the admin mode selector cannot switch to live while this flag is off.
- `GROQ_API_KEY`: private Groq key used by Admin chat and backend shipping research. Keep it out of GitHub Pages and the browser.
- `SHIPPING_ESTIMATE_MAX_AGE_DAYS`, `SHIPPING_ESTIMATE_MAX_DESTINATIONS_PER_RUN`, and `SHIPPING_MAX_DESTINATIONS_PER_UTC_DAY` configure rate freshness and the daily destination budget. Each destination is researched separately for DHL, FedEx, and Community Courier; each provider request may retry up to three times.

The Blueprint defines a daily Render Cron job (`electrohub-shipping-refresh`, 03:00 UTC) that automatically processes up to ten destinations per run. It makes one sequential Groq search request per destination, saves DHL/FedEx results only when official-carrier citations support the response, and leaves Community Courier in manual review until its carrier identity is configured. Progress, retry destinations, and the round-robin cursor are stored in PostgreSQL. A 429 response honors `Retry-After` when present; only one retry is attempted per request by default, and unfinished destinations resume on the next daily run. Render Cron has a minimum monthly charge. The checked-in `render.yaml` defines the Cron service and its environment, but a dashboard-created web service does not automatically gain a Cron service unless it is managed by a synced Blueprint. Confirm that the Render service list contains `electrohub-shipping-refresh`; if not, the Blueprint must be synced once (or an equivalent Cron service created) before the schedule can execute.

The app starts with Gunicorn and checks `/api/health` during deploy. Never put database, Cloudinary, or admin secrets in the GitHub Pages files.

## GitHub Pages frontend

`.github/workflows/pages.yml` publishes only `index.html`, `config.js`, and `assets/`; the Python backend is excluded. Set the repository Actions variable `API_BASE_URL` to the Render service URL (for example, `https://electrohub-api.onrender.com`). Enable GitHub Pages with **Build and deployment → Source: GitHub Actions**. The workflow injects the public API URL into `config.js` at deploy time.

Without `API_BASE_URL`, the storefront keeps its local demo catalog, but checkout is unavailable. With it, checkout offers only Modem Pay hosted checkout and PayPal approval when the matching credentials are configured for the active server-side payment mode. Unconfigured providers are disabled; checkout never presents a demo payment or accepts an order without a payment provider. Payment mode defaults to test. The admin Store Settings page controls test or live mode; live mode can charge real customers. Provider credentials remain server-side.

## API

Public routes:

- `GET /api/health`
- `GET /api/categories`
- `GET /api/products?q=&category=&featured=&limit=`
- `GET /api/products/<id>`
- `GET /api/shipping/destinations`, `POST /api/shipping/estimate`
- `GET /api/shipping-adjustments/<token>`, `POST /api/shipping-adjustments/<token>`
- `POST /api/orders` (cash on delivery)
- `GET /api/payments/config`
- `POST /api/payments/checkout`
- `GET /api/payments/orders/<order-id>`
- `POST /api/payments/modem/webhook` (verified Modem Pay webhook)
- `GET /api/payments/paypal/return` and `/api/payments/paypal/cancel`

Admin routes require a bearer token from `POST /api/admin/login`:

- `GET /api/admin/products`, `POST /api/admin/products`, `PATCH /api/admin/products/<id>`
- `GET /api/admin/categories`, `POST /api/admin/categories`
- `GET /api/admin/orders`, `PATCH /api/admin/orders/<order-id>`
- `GET /api/admin/payment-settings`, `PUT /api/admin/payment-settings`
- `GET /api/admin/shipping`, `GET /api/admin/shipping/<id>/history`, `PUT /api/admin/shipping/override`, `PUT /api/admin/shipping/mode`, `GET /api/admin/shipping/excel-template`, `POST /api/admin/shipping/excel-preview`, `POST /api/admin/shipping/excel-confirm`
- `PUT /api/admin/orders/<id>/shipping-reconciliation`, `POST /api/admin/orders/<id>/shipping-reconciliation/resolve`
- `POST /api/admin/uploads` (`multipart/form-data`, field name `image`)

Admin tokens expire after eight hours. Use HTTPS in production. With `API_BASE_URL` configured, the storefront Admin page can sign in to this API, view live product/category/order data, edit product stock, and update order statuses. The login token is kept in the current browser session. Settings and account registration are still frontend demo-only.

## Payments and security

Online checkout collects the customer's full delivery details, including phone, country, street, city, region, and postal code, then offers the enabled shipping methods. Catalog, shipping, and order totals are managed in USD. The API validates the address and selected carrier and recalculates order totals before creating provider-hosted checkout. PayPal charges USD directly; Modern Pay converts the final USD amount to GMD using the current cached exchange rate and confirms that GMD amount through a signature-verified webhook. The app defaults to test mode, and live mode also requires `ENABLE_LIVE_PAYMENTS=true`. Use provider sandbox accounts and test payment methods for checkout tests; never enter card details into the storefront itself.

The admin login uses an expiring signed bearer token and environment-based credentials. Before launch, use unique production secrets, restrict CORS to the exact Pages origin, and review rate limits, password reset, order privacy, database backups, and migrations.

## Shipping base rates

Admin → Shipping Settings supports Manual Excel Mode and AI Research Mode. Manual Excel Mode pauses scheduled research. Download the complete `.xlsx` template, fill only the yellow provider price cells, upload for a row-by-row preview, and explicitly confirm import. Blank prices preserve saved values. Invalid rows block the entire import. Numeric zero is a real free-shipping rate and requires a separate confirmation; approval for checkout is a separate explicit checkbox. Unapproved imports are stored in manual review and cannot be used at checkout. Imported prices update the same `shipping_rates` records used by checkout, carry `excel_import` provenance, and preserve previous values in rate history. Excel-supplied rates are protected from AI updates until an administrator changes or replaces them. AI workers check the persisted mode before each provider save; if Manual Excel Mode was selected during a Groq request, its response is discarded. The worker uses the database-backed lock, and switching back to AI Research Mode lets a later scheduled batch resume eligible destinations.

Checkout stores one USD base price per 0.5 kg for each supported country and provider. Destinations without a cited researched rate or approved administrator-managed rate remain in manual review with no usable price. Every product needs a positive shipping weight in kilograms; enter weights in Admin → Products & Inventory. Checkout sums product weight × quantity and rounds upward to the next 0.5 kg (minimum 0.5 kg), then multiplies the billable unit count by the selected provider's saved base rate. It never searches or estimates a price at checkout. Missing or stale rates require manual confirmation. Each order saves a versioned snapshot of the destination, provider, weights, rate, units, price, sources, and calculation timestamp; later rate changes do not update existing orders.

The backend uses the same `GROQ_API_KEY` as Admin chat and Groq's Responses API with built-in browser search. Shipping research uses Groq's `openai/gpt-oss-20b` model because Groq currently supports built-in browser search on GPT-OSS; Admin chat keeps its existing automatic model selection unchanged. This is a Groq-hosted API request, not an OpenAI API integration. For each destination, one sequential request researches DHL and FedEx together for a 0.5 kg parcel, converts supported source currencies to USD using the reference FX endpoint, and retains only official carrier citations. Community Courier remains in manual review until it is identified. Missing citations, unverified prices, or an unavailable Groq key result in manual review; the system does not invent or substitute a zero charge. Research estimates are indicative rather than binding carrier quotes. Additional shipping charges require explicit customer approval. The app does not automatically collect post-shipment differences or issue provider refunds; existing reconciliation remains manual. Admin overrides require an explicit positive USD rate and are retained in rate history.

Database changes use safe additive startup migrations, following the existing `db.create_all()` pattern; this project does not use Alembic. Existing products remain without weights after migration, so populate them before accepting shipped orders. Install backend requirements (including openpyxl for `.xlsx` support), then run shipping unit tests from `backend/` with `python -m unittest discover -s tests`.

### Groq Admin chat

Set `GROQ_API_KEY` in the Render service environment variables. Admin has no key or model controls. For each chat request, the backend calls Groq's `/models` endpoint, preserves the returned order, filters out IDs containing `whisper`, `guard`, `tts`, `orpheus`, `playai`, or `embed`, and uses the first remaining ID for `/chat/completions`, matching the working local example. On startup, the backend removes any legacy Admin-saved Groq key and model from `store_settings`. The bounded chat history uses a fixed helpful-assistant system prompt. Keep provider secrets in environment variables, not source files.
