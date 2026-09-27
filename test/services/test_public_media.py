import unittest

from app.services import public_media


class TestPublicMediaLicensePolicy(unittest.TestCase):
    def test_allows_commercial_editable_licenses(self):
        allowed = (
            "CC0",
            "CC BY 4.0",
            "CC-BY-3.0",
            "Public domain",
            "PDM",
        )
        for value in allowed:
            with self.subTest(value=value):
                self.assertTrue(public_media._license_is_reusable(value))

    def test_share_alike_requires_explicit_opt_in(self):
        for value in ("CC BY-SA 4.0", "CC-BY-SA-2.5"):
            with self.subTest(value=value):
                self.assertFalse(public_media._license_is_reusable(value))
                self.assertTrue(
                    public_media._license_is_reusable(
                        value,
                        allow_share_alike=True,
                    )
                )

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

    def test_relevance_beats_aspect_ratio_for_public_factual_media(self):
        query = "greenhouse tomato"
        relevant_landscape = {
            "title": "Tomatoes growing in greenhouse",
            "description": "Rows of tomato plants inside a commercial greenhouse",
            "credit": "",
            "width": 1600,
            "height": 900,
            "search_rank": 1,
        }
        irrelevant_vertical = {
            "title": "Glass building entrance",
            "description": "Vertical architectural detail",
            "credit": "",
            "width": 900,
            "height": 1600,
            "search_rank": 2,
        }

        relevant_score = public_media._search_score(
            relevant_landscape, 9 / 16, query
        )
        irrelevant_score = public_media._search_score(
            irrelevant_vertical, 9 / 16, query
        )

        self.assertLess(relevant_score, irrelevant_score)
        self.assertGreater(
            public_media._semantic_overlap(query, relevant_landscape),
            public_media._semantic_overlap(query, irrelevant_vertical),
        )

    def test_wikimedia_search_rank_breaks_equal_semantic_matches(self):
        query = "tomato plant"
        first = {
            "title": "Tomato plant",
            "description": "",
            "width": 1000,
            "height": 1000,
            "search_rank": 1,
        }
        later = dict(first, search_rank=5)
        self.assertLess(
            public_media._search_score(first, 1.0, query),
            public_media._search_score(later, 1.0, query),
        )

    def test_rejects_unknown_or_empty_license(self):
        for value in ("", "All rights reserved", "Unknown", None):
            with self.subTest(value=value):
                self.assertFalse(public_media._license_is_reusable(value))


if __name__ == "__main__":
    unittest.main()
