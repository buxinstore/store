# ElectroHub storefront

A static storefront frontend with a Flask API backend, PostgreSQL/Neon storage, and Cloudinary image uploads.

## Structure

- `index.html`, `assets/`, `config.js`: static storefront published by GitHub Pages.
- `backend/`: Flask REST API, PostgreSQL models, demo seed data, and Cloudinary uploader.
- `render.yaml`: Render Blueprint for the Flask web service and scheduled shipping refresh job.
- `.github/workflows/pages.yml`: publishes only the static frontend files.

## Local backend

See [backend/README.md](backend/README.md) for setup, environment variables, API routes, and the Neon/Cloudinary/Render steps. The API uses SQLite for local startup when `DATABASE_URL` is unset; production expects the Neon PostgreSQL URL.

## Deployment sequence

1. Configure Neon and Cloudinary, then add their credentials to the Render service environment.
2. Push this repository to GitHub and deploy `render.yaml` from Render as a Blueprint.
3. Set the GitHub repository variable `API_BASE_URL` to the Render API origin, enable Pages with GitHub Actions, and push to `main`.
4. Set `FRONTEND_ORIGINS` in Render to the GitHub Pages origin.

The API supports product/category browsing, authenticated admin endpoints, Cloudinary image uploads, and database-backed cash-on-delivery orders. Online card/UPI checkout remains disabled until a payment provider is selected and configured. Admin API credentials and Cloudinary/Neon secrets belong only in Render environment variables.
# store
