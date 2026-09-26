from __future__ import annotations

from types import SimpleNamespace
from typing import Literal

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import config
from app.controllers import base
from app.controllers.v1 import video as video_controller
from app.controllers.v1.base import new_router
from app.models.schema import TaskVideoRequest
from app.services import asc_ai
from app.utils import utils


router = new_router(dependencies=[Depends(base.verify_token)])


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
    style: str = Field(default="", max_length=2000)
    audience: str = Field(default="", max_length=1000)
    purpose: str = Field(default="", max_length=1000)
    character_id: str = Field(default="", max_length=128)
    public_research_enabled: bool | None = None

    def to_params(self) -> SimpleNamespace:
        return SimpleNamespace(
            video_subject=self.video_subject,
            video_script=self.video_script,
            video_language=self.video_language,
            video_aspect=self.video_aspect,
            director_enabled=True,
            director_target_duration_seconds=self.target_duration_seconds,
            director_max_local_video_scenes=self.max_local_video_scenes,
            director_style=self.style,
            director_audience=self.audience,
            director_purpose=self.purpose,
            director_character_id=self.character_id,
            director_public_research_enabled=self.public_research_enabled,
            video_source="asc_ai",
            subtitle_enabled=False,
            bgm_type="",
            custom_audio_file="",
        )

class ProductionRequest(DirectorPlanRequest):
    subtitle_enabled: bool = True
    bgm_type: Literal["", "random", "preset"] = "random"
    bgm_volume: float = Field(default=0.2, ge=0.0, le=1.0)
    voice_name: str = Field(default="", max_length=200)
    voice_volume: float = Field(default=1.0, ge=0.1, le=2.0)
    voice_rate: float = Field(default=1.0, ge=0.5, le=2.0)
    video_count: int = Field(default=1, ge=1, le=4)

    def to_task_request(self) -> TaskVideoRequest:
        configured_voice = str(
            config.ui.get("voice_name", "chatterbox:default-Female")
            or "chatterbox:default-Female"
        )
        return TaskVideoRequest(
            video_subject=self.video_subject,
            video_script=self.video_script,
            video_language=self.video_language,
            video_aspect=self.video_aspect,
            video_source="asc_ai",
            director_enabled=True,
            director_target_duration_seconds=self.target_duration_seconds,
            director_max_local_video_scenes=self.max_local_video_scenes,
            director_style=self.style,
            director_audience=self.audience,
            director_purpose=self.purpose,
            director_character_id=self.character_id,
            director_public_research_enabled=self.public_research_enabled,
            subtitle_enabled=self.subtitle_enabled,
            bgm_type=self.bgm_type,
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
            "director": {
                "provider": "asc-ai-local-qwen3",
                "scheduler_managed": True,
                "structured_scene_plan": True,
                "public_research_optional": True,
            },
            "image": {
                "adapter": "asc-ai-image-adapter",
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
                "queued": True,
                "uses_shared_task_manager": True,
            },
            "artifacts": {
                "director_plan": "director-plan.json",
                "production_manifest": "production-manifest.json",
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
    try:
        # Fail before queueing when the local production dependencies are not
        # ready; the worker runs the same preflight again before execution.
        asc_ai.preflight(task_request, stop_at="video")
    except asc_ai.AscAIError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return video_controller.create_task(
        request,
        task_request,
        stop_at="video",
    )
