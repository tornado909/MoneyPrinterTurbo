from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

import requests
from loguru import logger

from app.config import config
from app.services import asc_ai_director, asc_ai_qc, video
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


def _workflow_resolution(row: dict) -> tuple[int, int]:
    value = str(row.get("default_resolution") or "")
    match = re.fullmatch(r"(\d+)x(\d+)", value)
    if not match:
        raise AscAIError(
            f"ASC-AI workflow {row.get('workflow_id')} has invalid default resolution"
        )
    return int(match.group(1)), int(match.group(2))


def health() -> dict:
    return {
        "scheduler": _request_json(
            "GET", _scheduler_url() + "/health", timeout=(3, 10)
        ),
        "prompt_intelligence": _request_json(
            "GET", _prompt_url() + "/health", timeout=(3, 10)
        ),
        "prompt_llm": _request_json(
            "GET", _prompt_llm_url() + "/health", timeout=(3, 10)
        ),
        "image_adapter": _request_json(
            "GET", _image_url() + "/health", timeout=(3, 10)
        ),
        "visual_analyzer": _request_json(
            "GET", _visual_url() + "/health", timeout=(3, 10)
        ),
        "chatterbox_tts": _request_json(
            "GET", _chatterbox_root_url() + "/health", timeout=(3, 10)
        ),
    }


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
    voice_mode = str(config.ui.get("voice_mode", "tts") or "tts")
    tts_server = str(config.ui.get("tts_server", "chatterbox") or "chatterbox")
    if voice_mode == "tts" and tts_server not in _LOCAL_TTS_SERVERS:
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


def create_director_plan(params) -> dict:
    if not enabled():
        raise AscAIError("ASC-AI integration is disabled")
    try:
        plan = asc_ai_director.create_plan(
            params,
            dict(config.asc_ai),
            scheduler_url=_scheduler_url(),
            prompt_llm_url=_prompt_llm_url(),
        )
        return enrich_director_plan(plan)
    except asc_ai_director.DirectorError as exc:
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


def _image_binding() -> dict:
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
) -> str:
    binding = _image_binding()
    width, height = _workflow_resolution(binding)
    prompt = str(scene.get("visual_prompt") or "").strip()
    if not prompt:
        raise AscAIError("Director scene has no visual prompt")
    scene_id = str(scene.get("scene_id") or "scene")
    payload = {
        "workflow_id": str(binding["workflow_id"]),
        "model_id": str(binding["model_id"]),
        "prompt": prompt,
        "original_user_prompt": prompt,
        "prompt_compiler_id": "RAW",
        "prompt_compiler_version": "mpt-director-v1",
        "negative_prompt": str(
            _setting(
                "image_negative_prompt",
                "text, watermark, logo, duplicate subjects, distorted anatomy, low quality",
            )
        ),
        "overrides": {
            "width": width,
            "height": height,
            "steps": int(_setting("image_steps", 8)),
            "cfg": float(_setting("image_cfg", 1.0)),
            "batch": 1,
        },
        "priority": int(_setting("priority", 120)),
        "idempotency_key": (
            f"mpt-{task_id}-{scene_id}-image-attempt-{max(1, int(attempt))}"
        )[:200],
        "loras": list(scene.get("image_loras") or []),
        "experimental_resources_opt_in": False,
        "experimental_model_opt_in": False,
    }
    result = _request_json(
        "POST",
        _image_url() + "/api/v1/images/generate",
        json=payload,
        timeout=(5, float(_setting("generation_timeout_seconds", 1800))),
    )
    return _output_path(result, "image")


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


def generate_video_from_image(task_id: str, scene: dict, image_path: str) -> str:
    binding = _video_binding()
    scene_id = str(scene.get("scene_id") or "scene")
    input_name = stage_image_for_video(task_id, scene_id, image_path)
    prompt = str(scene.get("motion_prompt") or "").strip()
    if not prompt:
        prompt = (
            "subtle coherent natural motion, stable composition, no identity drift"
        )
    visual = str(scene.get("visual_prompt") or "").strip()
    combined_prompt = f"{visual}. Motion: {prompt}" if visual else prompt
    duration = int(_setting("video_duration_seconds", 5))
    if duration not in {3, 5}:
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
    return _output_path(result, "video")


