from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError, model_validator

from app.config import config
from app.controllers import base
from app.controllers.v1 import video as video_controller
from app.controllers.v1.base import new_router
from app.models.schema import TaskVideoRequest
from app.services import asc_ai
from app.services import state as sm
from app.utils import utils


router = new_router(dependencies=[Depends(base.verify_token)])


_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


def _production_fingerprint(task_request: TaskVideoRequest) -> str:
    payload = task_request.model_dump(mode="json")
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _production_task_id(idempotency_key: str) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            "moneyprinterturbo:asc-ai:production:" + idempotency_key,
        )
    )


def _retry_task_id(task_id: str) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            "moneyprinterturbo:asc-ai:retry:" + str(task_id),
        )
    )


def _read_task_json_artifact(task_id: str, filename: str):
    task_root = Path(utils.task_dir(task_id)).resolve()
    target = (task_root / filename).resolve()
    try:
        target.relative_to(task_root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="unsafe task artifact path") from exc
    if not target.is_file():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=500,
            detail=f"task artifact {filename} is unreadable",
        ) from exc
    return payload


def _idempotent_replay_or_conflict(
    task_id: str,
    fingerprint: str,
):
    existing = sm.state.get_task(task_id)
    if not existing:
        return None
    if str(existing.get("request_fingerprint") or "") != fingerprint:
        raise HTTPException(
            status_code=409,
            detail="Idempotency-Key was already used with a different production payload",
        )
    return utils.get_response(200, {"task_id": task_id})


class DirectorPlanRequest(BaseModel):
    video_subject: str = Field(min_length=1, max_length=4000)
    video_script: str = Field(default="", max_length=12000)
    video_language: str = Field(default="ru-RU", max_length=32)
    video_aspect: str = Field(
        default="9:16",
        pattern=r"^\d+(?:\.\d+)?:\d+(?:\.\d+)?$",
        max_length=32,
    )
    target_duration_seconds: int = Field(default=45, ge=5, le=300)
    max_local_video_scenes: int = Field(default=1, ge=0, le=6)
    max_public_image_scenes: int = Field(default=2, ge=0, le=6)
    style: str = Field(default="", max_length=2000)
    audience: str = Field(default="", max_length=1000)
    purpose: str = Field(default="", max_length=1000)
    character_id: str = Field(default="", max_length=128)
    public_research_enabled: bool | None = None
    public_media_enabled: bool | None = None

    def to_params(self) -> SimpleNamespace:
        return SimpleNamespace(
            video_subject=self.video_subject,
            video_script=self.video_script,
            video_language=self.video_language,
            video_aspect=self.video_aspect,
            director_enabled=True,
            director_target_duration_seconds=self.target_duration_seconds,
            director_max_local_video_scenes=self.max_local_video_scenes,
            director_max_public_image_scenes=self.max_public_image_scenes,
            director_style=self.style,
            director_audience=self.audience,
            director_purpose=self.purpose,
            director_character_id=self.character_id,
            director_public_research_enabled=self.public_research_enabled,
            director_public_media_enabled=self.public_media_enabled,
            video_source="asc_ai",
            subtitle_enabled=False,
            bgm_type="",
            custom_audio_file="",
        )

class ProductionRequest(DirectorPlanRequest):
    subtitle_enabled: bool = True
    bgm_type: Literal["", "random", "preset"] = "random"
    bgm_volume: float = Field(default=0.2, ge=0.0, le=1.0)
    bgm_file: str = Field(default="", max_length=255)
    voice_name: str = Field(default="", max_length=200)
    voice_volume: float = Field(default=1.0, ge=0.1, le=2.0)
    voice_rate: float = Field(default=1.0, ge=0.5, le=2.0)
    video_count: int = Field(default=1, ge=1, le=4)

    @model_validator(mode="after")
    def validate_local_media_options(self):
        if self.bgm_type == "preset" and not self.bgm_file.strip():
            raise ValueError("bgm_file is required when bgm_type=preset")
        return self

    def to_task_request(self) -> TaskVideoRequest:
        configured_voice = str(
            config.ui.get("voice_name", "chatterbox:ru-default")
            or "chatterbox:ru-default"
        )
        configured_tts = str(
            config.ui.get("tts_server", "chatterbox") or "chatterbox"
        )
        if configured_tts != "chatterbox" or not configured_voice.startswith(
            "chatterbox:"
        ):
            configured_voice = "chatterbox:ru-default"
        return TaskVideoRequest(
            video_subject=self.video_subject,
            video_script=self.video_script,
            video_language=self.video_language,
            video_aspect=self.video_aspect,
            video_source="asc_ai",
            director_enabled=True,
            director_target_duration_seconds=self.target_duration_seconds,
            director_max_local_video_scenes=self.max_local_video_scenes,
            director_max_public_image_scenes=self.max_public_image_scenes,
            director_style=self.style,
            director_audience=self.audience,
            director_purpose=self.purpose,
            director_character_id=self.character_id,
            director_public_research_enabled=self.public_research_enabled,
            director_public_media_enabled=self.public_media_enabled,
            subtitle_enabled=self.subtitle_enabled,
            bgm_type=self.bgm_type,
            bgm_file=self.bgm_file,
            bgm_volume=self.bgm_volume,
            voice_name=self.voice_name or configured_voice,
            voice_volume=self.voice_volume,
            voice_rate=self.voice_rate,
            video_count=self.video_count,
            match_materials_to_script=True,
        )



