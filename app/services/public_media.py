from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import requests


class PublicMediaError(RuntimeError):
    pass


_CACHE_LOCK = threading.RLock()
_SEARCH_CACHE: dict[tuple, tuple[float, list[dict[str, Any]]]] = {}
_ALLOWED_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
_LICENSE_MARKERS = (
    "cc by",
    "cc-by",
    "cc0",
    "public domain",
    "public-domain",
    "pdm",
)
_TAG_RE = re.compile(r"<[^>]+>")


def clear_cache() -> None:
    with _CACHE_LOCK:
        _SEARCH_CACHE.clear()


def _clean(value: object, *, max_chars: int = 2000) -> str:
    raw = value
    if isinstance(value, dict):
        raw = value.get("value")
    text = html.unescape(_TAG_RE.sub("", str(raw or "")))
    return re.sub(r"\s+", " ", text).strip()[:max_chars]


def _metadata_value(metadata: dict, key: str, *, max_chars: int = 2000) -> str:
    return _clean(metadata.get(key), max_chars=max_chars)


def _license_is_reusable(short_name: str) -> bool:
    """Allow only licenses safe for edited/commercial video reuse.

    Wikimedia's LicenseShortName can contain CC BY-NC / CC BY-ND variants.
    A naive "cc by" substring check accepts both, which is unsafe for a video
    pipeline that crops, zooms, composites and may later be monetized.
    """
    normalized = re.sub(r"\s+", " ", str(short_name or "").lower()).strip()
    if not normalized:
        return False
    if re.search(
        r"(?:^|[-\s])(?:nc|nd)(?:[-\s]|$)|non[-\s]?commercial|no[-\s]?derivatives",
        normalized,
    ):
        return False
    return any(marker in normalized for marker in _LICENSE_MARKERS)


def _safe_wikimedia_url(url: str) -> str:
    value = str(url or "").strip()
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (
        host == "upload.wikimedia.org"
        or host.endswith(".wikimedia.org")
        or host == "commons.wikimedia.org"
    ):
        raise PublicMediaError("Wikimedia API returned an unsafe media URL")
    return value


def _target_ratio(aspect: str) -> float | None:
    match = re.fullmatch(
        r"(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)",
        str(aspect or "").strip(),
    )
    if not match:
        return None
    denominator = float(match.group(2))
    return float(match.group(1)) / denominator if denominator > 0 else None


_QUERY_TOKEN_RE = re.compile(r"[A-Za-z0-9]{3,}")


def _query_tokens(value: str) -> set[str]:
    return {
        token.lower()
        for token in _QUERY_TOKEN_RE.findall(str(value or ""))
        if token.lower() not in {
            "the", "and", "for", "with", "from", "into", "photo", "image",
            "view", "close", "modern", "real", "realistic",
        }
    }


def _semantic_overlap(query: str, item: dict) -> float:
    query_tokens = _query_tokens(query)
    if not query_tokens:
        return 0.0
    haystack = " ".join(
        str(item.get(key) or "")
        for key in ("title", "description", "credit")
    )
    item_tokens = _query_tokens(haystack)
    return len(query_tokens & item_tokens) / max(1, len(query_tokens))


def _search_score(item: dict, target_ratio: float | None, query: str) -> tuple:
    width = max(1, int(item.get("width") or 1))
    height = max(1, int(item.get("height") or 1))
    ratio_error = (
        abs((width / height) - target_ratio)
        if target_ratio
        else 0.0
    )
    semantic = _semantic_overlap(query, item)
    search_rank = int(item.get("search_rank") or 9999)
    # Semantic match is intentionally first. Wikimedia's own search rank is
    # next. Aspect ratio is a production concern, but must not turn an
    # irrelevant vertical photo into the top factual result.
    return (-semantic, search_rank, ratio_error, -(width * height))


