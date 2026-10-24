"""Idempotently copy legacy bot-ID keyed MongoDB records to WZML-X v3 partitions.

Legacy documents and collections are never deleted. Existing partition values win,
while fields that exist only in the legacy document are retained.
"""

import argparse
import copy
import hashlib

COLLECTIONS = ("users", "rss", "pm_users", "tasks", "streams", "playlists", "plugins")
SETTINGS_COLLECTIONS = ("config", "aria2c", "qbittorrent", "deployConfig", "files", "nzb")


def partition_id(bot_id):
    raw = hashlib.sha256(b"wzmlx_v3_db_partition_salt" + str(bot_id).encode()).hexdigest()
    return f"p_{raw[:24]}"


def merge_documents(legacy, current):
    """Merge recursively, keeping current values wherever both versions define one."""
    merged = copy.deepcopy(legacy)
    for key, current_value in current.items():
        old_value = merged.get(key)
        if isinstance(old_value, dict) and isinstance(current_value, dict):
            merged[key] = merge_documents(old_value, current_value)
        else:
            merged[key] = copy.deepcopy(current_value)
    return merged


def _copy_doc(source, target, target_id=None, dry_run=False):
    legacy = copy.deepcopy(source)
    if target_id is not None:
        legacy["_id"] = target_id
    current = target.find_one({"_id": legacy["_id"]})
    if current is None:
        if not dry_run:
            target.insert_one(legacy)
        return "inserted"
    merged = merge_documents(legacy, current)
    if merged == current:
        return "unchanged"
    if not dry_run:
        target.replace_one({"_id": current["_id"]}, merged)
    return "merged"


def migrate_database(database, bot_id, dry_run=True):
    """Copy one bot's legacy records; returns counts without exposing user IDs."""
    partition = partition_id(bot_id)
    counts = {"inserted": 0, "merged": 0, "unchanged": 0, "missing": 0}
    existing = set(database.list_collection_names())

    for base in COLLECTIONS:
        legacy_name = f"{base}.{bot_id}"
        if legacy_name not in existing:
            continue
        source = database[legacy_name]
        target = database[f"{base}.{partition}"]
        for document in source.find({}):
            counts[_copy_doc(document, target, dry_run=dry_run)] += 1

    for base in SETTINGS_COLLECTIONS:
        collection_name = f"settings.{base}"
        if collection_name not in existing:
            continue
        collection = database[collection_name]
        source = collection.find_one({"_id": str(bot_id)})
        if source is None:
            source = collection.find_one({"_id": bot_id})
        if source is None:
            continue
        counts[_copy_doc(source, collection, target_id=partition, dry_run=dry_run)] += 1

    return partition, counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the additive migration")
    args = parser.parse_args()
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from config import BOT_TOKEN, DATABASE_URL
    from pymongo import MongoClient

    bot_id = BOT_TOKEN.split(":", 1)[0]
    with MongoClient(DATABASE_URL, serverSelectionTimeoutMS=15000) as client:
        client.admin.command("ping")
        partition, counts = migrate_database(client["wzmlx"], bot_id, dry_run=not args.apply)
    action = "Applied" if args.apply else "Dry run"
    print(f"{action}: partition={partition}; records={counts}")
    if not args.apply:
        print("Review counts, back up MongoDB, then rerun with --apply to write.")


if __name__ == "__main__":
    main()