@router.get(
    "/asc-ai/capabilities",
    summary="Describe the local ASC-AI production capabilities",
)
def capabilities(request: Request):
    return utils.get_response(
        200,
        {
            "schema_version": "mpt.asc-ai.capabilities.v1",
            "local_only": asc_ai.local_only(),
            "egress_policy": {
                "internal_ai_endpoints": "private-or-explicit-local-hosts-only",
                "external_ai_endpoints": "blocked",
                "public_http_default": "disabled-opt-in",
                "public_http_sources": [
                    "https://*.wikipedia.org/w/api.php",
                    "https://commons.wikimedia.org/w/api.php",
                    "https://upload.wikimedia.org/*",
                ],
            },
            "director": {
                "provider": "asc-ai-local-qwen3",
                "scheduler_managed": True,
                "structured_scene_plan": True,
                "public_research_optional": True,
            },
            "image": {
                "adapter": "asc-ai-image-adapter",
                "public_media": "wikimedia-commons",
                "public_media_local_fallback": True,
                "character_hub": True,
                "identity_reference": True,
                "local_qc": True,
            },
            "video": {
                "wan_i2v": True,
                "aspect_compatibility_gate": True,
                "still_fallback": True,
            },
            "production": {
                "submit_endpoint": "/api/v1/asc-ai/production",
                "task_status_template": "/api/v1/tasks/{task_id}",
                "retry_template": "/api/v1/asc-ai/production/{task_id}/retry",
                "queued": True,
                "uses_shared_task_manager": True,
                "persistent_idempotency_header": "Idempotency-Key",
            },
            "artifacts": {
                "evidence_template": "/api/v1/asc-ai/production/{task_id}/evidence",
                "director_plan": "director-plan.json",
                "director_execution_plan": "director-execution-plan.json",
                "production_manifest": "production-manifest.json",
                "public_media_credits": "public-media-credits.json",
            },
        },
    )


@router.get(
    "/asc-ai/health",
    summary="Check the default local ASC-AI production dependencies",
)
def health(request: Request):
    try:
        result = asc_ai.health()
    except asc_ai.AscAIError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return utils.get_response(200, result)


@router.get(
    "/asc-ai/characters",
    summary="List Character Hub identities available to Director",
)
def characters(request: Request):
    try:
        result = asc_ai.list_characters()
    except asc_ai.AscAIError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return utils.get_response(200, {"characters": result})


@router.get(
    "/asc-ai/characters/{character_id}",
    summary="Get bounded visual production identity from Character Hub",
)
def character(request: Request, character_id: str):
    try:
        result = asc_ai.get_character_identity(character_id)
    except asc_ai.AscAIError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return utils.get_response(200, result)


@router.post(
    "/asc-ai/director/plan",
    summary="Create a local scheduler-managed Director plan without rendering",
)
def director_plan(request: Request, body: DirectorPlanRequest):
    params = body.to_params()
    overrides = {}
    if body.public_research_enabled is not None:
        overrides["public_research_enabled"] = body.public_research_enabled
    if body.public_media_enabled is not None:
        overrides["public_media_enabled"] = body.public_media_enabled
    try:
        asc_ai.preflight(params, stop_at="script")
        plan = asc_ai.create_director_plan(
            params,
            settings_overrides=overrides or None,
        )
    except asc_ai.AscAIError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return utils.get_response(200, plan)


@router.post(
    "/asc-ai/production",
    summary="Queue a full local-only ASC-AI Director production render",
)
def production(request: Request, body: ProductionRequest):
    task_request = body.to_task_request()
    idempotency_key = str(request.headers.get("Idempotency-Key") or "").strip()

    if idempotency_key and not _IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key):
        raise HTTPException(
            status_code=400,
            detail=(
                "Idempotency-Key must be 8-128 characters using only "
                "letters, digits, '.', '_', ':', or '-'"
            ),
        )

    fingerprint = _production_fingerprint(task_request)
    deterministic_task_id = (
        _production_task_id(idempotency_key) if idempotency_key else None
    )
    if deterministic_task_id:
        replay = _idempotent_replay_or_conflict(
            deterministic_task_id,
            fingerprint,
        )
        if replay is not None:
            return replay

    try:
        # Fail before queueing when the local production dependencies are not
        # ready; the worker runs the same preflight again before execution.
        asc_ai.preflight(task_request, stop_at="video")
    except asc_ai.AscAIError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if not deterministic_task_id:
        return video_controller.create_task(
            request,
            task_request,
            stop_at="video",
        )

    created = sm.state.create_task_if_absent(
        deterministic_task_id,
        queue_executor="api",
        retryable=False,
        request_fingerprint=fingerprint,
        idempotency_scope="asc-ai-production",
        request_params=task_request.model_dump(mode="json", warnings=False),
        request_stop_at="video",
    )
    if not created:
        replay = _idempotent_replay_or_conflict(
            deterministic_task_id,
            fingerprint,
        )
        if replay is not None:
            return replay
        raise HTTPException(
            status_code=409,
            detail="Idempotency-Key claim could not be resolved",
        )

    return video_controller.create_task(
        request,
        task_request,
        stop_at="video",
        task_id_override=deterministic_task_id,
        initial_state_claimed=True,
    )





