import unittest

from scripts.convert_legacy_config import convert_value, convert_values


class LegacyConfigTests(unittest.TestCase):
    def test_converts_common_config_types(self):
        self.assertTrue(convert_value("yes", False))
        self.assertEqual(convert_value("42", 0), 42)
        self.assertEqual(convert_value("a,b", []), ["a", "b"])
        self.assertEqual(convert_value("{'x': 1}", {}), {"x": 1})

    def test_maps_renamed_and_inverted_settings(self):
        converted, unsupported = convert_values(
            {"FSUB_IDS": "-1001,-1002", "DISABLE_DRIVE_LINK": "true", "OLD_ONLY": "keep"},
            {"FORCE_SUB_IDS": "", "SHOW_CLOUD_LINK": True},
        )
        self.assertEqual(converted["FORCE_SUB_IDS"], "-1001,-1002")
        self.assertFalse(converted["SHOW_CLOUD_LINK"])
        self.assertEqual(unsupported, {"OLD_ONLY": "keep"})


if __name__ == "__main__":
    unittest.main()
