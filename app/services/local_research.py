from __future__ import annotations

import html
import re
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

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_TAG_RE = re.compile(r"<[^>]+>")


def _clean(value: object, *, max_chars: int) -> str:
    text = html.unescape(_TAG_RE.sub("", str(value or "")))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def wikipedia_research(
    query: str,
    *,
    language: str = "ru",
    max_pages: int = 3,
    max_chars_per_page: int = 1800,
    timeout: float = 10.0,
) -> list[dict[str, Any]]:
    """Fetch bounded public encyclopedia context without any AI API."""
    query = str(query or "").strip()
    if not query:
        return []

    lang = (language or "ru").split("-", 1)[0].lower()
    if not re.fullmatch(r"[a-z]{2,3}", lang):
        lang = "ru"
    max_pages = max(1, min(5, int(max_pages)))
    endpoint = f"https://{lang}.wikipedia.org/w/api.php"
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
            )
        )
    return [item.as_dict() for item in result]
