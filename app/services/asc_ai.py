from __future__ import annotations

import json
import math
import os
import re
import shutil
from pathlib import Path
from typing import Any

import requests
from loguru import logger

from app.config import config
from app.services import (
    asc_ai_character,
    asc_ai_director,
    asc_ai_qc,
    public_media,
    video,
)
from app.utils import utils


class AscAIError(RuntimeError):
    pass


_LOCAL_VIDEO_SOURCES = {"asc_ai", "local"}
_LOCAL_TTS_SERVERS = {"chatterbox"}
_LOCAL_BGM_TYPES = {"", "none", "random", "preset", "local", "custom"}


def _setting(name: str, default: Any = None) -> Any:
    return config.asc_ai.get(name, default)


def enabled() -> bool:
    return bool(_setting("enabled", True))


def local_only() -> bool:
    return bool(_setting("local_only", True))


def _prompt_url() -> str:
    return (
        os.getenv("ASC_AI_PROMPT_URL")
        or str(_setting("prompt_intelligence_url", "http://127.0.0.1:8094"))
    ).rstrip("/")


def _image_url() -> str:
    return (
        os.getenv("ASC_AI_IMAGE_ADAPTER_URL")
        or str(_setting("image_adapter_url", "http://127.0.0.1:8091"))
    ).rstrip("/")


def _visual_url() -> str:
    return (
        os.getenv("ASC_AI_VISUAL_ANALYZER_URL")
        or str(_setting("visual_analyzer_url", "http://127.0.0.1:8095"))
    ).rstrip("/")


def _character_url() -> str:
    return (
        os.getenv("ASC_AI_CHARACTER_HUB_URL")
        or str(_setting("character_hub_url", "http://127.0.0.1:8096"))
    ).rstrip("/")


def _scheduler_url() -> str:
    return (
        os.getenv("ASC_AI_SCHEDULER_URL")
        or str(_setting("scheduler_url", "http://127.0.0.1:8090"))
    ).rstrip("/")


def _prompt_llm_url() -> str:
    return (
        os.getenv("ASC_AI_PROMPT_LLM_URL")
        or str(_setting("prompt_llm_url", "http://127.0.0.1:8080"))
    ).rstrip("/")


def _chatterbox_root_url() -> str:
    base_url = (
        os.getenv("MPT_CHATTERBOX_BASE_URL")
        or str(config.chatterbox.get("base_url", "http://127.0.0.1:4123/v1"))
    ).rstrip("/")
    return base_url[:-3] if base_url.endswith("/v1") else base_url


def list_characters() -> list[str]:
    try:
        return asc_ai_character.list_characters(
            _character_url(),
            timeout=float(_setting("character_hub_timeout_seconds", 10)),
        )
    except asc_ai_character.CharacterHubError as exc:
        raise AscAIError(str(exc)) from exc


def get_character_identity(character_id: str) -> dict:
    try:
        return asc_ai_character.get_character(
            _character_url(),
            character_id,
            timeout=float(_setting("character_hub_timeout_seconds", 10)),
        )
    except asc_ai_character.CharacterHubError as exc:
        raise AscAIError(str(exc)) from exc


