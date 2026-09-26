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
            video_source="asc_ai",
            director_enabled=True,
            subtitle_enabled=True,
            bgm_type="random",
        )
        asc_ai.validate_local_only(params)

    def test_local_only_rejects_cloud_video_source(self):
        params = SimpleNamespace(
            video_source="ofox",
            director_enabled=True,
            subtitle_enabled=True,
            bgm_type="random",
        )
        with self.assertRaisesRegex(asc_ai.AscAIError, "local-only"):
            asc_ai.validate_local_only(params)

    def test_local_only_requires_director_for_asc_ai_visuals(self):
        params = SimpleNamespace(
            video_source="asc_ai",
            director_enabled=False,
            subtitle_enabled=True,
            bgm_type="random",
        )
        with self.assertRaisesRegex(asc_ai.AscAIError, "Director"):
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

    @patch("app.services.asc_ai._request_json")
    @patch(
        "app.services.asc_ai.stage_for_qc",
        return_value="moneyprinterturbo/task/qc/scene.png",
    )
    def test_quality_control_requires_local_egress_contract(
        self, _stage, request_json
    ):
        request_json.return_value = {
            "external_egress": False,
            "provider": "local-qwen3-vl",
            "provider_model": "Qwen3VL-8B-Instruct-Q4_K_M.gguf",
            "production_qc": {
                "visual_summary": "greenhouse",
                "semantic_score": 0.91,
                "technical_score": 0.95,
                "catastrophic_artifacts": False,
                "issues": [],
                "retry_prompt": "",
                "confidence": 0.9,
            },
        }
        scene = {"scene_id": "scene_01", "visual_prompt": "greenhouse"}
        result = asc_ai.quality_control(
            "task", scene, "/tmp/unused.png", media_kind="image"
        )
        self.assertTrue(result["passed"])
        self.assertFalse(result["skipped"])

        request_json.return_value["external_egress"] = True
        with self.assertRaisesRegex(asc_ai.AscAIError, "egress"):
            asc_ai.quality_control(
                "task", scene, "/tmp/unused.png", media_kind="image"
            )

    @patch("app.services.asc_ai._workflow_catalog")
    def test_runtime_catalog_binding_uses_catalog_model(self, catalog):
        catalog.return_value = [
            {
                "workflow_id": "wan_i2v_default.v1",
                "purpose": "image_to_video",
                "model_id": "mdl_diffusion_model_runtime",
                "operator_only": False,
                "validation_state": "production",
                "default_resolution": "640x640",
                "defaults": {"length": 89},
            }
        ]
        row = asc_ai._video_binding()
        self.assertEqual(row["model_id"], "mdl_diffusion_model_runtime")
        self.assertEqual(row["defaults"]["length"], 89)


if __name__ == "__main__":
    unittest.main()
