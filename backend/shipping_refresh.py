"""Render Cron entry point for the bounded daily shipping research refresh."""
import logging
import os

from app import app
from shipping import refresh_destinations


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

if __name__ == "__main__":
    with app.app_context():
        batch_size = int(os.getenv("SHIPPING_BATCH_SIZE", os.getenv("SHIPPING_ESTIMATE_MAX_DESTINATIONS_PER_RUN", "10")))
        job = refresh_destinations(maximum=batch_size)
        logging.info("Shipping refresh job=%s status=%s destinations=%s rates_saved=%s failed=%s pending=%s tokens=%s rate_limit_remaining=%s",
                     job.id, job.status, job.attempted, job.succeeded, job.failed, job.pending,
                     job.tokens_used, job.rate_limit_remaining)
        if job.status == "failed":
            raise SystemExit(1)
