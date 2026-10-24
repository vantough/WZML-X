import unittest
from urllib.parse import parse_qs, urlparse

from plugins.nzb_search.url_utils import authenticated_download_url


class ProwlarrDownloadUrlTests(unittest.TestCase):
    def test_rewrites_container_local_link_to_configured_service(self):
        from unittest.mock import patch

        with patch("plugins.nzb_search.url_utils.gethostbyname", return_value="172.20.0.5"):
            result = authenticated_download_url(
                "http://127.0.0.1:9696/1/download?link=release&file=item.nzb",
                "http://prowlarr:9696",
                "test-api-key",
            )
        parsed = urlparse(result)
        self.assertEqual(parsed.netloc, "172.20.0.5:9696")
        self.assertEqual(parsed.path, "/1/download")
        self.assertEqual(parse_qs(parsed.query)["link"], ["release"])
        self.assertEqual(parse_qs(parsed.query)["file"], ["item.nzb"])
        self.assertEqual(parse_qs(parsed.query)["apikey"], ["test-api-key"])

    def test_preserves_valid_service_url_and_adds_key(self):
        from unittest.mock import patch

        with patch("plugins.nzb_search.url_utils.gethostbyname", return_value="172.20.0.5"):
            result = authenticated_download_url(
                "http://prowlarr:9696/1/download?link=release",
                "http://prowlarr:9696",
                "test-api-key",
            )
        self.assertEqual(urlparse(result).netloc, "172.20.0.5:9696")
        self.assertEqual(parse_qs(urlparse(result).query)["apikey"], ["test-api-key"])

    def test_does_not_send_prowlarr_key_to_external_download_host(self):
        result = authenticated_download_url(
            "https://indexer.example/item.nzb",
            "http://prowlarr:9696",
            "test-api-key",
        )
        parsed = urlparse(result)
        self.assertEqual(parsed.netloc, "indexer.example")
        self.assertNotIn("apikey", parse_qs(parsed.query))


if __name__ == "__main__":
    unittest.main()

class NzbMirrorCommandTests(unittest.TestCase):
    def test_url_is_positional_before_upload_destination_option(self):
        from shlex import split
        from plugins.nzb_search.url_utils import nzb_mirror_command

        url = "http://prowlarr:9696/1/download?link=release&file=item.nzb"
        parts = split(nzb_mirror_command(url))
        self.assertEqual(parts, ["/nzbmirror", url, "-up", "gd"])
