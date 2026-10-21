"""Render Cron entry point for the bounded daily shipping research refresh."""
import logging
import os

from app import app
from shipping import refresh_destinations


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

if __name__ == "__main__":
    with app.app_context():
        job = refresh_destinations(maximum=int(os.getenv("SHIPPING_ESTIMATE_MAX_DESTINATIONS_PER_RUN", "10")))
        logging.info("Shipping refresh job=%s status=%s attempted=%s succeeded=%s failed=%s",
                     job.id, job.status, job.attempted, job.succeeded, job.failed)
        if job.status == "failed":
            raise SystemExit(1)