def _request_json(
    method: str,
    url: str,
    *,
    timeout: tuple[float, float] = (5, 900),
    **kwargs,
) -> dict:
    try:
        response = requests.request(method, url, timeout=timeout, **kwargs)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise AscAIError(f"ASC-AI request failed: {type(exc).__name__}") from exc
    except ValueError as exc:
        raise AscAIError("ASC-AI returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise AscAIError("ASC-AI returned a non-object response")
    return payload


def _workflow_catalog() -> list[dict]:
    try:
        response = requests.get(
            _image_url() + "/api/v1/image-workflows", timeout=(3, 20)
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise AscAIError(
            f"ASC-AI workflow catalog request failed: {type(exc).__name__}"
        ) from exc
    except ValueError as exc:
        raise AscAIError("ASC-AI workflow catalog returned invalid JSON") from exc
    if not isinstance(payload, list):
        raise AscAIError("ASC-AI workflow catalog returned a non-list response")
    return [row for row in payload if isinstance(row, dict)]


def _workflow_score(row: dict, *, purpose: str) -> tuple[int, int]:
    state = str(row.get("validation_state") or "").lower()
    preferred = any(
        marker in state
        for marker in ("preferred", "production", "user_approved", "validated")
    )
    risky = any(
        marker in state
        for marker in ("experimental", "candidate", "canary", "shadow")
    )
    return (2 if preferred else 1, 0 if risky else 1)


def _resolve_workflow(
    *,
    preferred_id: str,
    purpose: str,
    fallback_ids: tuple[str, ...] = (),
) -> dict:
    rows = [
        row
        for row in _workflow_catalog()
        if row.get("purpose") == purpose and not row.get("operator_only")
    ]
    if not rows:
        raise AscAIError(f"ASC-AI has no usable {purpose} workflow")

    by_id = {str(row.get("workflow_id") or ""): row for row in rows}
    for workflow_id in (preferred_id, *fallback_ids):
        if workflow_id and workflow_id in by_id:
            return by_id[workflow_id]

    return max(rows, key=lambda row: _workflow_score(row, purpose=purpose))


def _workflow_resolution(row: dict, aspect: Any = None) -> tuple[int, int]:
    values = []
    default_value = str(row.get("default_resolution") or "")
    if default_value:
        values.append(default_value)
    supported = row.get("supported_resolutions") or []
    if isinstance(supported, list):
        values.extend(str(value) for value in supported)

    candidates: list[tuple[int, int]] = []
    for value in values:
        match = re.fullmatch(r"(\d+)x(\d+)", value.strip())
        if not match:
            continue
        pair = (int(match.group(1)), int(match.group(2)))
        if pair not in candidates:
            candidates.append(pair)
    if not candidates:
        raise AscAIError(
            f"ASC-AI workflow {row.get('workflow_id')} has no valid resolution"
        )

    aspect_value = str(getattr(aspect, "value", aspect) or "").strip()
    aspect_match = re.fullmatch(r"(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)", aspect_value)
    if not aspect_match:
        default_match = re.fullmatch(r"(\d+)x(\d+)", default_value)
        if default_match:
            return int(default_match.group(1)), int(default_match.group(2))
        return candidates[0]

    target_ratio = float(aspect_match.group(1)) / float(aspect_match.group(2))
    return min(
        candidates,
        key=lambda pair: (
            abs((pair[0] / pair[1]) - target_ratio),
            -(pair[0] * pair[1]),
        ),
    )


def _effective_audio_policy(params=None) -> tuple[str, str, str]:
    voice_mode = str(config.ui.get("voice_mode", "tts") or "tts")
    tts_server = str(config.ui.get("tts_server", "chatterbox") or "chatterbox")
    custom_audio = str(
        getattr(params, "custom_audio_file", "") if params is not None else ""
    ).strip()
    task_voice = str(
        getattr(params, "voice_name", "") if params is not None else ""
    ).strip()
    # A headless/local task can explicitly carry a Chatterbox voice even when
    # an old WebUI session persisted Azure/another provider globally.
    if voice_mode == "tts" and task_voice.startswith("chatterbox:"):
        tts_server = "chatterbox"
    return voice_mode, tts_server, custom_audio


def _chatterbox_voice_info(params=None) -> dict:
    voice_name = str(
        getattr(params, "voice_name", "") if params is not None else ""
    ).strip()
    if voice_name.startswith("chatterbox:"):
        voice_name = voice_name.split(":", 1)[1].strip()
    if not voice_name:
        voice_name = str(
            config.chatterbox.get("default_voice", "ru-default") or "ru-default"
        ).strip()

    payload = _request_json(
        "GET",
        _chatterbox_root_url() + "/v1/voices",
        timeout=(3, 10),
    )
    voices = payload.get("voices") or []
    if not isinstance(voices, list):
        raise AscAIError("Chatterbox voice catalog returned an invalid response")

    selected = None
    for row in voices:
        if not isinstance(row, dict):
            continue
        aliases = [
            str(value or "").strip()
            for value in (row.get("aliases") or [])
            if str(value or "").strip()
        ]
        if voice_name == str(row.get("name") or "").strip() or voice_name in aliases:
            selected = row
            break
    if not selected:
        raise AscAIError(
            f"Chatterbox voice '{voice_name}' is not installed; upload a local "
            "voice to /v1/voices before production"
        )

    requested_language = str(
        getattr(params, "video_language", "") if params is not None else ""
    ).split("-", 1)[0].lower().strip()
    voice_language = str(selected.get("language") or "").lower().strip()
    if (
        requested_language
        and requested_language not in {"auto", "default"}
        and voice_language
        and requested_language != voice_language
    ):
        raise AscAIError(
            f"Chatterbox voice '{voice_name}' language={voice_language} does not "
            f"match requested video language={requested_language}"
        )
    return {
        "name": str(selected.get("name") or voice_name),
        "language": voice_language or None,
        "aliases": selected.get("aliases") or [],
    }


def _component_health(name: str, url: str) -> dict:
    result = _request_json("GET", url, timeout=(3, 10))
    status = str(result.get("status") or "").lower()
    if status and status not in {"ok", "healthy", "ready", "running"}:
        raise AscAIError(f"{name} is not ready: status={status}")
    return result


def health(params=None, *, stop_at: str = "video") -> dict:
    """Check only dependencies required by the current local production path."""
    director_enabled = (
        bool(getattr(params, "director_enabled", True))
        if params is not None
        else True
    )
    video_source = (
        str(getattr(params, "video_source", "asc_ai") or "asc_ai")
        if params is not None
        else "asc_ai"
    )
    voice_mode, tts_server, custom_audio = _effective_audio_policy(params)

    checks: list[tuple[str, str]] = []
    if director_enabled:
        checks.extend(
            [
                ("scheduler", _scheduler_url() + "/health"),
                ("prompt_llm", _prompt_llm_url() + "/health"),
            ]
        )
        if str(
            getattr(params, "director_character_id", "") if params is not None else ""
        ).strip():
            checks.append(("character_hub", _character_url() + "/health"))
    needs_visuals = video_source == "asc_ai" and stop_at in {"materials", "video"}
    if needs_visuals:
        checks.append(("image_adapter", _image_url() + "/health"))
        if bool(_setting("qc_enabled", True)):
            checks.extend(
                [
                    ("prompt_intelligence", _prompt_url() + "/health"),
                    ("visual_analyzer", _visual_url() + "/health"),
                ]
            )
    needs_audio = stop_at not in {"script", "terms"}
    if (
        needs_audio
        and not custom_audio
        and voice_mode == "tts"
        and tts_server == "chatterbox"
    ):
        checks.append(("chatterbox_tts", _chatterbox_root_url() + "/health"))

    result = {}
    seen = set()
    for name, url in checks:
        if name in seen:
            continue
        seen.add(name)
        result[name] = _component_health(name, url)
    if "chatterbox_tts" in seen:
        result["chatterbox_voice"] = _chatterbox_voice_info(params)
    return result


def preflight(params, *, stop_at: str = "video") -> dict:
    if not enabled():
        return {}
    validate_local_only(params)
    return health(params, stop_at=stop_at)


def validate_local_only(params) -> None:
    if not enabled() or not local_only():
        return
    if params.video_source not in _LOCAL_VIDEO_SOURCES:
        raise AscAIError(
            "local-only mode allows only ASC-AI or user-provided local visual materials"
        )
    if params.video_source == "asc_ai" and not getattr(
        params, "director_enabled", False
    ):
        raise AscAIError("ASC-AI visual source requires Director planning")
    voice_mode, tts_server, custom_audio = _effective_audio_policy(params)
    if (
        not custom_audio
        and voice_mode == "tts"
        and tts_server not in _LOCAL_TTS_SERVERS
    ):
        raise AscAIError(
            "local-only mode requires self-hosted Chatterbox TTS or uploaded/no voice"
        )
    if (
        not getattr(params, "director_enabled", False)
        and not str(getattr(params, "video_script", "") or "").strip()
    ):
        raise AscAIError(
            "local-only mode requires Director or a user-provided script; "
            "cloud LLM script generation is disabled"
        )
    subtitle_provider = str(
        config.app.get("subtitle_provider", "whisper") or "whisper"
    )
    if params.subtitle_enabled and subtitle_provider != "whisper":
        raise AscAIError("local-only mode requires local Whisper subtitles")
    if str(params.bgm_type or "").lower() not in _LOCAL_BGM_TYPES:
        raise AscAIError("local-only mode blocks remote AI music providers")
    if bool(config.app.get("upload_post_auto_upload", False)):
        raise AscAIError("local-only mode blocks automatic third-party publishing")


def create_director_plan(
    params,
    *,
    settings_overrides: dict | None = None,
) -> dict:
    if not enabled():
        raise AscAIError("ASC-AI integration is disabled")
    try:
        character_identity = None
        character_id = str(
            getattr(params, "director_character_id", "") or ""
        ).strip()
        if character_id:
            character_identity = asc_ai_character.get_character(
                _character_url(),
                character_id,
                timeout=float(_setting("character_hub_timeout_seconds", 10)),
            )
        director_settings = dict(config.asc_ai)
        task_research_override = getattr(
            params, "director_public_research_enabled", None
        )
        if task_research_override is not None:
            director_settings["public_research_enabled"] = bool(
                task_research_override
            )
        if settings_overrides:
            director_settings.update(settings_overrides)
        plan = asc_ai_director.create_plan(
            params,
            director_settings,
            scheduler_url=_scheduler_url(),
            prompt_llm_url=_prompt_llm_url(),
            character_identity=character_identity,
        )
        if character_identity:
            plan["character_identity"] = character_identity
            for scene in plan.get("scenes") or []:
                if isinstance(scene, dict):
                    scene["character_identity"] = character_identity
        return enrich_director_plan(plan)
    except (asc_ai_director.DirectorError, asc_ai_character.CharacterHubError) as exc:
        raise AscAIError(str(exc)) from exc


def enrich_director_plan(plan: dict) -> dict:
    """Use Prompt Intelligence only for expensive Director scenes.

    This intentionally does not replace the Director visual prompt: Prompt
    Intelligence contributes its governed resource routing/provenance while the
    production Director remains authoritative for story continuity.
    """
    # The current ASC-AI Prompt Intelligence router is local-first but may fall
    # back to an external provider. PlanCreate has no per-request "local only"
    # switch, so strict local-only production must never call /plans.
    if local_only() or not bool(_setting("pi_enrich_video_scenes", False)):
        return plan

    scenes = []
    for scene in plan.get("scenes") or []:
        current = dict(scene)
        if current.get("visual_strategy") != "LOCAL_VIDEO":
            scenes.append(current)
            continue

        request_text = str(current.get("visual_prompt") or "").strip()
        motion = str(current.get("motion_prompt") or "").strip()
        if motion:
            request_text += f". Intended motion: {motion}"
        if not request_text:
            scenes.append(current)
            continue

        try:
            pi_plan = _request_json(
                "POST",
                _prompt_url() + "/api/v1/prompt-intelligence/plans",
                json={
                    "raw_user_request": request_text,
                    "target_model": "krea",
                    "resource_mode": str(_setting("pi_resource_mode", "AUTO")),
                },
                timeout=(5, float(_setting("pi_planning_timeout_seconds", 180))),
            )
            selected = (
                ((pi_plan.get("derived_prompt") or {}).get("resource_plan") or {})
                .get("selected_resources")
                or []
            )
            loras = []
            for row in selected:
                if not isinstance(row, dict):
                    continue
                model_id = str(row.get("model_id") or row.get("lora_id") or "").strip()
                if not model_id:
                    continue
                try:
                    strength = float(row.get("strength", 1.0))
                except (TypeError, ValueError):
                    strength = 1.0
                loras.append(
                    {
                        "model_id": model_id,
                        "strength": max(0.0, min(2.0, strength)),
                    }
                )
            current["prompt_intelligence"] = {
                "plan_id": pi_plan.get("plan_id"),
                "scene_spec": pi_plan.get("scene_spec"),
                "evidence_trace": pi_plan.get("evidence_trace"),
                "selected_resources": selected,
            }
            current["image_loras"] = loras
        except Exception as exc:
            logger.warning(
                "ASC-AI Prompt Intelligence enrichment failed; keeping Director "
                f"scene unchanged: scene={current.get('scene_id')}, "
                f"error={type(exc).__name__}"
            )
        scenes.append(current)

    result = dict(plan)
    result["scenes"] = scenes
    return result


def persist_director_plan(task_id: str, plan: dict) -> str:
    target = Path(utils.task_dir(task_id)) / "director-plan.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.partial")
    temporary.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, target)
    return str(target)


