from __future__ import annotations

import html
import re
import threading
import time
from dataclasses import dataclass, asdict
from typing import Any

import requests


class ResearchError(RuntimeError):
    pass


@dataclass(slots=True)
class ResearchItem:
    title: str
    extract: str
    page_id: int | None = None
    source: str = "wikipedia"
    source_url: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_TAG_RE = re.compile(r"<[^>]+>")
_CACHE_LOCK = threading.RLock()
_CACHE: dict[tuple, tuple[float, list[dict[str, Any]]]] = {}


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _clean(value: object, *, max_chars: int) -> str:
    text = html.unescape(_TAG_RE.sub("", str(value or "")))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def _fetch_language(
    query: str,
    *,
    language: str,
    max_pages: int,
    max_chars_per_page: int,
    timeout: float,
) -> list[dict[str, Any]]:
    endpoint = f"https://{language}.wikipedia.org/w/api.php"
    params = {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "generator": "search",
        "gsrsearch": query,
        "gsrlimit": max_pages,
        "prop": "extracts",
        "exintro": "1",
        "explaintext": "1",
        "exchars": max_chars_per_page,
        "redirects": "1",
    }
    try:
        response = requests.get(
            endpoint,
            params=params,
            headers={
                "User-Agent": (
                    "MoneyPrinterTurbo-RU/1.0 "
                    "(https://github.com/tornado909/MoneyPrinterTurbo)"
                )
            },
            timeout=(3, timeout),
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise ResearchError(
            f"Wikipedia research request failed: {type(exc).__name__}"
        ) from exc
    except ValueError as exc:
        raise ResearchError("Wikipedia research returned invalid JSON") from exc

    pages = ((payload.get("query") or {}).get("pages") or [])
    result: list[ResearchItem] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        title = _clean(page.get("title"), max_chars=200)
        extract = _clean(page.get("extract"), max_chars=max_chars_per_page)
        if not title or not extract:
            continue
        page_id = page.get("pageid")
        result.append(
            ResearchItem(
                title=title,
                extract=extract,
                page_id=page_id if isinstance(page_id, int) else None,
                source=f"wikipedia:{language}",
                source_url=(
                    f"https://{language}.wikipedia.org/?curid={page_id}"
                    if isinstance(page_id, int)
                    else ""
                ),
            )
        )
        if len(result) >= max_pages:
            break
    return [item.as_dict() for item in result]


def wikipedia_research(
    query: str,
    *,
    language: str = "ru",
    max_pages: int = 3,
    max_chars_per_page: int = 1800,
    timeout: float = 10.0,
    fallback_languages: tuple[str, ...] = ("en",),
    cache_ttl_seconds: float = 3600.0,
) -> list[dict[str, Any]]:
    """Fetch bounded public encyclopedia context without any AI API.

    The requested language is tried first. If it yields too little context,
    fallback languages are queried until the bounded page budget is filled.
    Results are cached in-process so Director retries do not repeat public HTTP
    requests or waste latency.
    """
    query = str(query or "").strip()
    if not query:
        return []

    lang = (language or "ru").split("-", 1)[0].lower()
    if not re.fullmatch(r"[a-z]{2,3}", lang):
        lang = "ru"
    max_pages = max(1, min(5, int(max_pages)))
    max_chars_per_page = max(200, min(4000, int(max_chars_per_page)))
    timeout = max(1.0, min(30.0, float(timeout)))
    cache_ttl_seconds = max(0.0, min(86400.0, float(cache_ttl_seconds)))

    languages = [lang]
    for fallback in fallback_languages or ():
        value = str(fallback or "").split("-", 1)[0].lower().strip()
        if re.fullmatch(r"[a-z]{2,3}", value) and value not in languages:
            languages.append(value)

    cache_key = (
        query.casefold(),
        tuple(languages),
        max_pages,
        max_chars_per_page,
    )
    now = time.monotonic()
    if cache_ttl_seconds > 0:
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
            if cached and now - cached[0] <= cache_ttl_seconds:
                return [dict(item) for item in cached[1]]

    result: list[dict[str, Any]] = []
    seen = set()
    errors: list[Exception] = []
    for current_language in languages:
        remaining = max_pages - len(result)
        if remaining <= 0:
            break
        try:
            rows = _fetch_language(
                query,
                language=current_language,
                max_pages=remaining,
                max_chars_per_page=max_chars_per_page,
                timeout=timeout,
            )
        except ResearchError as exc:
            errors.append(exc)
            continue
        for row in rows:
            identity = (
                str(row.get("title") or "").casefold(),
                str(row.get("source_url") or ""),
            )
            if identity in seen:
                continue
            seen.add(identity)
            result.append(row)
            if len(result) >= max_pages:
                break

    if not result and errors:
        raise ResearchError(str(errors[0]))

    if cache_ttl_seconds > 0:
        with _CACHE_LOCK:
            _CACHE[cache_key] = (now, [dict(item) for item in result])
            # Bound memory even on a long-running WebUI process.
            if len(_CACHE) > 128:
                oldest = min(_CACHE.items(), key=lambda item: item[1][0])[0]
                _CACHE.pop(oldest, None)

    return result

