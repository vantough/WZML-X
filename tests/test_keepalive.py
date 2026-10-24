import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, Mock

from keepalive import HEARTBEAT_ID, update_keepalive, run_once


class KeepaliveTests(unittest.TestCase):
    def test_updates_one_isolated_heartbeat_document(self):
        collection = Mock()
        database = {"service_keepalive": collection}
        now = datetime(2026, 10, 8, tzinfo=timezone.utc)

        update_keepalive(database, now=now)

        collection.update_one.assert_called_once_with(
            {"_id": HEARTBEAT_ID},
            {"$set": {"service": "wzmlx", "last_seen": now}},
            upsert=True,
        )

    def test_run_once_pings_writes_and_closes_client(self):
        client = MagicMock()
        factory = Mock(return_value=client)
        now = datetime(2026, 10, 8, tzinfo=timezone.utc)

        run_once("mongodb://example", client_factory=factory, now=now)

        client.admin.command.assert_called_once_with("ping")
        client.__getitem__.assert_called_once_with("wzmlx")
        client.close.assert_called_once()

    def test_client_closes_when_ping_fails(self):
        client = Mock()
        client.admin.command.side_effect = RuntimeError("offline")

        with self.assertRaises(RuntimeError):
            run_once("mongodb://example", client_factory=Mock(return_value=client))

        client.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