def director_terms(plan: dict) -> list[str]:
    result = []
    for scene in plan.get("scenes") or []:
        prompt = str(scene.get("visual_prompt") or "").strip()
        if prompt:
            result.append(prompt)
    return result


def _workflow_by_id(workflow_id: str) -> dict | None:
    workflow_id = str(workflow_id or "").strip()
    if not workflow_id:
        return None
    for row in _workflow_catalog():
        if (
            str(row.get("workflow_id") or "") == workflow_id
            and not row.get("operator_only")
        ):
            return row
    return None


def _image_binding(scene: dict | None = None) -> dict:
    identity = (scene or {}).get("character_identity") or {}
    refs = identity.get("reference_artifact_ids") or []
    if refs:
        for workflow_id in (
            identity.get("preferred_workflow"),
            identity.get("fallback_workflow"),
        ):
            row = _workflow_by_id(str(workflow_id or ""))
            if row:
                return row

    return _resolve_workflow(
        preferred_id=str(
            _setting("image_workflow_id", "lustify_krea_v10_native_int8.v1")
        ),
        purpose="text_to_image",
        fallback_ids=("lustify_krea_v10_native_int8.v1", "krea_txt2img.v1"),
    )


def _video_binding() -> dict:
    return _resolve_workflow(
        preferred_id=str(_setting("video_workflow_id", "wan_i2v_default.v1")),
        purpose="image_to_video",
        fallback_ids=("wan_i2v_default.v1", "wan22_ti2v_5b.v1"),
    )


