import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import asgi
from app.config import config
from app.controllers.v1 import asc_ai as asc_ai_controller


class TestAscAIController(unittest.TestCase):
    def setUp(self):
        self.old_api_key = config.app.get("api_key", "")
        config.app["api_key"] = ""
        self.client = TestClient(asgi.app)

    def tearDown(self):
        config.app["api_key"] = self.old_api_key

    def test_capabilities_are_headless_and_local_first(self):
        response = self.client.get("/api/v1/asc-ai/capabilities")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(
            data["schema_version"], "mpt.asc-ai.capabilities.v1"
        )
        self.assertTrue(data["director"]["scheduler_managed"])
        self.assertTrue(data["video"]["aspect_compatibility_gate"])
        self.assertEqual(
            data["production"]["submit_endpoint"],
            "/api/v1/asc-ai/production",
        )
        self.assertTrue(data["production"]["uses_shared_task_manager"])

    @patch.object(
        asc_ai_controller.asc_ai,
        "health",
        return_value={"scheduler": {"status": "ok"}},
    )
    def test_health_endpoint_uses_asc_ai_service(self, health):
        response = self.client.get("/api/v1/asc-ai/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["data"]["scheduler"]["status"], "ok"
        )
        health.assert_called_once_with()

    @patch.object(
        asc_ai_controller.asc_ai,
        "list_characters",
        return_value=["aria", "nika"],
    )
    def test_character_catalog_endpoint(self, list_characters):
        response = self.client.get("/api/v1/asc-ai/characters")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["data"]["characters"], ["aria", "nika"]
        )
        list_characters.assert_called_once_with()

    @patch.object(asc_ai_controller.asc_ai, "create_director_plan")
    @patch.object(asc_ai_controller.asc_ai, "preflight")
    def test_director_plan_is_planning_only_and_supports_research_override(
        self, preflight, create_plan
    ):
        create_plan.return_value = {
            "schema_version": "mpt.director.v2",
            "script": "Локальный текст",
            "scenes": [
                {
                    "scene_id": "scene_01",
                    "visual_strategy": "LOCAL_IMAGE",
                    "visual_prompt": "modern greenhouse",
                }
            ],
        }
        response = self.client.post(
            "/api/v1/asc-ai/director/plan",
            json={
                "video_subject": "Как устроена теплица",
                "video_aspect": "9:16",
                "target_duration_seconds": 30,
                "max_local_video_scenes": 0,
                "character_id": "aria",
                "public_research_enabled": False,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["data"]["schema_version"], "mpt.director.v2"
        )
        params = preflight.call_args.args[0]
        self.assertEqual(params.video_source, "asc_ai")
        self.assertEqual(params.director_character_id, "aria")
        preflight.assert_called_once()
        self.assertEqual(preflight.call_args.kwargs["stop_at"], "script")
        self.assertEqual(
            create_plan.call_args.kwargs["settings_overrides"],
            {"public_research_enabled": False},
        )

    @patch.object(asc_ai_controller.video_controller, "create_task")
    @patch.object(asc_ai_controller.asc_ai, "preflight")
    def test_production_endpoint_maps_to_shared_video_task_queue(
        self, preflight, create_task
    ):
        create_task.return_value = {
            "status": 200,
            "message": "success",
            "data": {"task_id": "task-123"},
        }

        response = self.client.post(
            "/api/v1/asc-ai/production",
            json={
                "video_subject": "Как устроена современная теплица",
                "video_script": "",
                "video_language": "ru-RU",
                "video_aspect": "9:16",
                "target_duration_seconds": 35,
                "max_local_video_scenes": 1,
                "style": "cinematic realistic greenhouse documentary",
                "audience": "агрономы",
                "purpose": "обучающий ролик",
                "character_id": "aria",
                "public_research_enabled": False,
                "subtitle_enabled": True,
                "bgm_type": "random",
                "bgm_volume": 0.15,
                "video_count": 1,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["task_id"], "task-123")
        task_request = preflight.call_args.args[0]
        self.assertEqual(task_request.video_source, "asc_ai")
        self.assertTrue(task_request.director_enabled)
        self.assertEqual(task_request.director_target_duration_seconds, 35)
        self.assertEqual(task_request.director_character_id, "aria")
        self.assertFalse(task_request.director_public_research_enabled)
        self.assertEqual(task_request.bgm_type, "random")
        self.assertTrue(task_request.subtitle_enabled)
        preflight.assert_called_once()
        self.assertEqual(preflight.call_args.kwargs["stop_at"], "video")
        create_task.assert_called_once()
        self.assertIs(create_task.call_args.args[1], task_request)
        self.assertEqual(create_task.call_args.kwargs["stop_at"], "video")

    @patch.object(asc_ai_controller.video_controller, "create_task")
    @patch.object(
        asc_ai_controller.asc_ai,
        "preflight",
        side_effect=asc_ai_controller.asc_ai.AscAIError("scheduler unavailable"),
    )
    def test_production_endpoint_does_not_queue_when_preflight_fails(
        self, preflight, create_task
    ):
        response = self.client.post(
            "/api/v1/asc-ai/production",
            json={"video_subject": "Теплица"},
        )

        self.assertEqual(response.status_code, 503)
        self.assertIn("scheduler unavailable", response.json()["detail"])
        create_task.assert_not_called()
        preflight.assert_called_once()

    @patch.object(asc_ai_controller.video_controller, "create_task")
    @patch.object(asc_ai_controller.asc_ai, "preflight")
    @patch.object(
        asc_ai_controller,
        "_production_fingerprint",
        return_value="fingerprint-1",
    )
    def test_production_idempotency_claims_deterministic_task_once(
        self, _fingerprint, preflight, create_task
    ):
        key = "agent-run-0001"
        expected_task_id = asc_ai_controller._production_task_id(key)

        def create_response(_request, _body, _stop_at, **kwargs):
            return {
                "status": 200,
                "data": {"task_id": kwargs["task_id_override"]},
            }

        create_task.side_effect = create_response
        with patch.object(asc_ai_controller.sm, "state") as state:
            state.get_task.return_value = None
            state.create_task_if_absent.return_value = True
            response = self.client.post(
                "/api/v1/asc-ai/production",
                headers={"Idempotency-Key": key},
                json={"video_subject": "Теплица"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["task_id"], expected_task_id)
        preflight.assert_called_once()
        state.create_task_if_absent.assert_called_once()
        claim = state.create_task_if_absent.call_args
        self.assertEqual(claim.args[0], expected_task_id)
        self.assertEqual(claim.kwargs["request_fingerprint"], "fingerprint-1")
        self.assertEqual(claim.kwargs["idempotency_scope"], "asc-ai-production")
        self.assertEqual(claim.kwargs["request_stop_at"], "video")
        self.assertEqual(
            claim.kwargs["request_params"]["video_source"],
            "asc_ai",
        )
        self.assertTrue(
            claim.kwargs["request_params"]["director_enabled"],
        )
        create_task.assert_called_once()
        self.assertEqual(
            create_task.call_args.kwargs["task_id_override"],
            expected_task_id,
        )
        self.assertTrue(create_task.call_args.kwargs["initial_state_claimed"])

    @patch.object(asc_ai_controller.video_controller, "create_task")
    @patch.object(asc_ai_controller.asc_ai, "preflight")
    @patch.object(
        asc_ai_controller,
        "_production_fingerprint",
        return_value="fingerprint-1",
    )
    def test_production_idempotent_replay_skips_preflight_and_render_queue(
        self, _fingerprint, preflight, create_task
    ):
        key = "agent-run-0002"
        task_id = asc_ai_controller._production_task_id(key)
        with patch.object(asc_ai_controller.sm, "state") as state:
            state.get_task.return_value = {
                "task_id": task_id,
                "request_fingerprint": "fingerprint-1",
                "state": 4,
            }
            response = self.client.post(
                "/api/v1/asc-ai/production",
                headers={"Idempotency-Key": key},
                json={"video_subject": "Теплица"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["task_id"], task_id)
        preflight.assert_not_called()
        create_task.assert_not_called()
        state.create_task_if_absent.assert_not_called()

    @patch.object(asc_ai_controller.video_controller, "create_task")
    @patch.object(asc_ai_controller.asc_ai, "preflight")
    @patch.object(
        asc_ai_controller,
        "_production_fingerprint",
        return_value="fingerprint-new",
    )
    def test_production_idempotency_rejects_key_reuse_for_different_payload(
        self, _fingerprint, preflight, create_task
    ):
        key = "agent-run-0003"
        with patch.object(asc_ai_controller.sm, "state") as state:
            state.get_task.return_value = {
                "task_id": asc_ai_controller._production_task_id(key),
                "request_fingerprint": "fingerprint-old",
            }
            response = self.client.post(
                "/api/v1/asc-ai/production",
                headers={"Idempotency-Key": key},
                json={"video_subject": "Другая тема"},
            )

        self.assertEqual(response.status_code, 409)
        preflight.assert_not_called()
        create_task.assert_not_called()

    @patch.object(asc_ai_controller.video_controller, "create_task")
    @patch.object(asc_ai_controller.asc_ai, "preflight")
    @patch.object(
        asc_ai_controller,
        "_production_fingerprint",
        return_value="fingerprint-1",
    )
    def test_production_idempotency_lost_claim_race_becomes_replay(
        self, _fingerprint, preflight, create_task
    ):
        key = "agent-run-0004"
        task_id = asc_ai_controller._production_task_id(key)
        with patch.object(asc_ai_controller.sm, "state") as state:
            state.get_task.side_effect = [
                None,
                {
                    "task_id": task_id,
                    "request_fingerprint": "fingerprint-1",
                    "state": 4,
                },
            ]
            state.create_task_if_absent.return_value = False
            response = self.client.post(
                "/api/v1/asc-ai/production",
                headers={"Idempotency-Key": key},
                json={"video_subject": "Теплица"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["task_id"], task_id)
        preflight.assert_called_once()
        create_task.assert_not_called()

    @patch.object(asc_ai_controller.video_controller, "create_task")
    @patch.object(asc_ai_controller.asc_ai, "preflight")
    def test_production_rejects_invalid_idempotency_key_before_preflight(
        self, preflight, create_task
    ):
        response = self.client.post(
            "/api/v1/asc-ai/production",
            headers={"Idempotency-Key": "bad key"},
            json={"video_subject": "Теплица"},
        )
        self.assertEqual(response.status_code, 400)
        preflight.assert_not_called()
        create_task.assert_not_called()

    @patch.object(asc_ai_controller.video_controller, "create_task")
    @patch.object(asc_ai_controller.asc_ai, "preflight")
    def test_retry_production_reuses_durable_snapshot_and_links_tasks(
        self, preflight, create_task
    ):
        previous_params = asc_ai_controller.ProductionRequest(
            video_subject="Теплица",
            public_research_enabled=False,
        ).to_task_request().model_dump(warnings=False)
        create_task.return_value = {
            "status": 200,
            "message": "success",
            "data": {"task_id": "task-new"},
        }

        with patch.object(asc_ai_controller.video_controller.sm, "state") as state:
            state.get_task.return_value = {
                "task_id": "task-old",
                "retryable": True,
                "request_params": previous_params,
                "request_stop_at": "video",
            }
            state.patch_task.return_value = True
            response = self.client.post(
                "/api/v1/asc-ai/production/task-old/retry",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["task_id"], "task-new")
        task_request = preflight.call_args.args[0]
        self.assertEqual(task_request.video_source, "asc_ai")
        self.assertFalse(task_request.director_public_research_enabled)
        create_task.assert_called_once()
        self.assertEqual(
            create_task.call_args.kwargs["state_metadata"]["retry_of"],
            "task-old",
        )
        state.patch_task.assert_called_once_with(
            "task-old",
            retried_as="task-new",
            retryable=False,
            recovery_action="resubmitted",
        )

    @patch.object(asc_ai_controller.video_controller, "create_task")
    @patch.object(asc_ai_controller.asc_ai, "preflight")
    def test_retry_production_rejects_non_retryable_task(
        self, preflight, create_task
    ):
        with patch.object(asc_ai_controller.video_controller.sm, "state") as state:
            state.get_task.return_value = {
                "task_id": "task-old",
                "retryable": False,
            }
            response = self.client.post(
                "/api/v1/asc-ai/production/task-old/retry",
            )

        self.assertEqual(response.status_code, 409)
        preflight.assert_not_called()
        create_task.assert_not_called()

    def test_production_request_rejects_preset_without_local_filename(self):
        response = self.client.post(
            "/api/v1/asc-ai/production",
            json={
                "video_subject": "Теплица",
                "bgm_type": "preset",
                "bgm_file": "",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("bgm_file", response.text)

    @patch.object(asc_ai_controller.video_controller, "create_task")
    @patch.object(asc_ai_controller.asc_ai, "preflight")
    def test_production_normalizes_old_cloud_voice_to_local_chatterbox(
        self, preflight, create_task
    ):
        old_tts = config.ui.get("tts_server")
        old_voice = config.ui.get("voice_name")
        try:
            config.ui["tts_server"] = "azure-tts-v1"
            config.ui["voice_name"] = "ru-RU-SvetlanaNeural"
            create_task.return_value = {
                "status": 200,
                "message": "success",
                "data": {"task_id": "task-voice"},
            }
            response = self.client.post(
                "/api/v1/asc-ai/production",
                json={"video_subject": "Теплица"},
            )
            self.assertEqual(response.status_code, 200)
            task_request = preflight.call_args.args[0]
            self.assertEqual(
                task_request.voice_name,
                "chatterbox:ru-default",
            )
        finally:
            if old_tts is None:
                config.ui.pop("tts_server", None)
            else:
                config.ui["tts_server"] = old_tts
            if old_voice is None:
                config.ui.pop("voice_name", None)
            else:
                config.ui["voice_name"] = old_voice

    def test_director_plan_rejects_invalid_aspect_before_service_call(self):
        with patch.object(
            asc_ai_controller.asc_ai, "create_director_plan"
        ) as create_plan:
            response = self.client.post(
                "/api/v1/asc-ai/director/plan",
                json={
                    "video_subject": "Теплица",
                    "video_aspect": "vertical",
                },
            )
        self.assertEqual(response.status_code, 400)
        create_plan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
