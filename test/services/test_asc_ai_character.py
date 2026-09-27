import unittest
from unittest.mock import MagicMock, patch

from app.services import asc_ai_character


class TestAscAICharacterHub(unittest.TestCase):
    @patch("app.services.asc_ai_character.requests.get")
    def test_character_id_is_url_encoded_and_manifest_is_visual_only(self, get):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "character_id": "aria/main",
            "name": "Aria",
            "persona": {
                "description": "must not be copied into production identity",
                "memory_anchors": ["private narrative state"],
            },
            "appearance": {
                "description": "adult woman, platinum short hair, amber eye",
                "forbidden_traits": ["long red hair"],
            },
            "visual_identity": {
                "kind": "REFERENCE_ONLY",
                "reference_artifact_ids": ["art-face", "art-body"],
                "reference_captions": ["face close-up", "full body"],
                "lora_resource_ids": [],
                "preferred_workflow": "krea_identity_reference.v1",
            },
            "image_generation": {
                "model_id": "model-old",
                "grounding_px": 1024,
                "ref_boost": 1.1,
                "forbidden_prompt_terms": ["identity drift"],
            },
        }
        get.return_value = response

        identity = asc_ai_character.get_character(
            "http://character-chat:8096", "aria/main"
        )

        self.assertTrue(
            get.call_args.args[0].endswith("/characters/aria%2Fmain")
        )
        self.assertEqual(identity["character_id"], "aria/main")
        self.assertEqual(
            identity["reference_artifact_ids"], ["art-face", "art-body"]
        )
        self.assertNotIn("persona", identity)
        self.assertNotIn("memory_anchors", identity)

    @patch("app.services.asc_ai_character.requests.get")
    def test_character_catalog_is_bounded_and_deduplicated(self, get):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "characters": ["aria", "aria", "", "nika", *[f"c{i}" for i in range(300)]]
        }
        get.return_value = response
        rows = asc_ai_character.list_characters("http://character-chat:8096")
        self.assertEqual(rows[:2], ["aria", "nika"])
        self.assertLessEqual(len(rows), 200)

    def test_malformed_numeric_hints_are_safely_bounded(self):
        identity = asc_ai_character.production_identity(
            {
                "character_id": "broken-hints",
                "appearance": {"description": "recognizable adult character"},
                "image_generation": {
                    "grounding_px": "not-a-number",
                    "ref_boost": "NaN",
                },
            }
        )
        self.assertEqual(identity["grounding_px"], 1024)
        self.assertEqual(identity["ref_boost"], 1.0)

    def test_manifest_without_visual_identity_is_rejected(self):
        with self.assertRaises(asc_ai_character.CharacterHubError):
            asc_ai_character.production_identity(
                {
                    "character_id": "empty",
                    "name": "Empty",
                    "appearance": {"description": ""},
                }
            )


if __name__ == "__main__":
    unittest.main()