def _target_aspect_ratio(aspect: Any) -> float | None:
    value = str(getattr(aspect, "value", aspect) or "").strip()
    match = re.fullmatch(r"(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)", value)
    if not match:
        return None
    denominator = float(match.group(2))
    if denominator <= 0:
        return None
    return float(match.group(1)) / denominator


def _video_compatibility(aspect: Any) -> dict:
    binding = _video_binding()
    width, height = _workflow_resolution(binding, aspect)
    target = _target_aspect_ratio(aspect)
    actual = width / height
    log_error = (
        abs(math.log(actual / target))
        if target and actual > 0
        else 0.0
    )
    max_error = max(
        0.01,
        float(_setting("video_max_aspect_log_error", 0.18)),
    )
    return {
        "available": True,
        "compatible": log_error <= max_error,
        "workflow_id": binding.get("workflow_id"),
        "model_id": binding.get("model_id"),
        "resolution": f"{width}x{height}",
        "target_aspect": str(getattr(aspect, "value", aspect) or ""),
        "aspect_log_error": round(log_error, 4),
        "max_aspect_log_error": max_error,
        "binding": binding,
    }


def _execution_provenance(result: dict) -> dict:
    settings = result.get("settings") or {}
    if not isinstance(settings, dict):
        settings = {}
    keep_settings = {
        key: settings.get(key)
        for key in (
            "seed",
            "steps",
            "cfg",
            "width",
            "height",
            "length",
            "fps",
            "sampler",
            "scheduler",
            "denoise",
        )
        if settings.get(key) is not None
    }

    workflow = result.get("workflow") or {}
    if not isinstance(workflow, dict):
        workflow = {}
    model = result.get("model") or {}
    if not isinstance(model, dict):
        model = {}
    timing = result.get("timing") or {}
    if not isinstance(timing, dict):
        timing = {}
    telemetry = result.get("telemetry") or {}
    if not isinstance(telemetry, dict):
        telemetry = {}
    peak = telemetry.get("observed_peak") or {}
    if not isinstance(peak, dict):
        peak = {}

    outputs = []
    for row in (result.get("outputs") or [])[:4]:
        if not isinstance(row, dict):
            continue
        outputs.append(
            {
                key: row.get(key)
                for key in ("artifact_id", "role", "path", "sha256")
                if row.get(key) is not None
            }
        )

    return {
        "state": result.get("state"),
        "job_id": result.get("job_id"),
        "stage_id": result.get("stage_id"),
        "lease_id": result.get("lease_id"),
        "workflow": {
            key: workflow.get(key)
            for key in ("workflow_id", "version", "graph_sha256")
            if workflow.get(key) is not None
        },
        "model": {
            key: model.get(key)
            for key in ("model_id", "sha256")
            if model.get(key) is not None
        },
        "settings": keep_settings,
        "outputs": outputs,
        "peak": {
            key: peak.get(key)
            for key in (
                "vram_used_mb",
                "gpu_utilization_percent",
                "temperature_c",
                "power_draw_w",
            )
            if peak.get(key) is not None
        },
        "duration_seconds": timing.get("duration_seconds"),
    }


def _output_path(result: dict, media_type: str) -> str:
    outputs = result.get("outputs") or []
    if not outputs or not isinstance(outputs[0], dict):
        raise AscAIError(f"ASC-AI produced no {media_type} output")
    path = str(outputs[0].get("path") or "")
    if not path:
        raise AscAIError(f"ASC-AI {media_type} output has no path")
    if not Path(path).is_file():
        raise AscAIError(
            f"ASC-AI output is not visible to MoneyPrinterTurbo: {path}. "
            "Mount /srv/ai-data into the MoneyPrinterTurbo container."
        )
    return path


