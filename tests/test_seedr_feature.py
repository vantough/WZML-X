import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from bot.core.config_manager import Config
from bot.modules.bot_settings import SEEDR_VARS, seedr_display_value
from bot.core.seedr_api import SeedrApiClient, SeedrApiError
from bot.helper.ext_utils.torrent_metadata import (
    TorrentMetadataError,
    torrent_total_size,
)
from bot.helper.mirror_leech_utils.download_utils.seedr_download import (
    _content_files,
    _quota_space,
    _seedr_api_files,
    _task_finished,
    _task_id,
    _task_progress,
    should_delete_seedr_after_upload,
)
from bot.helper.mirror_leech_utils.download_utils import seedr_download


def bencode(value):
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        pairs = sorted(value.items())
        return b"d" + b"".join(bencode(key) + bencode(item) for key, item in pairs) + b"e"
    raise TypeError(value)


class SeedrFeatureTests(unittest.TestCase):
    def test_bsetting_includes_every_seedr_config_and_masks_credentials(self):
        self.assertEqual(
            set(SEEDR_VARS),
            {"DISABLE_SEEDR"}
            | {key for key in Config.get_all() if key.startswith("SEEDR_")},
        )
        self.assertEqual(seedr_display_value("SEEDR_API_TOKEN", "secret"), "configured")
        self.assertEqual(seedr_display_value("SEEDR_PASSWORD", "secret"), "configured")
        self.assertEqual(seedr_display_value("SEEDR_API_TOKEN", ""), "not set")

    def test_torrent_total_size_single_file(self):
        data = bencode({b"info": {b"length": 12345, b"name": b"video.mkv"}})
        self.assertEqual(torrent_total_size(data), 12345)

    def test_torrent_total_size_multifile(self):
        data = bencode(
            {
                b"info": {
                    b"files": [
                        {b"length": 100, b"path": [b"a.mkv"]},
                        {b"length": 250, b"path": [b"b.srt"]},
                    ],
                    b"name": b"bundle",
                }
            }
        )
        self.assertEqual(torrent_total_size(data), 350)

    def test_invalid_torrent_metadata_is_rejected(self):
        for data in (b"", b"d4:infoi1ee", b"d4:infod6:lengthi-1eee"):
            with self.subTest(data=data), self.assertRaises(TorrentMetadataError):
                torrent_total_size(data)

    def test_quota_parses_nested_storage_without_using_bandwidth(self):
        quota = {
            "bandwidth": {"total": 9999, "used": 8000},
            "storage": {"total": 1000, "used": 300},
        }
        self.assertEqual(_quota_space(quota), (1000, 300))

    def test_quota_accepts_available_bytes(self):
        self.assertEqual(_quota_space({"storage": {"available_bytes": 777}}), (777, 0))

    def test_task_id_accepts_seedr_response_variants(self):
        self.assertEqual(_task_id({"torrent_id": 123}), "123")
        self.assertEqual(
            _task_id({"result": {"user_torrent_id": "task-456"}}),
            "task-456",
        )
        self.assertEqual(_task_id({"task": {"id": "task-789"}}), "task-789")
        self.assertIsNone(_task_id({"ok": True}))

    def test_task_progress_and_completion_shapes(self):
        task, progress = _task_progress(
            {"task": {"status": "downloading", "progress": 0.25}}
        )
        self.assertEqual(task["status"], "downloading")
        self.assertEqual(progress, 25)
        self.assertTrue(_task_finished({"status": "completed"}, 0))
        self.assertTrue(_task_finished({"status": "downloading"}, 100))

    def test_failed_task_raises_with_status(self):
        with self.assertRaisesRegex(ValueError, "failed"):
            _task_finished({"status": "failed"}, 20)

    def test_seedr_cleanup_requires_confirmed_upload_success(self):
        self.assertFalse(should_delete_seedr_after_upload(False, True))
        self.assertFalse(should_delete_seedr_after_upload(True, False))
        self.assertTrue(should_delete_seedr_after_upload(True, True))


class SeedrContentShapeTests(unittest.TestCase):
    def test_nested_seedr_content_collections_are_traversed(self):
        payload = {
            "result": {
                "entries": [
                    {
                        "type": "file",
                        "id": "file-id",
                        "name": "movie.mp4",
                    }
                ]
            }
        }
        self.assertEqual(
            list(_content_files(payload)),
            [{"type": "file", "id": "file-id", "name": "movie.mp4"}],
        )

    def test_task_container_id_is_not_treated_as_a_file(self):
        task = {
            "id": "task-id",
            "name": "Sintel",
            "type": "torrent",
            "files": [
                {
                    "id": "file-id",
                    "name": "Sintel.mp4",
                    "size": 123,
                }
            ],
        }
        self.assertEqual(
            list(_content_files(task)),
            [{"id": "file-id", "name": "Sintel.mp4", "size": 123}],
        )


class SeedrFileResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_file_url_tries_folder_file_id_after_unknown_id_404(self):
        class Client:
            async def get_task_contents(self, task_id):
                return {
                    "files": [
                        {
                            "id": "task-local-id",
                            "folder_file_id": "drive-file-id",
                            "name": "Sintel.mp4",
                            "size": 123,
                        }
                    ]
                }

            async def get_file_url(self, file_id):
                if file_id == "task-local-id":
                    raise SeedrApiError("not found", status_code=404)
                return {"url": "https://download.example/file"}

        contents, total_size, files = await _seedr_api_files(Client(), "task-1")
        self.assertEqual(total_size, 123)
        self.assertEqual(len(files), 1)
        self.assertEqual(contents[0]["url"], "https://download.example/file")
        self.assertEqual(contents[0]["filename"], "Sintel.mp4")

    async def test_file_url_uses_direct_url_in_task_contents(self):
        class Client:
            async def get_task_contents(self, task_id):
                return {"files": [{"id": "file-1", "name": "movie.mp4", "url": "https://download.example/direct"}]}

            async def get_file_url(self, file_id):
                raise AssertionError("direct URL should avoid another API request")

        contents, _, _ = await _seedr_api_files(Client(), "task-1")
        self.assertEqual(contents[0]["url"], "https://download.example/direct")


class SeedrFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_token_uses_existing_qbittorrent_path(self):
        listener = unittest.mock.Mock()
        listener.link = "magnet:?xt=urn:btih:0123456789abcdef"
        listener.is_cancelled = False
        fallback = AsyncMock()

        with patch.object(seedr_download.Config, "SEEDR_API_TOKEN", ""):
            await seedr_download.add_seedr_first_qb(
                listener, "/tmp/download", None, None, fallback
            )

        fallback.assert_awaited_once_with(listener, "/tmp/download", None, None)

    def test_seedr_env_file_loads_only_allowlisted_settings(self):
        old_values = {
            "DISABLE_SEEDR": Config.DISABLE_SEEDR,
            "SEEDR_API_TOKEN": Config.SEEDR_API_TOKEN,
            "SEEDR_FIRST_TIMEOUT": Config.SEEDR_FIRST_TIMEOUT,
        }
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as env_file:
                env_file.write(
                    "DISABLE_SEEDR=false\n"
                    "SEEDR_API_TOKEN=test-token\n"
                    "SEEDR_FIRST_TIMEOUT=120\n"
                    "BOT_TOKEN=must-not-load\n"
                )
                env_file.flush()
                with patch.dict(os.environ, {"SEEDR_CONFIG_FILE": env_file.name}, clear=False):
                    for key in (
                        "DISABLE_SEEDR",
                        "SEEDR_API_TOKEN",
                        "SEEDR_FIRST_TIMEOUT",
                    ):
                        os.environ.pop(key, None)
                    Config.load_seedr_env_file()
            self.assertFalse(Config.DISABLE_SEEDR)
            self.assertEqual(Config.SEEDR_API_TOKEN, "test-token")
            self.assertEqual(Config.SEEDR_FIRST_TIMEOUT, 120)
            self.assertNotEqual(Config.BOT_TOKEN, "must-not-load")
        finally:
            Config.DISABLE_SEEDR = old_values["DISABLE_SEEDR"]
            Config.SEEDR_API_TOKEN = old_values["SEEDR_API_TOKEN"]
            Config.SEEDR_FIRST_TIMEOUT = old_values["SEEDR_FIRST_TIMEOUT"]


class SeedrApiClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_uses_bearer_auth_and_parses_json(self):
        response = unittest.mock.Mock(status_code=200)
        response.json.return_value = {"ok": True}
        session = unittest.mock.AsyncMock()
        session.__aenter__.return_value.request = AsyncMock(return_value=response)

        with patch("bot.core.seedr_api.AsyncSession", return_value=session):
            client = SeedrApiClient("test-token")
            result = await client.get_quota()

        self.assertTrue(result["ok"])
        kwargs = session.__aenter__.return_value.request.await_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-token")

    async def test_add_task_submits_form_fields(self):
        response = unittest.mock.Mock(status_code=200)
        response.json.return_value = {"result": True, "torrent_id": "task-1"}
        session = unittest.mock.AsyncMock()
        session.__aenter__.return_value.request = AsyncMock(return_value=response)

        with patch("bot.core.seedr_api.AsyncSession", return_value=session):
            client = SeedrApiClient("test-token")
            result = await client.add_task("magnet:?xt=urn:btih:0123456789abcdef")

        self.assertEqual(result["torrent_id"], "task-1")
        kwargs = session.__aenter__.return_value.request.await_args.kwargs
        self.assertEqual(
            kwargs["data"],
            {
                "folder_id": 0,
                "torrent_magnet": "magnet:?xt=urn:btih:0123456789abcdef",
            },
        )
        self.assertNotIn("json", kwargs)

    async def test_http_failure_includes_safe_endpoint_diagnostics(self):
        response = unittest.mock.Mock(status_code=400, headers={"Content-Type": "application/json"})
        response.json.return_value = {
            "message": "invalid magnet magnet:?xt=urn:btih:SECRET and Bearer test-token"
        }
        session = unittest.mock.AsyncMock()
        session.__aenter__.return_value.request = AsyncMock(return_value=response)

        with patch("bot.core.seedr_api.AsyncSession", return_value=session):
            client = SeedrApiClient("test-token")
            with self.assertRaises(SeedrApiError) as raised:
                await client.add_task("magnet:?xt=urn:btih:SECRET")

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("POST /tasks returned HTTP 400", str(raised.exception))
        self.assertIn("application/json", str(raised.exception))
        self.assertNotIn("test-token", str(raised.exception))
        self.assertNotIn("SECRET", str(raised.exception))
        self.assertIn("magnet:[redacted]", str(raised.exception))

    async def test_request_accepts_no_content_delete_response(self):
        response = unittest.mock.Mock(status_code=204)
        session = unittest.mock.AsyncMock()
        session.__aenter__.return_value.request = AsyncMock(return_value=response)

        with patch("bot.core.seedr_api.AsyncSession", return_value=session):
            client = SeedrApiClient("test-token")
            self.assertEqual(await client.delete_task("task-1"), {})


if __name__ == "__main__":
    unittest.main()