@router.get(
    "/asc-ai/production/{task_id}/evidence",
    summary="Read structured Director and production evidence for a task",
)
def production_evidence(request: Request, task_id: str):
    task = video_controller.sm.state.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")

    names = {
        "director_plan": "director-plan.json",
        "director_execution_plan": "director-execution-plan.json",
        "production_manifest": "production-manifest.json",
        "public_media_credits": "public-media-credits.json",
    }
    artifacts = {
        key: _read_task_json_artifact(task_id, filename)
        for key, filename in names.items()
    }
    return utils.get_response(
        200,
        {
            "task_id": task_id,
            "state": task.get("state"),
            "progress": task.get("progress", 0),
            "failed_stage": task.get("failed_stage"),
            "error": task.get("error"),
            "retryable": bool(task.get("retryable", False)),
            "retried_as": task.get("retried_as"),
            "artifacts": artifacts,
        },
    )

@router.post(
    "/asc-ai/production/{task_id}/retry",
    summary="Resubmit a retryable interrupted ASC-AI production task",
)
def retry_production(request: Request, task_id: str):
    previous = video_controller.sm.state.get_task(task_id)
    if not previous:
        raise HTTPException(status_code=404, detail="task not found")

    existing_retry_id = str(previous.get("retried_as") or "").strip()
    if existing_retry_id:
        return utils.get_response(200, {"task_id": existing_retry_id})

    if not bool(previous.get("retryable", False)):
        raise HTTPException(
            status_code=409,
            detail="task is not marked retryable",
        )

    raw_params = previous.get("request_params")
    if not isinstance(raw_params, dict):
        raise HTTPException(
            status_code=409,
            detail="retryable task has no durable request parameter snapshot",
        )
    if str(raw_params.get("video_source") or "") != "asc_ai":
        raise HTTPException(
            status_code=409,
            detail="only ASC-AI production tasks can be retried here",
        )
    if str(previous.get("request_stop_at") or "video") != "video":
        raise HTTPException(
            status_code=409,
            detail="retryable task is not a full video production request",
        )

    try:
        task_request = TaskVideoRequest(**raw_params)
    except (ValidationError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail=f"saved task parameters are stale or invalid: {exc}",
        ) from exc
    reusable_plan = None
    try:
        reusable_plan = asc_ai.load_director_plan(task_id)
    except asc_ai.AscAIError:
        reusable_plan = None
    if reusable_plan:
        requested_script = str(task_request.video_script or "").strip()
        resumed_script = str(reusable_plan.get("script") or "").strip()
        if requested_script and requested_script != resumed_script:
            reusable_plan = None

    try:
        asc_ai.preflight(
            task_request,
            stop_at="video",
            director_plan_ready=reusable_plan is not None,
        )
    except asc_ai.AscAIError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    retry_task_id = _retry_task_id(task_id)
    retry_fingerprint = _production_fingerprint(task_request)
    existing = video_controller.sm.state.get_task(retry_task_id)
    if existing:
        if str(existing.get("request_fingerprint") or "") != retry_fingerprint:
            raise HTTPException(
                status_code=409,
                detail="deterministic retry task exists with a different payload",
            )
        video_controller.sm.state.patch_task(
            task_id,
            retried_as=retry_task_id,
            retryable=False,
            recovery_action="resubmitted",
        )
        return utils.get_response(200, {"task_id": retry_task_id})

    created = video_controller.sm.state.create_task_if_absent(
        retry_task_id,
        queue_executor="api",
        retryable=False,
        request_fingerprint=retry_fingerprint,
        idempotency_scope="asc-ai-retry",
        request_params=task_request.model_dump(mode="json", warnings=False),
        request_stop_at="video",
        retry_of=task_id,
        retry_reason="manual_resubmit_after_interruption",
    )
    if not created:
        raced = video_controller.sm.state.get_task(retry_task_id)
        if not raced or str(raced.get("request_fingerprint") or "") != retry_fingerprint:
            raise HTTPException(
                status_code=409,
                detail="retry task claim could not be resolved",
            )
        video_controller.sm.state.patch_task(
            task_id,
            retried_as=retry_task_id,
            retryable=False,
            recovery_action="resubmitted",
        )
        return utils.get_response(200, {"task_id": retry_task_id})

    response = video_controller.create_task(
        request,
        task_request,
        stop_at="video",
        task_id_override=retry_task_id,
        initial_state_claimed=True,
    )
    video_controller.sm.state.patch_task(
        task_id,
        retried_as=retry_task_id,
        retryable=False,
        recovery_action="resubmitted",
    )
    return response

