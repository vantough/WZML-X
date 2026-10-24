import unittest

from scripts.migrate_legacy_db import merge_documents, migrate_database, partition_id


class FakeCollection:
    def __init__(self, documents=()):
        self.documents = {doc["_id"]: dict(doc) for doc in documents}

    def find(self, query):
        return list(self.documents.values())

    def find_one(self, query):
        return self.documents.get(query["_id"])

    def insert_one(self, document):
        self.documents[document["_id"]] = dict(document)

    def replace_one(self, query, document):
        self.documents[query["_id"]] = dict(document)


class FakeDatabase:
    def __init__(self, collections):
        self.collections = collections

    def list_collection_names(self):
        return list(self.collections)

    def __getitem__(self, name):
        if name not in self.collections:
            self.collections[name] = FakeCollection()
        return self.collections[name]


class LegacyMigrationTests(unittest.TestCase):
    def test_partition_id_matches_v3_namespace(self):
        self.assertEqual(
            partition_id("6018518807"),
            "p_1e5145bfd32ec7b5c6d2cf85",
        )

    def test_merge_keeps_current_values_and_fills_missing_legacy_fields(self):
        legacy = {"_id": 3, "prefs": {"old": True, "shared": "old"}, "count": 1}
        current = {"_id": 3, "prefs": {"new": True, "shared": "current"}, "seen": True}

        merged = merge_documents(legacy, current)

        self.assertEqual(merged, {
            "_id": 3,
            "prefs": {"old": True, "new": True, "shared": "current"},
            "count": 1,
            "seen": True,
        })
        self.assertEqual(legacy["prefs"]["shared"], "old")

    def test_migration_copies_without_deleting_legacy_records(self):
        bot_id = "6018518807"
        partition = partition_id(bot_id)
        collections = {
            f"users.{bot_id}": FakeCollection(
                [{"_id": 7, "prefs": {"legacy": True, "shared": "old"}}]
            ),
            f"users.{partition}": FakeCollection(
                [{"_id": 7, "prefs": {"current": True, "shared": "new"}}]
            ),
            "settings.config": FakeCollection(
                [{"_id": bot_id, "SUDO_USERS": "1 2"}, {"_id": partition, "OWNER_ID": 9}]
            ),
        }
        db = FakeDatabase(collections)

        result_partition, counts = migrate_database(db, bot_id, dry_run=False)

        self.assertEqual(result_partition, partition)
        self.assertEqual(collections[f"users.{bot_id}"].documents[7]["prefs"]["shared"], "old")
        self.assertEqual(
            collections[f"users.{partition}"].documents[7],
            {"_id": 7, "prefs": {"legacy": True, "current": True, "shared": "new"}},
        )
        self.assertEqual(
            collections["settings.config"].documents[partition],
            {"_id": partition, "SUDO_USERS": "1 2", "OWNER_ID": 9},
        )
        self.assertEqual(counts["merged"], 2)


if __name__ == "__main__":
    unittest.main()