def search_commons_images(
    query: str,
    *,
    aspect: str = "9:16",
    max_results: int = 6,
    thumb_width: int = 1600,
    timeout: float = 10.0,
    cache_ttl_seconds: float = 3600.0,
    user_agent: str = (
        "MoneyPrinterTurbo-RU/1.0 "
        "(https://github.com/tornado909/MoneyPrinterTurbo)"
    ),
) -> list[dict[str, Any]]:
    """Search Wikimedia Commons for bounded, reusable raster images."""
    query = str(query or "").strip()
    if not query:
        return []

    max_results = max(1, min(10, int(max_results)))
    thumb_width = max(640, min(2048, int(thumb_width)))
    timeout = max(1.0, min(30.0, float(timeout)))
    cache_ttl_seconds = max(0.0, min(86400.0, float(cache_ttl_seconds)))
    cache_key = (query.casefold(), aspect, max_results, thumb_width)
    now = time.monotonic()

    if cache_ttl_seconds:
        with _CACHE_LOCK:
            cached = _SEARCH_CACHE.get(cache_key)
            if cached and now - cached[0] <= cache_ttl_seconds:
                return [dict(row) for row in cached[1]]

    params = {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "generator": "search",
        "gsrnamespace": 6,
        "gsrsearch": query,
        "gsrlimit": max_results,
        "prop": "imageinfo",
        "iiprop": "url|mime|size|mediatype|extmetadata",
        "iiurlwidth": thumb_width,
        "iiextmetadatalanguage": "en",
        "iiextmetadatafilter": (
            "LicenseShortName|LicenseUrl|Artist|Credit|ImageDescription|"
            "AttributionRequired|UsageTerms|Restrictions"
        ),
        "maxlag": 5,
    }
    try:
        response = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params=params,
            headers={"User-Agent": user_agent},
            timeout=(3, timeout),
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise PublicMediaError(
            f"Wikimedia Commons search failed: {type(exc).__name__}"
        ) from exc
    except ValueError as exc:
        raise PublicMediaError(
            "Wikimedia Commons search returned invalid JSON"
        ) from exc

    pages = ((payload.get("query") or {}).get("pages") or [])
    candidates: list[dict[str, Any]] = []
    for page_position, page in enumerate(pages):
        if not isinstance(page, dict):
            continue
        imageinfo = page.get("imageinfo") or []
        if not imageinfo or not isinstance(imageinfo[0], dict):
            continue
        info = imageinfo[0]
        mime = str(info.get("mime") or "").lower().strip()
        if mime not in _ALLOWED_MIME:
            continue
        width = int(info.get("width") or 0)
        height = int(info.get("height") or 0)
        if width < 640 or height < 480:
            continue

        metadata = info.get("extmetadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        license_name = _metadata_value(
            metadata, "LicenseShortName", max_chars=120
        )
        if not _license_is_reusable(license_name):
            continue

        media_url = _safe_wikimedia_url(
            info.get("thumburl") or info.get("url") or ""
        )
        original_url = _safe_wikimedia_url(info.get("url") or media_url)
        title = str(page.get("title") or "").strip()[:500]
        page_url = (
            "https://commons.wikimedia.org/wiki/"
            + quote(title.replace(" ", "_"), safe=":_()-,.")
        )
        candidates.append(
            {
                "provider": "wikimedia_commons",
                "title": title,
                "search_rank": int(page.get("index") or page_position + 1),
                "page_id": page.get("pageid"),
                "page_url": page_url,
                "media_url": media_url,
                "original_url": original_url,
                "mime": mime,
                "width": width,
                "height": height,
                "license": license_name,
                "license_url": _metadata_value(
                    metadata, "LicenseUrl", max_chars=500
                ),
                "artist": _metadata_value(metadata, "Artist", max_chars=1000),
                "credit": _metadata_value(metadata, "Credit", max_chars=1000),
                "description": _metadata_value(
                    metadata, "ImageDescription", max_chars=1200
                ),
                "attribution_required": _metadata_value(
                    metadata, "AttributionRequired", max_chars=30
                ),
                "usage_terms": _metadata_value(
                    metadata, "UsageTerms", max_chars=300
                ),
                "restrictions": _metadata_value(
                    metadata, "Restrictions", max_chars=300
                ),
            }
        )

    ratio = _target_ratio(aspect)
    candidates.sort(key=lambda row: _search_score(row, ratio, query))
    if cache_ttl_seconds:
        with _CACHE_LOCK:
            _SEARCH_CACHE[cache_key] = (
                now,
                [dict(row) for row in candidates],
            )
            if len(_SEARCH_CACHE) > 128:
                oldest = min(
                    _SEARCH_CACHE.items(),
                    key=lambda item: item[1][0],
                )[0]
                _SEARCH_CACHE.pop(oldest, None)
    return candidates


