import json
import tempfile
import unittest
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

    @patch("app.services.asc_ai._component_health", return_value={"status": "ok"})
    def test_video_preflight_checks_full_local_pipeline(self, health):
        params = self.params(custom_audio_file="")
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
        self.assertEqual(payload["prompt_compiler_id"], "CHARACTER_HUB")

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
                manifest["scenes"][0]["fallback"], "video_generation_failed"
            )

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
