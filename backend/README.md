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
- `FRONTEND_URL`: the full storefront base URL, including the GitHub Pages repository path when applicable (for example, `https://your-user.github.io/store`).
- `MODEM_TEST_SECRET` and `MODEM_TEST_WEBHOOK_SECRET` for Modem Pay test mode; set the webhook endpoint to `/api/payments/modem/webhook`.
- `MODEM_LIVE_SECRET` and `MODEM_WEBHOOK_SECRET` for Modem Pay live mode. Set `PAYMENT_CURRENCY=GMD`; Modem Pay checkout uses GMD.
- `PAYPAL_SANDBOX_CLIENT_ID` and `PAYPAL_SANDBOX_SECRET` for PayPal sandbox; `PAYPAL_LIVE_CLIENT_ID` and `PAYPAL_LIVE_SECRET` for production.
- `ENABLE_LIVE_PAYMENTS`: leave `false` while testing. Set to `true` only when you are ready to enable live credentials; the admin mode selector cannot switch to live while this flag is off.

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
- `POST /api/admin/uploads` (`multipart/form-data`, field name `image`)

Admin tokens expire after eight hours. Use HTTPS in production. With `API_BASE_URL` configured, the storefront Admin page can sign in to this API, view live product/category/order data, edit product stock, and update order statuses. The login token is kept in the current browser session. Settings and account registration are still frontend demo-only.

## Payments and security

Online checkout uses provider-hosted payment pages. Prices, inventory, shipping, and totals are recalculated by the API. Modem Pay payments are only marked paid after a signature-verified webhook confirms the amount and currency. PayPal orders are captured server-side after the buyer returns from approval. The app defaults to test mode, and live mode also requires `ENABLE_LIVE_PAYMENTS=true`. Use provider sandbox accounts and test payment methods for checkout tests; never enter card details into the storefront itself.

The admin login uses an expiring signed bearer token and environment-based credentials. Before launch, use unique production secrets, restrict CORS to the exact Pages origin, and review rate limits, password reset, order privacy, database backups, and migrations.