def _download_to_cache(
    item: dict,
    *,
    cache_dir: str,
    timeout: float,
    max_bytes: int,
    user_agent: str,
) -> str:
    media_url = _safe_wikimedia_url(str(item.get("media_url") or ""))
    mime = str(item.get("mime") or "").lower()
    extension = _ALLOWED_MIME.get(mime)
    if not extension:
        raise PublicMediaError("unsupported Wikimedia image format")

    cache_root = Path(cache_dir).resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(media_url.encode("utf-8")).hexdigest()
    target = (cache_root / f"{digest}{extension}").resolve()
    try:
        target.relative_to(cache_root)
    except ValueError as exc:
        raise PublicMediaError("unsafe public media cache path") from exc
    if target.is_file() and 0 < target.stat().st_size <= max_bytes:
        return str(target)

    temporary = target.with_suffix(target.suffix + ".partial")
    size = 0
    try:
        with requests.get(
            media_url,
            stream=True,
            headers={"User-Agent": user_agent},
            timeout=(3, timeout),
        ) as response:
            response.raise_for_status()
            _safe_wikimedia_url(str(response.url))
            response_type = (
                response.headers.get("Content-Type", "")
                .split(";", 1)[0]
                .lower()
                .strip()
            )
            if response_type not in _ALLOWED_MIME:
                raise PublicMediaError(
                    f"Wikimedia download returned unsupported MIME {response_type!r}"
                )
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > max_bytes:
                raise PublicMediaError("Wikimedia image exceeds download limit")
            with temporary.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_bytes:
                        raise PublicMediaError(
                            "Wikimedia image exceeds download limit"
                        )
                    handle.write(chunk)
        if size <= 0:
            raise PublicMediaError("Wikimedia image download was empty")
        os.replace(temporary, target)
        return str(target)
    except requests.RequestException as exc:
        raise PublicMediaError(
            f"Wikimedia image download failed: {type(exc).__name__}"
        ) from exc
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def acquire_commons_image(
    query: str,
    *,
    task_dir: str,
    scene_id: str,
    cache_dir: str,
    aspect: str,
    max_results: int = 6,
    timeout: float = 12.0,
    cache_ttl_seconds: float = 3600.0,
    max_bytes: int = 20 * 1024 * 1024,
    user_agent: str = (
        "MoneyPrinterTurbo-RU/1.0 "
        "(https://github.com/tornado909/MoneyPrinterTurbo)"
    ),
) -> dict[str, Any]:
    candidates = search_commons_images(
        query,
        aspect=aspect,
        max_results=max_results,
        timeout=timeout,
        cache_ttl_seconds=cache_ttl_seconds,
        user_agent=user_agent,
    )
    if not candidates:
        raise PublicMediaError("no reusable Wikimedia image matched the scene")

    last_error: Exception | None = None
    for item in candidates:
        try:
            cached = _download_to_cache(
                item,
                cache_dir=cache_dir,
                timeout=timeout,
                max_bytes=max_bytes,
                user_agent=user_agent,
            )
            suffix = Path(cached).suffix.lower()
            task_root = Path(task_dir).resolve()
            target_dir = (task_root / "public-media").resolve()
            target_dir.mkdir(parents=True, exist_ok=True)
            safe_scene = re.sub(
                r"[^A-Za-z0-9_-]+", "-", str(scene_id or "scene")
            ).strip("-")[:80] or "scene"
            target = (target_dir / f"{safe_scene}{suffix}").resolve()
            target.relative_to(task_root)
            if target.exists() or target.is_symlink():
                target.unlink()
            try:
                os.link(cached, target)
            except OSError:
                shutil.copy2(cached, target)

            result = dict(item)
            result["query"] = query
            result["path"] = str(target)
            result["cache_path"] = cached
            return result
        except (OSError, ValueError, PublicMediaError) as exc:
            last_error = exc
            continue
    raise PublicMediaError(
        f"all reusable Wikimedia candidates failed: {last_error}"
    )


def write_credits(task_dir: str, items: list[dict[str, Any]]) -> tuple[str, str]:
    root = Path(task_dir)
    root.mkdir(parents=True, exist_ok=True)
    unique = []
    seen = set()
    for item in items:
        page_url = str(item.get("page_url") or "")
        if not page_url or page_url in seen:
            continue
        seen.add(page_url)
        unique.append(
            {
                key: item.get(key)
                for key in (
                    "title",
                    "page_url",
                    "original_url",
                    "license",
                    "license_url",
                    "artist",
                    "credit",
                    "attribution_required",
                    "usage_terms",
                )
                if item.get(key) not in (None, "")
            }
        )

    json_path = root / "public-media-credits.json"
    text_path = root / "public-media-credits.txt"
    json_path.write_text(
        json.dumps(unique, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    lines = []
    for index, item in enumerate(unique, start=1):
        parts = [
            f"{index}. {item.get('title', 'Wikimedia Commons image')}",
            f"Source: {item.get('page_url', '')}",
            f"License: {item.get('license', '')}",
        ]
        if item.get("artist"):
            parts.append(f"Author: {item['artist']}")
        if item.get("license_url"):
            parts.append(f"License URL: {item['license_url']}")
        lines.append("\n".join(parts))
    text_path.write_text("\n\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return str(json_path), str(text_path)
