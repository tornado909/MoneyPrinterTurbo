import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.config import config
from app.services import asc_ai


class TestAscAIIntegration(unittest.TestCase):
    def setUp(self):
        self.old_asc = dict(config.asc_ai)
        self.old_ui = dict(config.ui)
        self.old_app = dict(config.app)
        config.asc_ai.update({"enabled": True, "local_only": True})
        config.ui.update({"voice_mode": "tts", "tts_server": "chatterbox"})
        config.app.update(
            {"subtitle_provider": "whisper", "upload_post_auto_upload": False}
        )

    def tearDown(self):
        config.asc_ai.clear()
        config.asc_ai.update(self.old_asc)
        config.ui.clear()
        config.ui.update(self.old_ui)
        config.app.clear()
        config.app.update(self.old_app)

    def test_local_only_accepts_asc_ai_chatterbox_and_whisper(self):
        params = SimpleNamespace(
            video_source="asc_ai", subtitle_enabled=True, bgm_type="random"
        )
        asc_ai.validate_local_only(params)

    def test_local_only_rejects_cloud_video_source(self):
        params = SimpleNamespace(
            video_source="ofox", subtitle_enabled=True, bgm_type="random"
        )
        with self.assertRaisesRegex(asc_ai.AscAIError, "local-only"):
            asc_ai.validate_local_only(params)

    def test_director_terms_keep_scene_order(self):
        plan = {
            "scenes": [
                {"visual_prompt": "first"},
                {"visual_prompt": "second"},
                {"visual_prompt": ""},
            ]
        }
        self.assertEqual(asc_ai.director_terms(plan), ["first", "second"])

    @patch("app.services.asc_ai._request_json")
    def test_director_requires_local_scheduler_policy(self, request_json):
        request_json.return_value = {
            "local_only": False,
            "gpu_policy": "scheduler_managed",
            "script": "x",
            "scenes": [{"visual_prompt": "x"}],
        }
        params = SimpleNamespace(
            video_subject="topic",
            video_script="",
            video_language="ru-RU",
            video_aspect="9:16",
            director_target_duration_seconds=30,
            director_max_local_video_scenes=1,
            director_style="",
            director_audience="",
            director_purpose="",
        )
        with self.assertRaisesRegex(asc_ai.AscAIError, "local-only"):
            asc_ai.create_director_plan(params)


if __name__ == "__main__":
    unittest.main()
