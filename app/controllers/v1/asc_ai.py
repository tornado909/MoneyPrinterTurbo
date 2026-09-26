from __future__ import annotations

from types import SimpleNamespace

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.controllers import base
from app.controllers.v1.base import new_router
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
            video_source="asc_ai",
            subtitle_enabled=False,
            bgm_type="",
            custom_audio_file="",
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
