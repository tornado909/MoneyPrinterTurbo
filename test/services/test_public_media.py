import unittest

from app.services import public_media


class TestPublicMediaLicensePolicy(unittest.TestCase):
    def test_allows_commercial_editable_licenses(self):
        allowed = (
            "CC0",
            "CC BY 4.0",
            "CC-BY-3.0",
            "CC BY-SA 4.0",
            "CC-BY-SA-2.5",
            "Public domain",
            "PDM",
        )
        for value in allowed:
            with self.subTest(value=value):
                self.assertTrue(public_media._license_is_reusable(value))

    def test_rejects_noncommercial_and_no_derivatives_variants(self):
        blocked = (
            "CC BY-NC 4.0",
            "CC BY-NC-SA 3.0",
            "CC BY-ND 4.0",
            "CC BY-NC-ND 4.0",
            "Creative Commons Attribution-NonCommercial",
            "CC BY No-Derivatives",
        )
        for value in blocked:
            with self.subTest(value=value):
                self.assertFalse(public_media._license_is_reusable(value))

    def test_rejects_unknown_or_empty_license(self):
        for value in ("", "All rights reserved", "Unknown", None):
            with self.subTest(value=value):
                self.assertFalse(public_media._license_is_reusable(value))


if __name__ == "__main__":
    unittest.main()
