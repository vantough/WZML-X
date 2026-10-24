"""Keep the WZML-X MongoDB cluster active with one daily heartbeat write."""

import logging
import time
from datetime import datetime, timezone

LOGGER = logging.getLogger("wzmlx.mongo_keepalive")
HEARTBEAT_ID = "wzmlx-mongo-keepalive"
DATABASE_NAME = "wzmlx"
COLLECTION_NAME = "service_keepalive"
INTERVAL_SECONDS = 24 * 60 * 60
RETRY_SECONDS = 5 * 60


def update_keepalive(database, now=None):
    """Upsert the heartbeat document without touching bot-owned collections."""
    timestamp = now or datetime.now(timezone.utc)
    return database[COLLECTION_NAME].update_one(
        {"_id": HEARTBEAT_ID},
        {"$set": {"service": "wzmlx", "last_seen": timestamp}},
        upsert=True,
    )


def run_once(database_url=None, client_factory=None, now=None):
    """Write one heartbeat and close the database client."""
    if database_url is None:
        from config import DATABASE_URL

        database_url = DATABASE_URL
    if client_factory is None:
        from pymongo import MongoClient

        client_factory = MongoClient

    client = client_factory(
        database_url,
        appname="WZML-X-Mongo-Keepalive",
        serverSelectionTimeoutMS=15000,
        connectTimeoutMS=10000,
    )
    try:
        client.admin.command("ping")
        return update_keepalive(client[DATABASE_NAME], now=now)
    finally:
        client.close()


def run_forever(interval_seconds=INTERVAL_SECONDS, sleep=time.sleep):
    """Heartbeat immediately, then daily; retry transient errors after five minutes."""
    while True:
        try:
            run_once()
            LOGGER.info("MongoDB keepalive write succeeded")
            sleep(interval_seconds)
        except Exception as exc:  # keep the service alive through network interruptions
            LOGGER.error("MongoDB keepalive failed (%s); retrying in five minutes", type(exc).__name__)
            sleep(RETRY_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_forever()
