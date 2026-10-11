from __future__ import annotations

import json
import re
import threading
import uuid
from contextlib import contextmanager

from quota_budget import (
    add_video,
    get_setting,
    get_trend_search_cache,
    set_trend_search_cache,
)
from youtube_api import execute, youtube_service


CATEGORIES = {
    "1": "Film & Animation",
    "2": "Autos & Vehicles",
    "10": "Music",
    "15": "Pets & Animals",
    "17": "Sports",
    "19": "Travel & Events",
    "20": "Gaming",
    "22": "People & Blogs",
    "23": "Comedy",
    "24": "Entertainment",
    "25": "News & Politics",
    "26": "Howto & Style",
    "27": "Education",
    "28": "Science & Technology",
    "29": "Nonprofits & Activism",
}
LANGUAGES = {
    "hinglish": {"label": "Hinglish", "relevance": "hi", "query": "Hinglish"},
    "en": {"label": "English", "relevance": "en", "query": "trending"},
    "hi": {"label": "Hindi", "relevance": "hi", "query": "trending"},
    "ur": {"label": "Urdu", "relevance": "ur", "query": "trending"},
}
INDIA_REGIONS = {
    "all": {"label": "All India", "states": ()},
    "north": {
        "label": "North India",
        "states": (
            "Jammu and Kashmir", "Ladakh", "Himachal Pradesh", "Punjab",
            "Haryana", "Chandigarh", "Uttarakhand", "Delhi", "Uttar Pradesh",
            "Rajasthan",
        ),
    },
    "south": {
        "label": "South India",
        "states": (
            "Andhra Pradesh", "Karnataka", "Kerala", "Tamil Nadu",
            "Telangana", "Puducherry", "Lakshadweep", "Andaman and Nicobar",
        ),
    },
    "west": {
        "label": "West India",
        "states": (
            "Goa", "Gujarat", "Maharashtra", "Rajasthan",
            "Dadra and Nagar Haveli", "Daman and Diu",
        ),
    },
    "east": {
        "label": "East India",
        "states": (
            "Bihar", "Jharkhand", "Odisha", "West Bengal", "Sikkim",
            "Assam", "Arunachal Pradesh", "Manipur", "Meghalaya",
            "Mizoram", "Nagaland", "Tripura",
        ),
    },
}
TREND_REQUEST_TIMEOUT_SECONDS = 4
TREND_CACHE_TTL_SECONDS = 15 * 60
TREND_CACHE_LOCKS: dict[str, tuple[threading.Lock, int]] = {}
TREND_CACHE_LOCKS_GUARD = threading.Lock()
ISO_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?"
    r"(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)


def duration_seconds(value: str) -> float:
    match = ISO_DURATION.fullmatch(value)
    if not match:
        raise ValueError("YouTube returned an invalid ISO-8601 video duration.")
    parts = {name: float(amount or 0) for name, amount in match.groupdict().items()}
    return parts["days"] * 86400 + parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"]


def language_match_score(text: str, language: str) -> float:
    letters = [character for character in text if character.isalpha()]
    if not letters:
        return 0.0
    if language in {"hi", "hinglish"}:
        script_matches = sum("\u0900" <= character <= "\u097f" for character in letters)
        if language == "hi":
            return script_matches / len(letters)
        hindi_words = {
            "acha", "achha", "aur", "bhai", "hai", "hain", "ka", "ki", "kya",
            "mein", "mera", "nahi", "nahin", "se", "tha", "the", "toh", "yeh",
        }
        roman_matches = sum(
            word in hindi_words for word in re.findall(r"[a-z]+", text.casefold())
        )
        return max(script_matches / len(letters), roman_matches / max(1, len(letters) / 5))
    if language == "ur":
        return sum("\u0600" <= character <= "\u06ff" for character in letters) / len(letters)
    return sum(character.isascii() and character.isalpha() for character in letters) / len(letters)


@contextmanager
def _trend_cache_lock(cache_key: str):
    with TREND_CACHE_LOCKS_GUARD:
        entry = TREND_CACHE_LOCKS.get(cache_key)
        lock, users = entry if entry else (threading.Lock(), 0)
        TREND_CACHE_LOCKS[cache_key] = (lock, users + 1)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with TREND_CACHE_LOCKS_GUARD:
            _, users = TREND_CACHE_LOCKS[cache_key]
            if users == 1:
                del TREND_CACHE_LOCKS[cache_key]
            else:
                TREND_CACHE_LOCKS[cache_key] = (lock, users - 1)


def fetch_trending(
    region: str | None = None,
    category_id: str | None = None,
    *,
    language: str | None = None,
    india_region: str | None = None,
    task_id: str | None = None,
    force_refresh: bool = False,
) -> list[dict]:
    results, _ = fetch_trending_with_status(
        region,
        category_id,
        language=language,
        india_region=india_region,
        task_id=task_id,
        force_refresh=force_refresh,
    )
    return results


