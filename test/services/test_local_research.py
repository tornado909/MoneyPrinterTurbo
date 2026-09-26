import unittest
from unittest.mock import MagicMock, patch

from app.services import local_research


class TestLocalResearch(unittest.TestCase):
    @patch("app.services.local_research.requests.get")
    def test_wikipedia_research_is_bounded_and_keeps_source_provenance(self, get):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "query": {
                "pages": [
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
                        "extract": "Не должна запрашиваться при gsrlimit=2.",
                    },
                ]
            }
        }
        get.return_value = response

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
        self.assertEqual(
            rows[0]["source_url"], "https://ru.wikipedia.org/?curid=10"
        )
        call = get.call_args
        self.assertEqual(call.args[0], "https://ru.wikipedia.org/w/api.php")
        self.assertEqual(call.kwargs["params"]["gsrlimit"], 2)
        self.assertEqual(call.kwargs["params"]["gsrsearch"], "досветка теплицы")
        self.assertEqual(call.kwargs["timeout"], (3, 4))

    @patch("app.services.local_research.requests.get")
    def test_wikipedia_research_failure_is_typed(self, get):
        import requests

        get.side_effect = requests.Timeout("offline")
        with self.assertRaises(local_research.ResearchError):
            local_research.wikipedia_research("теплица")

    def test_wikipedia_research_does_not_call_network_for_empty_topic(self):
        with patch("app.services.local_research.requests.get") as get:
            self.assertEqual(local_research.wikipedia_research("   "), [])
            get.assert_not_called()

    def test_invalid_language_falls_back_to_russian(self):
        with patch("app.services.local_research.requests.get") as get:
            response = MagicMock()
            response.raise_for_status.return_value = None
            response.json.return_value = {"query": {"pages": []}}
            get.return_value = response
            local_research.wikipedia_research("greenhouse", language="../../evil")
            self.assertEqual(
                get.call_args.args[0], "https://ru.wikipedia.org/w/api.php"
            )


if __name__ == "__main__":
    unittest.main()