def generate_image(
    task_id: str,
    scene: dict,
    aspect: Any,
    *,
    attempt: int = 1,
    binding: dict | None = None,
    return_execution: bool = False,
):
    binding = binding or _image_binding(scene)
    width, height = _workflow_resolution(binding, aspect)
    prompt = str(scene.get("visual_prompt") or "").strip()
    if not prompt:
        raise AscAIError("Director scene has no visual prompt")
    identity = scene.get("character_identity") or {}
    appearance = str(identity.get("appearance") or "").strip()
    style_suffix = str(identity.get("style_prompt_suffix") or "").strip()
    if appearance:
        prompt = (
            f"Recurring character identity anchor: {appearance}. "
            f"Scene: {prompt}"
        )
    if style_suffix:
        prompt = f"{prompt}. {style_suffix}"

    generic_negative = str(
        _setting(
            "image_negative_prompt",
            "text, watermark, logo, duplicate subjects, distorted anatomy, low quality",
        )
    ).strip()
    negative_parts = [generic_negative] if generic_negative else []
    negative_parts.extend(
        str(value).strip()
        for value in (identity.get("forbidden_traits") or [])
        if str(value).strip()
    )
    negative_parts.extend(
        str(value).strip()
        for value in (identity.get("forbidden_prompt_terms") or [])
        if str(value).strip()
    )
    negative_prompt = ", ".join(dict.fromkeys(negative_parts))

    reference_inputs = [
        {
            "artifact_id": str(artifact_id),
            "role": "CHARACTER",
            "input_name": f"character_ref_{index}",
        }
        for index, artifact_id in enumerate(
            (identity.get("reference_artifact_ids") or [])[:8]
        )
        if str(artifact_id).strip()
    ]

    loras = list(scene.get("image_loras") or [])
    present_loras = {
        str(row.get("model_id") or "")
        for row in loras
        if isinstance(row, dict)
    }
    for lora_id in (identity.get("lora_resource_ids") or [])[:4]:
        lora_id = str(lora_id or "").strip()
        if lora_id and lora_id not in present_loras:
            loras.append({"model_id": lora_id, "strength": 1.0})
            present_loras.add(lora_id)

    scene_id = str(scene.get("scene_id") or "scene")
    overrides = {
        "width": width,
        "height": height,
        "steps": int(_setting("image_steps", 8)),
        "cfg": float(_setting("image_cfg", 1.0)),
        "batch": 1,
    }
    if reference_inputs:
        overrides["ref_boost"] = max(
            0.25, min(2.0, float(identity.get("ref_boost") or 1.0))
        )
        overrides["grounding_px"] = max(
            512, min(1536, int(identity.get("grounding_px") or 1024))
        )

    payload = {
        "workflow_id": str(binding["workflow_id"]),
        "model_id": str(binding["model_id"]),
        "prompt": prompt,
        "original_user_prompt": prompt,
        "prompt_compiler_id": "RAW",
        "prompt_compiler_version": (
            "mpt-director-character-v2" if identity else "mpt-director-v2"
        ),
        "negative_prompt": negative_prompt,
        "reference_inputs": reference_inputs,
        "overrides": overrides,
        "priority": int(_setting("priority", 120)),
        "idempotency_key": (
            f"mpt-{task_id}-{scene_id}-image-attempt-{max(1, int(attempt))}"
        )[:200],
        "loras": loras,
        "experimental_resources_opt_in": False,
        "experimental_model_opt_in": False,
    }
    result = _request_json(
        "POST",
        _image_url() + "/api/v1/images/generate",
        json=payload,
        timeout=(5, float(_setting("generation_timeout_seconds", 1800))),
    )
    output_path = _output_path(result, "image")
    if return_execution:
        return output_path, _execution_provenance(result)
    return output_path


def _safe_token(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-")[:80] or "item"


def stage_image_for_video(task_id: str, scene_id: str, image_path: str) -> str:
    input_root = Path(str(_setting("input_root", "/srv/ai-data/input"))).resolve()
    source = Path(image_path).resolve()
    if not source.is_file():
        raise AscAIError("generated source image is missing")
    suffix = source.suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise AscAIError("generated source image has an unsupported extension")
    relative = Path("moneyprinterturbo") / _safe_token(task_id) / (
        _safe_token(scene_id) + suffix
    )
    target = (input_root / relative).resolve()
    try:
        target.relative_to(input_root)
    except ValueError as exc:
        raise AscAIError("unsafe ASC-AI input path") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    # A task may be retried with the same task_id/scene_id after a better image
    # was generated. Never leave a stale I2V source from a previous run.
    if target.exists() or target.is_symlink():
        target.unlink()
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)
    return relative.as_posix()


def generate_video_from_image(
    task_id: str,
    scene: dict,
    image_path: str,
    *,
    binding: dict | None = None,
    return_execution: bool = False,
):
    binding = binding or _video_binding()
    scene_id = str(scene.get("scene_id") or "scene")
    input_name = stage_image_for_video(task_id, scene_id, image_path)
    prompt = str(scene.get("motion_prompt") or "").strip()
    if not prompt:
        prompt = (
            "subtle coherent natural motion, stable composition, no identity drift"
        )
    visual = str(scene.get("visual_prompt") or "").strip()
    combined_prompt = f"{visual}. Motion: {prompt}" if visual else prompt
    # Keep the GPU job on the validated ASC-AI production baseline. Director
    # timing is applied afterwards by local deterministic editing.
    duration = 5
    payload = {
        "workflow_id": str(binding["workflow_id"]),
        "model_id": str(binding["model_id"]),
        "prompt": combined_prompt,
        "negative_prompt": str(
            _setting(
                "video_negative_prompt",
                "identity drift, subject fusion, flicker, warped anatomy, text, watermark",
            )
        ),
        "input_name": input_name,
        "profile": str(_setting("video_profile", "STANDARD")),
        "duration_seconds": duration,
        "frames": int(
            (binding.get("defaults") or {}).get(
                "length", _setting("video_frames", 89)
            )
        ),
        "continuation_mode": "new",
        "segment_index": 1,
        "priority": int(_setting("priority", 120)),
        "idempotency_key": f"mpt-{task_id}-{scene_id}-video"[:200],
        "loras": [],
        "experimental_opt_in": False,
    }
    result = _request_json(
        "POST",
        _image_url() + "/api/v1/videos/generate",
        json=payload,
        timeout=(5, float(_setting("generation_timeout_seconds", 1800))),
    )
    output_path = _output_path(result, "video")
    if return_execution:
        return output_path, _execution_provenance(result)
    return output_path


def acquire_public_scene_image(
    task_id: str,
    scene: dict,
    aspect: Any,
) -> dict:
    if not bool(_setting("public_media_enabled", True)):
        raise AscAIError("public media routing is disabled")
    query = str(scene.get("public_media_query") or "").strip()
    if not query:
        raise AscAIError("PUBLIC_IMAGE scene has no public_media_query")
    aspect_value = str(getattr(aspect, "value", aspect) or "9:16")
    try:
        return public_media.acquire_commons_image(
            query,
            task_dir=utils.task_dir(task_id),
            scene_id=str(scene.get("scene_id") or "scene"),
            cache_dir=str(
                _setting(
                    "public_media_cache_dir",
                    utils.storage_dir("public_media", create=True),
                )
            ),
            aspect=aspect_value,
            max_results=int(_setting("public_media_max_results", 6)),
            timeout=float(_setting("public_media_timeout_seconds", 12)),
            cache_ttl_seconds=float(
                _setting("public_media_cache_ttl_seconds", 3600)
            ),
            max_bytes=int(
                _setting("public_media_max_bytes", 20 * 1024 * 1024)
            ),
        )
    except public_media.PublicMediaError as exc:
        raise AscAIError(str(exc)) from exc


