from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import Any

import requests


class QCError(RuntimeError):
    pass


def register_artifact(
    prompt_url: str,
    media_path: str,
    *,
    task_id: str,
    scene_id: str,
    timeout: float = 120.0,
) -> dict:
    path = Path(media_path)
    if not path.is_file():
        raise QCError("production QC source is missing")
    suffix = path.suffix.lower()
    is_video = suffix in {".mp4", ".webm"}
    endpoint = (
        "/api/v1/prompt-intelligence/artifacts/video"
        if is_video
        else "/api/v1/prompt-intelligence/artifacts"
    )
    provenance = {
        "source": "moneyprinterturbo",
        "task_id": task_id,
        "scene_id": scene_id,
        "purpose": "production_qc",
    }
    data = {"source_provenance": json.dumps(provenance, ensure_ascii=False)}
    if not is_video:
        data["role"] = "generated_output"
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    try:
        with path.open("rb") as handle:
            response = requests.post(
                prompt_url.rstrip("/") + endpoint,
                data=data,
                files={"file": (path.name, handle, mime)},
                timeout=(5, timeout),
            )
        response.raise_for_status()
        result = response.json()
    except requests.RequestException as exc:
        raise QCError(
            f"ASC-AI QC artifact registration failed: {type(exc).__name__}"
        ) from exc
    except ValueError as exc:
        raise QCError("ASC-AI QC artifact registration returned invalid JSON") from exc
    if not isinstance(result, dict) or not result.get("artifact_id"):
        raise QCError("ASC-AI QC artifact registration returned no artifact_id")
    return result


def analyze_evidence(
    visual_url: str,
    artifact_id: str,
    *,
    profile: str = "FAST",
    timeout: float = 300.0,
) -> dict:
    try:
        response = requests.post(
            visual_url.rstrip("/") + "/api/v1/evidence/analyze",
            json={
                "artifact_id": artifact_id,
                "profile": profile,
                "external_analysis_allowed": False,
            },
            timeout=(5, timeout),
        )
        response.raise_for_status()
        result = response.json()
    except requests.RequestException as exc:
        raise QCError(f"ASC-AI evidence analysis failed: {type(exc).__name__}") from exc
    except ValueError as exc:
        raise QCError("ASC-AI evidence analysis returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise QCError("ASC-AI evidence analysis returned a non-object response")
    if result.get("external_egress") is not False:
        raise QCError("production QC violated the local-only egress contract")
    return result


def quality_control(
    *,
    prompt_url: str,
    visual_url: str,
    task_id: str,
    scene: dict,
    media_path: str,
    media_kind: str,
    profile: str = "FAST",
    upload_timeout: float = 120.0,
    timeout: float = 300.0,
    run_visual_analysis: bool = True,
) -> dict[str, Any]:
    """Use bounded local QC without fabricating prompt similarity."""
    scene_id = str(scene.get("scene_id") or "scene")
    artifact = register_artifact(
        prompt_url,
        media_path,
        task_id=task_id,
        scene_id=f"{scene_id}-{media_kind}",
        timeout=upload_timeout,
    )
    if not run_visual_analysis:
        if not Path(media_path).is_file() or Path(media_path).stat().st_size <= 0:
            return {
                "passed": False,
                "technical_status": "missing_or_empty",
                "technical_score": 0.0,
                "semantic_score": None,
                "observation_confidence": 1.0,
                "issues": ["generated artifact is missing or empty"],
                "retry_prompt": str(scene.get("visual_prompt") or "").strip(),
                "provider": "local-structural",
                "provider_model": None,
                "artifact_id": artifact["artifact_id"],
                "actual_image_spec": None,
                "visual_analysis_skipped": True,
                "skipped": False,
            }
        return {
            "passed": True,
            "technical_status": "trusted_adapter_output",
            "technical_score": 1.0,
            "semantic_score": None,
            "observation_confidence": 1.0,
            "issues": [],
            "retry_prompt": "",
            "provider": "local-structural",
            "provider_model": None,
            "artifact_id": artifact["artifact_id"],
            "actual_image_spec": None,
            "visual_analysis_skipped": True,
            "skipped": False,
        }

    result = analyze_evidence(
        visual_url,
        str(artifact["artifact_id"]),
        profile=profile,
        timeout=timeout,
    )
    actual = result.get("actual_image_spec")
    if not isinstance(actual, dict):
        raise QCError("production QC returned no actual_image_spec")
    technical = actual.get("technical_assessment") or {}
    cues = actual.get("artifact_cues") or {}
    status = str(technical.get("status") or "unknown").lower()
    usable = bool(technical.get("artifact_usable", status == "valid"))
    issues = [str(x) for x in (technical.get("reasons") or []) if x]
    issues.extend(str(x) for x in (cues.get("defects") or []) if x)
    confidence = float(actual.get("confidence") or 0.0)
    passed = usable and status != "unsafe"
    retry_prompt = ""
    if not passed:
        retry_prompt = str(scene.get("visual_prompt") or "").strip()
        if issues:
            retry_prompt += ". Avoid these observed defects: " + "; ".join(issues[:5])
    return {
        "passed": passed,
        "technical_status": status,
        "technical_score": 1.0 if status == "valid" and usable else 0.0,
        "semantic_score": None,
        "observation_confidence": confidence,
        "issues": issues,
        "retry_prompt": retry_prompt,
        "provider": result.get("provider"),
        "provider_model": result.get("provider_model"),
        "artifact_id": artifact["artifact_id"],
        "actual_image_spec": actual,
        "visual_analysis_skipped": False,
        "skipped": False,
    }
