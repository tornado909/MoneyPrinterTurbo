from __future__ import annotations

from typing import Any
from urllib.parse import quote

import requests


class CharacterHubError(RuntimeError):
    pass


def _get_json(url: str, *, timeout: float = 10.0) -> dict:
    try:
        response = requests.get(url, timeout=(3, timeout))
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise CharacterHubError(
            f"Character Hub request failed: {type(exc).__name__}"
        ) from exc
    except ValueError as exc:
        raise CharacterHubError("Character Hub returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise CharacterHubError("Character Hub returned a non-object response")
    return payload


def list_characters(base_url: str, *, timeout: float = 10.0) -> list[str]:
    payload = _get_json(base_url.rstrip("/") + "/characters", timeout=timeout)
    rows = payload.get("characters") or []
    if not isinstance(rows, list):
        raise CharacterHubError("Character Hub characters response is invalid")
    result: list[str] = []
    for value in rows[:200]:
        item = str(value or "").strip()
        if item and len(item) <= 128 and item not in result:
            result.append(item)
    return result


def get_character(
    base_url: str,
    character_id: str,
    *,
    timeout: float = 10.0,
) -> dict[str, Any]:
    character_id = str(character_id or "").strip()
    if not character_id or len(character_id) > 128:
        raise CharacterHubError("invalid Character Hub character_id")
    safe_id = quote(character_id, safe="")
    manifest = _get_json(
        base_url.rstrip("/") + f"/characters/{safe_id}",
        timeout=timeout,
    )
    return production_identity(manifest, requested_id=character_id)


def _bounded_strings(values: object, *, limit: int, max_chars: int) -> list[str]:
    if not isinstance(values, list):
        return []
    result = []
    for value in values[:limit]:
        text = str(value or "").strip()
        if text:
            result.append(text[:max_chars])
    return result


def production_identity(
    manifest: dict[str, Any],
    *,
    requested_id: str = "",
) -> dict[str, Any]:
    """Extract only visual production fields from a Character Hub manifest."""
    appearance = manifest.get("appearance") or {}
    if not isinstance(appearance, dict):
        appearance = {}
    visual = manifest.get("visual_identity") or {}
    if not isinstance(visual, dict):
        visual = {}
    image = manifest.get("image_generation") or {}
    if not isinstance(image, dict):
        image = {}

    character_id = str(
        manifest.get("character_id") or requested_id or ""
    ).strip()[:128]
    name = str(manifest.get("name") or character_id).strip()[:200]
    description = str(appearance.get("description") or "").strip()[:4000]
    forbidden_traits = _bounded_strings(
        appearance.get("forbidden_traits"), limit=30, max_chars=300
    )
    reference_captions = _bounded_strings(
        visual.get("reference_captions"), limit=8, max_chars=500
    )
    reference_artifact_ids = _bounded_strings(
        visual.get("reference_artifact_ids"), limit=8, max_chars=100
    )
    lora_resource_ids = _bounded_strings(
        visual.get("lora_resource_ids"), limit=4, max_chars=100
    )
    forbidden_prompt_terms = _bounded_strings(
        image.get("forbidden_prompt_terms"), limit=30, max_chars=200
    )

    result = {
        "character_id": character_id,
        "name": name,
        "appearance": description,
        "forbidden_traits": forbidden_traits,
        "visual_identity_kind": str(visual.get("kind") or "").strip()[:64],
        "reference_artifact_ids": reference_artifact_ids,
        "reference_captions": reference_captions,
        "lora_resource_ids": lora_resource_ids,
        "preferred_workflow": str(
            visual.get("preferred_workflow")
            or image.get("preferred_workflow")
            or ""
        ).strip()[:128],
        "fallback_workflow": str(
            visual.get("fallback_workflow")
            or image.get("fallback_workflow")
            or ""
        ).strip()[:128],
        "model_id": str(image.get("model_id") or "").strip()[:128],
        "style_prompt_suffix": str(
            image.get("style_prompt_suffix") or ""
        ).strip()[:1000],
        "forbidden_prompt_terms": forbidden_prompt_terms,
        "grounding_px": int(image.get("grounding_px") or 1024),
        "ref_boost": float(image.get("ref_boost") or 1.0),
    }
    if not result["character_id"]:
        raise CharacterHubError("Character Hub manifest has no character identity")
    if not result["appearance"] and not reference_artifact_ids and not lora_resource_ids:
        raise CharacterHubError(
            "Character Hub manifest has no usable visual identity data"
        )
    return result