def _image_vlm_required(
    scene_index: int,
    attempt: int,
    scene: dict,
) -> bool:
    policy = str(_setting("qc_image_policy", "adaptive") or "adaptive").lower()
    if policy not in {"full", "adaptive", "structural"}:
        policy = "full"
    if policy == "full":
        return True
    if policy == "structural":
        return False

    stride = max(1, int(_setting("qc_image_vlm_stride", 3)))
    has_identity = bool(
        (scene.get("character_identity") or {}).get("character_id")
    )
    return (
        scene_index == 0
        or scene_index % stride == 0
        or has_identity
        or attempt > 0
    )


def quality_control(
    task_id: str,
    scene: dict,
    media_path: str,
    *,
    media_kind: str,
    run_visual_analysis: bool = True,
) -> dict:
    if not bool(_setting("qc_enabled", True)):
        return {
            "passed": True,
            "technical_status": "skipped",
            "semantic_score": None,
            "technical_score": 1.0,
            "issues": [],
            "retry_prompt": "",
            "skipped": True,
        }
    try:
        return asc_ai_qc.quality_control(
            prompt_url=_prompt_url(),
            visual_url=_visual_url(),
            task_id=task_id,
            scene=scene,
            media_path=media_path,
            media_kind=media_kind,
            profile=str(_setting("qc_profile", "FAST")),
            upload_timeout=float(_setting("qc_upload_timeout_seconds", 120)),
            timeout=float(_setting("qc_timeout_seconds", 300)),
            run_visual_analysis=run_visual_analysis,
        )
    except asc_ai_qc.QCError as exc:
        raise AscAIError(str(exc)) from exc


def retime_director_plan(plan: dict, audio_duration: float) -> dict:
    """Deterministically fit Director scene timing to the real TTS duration.

    Every scene can be fitted into the typed 2..15 second range. LOCAL_VIDEO
    still consumes only one canonical five-second Wan job; the approved clip is
    trimmed or extended locally afterwards. No extra LLM or GPU inference is
    needed for retiming.
    """
    result = dict(plan)
    source_scenes = [
        dict(scene)
        for scene in (plan.get("scenes") or [])
        if isinstance(scene, dict)
    ]
    if not source_scenes:
        return result

    target_seconds = max(1, int(math.ceil(float(audio_duration or 0.0))))
    scene_indices = list(range(len(source_scenes)))
    requested = {}
    for index, scene in enumerate(source_scenes):
        original = max(2, min(15, int(scene.get("duration_seconds") or 5)))
        requested[index] = original
        scene["planned_duration_seconds"] = original

    feasible_min = 2 * len(scene_indices)
    feasible_max = 15 * len(scene_indices)
    bounded_target = max(feasible_min, min(feasible_max, target_seconds))

    weights = []
    for index in scene_indices:
        narration = str(source_scenes[index].get("narration") or "")
        narration_units = len(re.sub(r"\s+", "", narration))
        weights.append(max(1, narration_units or requested[index]))
    weight_sum = float(sum(weights))
    ideals = [bounded_target * weight / weight_sum for weight in weights]
    durations = [max(2, min(15, int(math.floor(value)))) for value in ideals]

    delta = bounded_target - sum(durations)
    while delta > 0:
        candidates = [pos for pos, value in enumerate(durations) if value < 15]
        if not candidates:
            break
        pos = max(
            candidates,
            key=lambda item: (
                ideals[item] - durations[item],
                weights[item],
                -item,
            ),
        )
        durations[pos] += 1
        delta -= 1
    while delta < 0:
        candidates = [pos for pos, value in enumerate(durations) if value > 2]
        if not candidates:
            break
        pos = min(
            candidates,
            key=lambda item: (
                ideals[item] - durations[item],
                weights[item],
                item,
            ),
        )
        durations[pos] -= 1
        delta += 1

    for pos, scene_index in enumerate(scene_indices):
        source_scenes[scene_index]["duration_seconds"] = durations[pos]

    effective_total = sum(
        int(scene.get("duration_seconds") or 0) for scene in source_scenes
    )
    result["scenes"] = source_scenes
    result["timing"] = {
        "audio_duration_seconds": float(audio_duration or 0.0),
        "target_seconds": target_seconds,
        "effective_scene_seconds": effective_total,
        "feasible_min_seconds": feasible_min,
        "feasible_max_seconds": feasible_max,
        "fully_matched": effective_total == target_seconds,
        "weight_source": "scene_narration_characters",
        "retimed": any(
            int(scene.get("duration_seconds") or 0)
            != int(scene.get("planned_duration_seconds") or 0)
            for scene in source_scenes
        ),
    }
    return result


def persist_execution_plan(task_id: str, plan: dict) -> str:
    target = Path(utils.task_dir(task_id)) / "director-execution-plan.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.partial")
    temporary.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, target)
    return str(target)


def persist_production_manifest(task_id: str, manifest: dict) -> str:
    target = Path(utils.task_dir(task_id)) / "production-manifest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.partial")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, target)
    return str(target)


