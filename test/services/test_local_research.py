import unittest
from unittest.mock import MagicMock, patch

from app.services import local_research


class TestLocalResearch(unittest.TestCase):
    def setUp(self):
        local_research.clear_cache()

    def tearDown(self):
        local_research.clear_cache()

    @staticmethod
    def response(pages):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"query": {"pages": pages}}
        return response

    @patch("app.services.local_research.requests.get")
    def test_wikipedia_research_is_bounded_and_keeps_source_provenance(self, get):
        get.return_value = self.response(
            [
                {
                    "pageid": 10,
                    "title": "Теплица",
                    "extract": "<b>Теплица</b> — сооружение. " + ("Факт " * 500),
                },
                {
                    "pageid": 11,
                    "title": "Досвечивание",
                    "extract": "Искусственное освещение растений.",
                },
                {
                    "pageid": 12,
                    "title": "Лишняя страница",
                    "extract": "Не должна использоваться при max_pages=2.",
                },
            ]
        )

        rows = local_research.wikipedia_research(
            "досветка теплицы",
            language="ru-RU",
            max_pages=2,
            max_chars_per_page=240,
            timeout=4,
        )

        self.assertEqual(len(rows), 2)
        self.assertLessEqual(len(rows[0]["extract"]), 240)
        self.assertNotIn("<b>", rows[0]["extract"])
        self.assertEqual(rows[0]["source"], "wikipedia:ru")
        self.assertEqual(
            rows[0]["source_url"], "https://ru.wikipedia.org/?curid=10"
        )
        call = get.call_args
        self.assertEqual(call.args[0], "https://ru.wikipedia.org/w/api.php")
        self.assertEqual(call.kwargs["params"]["gsrlimit"], 2)
        self.assertEqual(call.kwargs["params"]["gsrsearch"], "досветка теплицы")
        self.assertEqual(call.kwargs["timeout"], (3, 4))

    @patch("app.services.local_research.requests.get")
    def test_requested_language_falls_back_to_english(self, get):
        get.side_effect = [
            self.response([]),
            self.response(
                [
                    {
                        "pageid": 42,
                        "title": "Greenhouse",
                        "extract": "A greenhouse is a structure used to grow plants.",
                    }
                ]
            ),
        ]

        result = local_research.wikipedia_research(
            "редкие тепличные технологии",
            language="ru-RU",
            max_pages=2,
            fallback_languages=("en",),
            cache_ttl_seconds=60,
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["source"], "wikipedia:en")
        self.assertIn("ru.wikipedia.org", get.call_args_list[0].args[0])
        self.assertIn("en.wikipedia.org", get.call_args_list[1].args[0])

    @patch("app.services.local_research.requests.get")
    def test_retries_reuse_in_process_cache(self, get):
        get.return_value = self.response(
            [
                {
                    "pageid": 7,
                    "title": "Теплица",
                    "extract": "Теплица — сооружение для выращивания растений.",
                }
            ]
        )

        first = local_research.wikipedia_research(
            "теплица",
            language="ru",
            max_pages=1,
            cache_ttl_seconds=3600,
        )
        second = local_research.wikipedia_research(
            "теплица",
            language="ru",
            max_pages=1,
            cache_ttl_seconds=3600,
        )

        self.assertEqual(first, second)
        self.assertEqual(get.call_count, 1)
        self.assertIsNot(first, second)
        self.assertIsNot(first[0], second[0])

    @patch("app.services.local_research.requests.get")
    def test_failed_primary_language_does_not_block_working_fallback(self, get):
        import requests

        get.side_effect = [
            requests.ConnectionError("offline primary"),
            self.response(
                [
                    {
                        "pageid": 9,
                        "title": "Tomato",
                        "extract": "The tomato is the edible berry of a plant.",
                    }
                ]
            ),
        ]

        result = local_research.wikipedia_research(
            "томат",
            language="ru",
            max_pages=1,
            fallback_languages=("en",),
            cache_ttl_seconds=0,
        )

        self.assertEqual([row["source"] for row in result], ["wikipedia:en"])

    @patch("app.services.local_research.requests.get")
    def test_wikipedia_research_failure_is_typed_after_all_languages_fail(self, get):
        import requests

        get.side_effect = requests.Timeout("offline")
        with self.assertRaises(local_research.ResearchError):
            local_research.wikipedia_research(
                "теплица",
                fallback_languages=("en",),
                cache_ttl_seconds=0,
            )
        self.assertEqual(get.call_count, 2)

    def test_wikipedia_research_does_not_call_network_for_empty_topic(self):
        with patch("app.services.local_research.requests.get") as get:
            self.assertEqual(local_research.wikipedia_research("   "), [])
            get.assert_not_called()

    def test_invalid_language_starts_with_russian_then_uses_fallback(self):
        with patch("app.services.local_research.requests.get") as get:
            get.side_effect = [self.response([]), self.response([])]
            local_research.wikipedia_research(
                "greenhouse",
                language="../../evil",
                fallback_languages=("en",),
                cache_ttl_seconds=0,
            )
            self.assertEqual(
                get.call_args_list[0].args[0],
                "https://ru.wikipedia.org/w/api.php",
            )
            self.assertEqual(
                get.call_args_list[1].args[0],
                "https://en.wikipedia.org/w/api.php",
            )


if __name__ == "__main__":
    unittest.main()
