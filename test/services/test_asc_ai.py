import json
import os
import tempfile
import unittest

import requests
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.config import config
from app.services import asc_ai, asc_ai_director, asc_ai_qc


class TestAscAIIntegration(unittest.TestCase):
    def setUp(self):
        self.old_asc = dict(config.asc_ai)
        self.old_ui = dict(config.ui)
        self.old_app = dict(config.app)
        config.asc_ai.update(
            {
                "enabled": True,
                "local_only": True,
                "scheduler_url": "http://scheduler:8090",
                "prompt_llm_url": "http://prompt-llm:8080",
            }
        )
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

    @staticmethod
    def params(**changes):
        values = {
            "video_source": "asc_ai",
            "director_enabled": True,
            "video_script": "",
            "subtitle_enabled": True,
            "bgm_type": "random",
            "video_subject": "Как работает теплица",
            "video_language": "ru-RU",
            "video_aspect": "9:16",
            "director_target_duration_seconds": 30,
            "director_max_local_video_scenes": 1,
            "director_style": "",
            "director_audience": "",
            "director_purpose": "",
        }
        values.update(changes)
        return SimpleNamespace(**values)

    def test_local_endpoint_policy_accepts_private_runtime_and_blocks_cloud(self):
        self.assertEqual(
            asc_ai._assert_local_endpoint(
                "image_adapter", "http://image-adapter:8091"
            ),
            "http://image-adapter:8091",
        )
        self.assertEqual(
            asc_ai._assert_local_endpoint(
                "scheduler", "http://10.55.2.8:8090/"
            ),
            "http://10.55.2.8:8090",
        )
        self.assertEqual(
            asc_ai._assert_local_endpoint(
                "custom", "https://service.agrom.local:9443"
            ),
            "https://service.agrom.local:9443",
        )
        with self.assertRaisesRegex(asc_ai.AscAIError, "blocks external"):
            asc_ai._assert_local_endpoint(
                "prompt_llm", "https://api.openai.com/v1"
            )

    def test_local_endpoint_policy_supports_explicit_internal_host_allowlist(self):
        config.asc_ai["local_service_hosts"] = ["asc-ai.internal.example"]
        self.assertEqual(
            asc_ai._assert_local_endpoint(
                "custom", "http://asc-ai.internal.example:9000"
            ),
            "http://asc-ai.internal.example:9000",
        )

    def test_env_cannot_redirect_image_adapter_to_external_gateway(self):
        with patch.dict(
            os.environ,
            {"ASC_AI_IMAGE_ADAPTER_URL": "https://external.example/v1"},
            clear=False,
        ):
            with self.assertRaisesRegex(asc_ai.AscAIError, "blocks external"):
                asc_ai._image_url()

    def test_scheduler_managed_llm_rejects_external_runtime_urls(self):
        with self.assertRaisesRegex(
            asc_ai_director.DirectorError,
            "blocks external",
        ):
            asc_ai_director.SchedulerManagedLocalLLM(
                "http://gpu-scheduler:8090",
                "https://api.openai.com/v1",
            )
        with self.assertRaisesRegex(
            asc_ai_director.DirectorError,
            "blocks external",
        ):
            asc_ai_director.SchedulerManagedLocalLLM(
                "https://scheduler.example.com",
                "http://prompt-llm-local:8080",
            )

    @patch("app.services.asc_ai_director._json_request")
    def test_scheduler_managed_llm_forwards_active_lease_header(self, request_json):
        request_json.side_effect = [
            {"job": {"job_id": "job-1"}, "initial_stage_id": "stage-1"},
            {"state": "READY"},
            {"state": "READY_FOR_GPU"},
            {"decision": "GRANTED", "lease": {"lease_id": "lease-1"}},
            {"vram_used_mb": 1000},
            {"lease_id": "lease-1"},
            {"choices": [{"message": {"content": "ok"}}]},
            {"vram_used_mb": 1000},
            {"lease_id": "lease-1"},
            {"state": "COMPLETED"},
        ]
        runtime = asc_ai_director.SchedulerManagedLocalLLM(
            "http://gpu-scheduler:8090",
            "http://prompt-llm-local:8080",
        )
        result = runtime.chat(
            "hello",
            timeout=30,
            temperature=0.1,
            max_tokens=64,
        )
        self.assertEqual(result, "ok")
        inference_call = next(
            call for call in request_json.call_args_list
            if call.args[1].endswith("/v1/chat/completions")
        )
        self.assertEqual(
            inference_call.kwargs["headers"]["X-ASC-Lease"],
            "lease-1",
        )

    @patch("app.services.asc_ai_qc.requests.post")
    def test_qc_http_error_preserves_status_and_body(self, post):
        response = post.return_value
        response.status_code = 502
        response.text = '{"detail":"visual provider failed: lease mismatch"}'
        response.raise_for_status.side_effect = requests.HTTPError("boom")
        with self.assertRaisesRegex(
            asc_ai_qc.QCError,
            r"evidence analysis failed at /api/v1/evidence/analyze: HTTP 502: .*lease mismatch",
        ):
            asc_ai_qc.analyze_evidence(
                "http://visual-analyzer:8095",
                "artifact-1",
            )

    def test_local_only_accepts_asc_ai_chatterbox_and_whisper(self):
        asc_ai.validate_local_only(self.params())

    def test_local_only_rejects_cloud_video_source(self):
        with self.assertRaisesRegex(asc_ai.AscAIError, "local-only"):
            asc_ai.validate_local_only(self.params(video_source="ofox"))

    def test_local_only_requires_director_for_asc_ai_visuals(self):
        with self.assertRaisesRegex(asc_ai.AscAIError, "Director"):
            asc_ai.validate_local_only(self.params(director_enabled=False))

    def test_local_only_without_director_never_falls_through_to_cloud_llm(self):
        params = self.params(
            video_source="local",
            director_enabled=False,
            video_script="",
        )
        with self.assertRaisesRegex(asc_ai.AscAIError, "user-provided script"):
            asc_ai.validate_local_only(params)

    @patch("app.services.asc_ai._component_health", return_value={"status": "ok"})
    def test_script_preflight_checks_only_local_director_dependencies(self, health):
        asc_ai.preflight(self.params(), stop_at="script")
        names = [call.args[0] for call in health.call_args_list]
        self.assertEqual(names, ["scheduler", "prompt_llm"])

    @patch(
        "app.services.asc_ai._chatterbox_voice_info",
        return_value={"name": "ru-default", "language": "ru", "aliases": []},
    )
    @patch("app.services.asc_ai._component_health", return_value={"status": "ok"})
    def test_video_preflight_checks_full_local_pipeline(self, health, _voice):
        params = self.params(
            custom_audio_file="",
            voice_name="chatterbox:ru-default",
        )
        asc_ai.preflight(params, stop_at="video")
        names = [call.args[0] for call in health.call_args_list]
        self.assertEqual(
            names,
            [
                "scheduler",
                "prompt_llm",
                "image_adapter",
                "prompt_intelligence",
                "visual_analyzer",
                "chatterbox_tts",
            ],
        )

    @patch(
        "app.services.asc_ai._chatterbox_voice_info",
        return_value={"name": "ru-default", "language": "ru", "aliases": []},
    )
    @patch("app.services.asc_ai._component_health", return_value={"status": "ok"})
    def test_task_chatterbox_voice_overrides_stale_cloud_tts_provider(
        self, health, _voice
    ):
        config.ui["voice_mode"] = "tts"
        config.ui["tts_server"] = "azure-tts-v1"
        params = self.params(
            custom_audio_file="",
            voice_name="chatterbox:ru-default",
        )

        asc_ai.preflight(params, stop_at="video")

        names = [call.args[0] for call in health.call_args_list]
        self.assertIn("chatterbox_tts", names)
        self.assertNotIn("azure-tts-v1", names)

    @patch(
        "app.services.asc_ai._chatterbox_voice_info",
        return_value={"name": "ru-default", "language": "ru", "aliases": []},
    )
    @patch("app.services.asc_ai._component_health", return_value={"status": "ok"})
    def test_resume_preflight_skips_qwen_and_tts_but_keeps_visual_stack(
        self, health, voice_info
    ):
        params = self.params(
            custom_audio_file="",
            voice_name="chatterbox:ru-default",
        )
        asc_ai.preflight(
            params,
            stop_at="video",
            director_plan_ready=True,
            narration_audio_ready=True,
        )
        names = [call.args[0] for call in health.call_args_list]
        self.assertIn("scheduler", names)
        self.assertIn("image_adapter", names)
        self.assertIn("prompt_intelligence", names)
        self.assertIn("visual_analyzer", names)
        self.assertNotIn("prompt_llm", names)
        self.assertNotIn("chatterbox_tts", names)
        voice_info.assert_not_called()

    @patch("app.services.asc_ai._request_json")
    def test_chatterbox_voice_preflight_requires_installed_matching_language(
        self, request_json
    ):
        request_json.return_value = {
            "voices": [
                {
                    "name": "ru-default",
                    "language": "ru",
                    "aliases": ["russian"],
                }
            ]
        }
        info = asc_ai._chatterbox_voice_info(
            self.params(
                video_language="ru-RU",
                voice_name="chatterbox:ru-default",
            )
        )
        self.assertEqual(info["name"], "ru-default")
        self.assertEqual(info["language"], "ru")
        self.assertTrue(
            request_json.call_args.args[1].endswith("/v1/voices")
        )

    @patch("app.services.asc_ai._request_json")
    def test_chatterbox_voice_preflight_rejects_missing_voice(self, request_json):
        request_json.return_value = {"voices": []}
        with self.assertRaisesRegex(asc_ai.AscAIError, "is not installed"):
            asc_ai._chatterbox_voice_info(
                self.params(
                    video_language="ru-RU",
                    voice_name="chatterbox:ru-default",
                )
            )

    @patch("app.services.asc_ai._request_json")
    def test_chatterbox_voice_preflight_rejects_language_mismatch(
        self, request_json
    ):
        request_json.return_value = {
            "voices": [
                {
                    "name": "ru-default",
                    "language": "en",
                    "aliases": [],
                }
            ]
        }
        with self.assertRaisesRegex(asc_ai.AscAIError, "does not match"):
            asc_ai._chatterbox_voice_info(
                self.params(
                    video_language="ru-RU",
                    voice_name="chatterbox:ru-default",
                )
            )

    @patch("app.services.asc_ai._component_health", return_value={"status": "ok"})
    def test_custom_audio_preflight_does_not_require_tts(self, health):
        params = self.params(custom_audio_file="voice.wav")
        asc_ai.preflight(params, stop_at="video")
        names = [call.args[0] for call in health.call_args_list]
        self.assertNotIn("chatterbox_tts", names)

    def test_director_terms_keep_scene_order(self):
        plan = {
            "scenes": [
                {"visual_prompt": "first"},
                {"visual_prompt": "second"},
                {"visual_prompt": ""},
            ]
        }
        self.assertEqual(asc_ai.director_terms(plan), ["first", "second"])

    @patch("app.services.asc_ai_director.create_plan")
    def test_director_uses_scheduler_managed_local_module(self, create_plan):
        create_plan.return_value = {
            "schema_version": "mpt.director.v2",
            "local_only": True,
            "gpu_policy": "scheduler_managed",
            "script": "Локальный сценарий",
            "scenes": [{"scene_id": "scene_01", "visual_prompt": "greenhouse"}],
        }
        result = asc_ai.create_director_plan(self.params())
        self.assertTrue(result["local_only"])
        create_plan.assert_called_once()
        kwargs = create_plan.call_args.kwargs
        self.assertEqual(kwargs["scheduler_url"], "http://scheduler:8090")
        self.assertEqual(kwargs["prompt_llm_url"], "http://prompt-llm:8080")

    @patch("app.services.asc_ai_director.create_plan")
    @patch("app.services.asc_ai_character.get_character")
    def test_director_loads_selected_character_identity(
        self, get_character, create_plan
    ):
        identity = {
            "character_id": "aria",
            "name": "Aria",
            "appearance": "platinum short hair",
            "reference_artifact_ids": ["ref-1"],
        }
        get_character.return_value = identity
        create_plan.return_value = {
            "schema_version": "mpt.director.v2",
            "local_only": True,
            "gpu_policy": "scheduler_managed",
            "script": "Текст",
            "scenes": [
                {
                    "scene_id": "scene_01",
                    "visual_strategy": "LOCAL_IMAGE",
                    "visual_prompt": "city street",
                }
            ],
        }
        params = self.params(director_character_id="aria")
        result = asc_ai.create_director_plan(params)

        self.assertEqual(result["character_identity"]["character_id"], "aria")
        self.assertEqual(
            result["scenes"][0]["character_identity"]["character_id"], "aria"
        )
        self.assertEqual(
            create_plan.call_args.kwargs["character_identity"], identity
        )

    @patch("app.services.asc_ai._output_path", return_value="/tmp/generated.png")
    @patch("app.services.asc_ai._request_json", return_value={"outputs": [{"path": "/tmp/generated.png"}]})
    @patch("app.services.asc_ai._request_json")
    @patch("app.services.asc_ai.stage_image_for_video", return_value="scene.png")
    def test_wan_generation_uses_normalized_scene_duration(
        self, _stage, request_json
    ):
        with tempfile.NamedTemporaryFile(suffix=".mp4") as output:
            request_json.return_value = {
                "outputs": [{"path": output.name}],
                "settings": {"duration_seconds": 5},
            }
            binding = {
                "workflow_id": "wan_i2v_default.v1",
                "model_id": "video-model",
                "defaults": {"length": 89},
            }
            result = asc_ai.generate_video_from_image(
                "task",
                {
                    "scene_id": "scene_01",
                    "duration_seconds": 11,
                    "visual_prompt": "greenhouse",
                    "motion_prompt": "slow push in",
                },
                "/tmp/source.png",
                binding=binding,
            )

        self.assertTrue(result.endswith(".mp4"))
        payload = request_json.call_args.kwargs["json"]
        self.assertEqual(payload["duration_seconds"], 5)

    @patch("app.services.asc_ai._workflow_catalog")
    def test_character_identity_request_uses_references_lora_and_portrait_resolution(
        self, catalog, request_json, _output
    ):
        catalog.return_value = [
            {
                "workflow_id": "krea_identity_reference.v1",
                "purpose": "identity_reference",
                "model_id": "runtime-krea-model",
                "operator_only": False,
                "validation_state": "production",
                "default_resolution": "1024x1024",
                "supported_resolutions": [
                    "1024x1024",
                    "768x1344",
                    "1344x768",
                ],
            }
        ]
        scene = {
            "scene_id": "scene_01",
            "visual_prompt": "walking through a neon street",
            "character_identity": {
                "character_id": "aria",
                "appearance": "adult woman, platinum short hair, amber right eye",
                "forbidden_traits": ["long red hair"],
                "forbidden_prompt_terms": ["identity drift"],
                "reference_artifact_ids": ["ref-face", "ref-body"],
                "lora_resource_ids": ["lora-character"],
                "preferred_workflow": "krea_identity_reference.v1",
                "ref_boost": 1.1,
                "grounding_px": 1024,
            },
        }

        asc_ai.generate_image("task", scene, "9:16", attempt=1)
        payload = request_json.call_args.kwargs["json"]

        self.assertEqual(payload["workflow_id"], "krea_identity_reference.v1")
        self.assertEqual(payload["model_id"], "runtime-krea-model")
        self.assertEqual(payload["overrides"]["width"], 768)
        self.assertEqual(payload["overrides"]["height"], 1344)
        self.assertEqual(
            [row["artifact_id"] for row in payload["reference_inputs"]],
            ["ref-face", "ref-body"],
        )
        self.assertIn(
            {"model_id": "lora-character", "strength": 1.0},
            payload["loras"],
        )
        self.assertIn("platinum short hair", payload["prompt"])
        self.assertIn("long red hair", payload["negative_prompt"])
        self.assertEqual(payload["prompt_compiler_id"], "RAW")
        self.assertEqual(
            payload["prompt_compiler_version"], "mpt-director-character-v2"
        )

    @patch("app.services.asc_ai._request_json")
    def test_local_only_never_calls_prompt_intelligence_planning(self, request_json):
        plan = {
            "local_only": True,
            "gpu_policy": "scheduler_managed",
            "script": "Текст",
            "scenes": [
                {
                    "scene_id": "scene_01",
                    "visual_strategy": "LOCAL_VIDEO",
                    "visual_prompt": "modern greenhouse",
                }
            ],
        }
        result = asc_ai.enrich_director_plan(plan)
        self.assertEqual(result, plan)
        request_json.assert_not_called()

    def test_local_runtime_url_allowlist_is_exact(self):
        allowed = ["llm.internal.example"]
        self.assertEqual(
            asc_ai_director._assert_local_runtime_url(
                "prompt_llm_url",
                "http://llm.internal.example:8080",
                allowed,
            ),
            "http://llm.internal.example:8080",
        )
        with self.assertRaisesRegex(
            asc_ai_director.DirectorError,
            "blocks external",
        ):
            asc_ai_director._assert_local_runtime_url(
                "prompt_llm_url",
                "https://api.openai.com/v1",
                allowed,
            )
        with self.assertRaisesRegex(
            asc_ai_director.DirectorError,
            "blocks external",
        ):
            asc_ai_director._assert_local_runtime_url(
                "prompt_llm_url",
                "http://evil.llm.internal.example:8080",
                allowed,
            )

    def test_local_runtime_host_parser_rejects_urls_and_bad_entries(self):
        parsed = asc_ai_director._normalized_allowed_hosts(
            [
                "llm.internal",
                "http://bad.example",
                "bad host",
                "",
                "UPPER.INTERNAL",
            ]
        )
        self.assertEqual(
            parsed,
            {"llm.internal", "upper.internal"},
        )

    @patch("app.services.asc_ai_director.SchedulerManagedLocalLLM")
    @patch("app.services.asc_ai_director.local_research.wikipedia_research")
    def test_director_research_keeps_sources_in_provenance_not_qwen_prompt(
        self, research, runtime_cls
    ):
        research.return_value = [
            {
                "title": "Теплица",
                "extract": "Теплица — сооружение для выращивания растений.",
                "source": "wikipedia",
                "source_url": "https://ru.wikipedia.org/?curid=123",
            }
        ]
        runtime = runtime_cls.return_value
        runtime.chat.return_value = (
            '{"script":"Текст","scenes":[{"scene_id":"scene_01",'
            '"narration":"Текст","duration_seconds":5,'
            '"visual_strategy":"LOCAL_IMAGE",'
            '"visual_prompt":"modern greenhouse","motion_prompt":"",'
            '"transition":"cut","overlay_text":""}]}'
        )
        result = asc_ai_director.create_plan(
            self.params(),
            {
                "public_research_enabled": True,
                "research_max_pages": 2,
                "research_max_chars_per_page": 1200,
            },
            scheduler_url="http://scheduler:8090",
            prompt_llm_url="http://prompt-llm:8080",
        )

        prompt = runtime.chat.call_args.args[0]
        self.assertIn("Теплица — сооружение", prompt)
        self.assertNotIn("https://ru.wikipedia.org", prompt)
        self.assertEqual(
            result["research"][0]["source_url"],
            "https://ru.wikipedia.org/?curid=123",
        )

    @patch("app.services.asc_ai_director.SchedulerManagedLocalLLM")
    @patch("app.services.asc_ai_director.local_research.wikipedia_research")
    def test_director_can_disable_public_research(self, research, runtime_cls):
        runtime = runtime_cls.return_value
        runtime.chat.return_value = (
            '{"script":"Текст","scenes":[{"scene_id":"scene_01",'
            '"narration":"Текст","duration_seconds":5,'
            '"visual_strategy":"LOCAL_IMAGE",'
            '"visual_prompt":"modern greenhouse","motion_prompt":"",'
            '"transition":"cut","overlay_text":""}]}'
        )
        result = asc_ai_director.create_plan(
            self.params(),
            {"public_research_enabled": False},
            scheduler_url="http://scheduler:8090",
            prompt_llm_url="http://prompt-llm:8080",
        )
        research.assert_not_called()
        self.assertEqual(result["research"], [])

    def test_adaptive_image_qc_samples_first_stride_character_and_retry(self):
        old_policy = config.asc_ai.get("qc_image_policy")
        old_stride = config.asc_ai.get("qc_image_vlm_stride")
        try:
            config.asc_ai["qc_image_policy"] = "adaptive"
            config.asc_ai["qc_image_vlm_stride"] = 3

            plain = {"scene_id": "plain"}
            character = {
                "scene_id": "character",
                "character_identity": {"character_id": "aria"},
            }
            self.assertTrue(asc_ai._image_vlm_required(0, 0, plain))
            self.assertFalse(asc_ai._image_vlm_required(1, 0, plain))
            self.assertTrue(asc_ai._image_vlm_required(3, 0, plain))
            self.assertTrue(asc_ai._image_vlm_required(1, 0, character))
            self.assertTrue(asc_ai._image_vlm_required(1, 1, plain))

            config.asc_ai["qc_image_policy"] = "typo"
            self.assertTrue(asc_ai._image_vlm_required(1, 0, plain))
        finally:
            if old_policy is None:
                config.asc_ai.pop("qc_image_policy", None)
            else:
                config.asc_ai["qc_image_policy"] = old_policy
            if old_stride is None:
                config.asc_ai.pop("qc_image_vlm_stride", None)
            else:
                config.asc_ai["qc_image_vlm_stride"] = old_stride

    @patch("app.services.asc_ai_director.SchedulerManagedLocalLLM")
    @patch("app.services.asc_ai_director.local_research.wikipedia_research")
    def test_director_repairs_invalid_first_plan_with_schema_error(
        self, research, runtime_cls
    ):
        research.return_value = []
        runtime = runtime_cls.return_value
        runtime.chat.side_effect = [
            (
                '{"script":"Текст","scenes":[{"scene_id":"scene_01",'
                '"duration_seconds":99,"visual_strategy":"BROKEN",'
                '"visual_prompt":""}]}'
            ),
            (
                '{"script":"Текст","scenes":[{"scene_id":"scene_01",'
                '"narration":"Текст","duration_seconds":5,'
                '"visual_strategy":"LOCAL_VIDEO",'
                '"visual_prompt":"modern greenhouse","motion_prompt":"slow push in",'
                '"transition":"cut","overlay_text":""}]}'
            ),
        ]

        result = asc_ai_director.create_plan(
            self.params(director_max_local_video_scenes=1),
            {"public_research_enabled": False},
            scheduler_url="http://scheduler:8090",
            prompt_llm_url="http://prompt-llm:8080",
        )

        self.assertEqual(result["schema_version"], "mpt.director.v3")
        self.assertEqual(result["scenes"][0]["visual_strategy"], "LOCAL_VIDEO")
        self.assertEqual(runtime.chat.call_count, 2)
        repair_prompt = runtime.chat.call_args_list[1].args[0]
        self.assertIn("Validation error", repair_prompt)
        self.assertIn("Previous response", repair_prompt)
        self.assertIn("duration_seconds", repair_prompt)

    @patch("app.services.asc_ai_director.SchedulerManagedLocalLLM")
    @patch("app.services.asc_ai_director.local_research.wikipedia_research")
    def test_director_fails_after_two_invalid_typed_plans(
        self, research, runtime_cls
    ):
        research.return_value = []
        runtime = runtime_cls.return_value
        runtime.chat.return_value = (
            '{"script":"Текст","scenes":[{"visual_strategy":"LOCAL_IMAGE"}]}'
        )

        with self.assertRaisesRegex(
            asc_ai_director.DirectorError, "schema validation failed"
        ):
            asc_ai_director.create_plan(
                self.params(),
                {"public_research_enabled": False},
                scheduler_url="http://scheduler:8090",
                prompt_llm_url="http://prompt-llm:8080",
            )

        self.assertEqual(runtime.chat.call_count, 2)

    def test_director_schema_enforces_video_budget_after_validation(self):
        raw = {
            "script": "Первая часть. Вторая часть.",
            "scenes": [
                {
                    "scene_id": "scene_01",
                    "narration": "Первая часть.",
                    "duration_seconds": 5,
                    "visual_strategy": "LOCAL_VIDEO",
                    "visual_prompt": "first",
                },
                {
                    "scene_id": "scene_02",
                    "narration": "Вторая часть.",
                    "duration_seconds": 5,
                    "visual_strategy": "LOCAL_VIDEO",
                    "visual_prompt": "second",
                },
            ],
        }
        result = asc_ai_director._normalize(
            raw,
            self.params(director_max_local_video_scenes=1),
        )
        self.assertEqual(
            [row["visual_strategy"] for row in result["scenes"]],
            ["LOCAL_VIDEO", "LOCAL_IMAGE"],
        )
        self.assertEqual(result["scenes"][0]["duration_seconds"], 5)
        self.assertEqual(result["scenes"][1]["duration_seconds"], 5)

    def test_supplied_script_is_partitioned_verbatim_even_if_qwen_paraphrases(self):
        script = (
            "Первая фраза про теплицу. "
            "Вторая фраза объясняет досветку и микроклимат. "
            "Третья фраза завершает рассказ."
        )
        raw = {
            "script": "модель попыталась переписать исходный текст",
            "scenes": [
                {
                    "scene_id": "scene_01",
                    "narration": "Короткий пересказ первой части.",
                    "duration_seconds": 5,
                    "visual_strategy": "LOCAL_IMAGE",
                    "visual_prompt": "greenhouse exterior",
                },
                {
                    "scene_id": "scene_02",
                    "narration": "Очень длинный пересказ второй части с лишними словами.",
                    "duration_seconds": 7,
                    "visual_strategy": "LOCAL_IMAGE",
                    "visual_prompt": "greenhouse lighting",
                },
                {
                    "scene_id": "scene_03",
                    "narration": "Финал.",
                    "duration_seconds": 4,
                    "visual_strategy": "LOCAL_IMAGE",
                    "visual_prompt": "healthy plants",
                },
            ],
        }

        result = asc_ai_director._normalize(
            raw,
            self.params(video_script=script),
        )

        narrations = [row["narration"] for row in result["scenes"]]
        self.assertEqual(
            "".join(value.split() for value in narrations),
            "".join(script.split()),
        )
        self.assertEqual(result["script"], script)
        self.assertTrue(narrations[0].startswith("Первая фраза"))
        self.assertTrue(narrations[-1].endswith("завершает рассказ."))
        self.assertNotIn("пересказ", " ".join(narrations).lower())

    def test_verbatim_partition_prefers_natural_boundaries(self):
        script = "Один короткий факт. Второй факт немного длиннее. Третий факт."
        parts = asc_ai_director._partition_script_verbatim(
            script,
            [5, 10, 5],
        )
        self.assertEqual(len(parts), 3)
        self.assertEqual("".join(x.split() for x in parts), "".join(script.split()))
        self.assertTrue(parts[0].endswith("."))
        self.assertTrue(parts[1].endswith("."))

    def test_director_public_image_budget_and_character_identity_are_fail_closed(self):
        raw = {
            "script": "Первая сцена. Вторая сцена. Третья сцена.",
            "scenes": [
                {
                    "scene_id": "scene_01",
                    "narration": "Первая сцена.",
                    "duration_seconds": 5,
                    "visual_strategy": "PUBLIC_IMAGE",
                    "visual_prompt": "real greenhouse exterior",
                    "public_media_query": "modern greenhouse exterior",
                },
                {
                    "scene_id": "scene_02",
                    "narration": "Вторая сцена.",
                    "duration_seconds": 5,
                    "visual_strategy": "PUBLIC_IMAGE",
                    "visual_prompt": "tomato plants",
                    "public_media_query": "greenhouse tomato plants",
                },
                {
                    "scene_id": "scene_03",
                    "narration": "Третья сцена.",
                    "duration_seconds": 5,
                    "visual_strategy": "PUBLIC_IMAGE",
                    "visual_prompt": "LED fixtures",
                    "public_media_query": "horticulture LED lighting",
                },
            ],
        }

        limited = asc_ai_director._normalize(
            raw,
            self.params(director_max_public_image_scenes=2),
        )
        self.assertEqual(
            [row["visual_strategy"] for row in limited["scenes"]],
            ["PUBLIC_IMAGE", "PUBLIC_IMAGE", "LOCAL_IMAGE"],
        )
        self.assertEqual(
            limited["scenes"][0]["public_media_query"],
            "modern greenhouse exterior",
        )
        self.assertEqual(limited["scenes"][2]["public_media_query"], "")

        character = asc_ai_director._normalize(
            raw,
            self.params(
                director_max_public_image_scenes=3,
                director_character_id="aria",
            ),
        )
        self.assertTrue(
            all(
                row["visual_strategy"] == "LOCAL_IMAGE"
                for row in character["scenes"]
            )
        )
        self.assertTrue(
            all(not row["public_media_query"] for row in character["scenes"])
        )

    def test_director_schema_rejects_scene_narration_that_does_not_match_script(self):
        raw = {
            "script": "Первая часть. Вторая часть.",
            "scenes": [
                {
                    "scene_id": "scene_01",
                    "narration": "Первая часть.",
                    "duration_seconds": 5,
                    "visual_strategy": "LOCAL_IMAGE",
                    "visual_prompt": "first",
                },
                {
                    "scene_id": "scene_02",
                    "narration": "Совсем другой текст.",
                    "duration_seconds": 5,
                    "visual_strategy": "LOCAL_IMAGE",
                    "visual_prompt": "second",
                },
            ],
        }
        with self.assertRaisesRegex(
            asc_ai_director.DirectorError,
            "partition the script verbatim",
        ):
            asc_ai_director._normalize(raw, self.params())

    def test_retime_prefers_verbatim_scene_narration_length_over_initial_guess(self):
        plan = {
            "scenes": [
                {
                    "scene_id": "short",
                    "narration": "Коротко.",
                    "visual_strategy": "LOCAL_IMAGE",
                    "duration_seconds": 10,
                },
                {
                    "scene_id": "long",
                    "narration": "Это заметно более длинная часть озвучки для второй сцены.",
                    "visual_strategy": "LOCAL_IMAGE",
                    "duration_seconds": 2,
                },
            ]
        }
        result = asc_ai.retime_director_plan(plan, 16.0)
        durations = [row["duration_seconds"] for row in result["scenes"]]
        self.assertEqual(sum(durations), 16)
        self.assertLess(durations[0], durations[1])
        self.assertEqual(
            result["timing"]["weight_source"],
            "scene_narration_characters",
        )

    def test_local_video_scene_uses_one_wan_job_then_local_timing_fit(self):
        with tempfile.TemporaryDirectory() as temp_dir, (
            patch.object(asc_ai.utils, "task_dir", return_value=temp_dir),
            patch.object(
                asc_ai,
                "_image_binding",
                return_value={
                    "workflow_id": "image-v1",
                    "model_id": "image-model",
                    "default_resolution": "1024x1024",
                },
            ),
            patch.object(
                asc_ai,
                "_video_compatibility",
                return_value={
                    "available": True,
                    "compatible": True,
                    "workflow_id": "wan_i2v_default.v1",
                    "model_id": "wan-model",
                    "resolution": "543x960",
                    "target_aspect": "9:16",
                    "aspect_log_error": 0.0,
                    "max_aspect_log_error": 0.18,
                    "binding": {
                        "workflow_id": "wan_i2v_default.v1",
                        "model_id": "wan-model",
                        "defaults": {"length": 89},
                    },
                },
            ),
            patch.object(
                asc_ai,
                "generate_image",
                return_value=str(Path(temp_dir) / "image.png"),
            ),
            patch.object(
                asc_ai,
                "quality_control",
                return_value={
                    "passed": True,
                    "technical_status": "valid",
                    "technical_score": 1.0,
                    "semantic_score": None,
                    "issues": [],
                    "retry_prompt": "",
                },
            ),
            patch.object(
                asc_ai,
                "generate_video_from_image",
                return_value=(
                    str(Path(temp_dir) / "wan.mp4"),
                    {"job_id": "wan-job"},
                ),
            ) as generate_video,
            patch.object(
                asc_ai.video,
                "fit_director_video_duration",
                return_value=str(Path(temp_dir) / "wan.timed.mp4"),
            ) as fit_duration,
        ):
            paths = asc_ai.generate_scene_materials(
                task_id="task",
                plan={
                    "schema_version": "mpt.director.v3",
                    "director_provider": "asc-ai-local-qwen3",
                    "gpu_policy": "scheduler_managed",
                    "scenes": [
                        {
                            "scene_id": "scene_01",
                            "narration": "Длинная динамическая сцена.",
                            "visual_strategy": "LOCAL_VIDEO",
                            "duration_seconds": 9,
                            "visual_prompt": "modern greenhouse",
                            "motion_prompt": "slow camera push",
                            "transition": "cut",
                            "overlay_text": "",
                        }
                    ],
                },
                audio_duration=9.0,
                aspect="9:16",
                clip_duration=5,
            )

            self.assertEqual(paths, [str(Path(temp_dir) / "wan.timed.mp4")])
            generate_video.assert_called_once()
            fit_duration.assert_called_once_with(
                str(Path(temp_dir) / "wan.mp4"),
                str(Path(temp_dir) / "image.png"),
                9,
            )
            manifest = json.loads(
                (Path(temp_dir) / "production-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            video_record = manifest["scenes"][0]["video"]
            self.assertEqual(video_record["native_contract_seconds"], 5)
            self.assertEqual(video_record["requested_scene_seconds"], 9)
            self.assertEqual(
                video_record["timed_path"],
                str(Path(temp_dir) / "wan.timed.mp4"),
            )
            self.assertTrue(video_record["timing_adjusted"])
            self.assertEqual(
                manifest["scenes"][0]["effective_duration_seconds"],
                9,
            )

    def test_scene_postprocess_executes_director_transition_and_callout(self):
        with tempfile.TemporaryDirectory() as temp_dir, (
            patch.object(asc_ai.utils, "task_dir", return_value=temp_dir),
            patch.object(
                asc_ai,
                "_image_binding",
                return_value={
                    "workflow_id": "image-v1",
                    "model_id": "image-model",
                    "default_resolution": "1024x1024",
                },
            ),
            patch.object(
                asc_ai,
                "generate_image",
                return_value=str(Path(temp_dir) / "image.png"),
            ),
            patch.object(
                asc_ai,
                "quality_control",
                return_value={
                    "passed": True,
                    "technical_status": "valid",
                    "technical_score": 1.0,
                    "semantic_score": None,
                    "issues": [],
                    "retry_prompt": "",
                },
            ),
            patch.object(
                asc_ai.video,
                "render_image_zoom_video",
                return_value=str(Path(temp_dir) / "scene.mp4"),
            ),
            patch.object(
                asc_ai.video,
                "render_director_scene_effects",
                return_value=str(Path(temp_dir) / "scene.director.mp4"),
            ) as postprocess,
        ):
            paths = asc_ai.generate_scene_materials(
                task_id="task",
                plan={
                    "schema_version": "mpt.director.v3",
                    "director_provider": "asc-ai-local-qwen3",
                    "gpu_policy": "scheduler_managed",
                    "scenes": [
                        {
                            "scene_id": "scene_01",
                            "visual_strategy": "LOCAL_IMAGE",
                            "duration_seconds": 4,
                            "visual_prompt": "modern greenhouse",
                            "motion_prompt": "",
                            "transition": "fade_in",
                            "overlay_text": "Урожай +20%",
                        }
                    ],
                },
                audio_duration=4.0,
                aspect="9:16",
                clip_duration=5,
            )

            self.assertEqual(
                paths, [str(Path(temp_dir) / "scene.director.mp4")]
            )
            postprocess.assert_called_once()
            self.assertEqual(postprocess.call_args.kwargs["transition"], "fade_in")
            self.assertEqual(
                postprocess.call_args.kwargs["overlay_text"], "Урожай +20%"
            )
            manifest = json.loads(
                (Path(temp_dir) / "production-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(manifest["scenes"][0]["postprocess"]["applied"])
            self.assertEqual(
                manifest["scenes"][0]["final_output"],
                str(Path(temp_dir) / "scene.director.mp4"),
            )

    def test_scene_postprocess_failure_keeps_approved_source_clip(self):
        with tempfile.TemporaryDirectory() as temp_dir, (
            patch.object(asc_ai.utils, "task_dir", return_value=temp_dir),
            patch.object(
                asc_ai,
                "_image_binding",
                return_value={
                    "workflow_id": "image-v1",
                    "model_id": "image-model",
                    "default_resolution": "1024x1024",
                },
            ),
            patch.object(
                asc_ai,
                "generate_image",
                return_value=str(Path(temp_dir) / "image.png"),
            ),
            patch.object(
                asc_ai,
                "quality_control",
                return_value={
                    "passed": True,
                    "technical_status": "valid",
                    "technical_score": 1.0,
                    "semantic_score": None,
                    "issues": [],
                    "retry_prompt": "",
                },
            ),
            patch.object(
                asc_ai.video,
                "render_image_zoom_video",
                return_value=str(Path(temp_dir) / "scene.mp4"),
            ),
            patch.object(
                asc_ai.video,
                "render_director_scene_effects",
                side_effect=RuntimeError("font failure"),
            ),
        ):
            paths = asc_ai.generate_scene_materials(
                task_id="task",
                plan={
                    "schema_version": "mpt.director.v3",
                    "director_provider": "asc-ai-local-qwen3",
                    "gpu_policy": "scheduler_managed",
                    "scenes": [
                        {
                            "scene_id": "scene_01",
                            "visual_strategy": "LOCAL_IMAGE",
                            "duration_seconds": 4,
                            "visual_prompt": "modern greenhouse",
                            "transition": "fade_in",
                            "overlay_text": "Факт",
                        }
                    ],
                },
                audio_duration=4.0,
                aspect="9:16",
                clip_duration=5,
            )
            self.assertEqual(paths, [str(Path(temp_dir) / "scene.mp4")])
            manifest = json.loads(
                (Path(temp_dir) / "production-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            post = manifest["scenes"][0]["postprocess"]
            self.assertFalse(post["applied"])
            self.assertIn("RuntimeError", post["error"])

    def test_retime_director_plan_fits_real_audio_without_extra_inference(self):
        plan = {
            "scenes": [
                {
                    "scene_id": "scene_01",
                    "visual_strategy": "LOCAL_IMAGE",
                    "duration_seconds": 5,
                },
                {
                    "scene_id": "scene_02",
                    "visual_strategy": "LOCAL_VIDEO",
                    "duration_seconds": 5,
                },
                {
                    "scene_id": "scene_03",
                    "visual_strategy": "LOCAL_IMAGE",
                    "duration_seconds": 10,
                },
            ]
        }

        result = asc_ai.retime_director_plan(plan, 30.0)

        self.assertEqual(
            [row["duration_seconds"] for row in result["scenes"]],
            [8, 7, 15],
        )
        self.assertEqual(result["timing"]["effective_scene_seconds"], 30)
        self.assertTrue(result["timing"]["fully_matched"])
        self.assertTrue(result["timing"]["retimed"])
        self.assertEqual(
            [row["planned_duration_seconds"] for row in result["scenes"]],
            [5, 5, 10],
        )

    def test_retime_director_plan_can_extend_video_scene_without_extra_wan_job(self):
        plan = {
            "scenes": [
                {
                    "scene_id": "scene_01",
                    "visual_strategy": "LOCAL_VIDEO",
                    "duration_seconds": 5,
                }
            ]
        }
        result = asc_ai.retime_director_plan(plan, 12.0)
        self.assertEqual(result["scenes"][0]["duration_seconds"], 12)
        self.assertTrue(result["timing"]["fully_matched"])
        self.assertEqual(result["timing"]["feasible_max_seconds"], 15)

    def test_retime_director_plan_reports_only_true_typed_bound_overflow(self):
        plan = {
            "scenes": [
                {
                    "scene_id": "scene_01",
                    "visual_strategy": "LOCAL_VIDEO",
                    "duration_seconds": 5,
                }
            ]
        }
        result = asc_ai.retime_director_plan(plan, 25.0)
        self.assertEqual(result["scenes"][0]["duration_seconds"], 15)
        self.assertFalse(result["timing"]["fully_matched"])
        self.assertEqual(result["timing"]["feasible_max_seconds"], 15)

    def test_resumed_plan_cannot_restore_public_media_without_current_opt_in(self):
        plan = {
            "local_only": True,
            "script": "Текст.",
            "public_egress": {
                "research_enabled": True,
                "media_enabled": True,
            },
            "scenes": [
                {
                    "scene_id": "scene_01",
                    "visual_strategy": "PUBLIC_IMAGE",
                    "public_media_query": "greenhouse",
                    "visual_prompt": "greenhouse",
                }
            ],
        }
        sanitized = asc_ai.sanitize_resumed_plan(
            plan,
            self.params(
                director_public_research_enabled=False,
                director_public_media_enabled=False,
            ),
        )

        self.assertFalse(sanitized["public_egress"]["media_enabled"])
        self.assertFalse(sanitized["public_egress"]["research_enabled"])
        self.assertEqual(
            sanitized["scenes"][0]["visual_strategy"],
            "LOCAL_IMAGE",
        )
        self.assertEqual(sanitized["scenes"][0]["public_media_query"], "")
        self.assertEqual(
            sanitized["scenes"][0]["resume_privacy_downgrade"],
            "PUBLIC_IMAGE_TO_LOCAL_IMAGE",
        )
        self.assertEqual(plan["scenes"][0]["visual_strategy"], "PUBLIC_IMAGE")

    def test_scene_resume_reuses_matching_completed_clip_without_gpu_work(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old_dir = root / "old-task"
            new_dir = root / "new-task"
            old_dir.mkdir()
            new_dir.mkdir()
            old_clip = old_dir / "scene.mp4"
            old_clip.write_bytes(b"fake-video")
            old_manifest = {
                "status": "failed",
                "scenes": [
                    {
                        "scene_id": "scene_01",
                        "requested_strategy": "LOCAL_IMAGE",
                        "visual_prompt": "greenhouse",
                        "motion_prompt": "",
                        "character": None,
                        "image_workflow": {
                            "workflow_id": "image-v1",
                            "model_id": "image-model",
                            "resolution": "768x1344",
                        },
                        "public_media": None,
                        "postprocess": {},
                        "final_output": str(old_clip),
                        "effective_duration_seconds": 5,
                        "image_source": "local_generation",
                    }
                ],
            }
            (old_dir / "production-manifest.json").write_text(
                json.dumps(old_manifest),
                encoding="utf-8",
            )

            def task_dir(task_id=""):
                if task_id == "old-task":
                    return str(old_dir)
                if task_id == "new-task":
                    return str(new_dir)
                return str(root)

            fake_clip = MagicMock()
            fake_clip.duration = 5.0
            with (
                patch.object(asc_ai.utils, "task_dir", side_effect=task_dir),
                patch.object(
                    asc_ai,
                    "_image_binding",
                    return_value={
                        "workflow_id": "image-v1",
                        "model_id": "image-model",
                        "default_resolution": "768x1344",
                    },
                ),
                patch.object(
                    asc_ai.video,
                    "_open_video_clip_quietly",
                    return_value=fake_clip,
                ),
                patch.object(asc_ai.video, "close_clip"),
                patch.object(asc_ai, "generate_image") as generate_image,
                patch.object(asc_ai, "quality_control") as quality_control,
            ):
                result = asc_ai.generate_scene_materials(
                    task_id="new-task",
                    plan={
                        "schema_version": "mpt.director.v3",
                        "local_only": True,
                        "director_provider": "asc-ai-local-qwen3",
                        "gpu_policy": "scheduler_managed",
                        "scenes": [
                            {
                                "scene_id": "scene_01",
                                "visual_strategy": "LOCAL_IMAGE",
                                "visual_prompt": "greenhouse",
                                "motion_prompt": "",
                                "transition": "cut",
                                "overlay_text": "",
                                "duration_seconds": 5,
                            }
                        ],
                    },
                    audio_duration=5.0,
                    aspect="9:16",
                    clip_duration=5,
                    resume_from_task_id="old-task",
                )

            self.assertEqual(len(result), 1)
            self.assertTrue(Path(result[0]).is_file())
            self.assertIn("resumed-scenes", result[0])
            generate_image.assert_not_called()
            quality_control.assert_not_called()
            manifest = json.loads(
                (new_dir / "production-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(manifest["scenes"][0]["resume"]["reused"])
            self.assertEqual(
                manifest["scenes"][0]["resume"]["source_task_id"],
                "old-task",
            )

    def test_scene_resume_duration_mismatch_forces_normal_regeneration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old_dir = root / "old-task"
            new_dir = root / "new-task"
            old_dir.mkdir()
            new_dir.mkdir()
            old_clip = old_dir / "scene.mp4"
            old_clip.write_bytes(b"fake-video")
            (old_dir / "production-manifest.json").write_text(
                json.dumps(
                    {
                        "scenes": [
                            {
                                "scene_id": "scene_01",
                                "requested_strategy": "LOCAL_IMAGE",
                                "visual_prompt": "greenhouse",
                                "motion_prompt": "",
                                "character": None,
                                "postprocess": {},
                                "final_output": str(old_clip),
                                "effective_duration_seconds": 5,
                                "image_source": "local_generation",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            generated_image = new_dir / "generated.png"
            generated_image.write_bytes(b"image")
            generated_clip = str(new_dir / "generated.mp4")

            def task_dir(task_id=""):
                return str(
                    old_dir if task_id == "old-task"
                    else new_dir if task_id == "new-task"
                    else root
                )

            with (
                patch.object(asc_ai.utils, "task_dir", side_effect=task_dir),
                patch.object(
                    asc_ai,
                    "_image_binding",
                    return_value={
                        "workflow_id": "image-v1",
                        "model_id": "image-model",
                        "default_resolution": "768x1344",
                    },
                ),
                patch.object(
                    asc_ai,
                    "_workflow_resolution",
                    return_value=(768, 1344),
                ),
                patch.object(
                    asc_ai,
                    "generate_image",
                    return_value=(str(generated_image), {"job_id": "job-new"}),
                ) as generate_image,
                patch.object(
                    asc_ai,
                    "quality_control",
                    return_value={
                        "passed": True,
                        "technical_status": "valid",
                        "technical_score": 1.0,
                        "semantic_score": None,
                        "issues": [],
                        "retry_prompt": "",
                    },
                ),
                patch.object(
                    asc_ai.video,
                    "render_image_zoom_video",
                    return_value=generated_clip,
                ),
            ):
                result = asc_ai.generate_scene_materials(
                    task_id="new-task",
                    plan={
                        "schema_version": "mpt.director.v3",
                        "local_only": True,
                        "director_provider": "asc-ai-local-qwen3",
                        "gpu_policy": "scheduler_managed",
                        "scenes": [
                            {
                                "scene_id": "scene_01",
                                "visual_strategy": "LOCAL_IMAGE",
                                "visual_prompt": "greenhouse",
                                "motion_prompt": "",
                                "transition": "cut",
                                "overlay_text": "",
                                "duration_seconds": 8,
                            }
                        ],
                    },
                    audio_duration=8.0,
                    aspect="9:16",
                    clip_duration=5,
                    resume_from_task_id="old-task",
                )

            self.assertEqual(result, [generated_clip])
            generate_image.assert_called_once()

    def test_public_image_scene_skips_local_diffusion_and_writes_credits(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            public_image = Path(temp_dir) / "public.png"
            public_image.write_bytes(b"public-image")
            final_clip = str(Path(temp_dir) / "public.mp4")
            public_result = {
                "provider": "wikimedia_commons",
                "title": "Greenhouse",
                "page_url": "https://commons.wikimedia.org/wiki/File:Greenhouse.jpg",
                "original_url": "https://upload.wikimedia.org/example.jpg",
                "license": "CC BY-SA 4.0",
                "license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
                "artist": "Example",
                "path": str(public_image),
                "cache_path": str(public_image),
                "query": "modern greenhouse",
            }
            with (
                patch.object(asc_ai.utils, "task_dir", return_value=temp_dir),
                patch.object(
                    asc_ai,
                    "_image_binding",
                    return_value={
                        "workflow_id": "image-v1",
                        "model_id": "image-model",
                        "default_resolution": "768x1344",
                    },
                ),
                patch.object(
                    asc_ai,
                    "_workflow_resolution",
                    return_value=(768, 1344),
                ),
                patch.object(
                    asc_ai,
                    "acquire_public_scene_image",
                    return_value=public_result,
                ) as acquire,
                patch.object(
                    asc_ai,
                    "quality_control",
                    return_value={
                        "passed": True,
                        "technical_status": "valid",
                        "technical_score": 1.0,
                        "semantic_score": None,
                        "issues": [],
                        "retry_prompt": "",
                    },
                ),
                patch.object(asc_ai, "generate_image") as generate_image,
                patch.object(
                    asc_ai.video,
                    "render_image_zoom_video",
                    return_value=final_clip,
                ),
            ):
                result = asc_ai.generate_scene_materials(
                    task_id="task-public",
                    plan={
                        "schema_version": "mpt.director.v3",
                        "director_provider": "asc-ai-local-qwen3",
                        "gpu_policy": "scheduler_managed",
                        "scenes": [
                            {
                                "scene_id": "scene_01",
                                "visual_strategy": "PUBLIC_IMAGE",
                                "public_media_query": "modern greenhouse",
                                "visual_prompt": "modern glass greenhouse",
                                "duration_seconds": 5,
                            }
                        ],
                    },
                    audio_duration=4.0,
                    aspect="9:16",
                    clip_duration=5,
                )

            self.assertEqual(result, [final_clip])
            acquire.assert_called_once()
            generate_image.assert_not_called()
            manifest = json.loads(
                (Path(temp_dir) / "production-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                manifest["scenes"][0]["image_source"],
                "wikimedia_commons",
            )
            self.assertEqual(manifest["public_media_credits"]["count"], 1)
            self.assertTrue(
                (Path(temp_dir) / "public-media-credits.json").is_file()
            )

    def test_public_image_failure_falls_back_to_local_generation_without_credits(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            local_image = Path(temp_dir) / "generated.png"
            local_image.write_bytes(b"local-image")
            final_clip = str(Path(temp_dir) / "generated.mp4")
            with (
                patch.object(asc_ai.utils, "task_dir", return_value=temp_dir),
                patch.object(
                    asc_ai,
                    "_image_binding",
                    return_value={
                        "workflow_id": "image-v1",
                        "model_id": "image-model",
                        "default_resolution": "768x1344",
                    },
                ),
                patch.object(
                    asc_ai,
                    "_workflow_resolution",
                    return_value=(768, 1344),
                ),
                patch.object(
                    asc_ai,
                    "acquire_public_scene_image",
                    side_effect=asc_ai.AscAIError("commons offline"),
                ),
                patch.object(
                    asc_ai,
                    "generate_image",
                    return_value=(str(local_image), {"job_id": "job-1"}),
                ) as generate_image,
                patch.object(
                    asc_ai,
                    "quality_control",
                    return_value={
                        "passed": True,
                        "technical_status": "valid",
                        "technical_score": 1.0,
                        "semantic_score": None,
                        "issues": [],
                        "retry_prompt": "",
                    },
                ),
                patch.object(
                    asc_ai.public_media,
                    "write_credits",
                ) as write_credits,
                patch.object(
                    asc_ai.video,
                    "render_image_zoom_video",
                    return_value=final_clip,
                ),
            ):
                result = asc_ai.generate_scene_materials(
                    task_id="task-public-fallback",
                    plan={
                        "schema_version": "mpt.director.v3",
                        "director_provider": "asc-ai-local-qwen3",
                        "gpu_policy": "scheduler_managed",
                        "scenes": [
                            {
                                "scene_id": "scene_01",
                                "visual_strategy": "PUBLIC_IMAGE",
                                "public_media_query": "modern greenhouse",
                                "visual_prompt": "modern glass greenhouse",
                                "duration_seconds": 5,
                            }
                        ],
                    },
                    audio_duration=4.0,
                    aspect="9:16",
                    clip_duration=5,
                )

            self.assertEqual(result, [final_clip])
            generate_image.assert_called_once()
            write_credits.assert_not_called()
            manifest = json.loads(
                (Path(temp_dir) / "production-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                manifest["scenes"][0]["fallback"],
                "public_media_unavailable",
            )
            self.assertEqual(
                manifest["scenes"][0]["image_source"],
                "local_generation",
            )
            self.assertNotIn("public_media_credits", manifest)

    @patch("app.services.asc_ai_qc.quality_control")
    def test_quality_control_uses_canonical_qc_module(self, qc):
        qc.return_value = {
            "passed": True,
            "technical_status": "valid",
            "technical_score": 1.0,
            "semantic_score": None,
            "issues": [],
            "retry_prompt": "",
            "skipped": False,
        }
        result = asc_ai.quality_control(
            "task",
            {"scene_id": "scene_01", "visual_prompt": "greenhouse"},
            "/tmp/unused.png",
            media_kind="image",
        )
        self.assertTrue(result["passed"])
        self.assertIsNone(result["semantic_score"])

    @patch("app.services.asc_ai_qc.requests.post")
    def test_structural_qc_registers_artifact_without_vlm_call(self, post):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "scene.png"
            path.write_bytes(b"non-empty-generated-artifact")
            upload = MagicMock()
            upload.raise_for_status.return_value = None
            upload.json.return_value = {"artifact_id": "art-structural"}
            post.return_value = upload

            result = asc_ai_qc.quality_control(
                prompt_url="http://prompt:8094",
                visual_url="http://visual:8095",
                task_id="task",
                scene={"scene_id": "scene_02", "visual_prompt": "greenhouse"},
                media_path=str(path),
                media_kind="image",
                run_visual_analysis=False,
            )

            self.assertTrue(result["passed"])
            self.assertTrue(result["visual_analysis_skipped"])
            self.assertEqual(result["provider"], "local-structural")
            self.assertEqual(result["artifact_id"], "art-structural")
            self.assertEqual(post.call_count, 1)
            self.assertTrue(
                post.call_args.args[0].endswith(
                    "/api/v1/prompt-intelligence/artifacts"
                )
            )

    @patch("app.services.asc_ai_qc.requests.post")
    def test_qc_calls_real_evidence_endpoint_and_forbids_external_egress(self, post):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "scene.png"
            path.write_bytes(b"fake-image")
            upload = MagicMock()
            upload.raise_for_status.return_value = None
            upload.json.return_value = {"artifact_id": "art-123"}
            analyze = MagicMock()
            analyze.raise_for_status.return_value = None
            analyze.json.return_value = {
                "artifact_id": "art-123",
                "external_egress": False,
                "provider": "local-qwen3-vl",
                "provider_model": "Qwen3VL-8B-Instruct-Q4_K_M.gguf",
                "actual_image_spec": {
                    "confidence": 0.9,
                    "artifact_cues": {"defects": []},
                    "technical_assessment": {
                        "status": "valid",
                        "artifact_usable": True,
                        "reasons": [],
                    },
                },
            }
            post.side_effect = [upload, analyze]
            result = asc_ai_qc.quality_control(
                prompt_url="http://prompt:8094",
                visual_url="http://visual:8095",
                task_id="task",
                scene={"scene_id": "scene_01", "visual_prompt": "greenhouse"},
                media_path=str(path),
                media_kind="image",
            )
            self.assertTrue(result["passed"])
            self.assertIsNone(result["semantic_score"])
            self.assertTrue(
                post.call_args_list[1].args[0].endswith("/api/v1/evidence/analyze")
            )
            self.assertFalse(
                post.call_args_list[1].kwargs["json"]["external_analysis_allowed"]
            )

    @patch("app.services.asc_ai_director._json_request")
    def test_director_lease_uses_scheduler_contract(self, request):
        request.side_effect = [
            {"job": {"job_id": "job-1"}, "initial_stage_id": "stage-1"},
            {},
            {},
            {"decision": "GRANTED", "lease": {"lease_id": "lease-1"}},
            {"vram_used_mb": 1200},
            {},
            {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"script":"Текст","scenes":[{"scene_id":"scene_01",'
                                '"narration":"Текст","duration_seconds":5,'
                                '"visual_strategy":"LOCAL_IMAGE",'
                                '"visual_prompt":"modern greenhouse","motion_prompt":"",'
                                '"transition":"cut","overlay_text":""}]}'
                            )
                        }
                    }
                ]
            },
            {"vram_used_mb": 1200},
            {},
            {},
        ]
        runtime = asc_ai_director.SchedulerManagedLocalLLM(
            "http://scheduler:8090",
            "http://prompt-llm:8080",
            admission_timeout=1,
        )
        text = runtime.chat("plan", timeout=10, temperature=0.2, max_tokens=100)
        self.assertIn('"script"', text)
        urls = [call.args[1] for call in request.call_args_list]
        self.assertIn("http://scheduler:8090/api/v1/leases/request", urls)
        self.assertIn("http://prompt-llm:8080/v1/chat/completions", urls)

    def test_production_manifest_survives_unavailable_video_workflow(self):
        with tempfile.TemporaryDirectory() as temp_dir, (
            patch.object(asc_ai.utils, "task_dir", return_value=temp_dir),
            patch.object(
                asc_ai,
                "_image_binding",
                return_value={
                    "workflow_id": "image-v1",
                    "model_id": "image-model",
                    "default_resolution": "1024x1024",
                },
            ),
            patch.object(
                asc_ai,
                "_video_binding",
                side_effect=asc_ai.AscAIError("no Wan workflow"),
            ),
            patch.object(
                asc_ai,
                "generate_image",
                return_value=str(Path(temp_dir) / "image.png"),
            ),
            patch.object(
                asc_ai,
                "quality_control",
                return_value={
                    "passed": True,
                    "technical_status": "valid",
                    "technical_score": 1.0,
                    "semantic_score": None,
                    "issues": [],
                    "retry_prompt": "",
                },
            ),
            patch.object(
                asc_ai,
                "generate_video_from_image",
                side_effect=asc_ai.AscAIError("video unavailable"),
            ),
            patch.object(
                asc_ai.video,
                "render_image_zoom_video",
                return_value=str(Path(temp_dir) / "fallback.mp4"),
            ),
        ):
            paths = asc_ai.generate_scene_materials(
                task_id="task",
                plan={
                    "schema_version": "mpt.director.v2",
                    "director_provider": "asc-ai-local-qwen3",
                    "gpu_policy": "scheduler_managed",
                    "scenes": [
                        {
                            "scene_id": "scene_01",
                            "visual_strategy": "LOCAL_VIDEO",
                            "visual_prompt": "greenhouse",
                            "motion_prompt": "slow push in",
                            "duration_seconds": 7,
                        }
                    ],
                },
                audio_duration=4.0,
                aspect="9:16",
                clip_duration=5,
            )
            self.assertEqual(paths, [str(Path(temp_dir) / "fallback.mp4")])
            manifest = json.loads(
                (Path(temp_dir) / "production-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["status"], "complete")
            self.assertFalse(manifest["workflow_snapshot"]["video"]["available"])
            self.assertEqual(
                manifest["scenes"][0]["fallback"], "video_aspect_unsupported"
            )
            self.assertEqual(
                manifest["scenes"][0]["effective_duration_seconds"], 7
            )
            asc_ai.video.render_image_zoom_video.assert_called_once_with(
                str(Path(temp_dir) / "image.png"), 7
            )
            asc_ai.generate_video_from_image.assert_not_called()

    @patch("app.services.asc_ai._output_path", return_value="/tmp/generated.png")
    @patch("app.services.asc_ai._request_json", return_value={"outputs": [{"path": "/tmp/generated.png"}]})
    @patch("app.services.asc_ai._workflow_resolution", return_value=(1024, 1024))
    @patch(
        "app.services.asc_ai._image_binding",
        return_value={"workflow_id": "image-v1", "model_id": "model-v1"},
    )
    def test_image_qc_retries_use_distinct_idempotency_keys(
        self, _binding, _resolution, request_json, _output
    ):
        scene = {"scene_id": "scene_01", "visual_prompt": "greenhouse"}
        asc_ai.generate_image("task-1", scene, "9:16", attempt=1)
        asc_ai.generate_image("task-1", scene, "9:16", attempt=2)

        first = request_json.call_args_list[0].kwargs["json"]["idempotency_key"]
        second = request_json.call_args_list[1].kwargs["json"]["idempotency_key"]
        self.assertNotEqual(first, second)
        self.assertTrue(first.endswith("-attempt-1"))
        self.assertTrue(second.endswith("-attempt-2"))

    def test_execution_provenance_is_replayable_but_omits_internal_model_path(self):
        result = asc_ai._execution_provenance(
            {
                "state": "COMPLETED",
                "job_id": "job-1",
                "stage_id": "stage-1",
                "lease_id": "lease-1",
                "workflow": {
                    "workflow_id": "krea_txt2img.v1",
                    "version": 3,
                    "graph_sha256": "graph-sha",
                },
                "model": {
                    "model_id": "mdl_diffusion_model_123",
                    "sha256": "model-sha",
                    "path": "/srv/ai/models/private/model.safetensors",
                },
                "settings": {
                    "seed": 123,
                    "steps": 8,
                    "cfg": 1.0,
                    "width": 768,
                    "height": 1344,
                    "internal_only": "drop-me",
                },
                "outputs": [
                    {
                        "artifact_id": "artifact-1",
                        "path": "/srv/ai-data/output/frame.png",
                        "sha256": "output-sha",
                        "source_path": "/internal/comfy/path",
                    }
                ],
                "telemetry": {
                    "observed_peak": {
                        "vram_used_mb": 12000,
                        "temperature_c": 72,
                        "private_metric": "drop-me",
                    }
                },
                "timing": {"duration_seconds": 12.5},
            }
        )

        self.assertEqual(result["job_id"], "job-1")
        self.assertEqual(result["settings"]["seed"], 123)
        self.assertEqual(result["outputs"][0]["artifact_id"], "artifact-1")
        self.assertEqual(result["peak"]["vram_used_mb"], 12000)
        self.assertNotIn("path", result["model"])
        self.assertNotIn("internal_only", result["settings"])
        self.assertNotIn("source_path", result["outputs"][0])
        self.assertNotIn("private_metric", result["peak"])

    def test_i2v_staging_replaces_stale_source_on_task_retry(self):
        old_root = config.asc_ai.get("input_root")
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                input_root = Path(temp_dir) / "input"
                input_root.mkdir()
                config.asc_ai["input_root"] = str(input_root)
                first = Path(temp_dir) / "first.png"
                second = Path(temp_dir) / "second.png"
                first.write_bytes(b"first-image")
                second.write_bytes(b"second-image")

                relative = asc_ai.stage_image_for_video(
                    "task-1", "scene_01", str(first)
                )
                target = input_root / relative
                self.assertEqual(target.read_bytes(), b"first-image")

                same_relative = asc_ai.stage_image_for_video(
                    "task-1", "scene_01", str(second)
                )
                self.assertEqual(relative, same_relative)
                self.assertEqual(target.read_bytes(), b"second-image")
        finally:
            if old_root is None:
                config.asc_ai.pop("input_root", None)
            else:
                config.asc_ai["input_root"] = old_root

    @patch("app.services.asc_ai._workflow_catalog")
    def test_video_aspect_gate_skips_vertical_when_production_wan_is_landscape_only(
        self, catalog
    ):
        catalog.return_value = [
            {
                "workflow_id": "wan_i2v_default.v1",
                "purpose": "image_to_video",
                "model_id": "mdl_diffusion_model_runtime",
                "operator_only": False,
                "validation_state": "production",
                "default_resolution": "640x640",
                "supported_resolutions": ["640x640", "832x480"],
                "defaults": {"length": 81},
            }
        ]
        vertical = asc_ai._video_compatibility("9:16")
        landscape = asc_ai._video_compatibility("16:9")

        self.assertFalse(vertical["compatible"])
        self.assertTrue(landscape["compatible"])
        self.assertEqual(landscape["resolution"], "832x480")

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


    @patch.object(asc_ai, "persist_final_quality_report")
    @patch.object(asc_ai, "quality_control")
    @patch.object(asc_ai, "_inspect_final_video")
    def test_final_quality_control_combines_structural_and_visual_checks(
        self, inspect_video, quality_control, persist_report
    ):
        config.asc_ai.update(
            {
                "final_qc_enabled": True,
                "final_qc_required": True,
                "final_qc_visual_analysis": True,
            }
        )
        inspect_video.return_value = {
            "passed": True,
            "issues": [],
            "duration_seconds": 12.0,
            "width": 1080,
            "height": 1920,
            "has_audio": True,
            "size_bytes": 1024,
        }
        quality_control.return_value = {
            "passed": True,
            "technical_status": "valid",
            "issues": [],
            "provider": "local",
        }

        report = asc_ai.final_quality_control(
            "final-qc-task",
            ["final-1.mp4", "final-2.mp4"],
            expected_duration=12.0,
        )

        self.assertTrue(report["passed"])
        self.assertEqual(report["status"], "passed")
        self.assertEqual(len(report["outputs"]), 2)
        self.assertEqual(inspect_video.call_count, 2)
        self.assertEqual(quality_control.call_count, 2)
        for call in quality_control.call_args_list:
            self.assertEqual(call.kwargs["media_kind"], "final_video")
            self.assertTrue(call.kwargs["run_visual_analysis"])
        persist_report.assert_called_once()
        self.assertEqual(
            persist_report.call_args.args[1]["schema_version"],
            "mpt.final-qc.v1",
        )

    @patch.object(asc_ai, "persist_final_quality_report")
    @patch.object(asc_ai, "quality_control")
    @patch.object(asc_ai, "_inspect_final_video")
    def test_final_quality_control_fails_closed_on_structural_error(
        self, inspect_video, quality_control, persist_report
    ):
        config.asc_ai.update(
            {
                "final_qc_enabled": True,
                "final_qc_required": True,
                "final_qc_visual_analysis": True,
            }
        )
        inspect_video.return_value = {
            "passed": False,
            "issues": ["final video has no audio stream"],
            "duration_seconds": 12.0,
            "width": 1080,
            "height": 1920,
            "has_audio": False,
            "size_bytes": 1024,
        }

        with self.assertRaisesRegex(
            asc_ai.AscAIError,
            "final production QC failed: final video has no audio stream",
        ):
            asc_ai.final_quality_control(
                "final-qc-bad",
                ["final.mp4"],
                expected_duration=12.0,
            )

        quality_control.assert_not_called()
        persist_report.assert_called_once()
        persisted = persist_report.call_args.args[1]
        self.assertFalse(persisted["passed"])
        self.assertEqual(persisted["status"], "failed")



if __name__ == "__main__":
    unittest.main()