def generate_scene_materials(
    task_id: str,
    plan: dict,
    audio_duration: float,
    aspect: Any,
    clip_duration: int,
) -> list[str]:
    scenes = list(plan.get("scenes") or [])
    if not scenes:
        raise AscAIError("Director plan has no scenes")

    paths: list[str] = []
    used_public_media: list[dict] = []
    covered = 0.0
    required = max(float(audio_duration or 0), 0.1)
    clip_seconds = max(1, int(clip_duration or 5))
    manifest = {
        "schema_version": "mpt.production.v1",
        "task_id": task_id,
        "local_only": local_only(),
        "audio_duration_seconds": required,
        "clip_duration_seconds": clip_seconds,
        "director": {
            "schema_version": plan.get("schema_version"),
            "provider": plan.get("director_provider"),
            "gpu_policy": plan.get("gpu_policy"),
            "production_notes": plan.get("production_notes") or [],
            "research": plan.get("research") or [],
        },
        "workflow_snapshot": {
            "image": _image_binding(),
            "video": None,
        },
        "scenes": [],
        "outputs": [],
        "status": "running",
    }
    video_capability = None
    if any(row.get("visual_strategy") == "LOCAL_VIDEO" for row in scenes):
        try:
            video_capability = _video_compatibility(aspect)
            manifest["workflow_snapshot"]["video"] = {
                key: value
                for key, value in video_capability.items()
                if key != "binding"
            }
        except AscAIError as exc:
            manifest["workflow_snapshot"]["video"] = {
                "available": False,
                "compatible": False,
                "error": str(exc),
            }
    persist_production_manifest(task_id, manifest)

    try:
        for scene_index, scene in enumerate(scenes):
            working_scene = dict(scene)
            scene_binding = _image_binding(working_scene)
            scene_width, scene_height = _workflow_resolution(
                scene_binding, aspect
            )
            identity = working_scene.get("character_identity") or {}
            scene_record = {
                "scene_id": scene.get("scene_id"),
                "requested_strategy": scene.get("visual_strategy"),
                "visual_prompt": scene.get("visual_prompt"),
                "motion_prompt": scene.get("motion_prompt"),
                "transition": scene.get("transition") or "cut",
                "overlay_text": scene.get("overlay_text") or "",
                "postprocess": None,
                "character": {
                    "character_id": identity.get("character_id"),
                    "visual_identity_kind": identity.get(
                        "visual_identity_kind"
                    ),
                    "reference_count": len(
                        identity.get("reference_artifact_ids") or []
                    ),
                    "lora_count": len(identity.get("lora_resource_ids") or []),
                }
                if identity
                else None,
                "image_workflow": {
                    "workflow_id": scene_binding.get("workflow_id"),
                    "model_id": scene_binding.get("model_id"),
                    "resolution": f"{scene_width}x{scene_height}",
                },
                "public_media": None,
                "image_attempts": [],
                "video": None,
                "fallback": None,
                "final_output": None,
            }
            manifest["scenes"].append(scene_record)
            persist_production_manifest(task_id, manifest)

            image_path = ""
            image_qc = None
            image_source = "local_generation"
            selected_public_media = None

            if scene.get("visual_strategy") == "PUBLIC_IMAGE":
                try:
                    public_result = acquire_public_scene_image(
                        task_id,
                        working_scene,
                        aspect,
                    )
                    image_path = str(public_result["path"])
                    image_source = "wikimedia_commons"
                    selected_public_media = dict(public_result)
                    scene_record["public_media"] = {
                        key: value
                        for key, value in public_result.items()
                        if key not in {"cache_path"}
                    }
                    run_image_vlm = _image_vlm_required(
                        scene_index,
                        0,
                        working_scene,
                    )
                    image_qc = quality_control(
                        task_id,
                        working_scene,
                        image_path,
                        media_kind="image",
                        run_visual_analysis=run_image_vlm,
                    )
                    scene_record["image_attempts"].append(
                        {
                            "attempt": 1,
                            "source": image_source,
                            "path": image_path,
                            "query": working_scene.get("public_media_query"),
                            "prompt": working_scene.get("visual_prompt"),
                            "qc": image_qc,
                            "execution": None,
                        }
                    )
                    if not image_qc.get("passed"):
                        scene_record["fallback"] = "public_media_qc_failed"
                        image_path = ""
                        selected_public_media = None
                except Exception as exc:
                    scene_record["fallback"] = "public_media_unavailable"
                    scene_record["public_media"] = {
                        "query": working_scene.get("public_media_query"),
                        "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                    }
                    image_path = ""
                    selected_public_media = None
                    logger.info(
                        "ASC-AI public media unavailable; falling back to local "
                        f"image generation: scene={scene.get('scene_id')}, "
                        f"error={type(exc).__name__}"
                    )
                persist_production_manifest(task_id, manifest)

            if not image_path:
                image_source = "local_generation"
                max_regenerations = max(
                    0, int(_setting("qc_max_image_regenerations", 1))
                )
                for attempt in range(max_regenerations + 1):
                    image_result = generate_image(
                        task_id,
                        working_scene,
                        aspect,
                        attempt=attempt + 1,
                        binding=scene_binding,
                        return_execution=True,
                    )
                    if isinstance(image_result, tuple):
                        image_path, image_execution = image_result
                    else:
                        image_path, image_execution = image_result, None
                    run_image_vlm = _image_vlm_required(
                        scene_index,
                        attempt,
                        working_scene,
                    )
                    image_qc = quality_control(
                        task_id,
                        working_scene,
                        image_path,
                        media_kind="image",
                        run_visual_analysis=run_image_vlm,
                    )
                    scene_record["image_attempts"].append(
                        {
                            "attempt": attempt + 1,
                            "source": image_source,
                            "path": image_path,
                            "prompt": working_scene.get("visual_prompt"),
                            "qc": image_qc,
                            "execution": image_execution,
                        }
                    )
                    persist_production_manifest(task_id, manifest)
                    if image_qc.get("passed"):
                        break
                    if attempt < max_regenerations:
                        retry_prompt = str(
                            image_qc.get("retry_prompt") or ""
                        ).strip()
                        if retry_prompt:
                            working_scene["visual_prompt"] = retry_prompt
                        logger.warning(
                            "ASC-AI image QC requested one local regeneration: "
                            f"scene={scene.get('scene_id')}, "
                            f"technical={image_qc.get('technical_score')}"
                        )

            if not image_qc or not image_qc.get("passed"):
                if bool(_setting("qc_required", True)):
                    issues = ", ".join(
                        (image_qc or {}).get("issues") or []
                    )
                    raise AscAIError(
                        "ASC-AI image failed local production QC: "
                        f"scene={scene.get('scene_id')}, "
                        f"issues={issues or 'technical QC failed'}"
                    )
                logger.warning(
                    "ASC-AI image QC did not pass but qc_required=false: "
                    f"scene={scene.get('scene_id')}"
                )

            requested_scene_seconds = max(
                2,
                min(
                    15,
                    int(scene.get("duration_seconds") or clip_seconds),
                ),
            )
            effective_scene_seconds = requested_scene_seconds
            output_path = ""
            if scene.get("visual_strategy") == "LOCAL_VIDEO":
                video_record = {
                    "candidate_path": None,
                    "timed_path": None,
                    "native_contract_seconds": 5,
                    "requested_scene_seconds": requested_scene_seconds,
                    "qc": None,
                    "error": None,
                    "capability": (
                        {
                            key: value
                            for key, value in video_capability.items()
                            if key != "binding"
                        }
                        if video_capability
                        else None
                    ),
                }
                scene_record["video"] = video_record

                if not video_capability or not video_capability.get("compatible"):
                    scene_record["fallback"] = "video_aspect_unsupported"
                    message = (
                        "ASC-AI production video workflow does not support "
                        f"target aspect {getattr(aspect, 'value', aspect)}; "
                        "using the approved still without starting a Wan GPU job"
                    )
                    video_record["error"] = message
                    if not bool(_setting("video_fallback_to_image", True)):
                        raise AscAIError(message)
                    logger.info(
                        f"{message}: scene={scene.get('scene_id')}"
                    )
                else:
                    try:
                        candidate_result = generate_video_from_image(
                            task_id,
                            working_scene,
                            image_path,
                            binding=video_capability["binding"],
                            return_execution=True,
                        )
                        if isinstance(candidate_result, tuple):
                            candidate_video, video_execution = candidate_result
                        else:
                            candidate_video, video_execution = candidate_result, None
                        video_record["candidate_path"] = candidate_video
                        video_record["execution"] = video_execution
                        video_qc = quality_control(
                            task_id,
                            working_scene,
                            candidate_video,
                            media_kind="video",
                        )
                        video_record["qc"] = video_qc
                        if video_qc.get("passed"):
                            timed_video = video.fit_director_video_duration(
                                candidate_video,
                                image_path,
                                requested_scene_seconds,
                            )
                            video_record["timed_path"] = timed_video
                            video_record["timing_adjusted"] = (
                                timed_video != candidate_video
                            )
                            output_path = timed_video
                            effective_scene_seconds = requested_scene_seconds
                        else:
                            scene_record["fallback"] = "video_qc_failed"
                            logger.warning(
                                "ASC-AI local video failed production QC; using "
                                "the already-approved still instead: "
                                f"scene={scene.get('scene_id')}, "
                                f"technical={video_qc.get('technical_score')}"
                            )
                    except Exception as exc:
                        video_record["error"] = (
                            f"{type(exc).__name__}: {str(exc)[:500]}"
                        )
                        if not bool(_setting("video_fallback_to_image", True)):
                            raise
                        scene_record["fallback"] = "video_generation_failed"
                        logger.warning(
                            "ASC-AI local video failed; falling back to "
                            "generated still: "
                            f"scene={scene.get('scene_id')}, "
                            f"error={type(exc).__name__}"
                        )
                persist_production_manifest(task_id, manifest)

            if not output_path:
                output_path = video.render_image_zoom_video(
                    image_path, requested_scene_seconds
                )
                effective_scene_seconds = requested_scene_seconds
                if (
                    scene.get("visual_strategy") == "LOCAL_VIDEO"
                    and not scene_record["fallback"]
                ):
                    scene_record["fallback"] = "video_not_selected"
            if not output_path:
                raise AscAIError(
                    "failed to render ASC-AI scene "
                    f"{scene.get('scene_id') or '<unknown>'}"
                )

            transition = str(scene.get("transition") or "cut").strip().lower()
            overlay_text = str(scene.get("overlay_text") or "").strip()
            if transition != "cut" or overlay_text:
                original_output = output_path
                try:
                    processed_output = video.render_director_scene_effects(
                        original_output,
                        transition=transition,
                        overlay_text=overlay_text,
                        font_name=str(
                            _setting(
                                "director_overlay_font",
                                "MicrosoftYaHeiBold.ttc",
                            )
                        ),
                    )
                    output_path = processed_output
                    scene_record["postprocess"] = {
                        "transition": transition,
                        "overlay_text": overlay_text,
                        "source_output": original_output,
                        "output": processed_output,
                        "applied": processed_output != original_output,
                        "error": None,
                    }
                except Exception as exc:
                    scene_record["postprocess"] = {
                        "transition": transition,
                        "overlay_text": overlay_text,
                        "source_output": original_output,
                        "output": original_output,
                        "applied": False,
                        "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                    }
                    logger.warning(
                        "Director scene post-process failed; keeping approved "
                        f"source clip: scene={scene.get('scene_id')}, "
                        f"error={type(exc).__name__}"
                    )
                persist_production_manifest(task_id, manifest)

            scene_record["final_output"] = output_path
            scene_record["effective_duration_seconds"] = effective_scene_seconds
            scene_record["image_source"] = image_source
            if selected_public_media is not None and image_source == "wikimedia_commons":
                used_public_media.append(selected_public_media)
            paths.append(output_path)
            manifest["outputs"] = list(paths)
            covered += effective_scene_seconds
            persist_production_manifest(task_id, manifest)
            if covered >= required:
                break

        if not paths:
            raise AscAIError(
                "ASC-AI Director produced no usable visual materials"
            )

        manifest["status"] = "complete"
        manifest["covered_seconds"] = covered
        if used_public_media:
            credits_json, credits_text = public_media.write_credits(
                utils.task_dir(task_id),
                used_public_media,
            )
            manifest["public_media_credits"] = {
                "json": credits_json,
                "text": credits_text,
                "count": len(used_public_media),
            }
        persist_production_manifest(task_id, manifest)
        return paths
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error"] = f"{type(exc).__name__}: {str(exc)[:1000]}"
        manifest["outputs"] = list(paths)
        persist_production_manifest(task_id, manifest)
        raise