def fetch_trending_with_status(
    region: str | None = None,
    category_id: str | None = None,
    *,
    language: str | None = None,
    india_region: str | None = None,
    task_id: str | None = None,
    force_refresh: bool = False,
) -> tuple[list[dict], bool]:
    region = (region or get_setting("region")).upper()
    category_id = str(category_id or get_setting("category_id"))
    language = language or get_setting("trend_language")
    india_region = india_region or get_setting("india_region")
    if not re.fullmatch(r"[A-Z]{2}", region):
        raise ValueError("Choose a valid two-letter country code.")
    if category_id not in CATEGORIES:
        raise ValueError("Choose a supported YouTube video category.")
    if language not in LANGUAGES:
        raise ValueError("Choose Hinglish, English, Hindi, or Urdu for trend discovery.")
    if india_region not in INDIA_REGIONS:
        raise ValueError("Choose All India, North, South, West, or East India.")
    if india_region != "all" and region != "IN":
        raise ValueError("Choose India (IN) to use an Indian regional filter.")

    cache_key = json.dumps(
        [region, category_id, language, india_region],
        separators=(",", ":"),
    )
    with _trend_cache_lock(cache_key):
        if not force_refresh:
            cached = get_trend_search_cache(cache_key, TREND_CACHE_TTL_SECONDS)
            if cached is not None:
                for video in cached:
                    add_video(video)
                return cached, True
        discovered = _fetch_trending_from_api(
            region,
            category_id,
            language=language,
            india_region=india_region,
            task_id=task_id,
        )
        set_trend_search_cache(cache_key, discovered)
        return discovered, False


def _fetch_trending_from_api(
    region: str,
    category_id: str,
    *,
    language: str,
    india_region: str,
    task_id: str | None,
) -> list[dict]:
    service = youtube_service(timeout=TREND_REQUEST_TIMEOUT_SECONDS)
    states = INDIA_REGIONS[india_region]["states"]
    response = execute(
        service.search()
        .list(
            part="snippet",
            type="video",
            q="trending",
            regionCode=region,
            videoCategoryId=category_id,
            relevanceLanguage=LANGUAGES[language]["relevance"],
            maxResults=50,
        ),
        "search.list",
        "trends",
        task_id=task_id or uuid.uuid4().hex,
        task_payload={
            "kind": "trend_scout",
            "region": region,
            "category_id": category_id,
            "language": language,
            "india_region": india_region,
        },
    )
    search_results = []
    for item in response.get("items", []):
        snippet = item.get("snippet") or {}
        video_id = (item.get("id") or {}).get("videoId")
        if not video_id or not snippet.get("title"):
            continue
        search_text = " ".join(
            (snippet.get("title", ""), snippet.get("description", ""), snippet.get("channelTitle", ""))
        ).casefold()
        matched_states = [
            state for state in states if state.casefold() in search_text
        ]
        language_score = language_match_score(search_text, language)
        search_results.append(
            {
                "video_id": video_id,
                "snippet": snippet,
                "matched_states": matched_states,
                "language_score": language_score,
            }
        )

    detail_ids = [result["video_id"] for result in search_results]
    details_by_id = {}
    if detail_ids:
        detail_response = execute(
            service.videos().list(
                part="snippet,contentDetails,statistics",
                id=",".join(detail_ids),
                maxResults=len(detail_ids),
            ),
            "videos.list",
            "trends",
            task_id=f"{task_id or uuid.uuid4().hex}:details",
            task_payload={
                "kind": "trend_scout",
                "region": region,
                "category_id": category_id,
                "language": language,
                "india_region": india_region,
            },
        )
        details_by_id = {
            item.get("id"): item
            for item in detail_response.get("items", [])
            if item.get("id")
        }

    search_results.sort(
        key=lambda result: (
            bool(result["matched_states"]),
            result["language_score"],
        ),
        reverse=True,
    )

    discovered = []
    for result in search_results:
        video_id = result["video_id"]
        snippet = result["snippet"]
        detail = details_by_id.get(video_id, {})
        statistics = detail.get("statistics") or {}
        content_details = detail.get("contentDetails") or {}
        detail_snippet = detail.get("snippet") or {}
        duration_value = content_details.get("duration")
        duration = duration_seconds(duration_value or "PT0S")
        live_status = detail_snippet.get("liveBroadcastContent", "")
        video = {
            "video_id": video_id,
            "title": snippet.get("title", "Untitled video"),
            "channel_title": snippet.get("channelTitle", ""),
            "description": snippet.get("description", ""),
            "thumbnail": (
                (snippet.get("thumbnails") or {}).get("high")
                or (snippet.get("thumbnails") or {}).get("medium")
                or (snippet.get("thumbnails") or {}).get("default")
                or {}
            ).get("url", ""),
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "category_id": category_id,
            "duration_seconds": duration,
            "view_count": int(statistics.get("viewCount", 0)),
            "metadata": json.dumps(
                {
                    "region": region,
                    "category_name": CATEGORIES[category_id],
                    "language": language,
                    "language_name": LANGUAGES[language]["label"],
                    "india_region": india_region,
                    "india_region_name": INDIA_REGIONS[india_region]["label"],
                    "india_region_matches": result["matched_states"],
                    "language_match_score": round(result["language_score"], 3),
                    "language_match": result["language_score"] >= 0.15,
                    "statistics_available": bool(detail.get("statistics")),
                    "duration_available": bool(duration_value and duration > 0),
                    "live_status": live_status,
                    "view_count": (
                        int(statistics["viewCount"])
                        if "viewCount" in statistics
                        else None
                    ),
                    "like_count": (
                        int(statistics["likeCount"])
                        if "likeCount" in statistics
                        else None
                    ),
                    "comment_count": (
                        int(statistics["commentCount"])
                        if "commentCount" in statistics
                        else None
                    ),
                    "published_at": snippet.get("publishedAt", ""),
                    "channel_id": snippet.get("channelId", ""),
                }
            ),
        }
        add_video(video)
        discovered.append(video)
    return discovered


def categories() -> list[dict]:
    return [{"id": key, "name": value} for key, value in CATEGORIES.items()]


def languages() -> list[dict]:
    return [{"id": key, "name": value["label"]} for key, value in LANGUAGES.items()]


def india_regions() -> list[dict]:
    return [{"id": key, "name": value["label"]} for key, value in INDIA_REGIONS.items()]