def stage_for_qc(
    task_id: str,
    scene_id: str,
    media_path: str,
) -> str:
    input_root = Path(str(_setting("input_root", "/srv/ai-data/input"))).resolve()
    source = Path(media_path).resolve()
    if not source.is_file():
        raise AscAIError("production QC source is missing")
    suffix = source.suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".webm", ".mov", ".mkv"}:
        raise AscAIError("production QC source has an unsupported extension")
    relative = (
        Path("moneyprinterturbo")
        / _safe_token(task_id)
        / "qc"
        / (_safe_token(scene_id) + suffix)
    )
    target = (input_root / relative).resolve()
    try:
        target.relative_to(input_root)
    except ValueError as exc:
        raise AscAIError("unsafe production QC input path") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)
    return relative.as_posix()


def quality_control(
    task_id: str,
    scene: dict,
    media_path: str,
    *,
    media_kind: str,
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
        )
    except asc_ai_qc.QCError as exc:
        raise AscAIError(str(exc)) from exc


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
        },
        "workflow_snapshot": {
            "image": _image_binding(),
            "video": None,
        },
        "scenes": [],
        "outputs": [],
        "status": "running",
    }
    if any(row.get("visual_strategy") == "LOCAL_VIDEO" for row in scenes):
        try:
            manifest["workflow_snapshot"]["video"] = _video_binding()
        except AscAIError as exc:
            manifest["workflow_snapshot"]["video"] = {
                "available": False,
                "error": str(exc),
            }
    persist_production_manifest(task_id, manifest)

    try:
        for scene in scenes:
            working_scene = dict(scene)
            scene_record = {
                "scene_id": scene.get("scene_id"),
                "requested_strategy": scene.get("visual_strategy"),
                "visual_prompt": scene.get("visual_prompt"),
                "motion_prompt": scene.get("motion_prompt"),
                "image_attempts": [],
                "video": None,
                "fallback": None,
                "final_output": None,
            }
            manifest["scenes"].append(scene_record)
            persist_production_manifest(task_id, manifest)

            image_path = ""
            image_qc = None
            max_regenerations = max(
                0, int(_setting("qc_max_image_regenerations", 1))
            )
            for attempt in range(max_regenerations + 1):
                image_path = generate_image(
                    task_id,
                    working_scene,
                    aspect,
                    attempt=attempt + 1,
                )
                image_qc = quality_control(
                    task_id, working_scene, image_path, media_kind="image"
                )
                scene_record["image_attempts"].append(
                    {
                        "attempt": attempt + 1,
                        "path": image_path,
                        "prompt": working_scene.get("visual_prompt"),
                        "qc": image_qc,
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

            output_path = ""
            if scene.get("visual_strategy") == "LOCAL_VIDEO":
                video_record = {
                    "candidate_path": None,
                    "qc": None,
                    "error": None,
                }
                scene_record["video"] = video_record
                try:
                    candidate_video = generate_video_from_image(
                        task_id, working_scene, image_path
                    )
                    video_record["candidate_path"] = candidate_video
                    video_qc = quality_control(
                        task_id,
                        working_scene,
                        candidate_video,
                        media_kind="video",
                    )
                    video_record["qc"] = video_qc
                    if video_qc.get("passed"):
                        output_path = candidate_video
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
                    image_path, clip_seconds
                )
                if scene.get("visual_strategy") == "LOCAL_VIDEO" and not scene_record["fallback"]:
                    scene_record["fallback"] = "video_not_selected"
            if not output_path:
                raise AscAIError(
                    "failed to render ASC-AI scene "
                    f"{scene.get('scene_id') or '<unknown>'}"
                )

            scene_record["final_output"] = output_path
            paths.append(output_path)
            manifest["outputs"] = list(paths)
            covered += clip_seconds
            persist_production_manifest(task_id, manifest)
            if covered >= required:
                break

        if not paths:
            raise AscAIError(
                "ASC-AI Director produced no usable visual materials"
            )

        manifest["status"] = "complete"
        manifest["covered_seconds"] = covered
        persist_production_manifest(task_id, manifest)
        return paths
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error"] = f"{type(exc).__name__}: {str(exc)[:1000]}"
        manifest["outputs"] = list(paths)
        persist_production_manifest(task_id, manifest)
        raise
