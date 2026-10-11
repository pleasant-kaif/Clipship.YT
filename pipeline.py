from __future__ import annotations

import json
import logging
import mimetypes
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Blueprint, abort, jsonify, redirect, render_template, request, send_file
from werkzeug.utils import secure_filename

import clip_factory
import captioning
import engagement
import quota_budget as store
import seo_agent
import trend_scout
import youtube_api
from quota_budget import (
    APP_DATA_DIR,
    QuotaExceeded,
    connect,
    local_now,
    set_settings,
    upload_daily_limit,
)
from youtube_api import execute, youtube_service


pipeline = Blueprint("pipeline", __name__)
logger = logging.getLogger(__name__)
pipeline_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="clipship-pipeline")
SOURCE_DIR = APP_DATA_DIR / "sources"
SOURCE_DIR.mkdir(parents=True, exist_ok=True)
MAX_SOURCE_BYTES = 2 * 1024 * 1024 * 1024
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
CLIP_SECONDS = 60
SCHEDULE_POLL_LOCK = threading.Lock()
DAILY_TIME = re.compile(r"^\d{2}:\d{2}$")
COUNTRY_CODE = re.compile(r"^[A-Z]{2}$")


def video_for_json(video: dict) -> dict:
    try:
        trend_metadata = json.loads(video.get("metadata") or "{}")
    except json.JSONDecodeError:
        trend_metadata = {}
    result = {
        key: value
        for key, value in video.items()
        if key not in {"source_path", "source_caption_path", "clip_path", "metadata"}
    }
    result["trend_region"] = trend_metadata.get("region", "")
    result["trend_category_id"] = str(video.get("category_id") or "")
    result["trend_language"] = trend_metadata.get("language", "")
    result["trend_language_name"] = trend_metadata.get("language_name", "")
    result["language_match"] = bool(trend_metadata.get("language_match", False))
    result["language_match_score"] = float(
        trend_metadata.get("language_match_score", 0.0)
    )
    result["india_region"] = trend_metadata.get("india_region", "all")
    result["india_region_name"] = trend_metadata.get("india_region_name", "All India")
    result["india_region_matches"] = trend_metadata.get("india_region_matches", [])
    result["statistics_available"] = bool(
        trend_metadata.get("statistics_available", video.get("view_count"))
    )
    result["view_count"] = (
        trend_metadata.get("view_count", video.get("view_count"))
        if result["statistics_available"]
        else None
    )
    result["like_count"] = trend_metadata.get("like_count")
    result["comment_count"] = trend_metadata.get("comment_count")
    duration_available = trend_metadata.get("duration_available")
    if duration_available is None:
        duration_value = video.get("duration_seconds")
        duration_available = (
            isinstance(duration_value, (int, float)) and duration_value > 0
        )
    result["duration_available"] = bool(duration_available)
    result["live_status"] = trend_metadata.get("live_status", "")
    result["rights_confirmed"] = bool(video["rights_confirmed"])
    result["has_local_source"] = bool(video["source_path"]) and Path(video["source_path"]).is_file()
    result["has_source_captions"] = (
        bool(video["source_caption_path"])
        and Path(video["source_caption_path"]).is_file()
    )
    result["source_captions_url"] = (
        f"/api/pipeline/videos/{video['video_id']}/captions"
        if result["has_source_captions"]
        else ""
    )
    result["has_preview"] = bool(video["clip_path"]) and Path(video["clip_path"]).is_file()
    result["source_url"] = f"/api/pipeline/videos/{video['video_id']}/source"
    result["preview_url"] = f"/api/pipeline/videos/{video['video_id']}/preview"
    if video.get("uploaded_video_id"):
        result["published_url"] = (
            f"https://www.youtube.com/watch?v={video['uploaded_video_id']}"
        )
    return result


def dashboard_state() -> dict:
    settings = store.get_settings()
    videos = store.list_dashboard_videos()
    unpublished = sum(
        item["status"] in {
            "source_uploaded",
            "processing",
            "captioning",
            "auto_clipping",
            "uploading",
            "failed",
            "pending_approval",
            "approved",
            "upload_failed",
        }
        for item in videos
    )
    return {
        "quota": store.QuotaBudget().snapshot(),
        "settings": {
            key: value
            for key, value in settings.items()
            if key not in {"oauth_state", "oauth_code_verifier"}
        },
        "youtube": youtube_api.credentials_status(),
        "clip_duration_seconds": CLIP_SECONDS,
        "daily_clip_target": int(settings["clips_per_day"]),
        "queued_source_videos": unpublished,
        "categories": trend_scout.categories(),
        "trend_languages": trend_scout.languages(),
        "india_regions": trend_scout.india_regions(),
        "videos": [video_for_json(item) for item in videos],
        "engagement": {
            "comments": engagement.list_engagement(),
            "published_videos": [
                {
                    "video_id": video["video_id"],
                    "uploaded_video_id": video["uploaded_video_id"],
                    "title": video["title"],
                    "preview_url": f"/api/pipeline/videos/{video['video_id']}/preview",
                }
                for video in engagement.published_videos()
            ],
        },
    }


def save_schedule_settings(data: dict) -> dict[str, str]:
    timezone = data.get("timezone", store.get_setting("timezone"))
    if not isinstance(timezone, str):
        raise ValueError("Choose a valid IANA timezone.")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as error:
        raise ValueError("Choose a valid IANA timezone, such as Asia/Kolkata.") from error

    cap = data.get("quota_cap", int(store.get_setting("quota_cap")))
    if isinstance(cap, bool) or not isinstance(cap, int) or not 10_000 <= cap <= 1_000_000:
        raise ValueError("Daily quota cap must be between 10,000 and 1,000,000 units.")
    region = data.get("region", store.get_setting("region"))
    if not isinstance(region, str) or not COUNTRY_CODE.fullmatch(region.upper()):
        raise ValueError("Country/region must be a two-letter ISO country code.")
    category = str(data.get("category_id", store.get_setting("category_id")))
    if category not in trend_scout.CATEGORIES:
        raise ValueError("Choose a supported YouTube video category.")
    trend_language = data.get(
        "trend_language", store.get_setting("trend_language")
    )
    if not isinstance(trend_language, str) or trend_language not in trend_scout.LANGUAGES:
        raise ValueError("Choose Hinglish, English, Hindi, or Urdu for trend discovery.")
    india_region = data.get("india_region", store.get_setting("india_region"))
    if not isinstance(india_region, str) or india_region not in trend_scout.INDIA_REGIONS:
        raise ValueError("Choose All India, North, South, West, or East India.")
    if india_region != "all" and region.upper() != "IN":
        raise ValueError("Choose India (IN) to use an Indian regional filter.")
    schedule_time = data.get("schedule_time", store.get_setting("schedule_time"))
    if not isinstance(schedule_time, str) or not DAILY_TIME.fullmatch(schedule_time):
        raise ValueError("Schedule time must use 24-hour HH:MM format.")
    try:
        time.fromisoformat(schedule_time)
    except ValueError as error:
        raise ValueError("Schedule time must use 24-hour HH:MM format.") from error
    clips_per_day = data.get("clips_per_day", int(store.get_setting("clips_per_day")))
    if (
        isinstance(clips_per_day, bool)
        or not isinstance(clips_per_day, int)
        or not 1 <= clips_per_day <= 10
    ):
        raise ValueError("The daily clip target must be between one and ten.")
    auto_scout = data.get("auto_scout", store.get_setting("auto_scout") == "true")
    if not isinstance(auto_scout, bool):
        raise ValueError("Auto-discovery must be enabled or disabled.")
    privacy = data.get("upload_privacy", store.get_setting("upload_privacy"))
    if privacy not in {"private", "unlisted", "public"}:
        raise ValueError("Choose private, unlisted, or public upload visibility.")

    settings = {
        "timezone": timezone,
        "quota_cap": str(cap),
        "region": region.upper(),
        "category_id": category,
        "trend_language": trend_language,
        "india_region": india_region,
        "schedule_time": schedule_time,
        "clips_per_day": str(clips_per_day),
        "auto_scout": str(auto_scout).lower(),
        "upload_privacy": privacy,
    }
    set_settings(settings)
    return settings


def schedule_greenlit_video(video_id: str) -> dict:
    video = store.get_video(video_id)
    if not video:
        raise KeyError("This trend is no longer available.")
    if (
        not video["rights_confirmed"]
        or not video["clip_path"]
        or not Path(video["clip_path"]).is_file()
        or video["status"] not in {"pending_approval", "approved", "upload_failed"}
    ):
        raise ValueError("Only a finished clip with confirmed rights can be approved.")

    now = local_now()
    settings = store.get_settings()
    zone = ZoneInfo(settings["timezone"])
    publish_hour, publish_minute = (int(part) for part in settings["schedule_time"].split(":"))
    per_day = upload_daily_limit(int(settings["quota_cap"]))
    if per_day < 1:
        raise ValueError("The daily quota does not leave room for a video upload.")

    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for offset in range(370):
            day = now.date() + timedelta(days=offset)
            published_time = time(hour=publish_hour, minute=publish_minute, tzinfo=zone)
            day_start = datetime.combine(day, published_time)
            if day_start <= now:
                continue
            quota_used = db.execute(
                "SELECT COALESCE(SUM(cost), 0) FROM quota_reservations "
                "WHERE local_day = ? AND priority = 'uploads'",
                (day.isoformat(),),
            ).fetchone()[0] // 1_600
            scheduled_count = db.execute(
                "SELECT COUNT(*) FROM videos WHERE status IN ('approved', 'published') "
                "AND substr(scheduled_at, 1, 10) = ? AND video_id != ?",
                (day.isoformat(), video_id),
            ).fetchone()[0]
            count = max(quota_used, scheduled_count)
            if count >= per_day:
                continue
            scheduled_at = (day_start + timedelta(minutes=30 * count)).isoformat()
            db.execute(
                "UPDATE videos SET status='approved', error='', scheduled_at=?, "
                "upload_privacy=?, updated_at=? "
                "WHERE video_id=? AND rights_confirmed=1 AND clip_path!='' "
                "AND status IN ('pending_approval', 'approved', 'upload_failed')",
                (scheduled_at, settings["upload_privacy"], now.isoformat(), video_id),
            )
            row = db.execute(
                "SELECT status FROM videos WHERE video_id=?", (video_id,)
            ).fetchone()
            if not row or row["status"] != "approved":
                db.rollback()
                raise ValueError("Only a finished clip with confirmed rights can be approved.")
            db.commit()
            return {
                "scheduled_at": scheduled_at,
                "date": day.isoformat(),
                "slot": count + 1,
                "timezone": settings["timezone"],
                "privacy": settings["upload_privacy"],
            }
        db.rollback()
    raise ValueError("Could not find an available upload slot within the next year.")


def process_uploaded_source(video_id: str) -> None:
    try:
        clip_factory.create_pipeline_clip(video_id)
    except Exception:
        logger.exception("Could not create a vertical clip from source %s", video_id)


def upload_greenlit_video(video: dict) -> dict:
    if (
        video["status"] != "approved"
        or not video["rights_confirmed"]
        or not Path(video["clip_path"]).is_file()
    ):
        raise ValueError(
            "Only an explicitly approved clip with confirmed rights and an available video file can be uploaded."
        )
    privacy = video.get("upload_privacy") or store.get_setting("upload_privacy")
    if privacy not in {"private", "unlisted", "public"}:
        raise ValueError("The approved clip has an invalid upload privacy setting.")
    try:
        from googleapiclient.http import MediaFileUpload
    except ImportError as error:
        raise RuntimeError(
            "The Google upload client is missing. Stop the app and run run.bat."
        ) from error

    service = youtube_service()
    description = seo_agent.write_description(video["video_id"])
    metadata = {
        "snippet": {
            "title": f"{video['title'][:85]} #Shorts",
            "description": description,
            "categoryId": str(video["category_id"] or store.get_setting("category_id")),
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(video["clip_path"], mimetype="video/mp4", resumable=False)
    result = execute(
        service.videos()
        .insert(
            part="snippet,status",
            body=metadata,
            media_body=media,
        ),
        "videos.insert",
        "uploads",
        task_id=video["video_id"],
    )
    uploaded_id = result.get("id")
    if not uploaded_id:
        raise RuntimeError("YouTube accepted the upload but returned no video ID.")
    store.update_video(
        video["video_id"],
        uploaded_video_id=uploaded_id,
        status="published",
        error="",
    )
    return {"uploaded_video_id": uploaded_id, "url": f"https://youtu.be/{uploaded_id}"}


def upload_original_source(video_id: str) -> dict:
    video = store.get_video(video_id)
    if (
        not video
        or video["status"] != "uploading"
        or video["upload_mode"] != "direct"
        or not video["rights_confirmed"]
        or not video["source_path"]
        or not Path(video["source_path"]).is_file()
    ):
        raise ValueError("The authorized original video is no longer available to upload.")
    try:
        from googleapiclient.http import MediaFileUpload
    except ImportError as error:
        raise RuntimeError(
            "The Google upload client is missing. Stop the app and run run.bat."
        ) from error

    source_path = Path(video["source_path"])
    mime_type = mimetypes.guess_type(source_path.name)[0] or "application/octet-stream"
    service = youtube_service()
    description = seo_agent.write_description(video_id)
    metadata = {
        "snippet": {
            "title": video["title"][:100],
            "description": description,
            "categoryId": str(video["category_id"] or store.get_setting("category_id")),
        },
        "status": {
            "privacyStatus": video.get("upload_privacy") or store.get_setting("upload_privacy"),
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(
        str(source_path),
        mimetype=mime_type,
        resumable=False,
    )
    result = execute(
        service.videos()
        .insert(
            part="snippet,status",
            body=metadata,
            media_body=media,
        ),
        "videos.insert",
        "uploads",
        task_id=video_id,
    )
    uploaded_id = result.get("id")
    if not uploaded_id:
        raise RuntimeError("YouTube accepted the upload but returned no video ID.")
    store.update_video(
        video_id,
        uploaded_video_id=uploaded_id,
        status="published",
        upload_mode="direct",
        scheduled_at="",
        error="",
    )
    return {"uploaded_video_id": uploaded_id, "url": f"https://youtu.be/{uploaded_id}"}


def process_original_video_upload(video_id: str) -> None:
    try:
        upload_original_source(video_id)
    except Exception as error:
        store.update_video(
            video_id,
            status="upload_failed",
            error=str(error)[:1000],
        )
        logger.exception("Could not publish original video %s", video_id)


def start_original_video_upload(video_id: str, *, require_connection: bool = True) -> bool:
    youtube_status = youtube_api.credentials_status()
    if not youtube_status["youtube_connected"] or not youtube_status["oauth_client_uploaded"]:
        if require_connection:
            raise PermissionError(
                "Connect your YouTube account before publishing the original video."
            )
        return False
    video = store.claim_source_for_upload(video_id)
    if not video:
        raise ValueError("This video is already uploading, published, or cannot be uploaded.")
    try:
        future = pipeline_executor.submit(process_original_video_upload, video_id)
    except Exception as error:
        store.update_video(
            video_id,
            status="upload_failed",
            error=f"Could not start YouTube upload: {error}"[:1000],
        )
        raise
    future.add_done_callback(
        lambda task: logger.error(
            "Direct YouTube upload task failed for %s: %s", video_id, task.exception()
        )
        if task.exception()
        else None
    )
    return True


def retry_deferred_api_tasks(result: dict) -> None:
    for task in store.due_api_tasks():
        payload = task["payload"]
        try:
            kind = payload.get("kind")
            if kind == "trend_scout":
                discovered = trend_scout.fetch_trending(
                    payload.get("region"),
                    payload.get("category_id"),
                    language=payload.get("language"),
                    india_region=payload.get("india_region", "all"),
                    task_id=task["task_id"],
                )
                result["scouted"] += len(discovered)
            elif kind == "comment_sync":
                published_ids = {
                    video["uploaded_video_id"] for video in engagement.published_videos()
                }
                if payload.get("video_id") not in published_ids:
                    raise ValueError("Deferred comment task is no longer for a published Clipship video.")
                engagement.sync_video_comments(
                    payload["video_id"], task_id=task["task_id"]
                )
            elif kind == "reply":
                engagement.send_approved_reply(
                    payload["comment_id"],
                    payload["reply"],
                    task_id=task["task_id"],
                )
            elif kind == "like":
                engagement.like_own_video(payload["video_id"], task_id=task["task_id"])
            else:
                raise ValueError("Unknown deferred YouTube API task.")
            store.finish_api_task(task["task_id"], "completed")
            result["status"] = "complete"
        except QuotaExceeded as error:
            result["errors"].append(str(error))
        except Exception as error:
            logger.exception("Deferred YouTube task %s failed.", task["task_id"])
            store.finish_api_task(task["task_id"], "failed", str(error))
            result["errors"].append(f"{task['operation']}: {error}")


def run_scheduler_once(now: datetime | None = None, *, force_scout: bool = False) -> dict:
    if not SCHEDULE_POLL_LOCK.acquire(blocking=False):
        return {"status": "already_running"}
    result = {
        "status": "idle",
        "scouted": 0,
        "uploaded": 0,
        "deferred_retried": 0,
        "errors": [],
    }
    try:
        settings = store.get_settings()
        current = now or local_now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=ZoneInfo(settings["timezone"]))
        current = current.astimezone(ZoneInfo(settings["timezone"]))
        today = current.date().isoformat()
        due_tasks = store.due_api_tasks()
        retry_deferred_api_tasks(result)
        result["deferred_retried"] = len(due_tasks)
        last_scout = settings.get("last_scout_date", "")
        due_time = time.fromisoformat(settings["schedule_time"])
        if (
            settings["auto_scout"] == "true"
            and last_scout != today
            and (force_scout or current.timetz().replace(tzinfo=None) >= due_time)
        ):
            status = youtube_api.credentials_status()
            if status["youtube_connected"] and status["oauth_client_uploaded"]:
                try:
                    discovered = trend_scout.fetch_trending(
                        language=settings["trend_language"],
                        india_region=settings["india_region"],
                        task_id=f"daily-scout:{today}"
                    )
                    result["scouted"] = len(discovered)
                    set_settings({"last_scout_date": today})
                    result["status"] = "complete"
                except QuotaExceeded as error:
                    result["errors"].append(str(error))
                    set_settings({"last_scout_date": today})
                except Exception as error:
                    logger.exception("Daily trend scout failed")
                    result["errors"].append(str(error))

        due_videos = [
            video
            for video in store.list_videos(limit=500)
            if video["status"] == "approved"
            and video["scheduled_at"]
            and datetime.fromisoformat(video["scheduled_at"]) <= current
        ]
        for video in due_videos:
            try:
                upload_greenlit_video(video)
                result["uploaded"] += 1
                result["status"] = "complete"
            except QuotaExceeded as error:
                result["errors"].append(str(error))
                break
            except Exception as error:
                logger.exception("Scheduled upload for %s failed", video["video_id"])
                store.update_video(
                    video["video_id"],
                    status="upload_failed",
                    error=str(error)[:1000],
                )
                result["errors"].append(f"{video['title']}: {error}")
                break
        for video in store.list_videos(limit=500):
            if video["status"] != "source_uploaded" or video["upload_mode"] == "direct":
                continue
            if (
                not store.reserve_daily_clip(
                    video["video_id"], int(settings["clips_per_day"])
                )
                and not store.daily_clip_reserved_today(video["video_id"])
            ):
                continue
            pipeline_executor.submit(process_uploaded_source, video["video_id"])
            result["status"] = "complete"
        return result
    finally:
        SCHEDULE_POLL_LOCK.release()


@pipeline.get("/automation")
def automation_page():
    return render_template("automation.html")


@pipeline.get("/api/dashboard")
def get_dashboard():
    return jsonify(dashboard_state())


@pipeline.get("/api/quota")
@pipeline.get("/quota")
def get_quota():
    return jsonify(store.QuotaBudget().snapshot())


@pipeline.put("/api/settings")
def update_settings():
    data = request.get_json(silent=True) or {}
    try:
        settings = save_schedule_settings(data)
    except (ValueError, TypeError) as error:
        return jsonify({"error": str(error)}), 400
    return jsonify({"settings": settings, "quota": store.QuotaBudget().snapshot()})


@pipeline.get("/api/youtube/status")
def get_youtube_status():
    return jsonify(youtube_api.credentials_status())


@pipeline.get("/api/ollama/status")
def get_ollama_status():
    return jsonify(engagement.ollama_status())


@pipeline.post("/api/ollama/start")
def start_ollama():
    """Launch Ollama serve in the background and wait up to 12 s for it to respond."""
    import threading as _threading
    result_holder: list[dict] = []
    done = _threading.Event()

    def _launch() -> None:
        result_holder.append(engagement.ollama_start())
        done.set()

    _threading.Thread(target=_launch, daemon=True).start()
    done.wait(timeout=12)
    if not result_holder:
        return jsonify({
            "started": True,
            "available": False,
            "message": "Ollama launch timed out waiting for a response. Check if it started.",
        }), 504
    result = result_holder[0]
    status_code = 200 if result.get("available") else 503
    return jsonify(result), status_code


@pipeline.post("/api/youtube/client-secret")
def upload_client_secret():
    uploaded = request.files.get("client_secret")
    if not uploaded or not uploaded.filename or not uploaded.filename.lower().endswith(".json"):
        return jsonify({"error": "Choose the Google OAuth client-secret .json file."}), 400
    contents = uploaded.stream.read(512 * 1024 + 1)
    if len(contents) > 512 * 1024:
        return jsonify({"error": "The OAuth client-secret file must be under 512 KB."}), 400
    try:
        youtube_api.save_uploaded_client_secret(contents)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    return jsonify({"oauth_client_uploaded": True})


@pipeline.post("/api/youtube/auth/start")
def start_youtube_oauth():
    try:
        return jsonify({"authorization_url": youtube_api.oauth_authorization_url()})
    except (RuntimeError, FileNotFoundError, ValueError) as error:
        return jsonify({"error": str(error)}), 400


@pipeline.get("/api/youtube/callback")
def youtube_oauth_callback():
    if request.args.get("error"):
        return jsonify({"error": "Google account authorization was cancelled."}), 400
    try:
        youtube_api.finish_oauth_callback(
            request.args.get("code", ""),
            request.args.get("state", ""),
        )
    except (RuntimeError, FileNotFoundError, ValueError) as error:
        return jsonify({"error": str(error)}), 400
    return redirect("/automation?youtube_connected=1")


@pipeline.post("/api/youtube/disconnect")
def disconnect_youtube():
    youtube_api.disconnect_youtube()
    return jsonify({"youtube_connected": False})


@pipeline.get("/api/categories")
def get_categories():
    return jsonify({"categories": trend_scout.categories()})


@pipeline.post("/api/trends/scout")
def discover_trending():
    data = request.get_json(silent=True) or {}
    region = data.get("region", store.get_setting("region"))
    category_id = str(data.get("category_id", store.get_setting("category_id")))
    trend_language = data.get(
        "trend_language", store.get_setting("trend_language")
    )
    india_region = data.get("india_region", store.get_setting("india_region"))
    force_refresh = data.get("force_refresh", False)
    try:
        if not isinstance(force_refresh, bool):
            raise ValueError("force_refresh must be a boolean.")
        settings = save_schedule_settings(
            {
                "region": region,
                "category_id": category_id,
                "trend_language": trend_language,
                "india_region": india_region,
            }
        )
        discovered, cached = trend_scout.fetch_trending_with_status(
            settings["region"],
            settings["category_id"],
            language=settings["trend_language"],
            india_region=settings["india_region"],
            task_id=f"manual-scout:{uuid.uuid4().hex}",
            force_refresh=force_refresh,
        )
    except QuotaExceeded as error:
        return jsonify({"error": str(error), "quota": store.QuotaBudget().snapshot()}), 429
    except (PermissionError, FileNotFoundError) as error:
        return jsonify({"error": str(error)}), 401
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except TimeoutError:
        return jsonify(
            {"error": "YouTube trend search exceeded its 10-second limit. Try again shortly."}
        ), 504
    except Exception as error:
        logger.exception("YouTube trend discovery failed")
        return jsonify({"error": f"YouTube trend discovery failed: {error}"}), 502
    discovered_ids = {entry["video_id"] for entry in discovered}
    return jsonify(
        {
            "videos": [
                video_for_json(item)
                for item in store.list_dashboard_videos()
                if item["video_id"] in discovered_ids
            ],
            "quota": store.QuotaBudget().snapshot(),
            "cached": cached,
            "quota_cost": 0 if cached else 100 + int(bool(discovered)),
        }
    )


@pipeline.get("/api/pipeline/videos")
def pipeline_videos():
    return jsonify({"videos": [video_for_json(item) for item in store.list_videos()]})


@pipeline.post("/api/pipeline/videos/<video_id>/source")
def upload_source(video_id: str):
    record = store.get_video(video_id)
    if not record:
        return jsonify({"error": "Scout trending videos before adding local source footage."}), 404
    if request.form.get("rights_confirmed") != "true":
        return jsonify(
            {"error": "Confirm that you own this source file or have permission to create clips from it."}
        ), 400
    if record["status"] not in {
        "trending", "source_uploaded", "failed", "upload_failed"
    }:
        return jsonify({"error": "A source file cannot be changed while it is uploading or already published."}), 409
    uploaded = request.files.get("source_file")
    if not uploaded or not uploaded.filename:
        return jsonify({"error": "Choose the original source video file from your computer."}), 400
    extension = Path(secure_filename(uploaded.filename)).suffix.lower()
    if extension not in VIDEO_EXTENSIONS:
        return jsonify({"error": "Choose an MP4, MOV, MKV, WebM or M4V video file."}), 400
    target = SOURCE_DIR / f"{uuid.uuid4().hex}{extension}"
    try:
        if request.content_length and request.content_length > MAX_SOURCE_BYTES + 512 * 1024:
            return jsonify({"error": "Source files must be under 2 GB."}), 413
        target.parent.mkdir(parents=True, exist_ok=True)
        uploaded.save(target)
        if target.stat().st_size <= 0 or target.stat().st_size > MAX_SOURCE_BYTES:
            target.unlink(missing_ok=True)
            return jsonify({"error": "Source files must be under 2 GB and cannot be empty."}), 413
        store.update_video(
            video_id,
            source_path=str(target),
            source_caption_path="",
            clip_path="",
            caption_text="",
            seo_description="",
            seo_status="pending",
            start_seconds=0,
            rights_confirmed=1,
            upload_mode="direct",
            upload_privacy=store.get_setting("upload_privacy"),
            status="source_uploaded",
            error="",
        )
    except Exception as error:
        target.unlink(missing_ok=True)
        logger.exception("Could not save local source video.")
        return jsonify({"error": f"Could not save the uploaded video: {error}"}), 500
    try:
        started = start_original_video_upload(video_id, require_connection=False)
    except ValueError as error:
        logger.warning("Could not start direct upload for %s: %s", video_id, error)
        return jsonify({"error": str(error)}), 409
    if not started:
        store.update_video(
            video_id,
            error="Connect your YouTube account, then choose Publish original video to start the upload.",
        )
    return jsonify(
        {
            "status": "uploading" if started else "source_uploaded",
            "video_id": video_id,
            "direct_upload": started,
        }
    ), 202


@pipeline.post("/api/pipeline/videos/<video_id>/publish-source")
def publish_saved_source(video_id: str):
    video = store.get_video(video_id)
    if (
        not video
        or video["status"] not in {"source_uploaded", "failed", "upload_failed"}
        or not video["rights_confirmed"]
        or not video["source_path"]
        or not Path(video["source_path"]).is_file()
        or video["uploaded_video_id"]
    ):
        return jsonify(
            {"error": "Only an authorized, unpublished local source can be uploaded."}
        ), 409
    try:
        started = start_original_video_upload(video_id)
    except PermissionError as error:
        return jsonify({"error": str(error)}), 401
    except ValueError as error:
        return jsonify({"error": str(error)}), 409
    except RuntimeError as error:
        return jsonify({"error": str(error)}), 503
    return jsonify({"status": "uploading", "direct_upload": started}), 202


@pipeline.post("/api/pipeline/videos/<video_id>/retry-clip")
def retry_clip_generation(video_id: str):
    video = store.get_video(video_id)
    if (
        not video
        or video["status"] != "failed"
        or not video["rights_confirmed"]
        or not video["source_path"]
        or not Path(video["source_path"]).is_file()
        or video["clip_path"]
    ):
        return jsonify(
            {"error": "Only a failed clip with an available, authorized source can be retried."}
        ), 409
    if (
        not store.reserve_daily_clip(
            video_id, int(store.get_setting("clips_per_day"))
        )
        and not store.daily_clip_reserved_today(video_id)
    ):
        store.update_video(
            video_id,
            status="source_uploaded",
            error="Today's clip target is full. This retry is queued for the next day.",
        )
        return jsonify(
            {"status": "source_uploaded", "queued_for_tomorrow": True}
        ), 202
    store.update_video(video_id, status="source_uploaded", error="")
    future = pipeline_executor.submit(process_uploaded_source, video_id)
    future.add_done_callback(
        lambda task: logger.error(
            "Source retry task failed to run for %s: %s", video_id, task.exception()
        )
        if task.exception()
        else None
    )
    return jsonify({"status": "source_uploaded", "queued_for_tomorrow": False}), 202


@pipeline.get("/api/pipeline/videos/<video_id>/source")
def preview_source(video_id: str):
    video = store.get_video(video_id)
    if not video or not video["source_path"] or not Path(video["source_path"]).is_file():
        abort(404)
    return send_file(video["source_path"], mimetype="video/mp4", conditional=True)


@pipeline.get("/api/pipeline/videos/<video_id>/captions")
def download_source_captions(video_id: str):
    video = store.get_video(video_id)
    if (
        not video
        or not video["source_caption_path"]
        or not Path(video["source_caption_path"]).is_file()
    ):
        abort(404)
    filename = secure_filename(video["title"][:80]) or "source-video"
    return send_file(
        video["source_caption_path"],
        mimetype="application/x-subrip",
        as_attachment=True,
        download_name=f"{filename}.srt",
        conditional=True,
    )


@pipeline.get("/api/pipeline/videos/<video_id>/preview")
def preview_pipeline_clip(video_id: str):
    video = store.get_video(video_id)
    if not video or not video["clip_path"] or not Path(video["clip_path"]).is_file():
        abort(404)
    return send_file(video["clip_path"], mimetype="video/mp4", conditional=True)


@pipeline.post("/api/pipeline/videos/<video_id>/approve")
def approve_clip(video_id: str):
    data = request.get_json(silent=True) or {}
    video = store.get_video(video_id)
    if not video:
        return jsonify({"error": "This clip is no longer in your review queue."}), 404
    if data.get("approved") is not True:
        if video["status"] == "published":
            return jsonify({"error": "Published videos cannot be rejected."}), 409
        if video["status"] not in {"pending_approval", "approved"}:
            return jsonify({"error": "Only a finished clip can be rejected."}), 409
        try:
            store.update_video(video_id, status="rejected")
        except KeyError:
            return jsonify({"error": "This clip is no longer in your review queue."}), 404
        return jsonify({"status": "rejected"})
    try:
        slot = schedule_greenlit_video(video_id)
    except KeyError as error:
        return jsonify({"error": str(error)}), 404
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    return jsonify({"status": "approved", **slot, "quota": store.QuotaBudget().snapshot()})


@pipeline.post("/api/pipeline/videos/<video_id>/retry-upload")
def retry_failed_upload(video_id: str):
    video = store.get_video(video_id)
    if not video or video["status"] != "upload_failed":
        return jsonify({"error": "This clip does not have a failed upload to retry."}), 409
    if video.get("upload_mode") == "direct":
        try:
            start_original_video_upload(video_id)
        except PermissionError as error:
            return jsonify({"error": str(error)}), 401
        except ValueError as error:
            return jsonify({"error": str(error)}), 409
        except RuntimeError as error:
            return jsonify({"error": str(error)}), 503
        return jsonify({"status": "uploading", "direct_upload": True}), 202
    try:
        slot = schedule_greenlit_video(video_id)
    except (KeyError, ValueError) as error:
        return jsonify({"error": str(error)}), 400
    return jsonify({"status": "approved", **slot, "quota": store.QuotaBudget().snapshot()})


@pipeline.post("/api/engagement/sync")
def sync_engagement():
    try:
        result = engagement.sync_new_comments()
    except QuotaExceeded as error:
        return jsonify({"error": str(error), "quota": store.QuotaBudget().snapshot()}), 429
    except PermissionError as error:
        return jsonify({"error": str(error)}), 401
    except Exception as error:
        logger.exception("Comment sync failed.")
        return jsonify({"error": f"Could not load comments: {error}"}), 502
    return jsonify(result)


@pipeline.post("/api/engagement/<comment_id>/draft")
def create_reply_draft(comment_id: str):
    try:
        reply = engagement.draft_reply(comment_id)
    except KeyError as error:
        return jsonify({"error": str(error)}), 404
    except (ConnectionError, RuntimeError, ValueError) as error:
        return jsonify({"error": str(error)}), 503
    return jsonify({"reply": reply, "comment_id": comment_id})


@pipeline.post("/api/engagement/<comment_id>/reply")
def reply_to_comment(comment_id: str):
    data = request.get_json(silent=True) or {}
    reply = data.get("reply")
    if not isinstance(reply, str):
        return jsonify({"error": "Review and enter a reply before sending."}), 400
    try:
        result = engagement.send_approved_reply(comment_id, reply)
    except QuotaExceeded as error:
        return jsonify({"error": str(error), "quota": store.QuotaBudget().snapshot()}), 429
    except PermissionError as error:
        return jsonify({"error": str(error)}), 401
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except Exception as error:
        logger.exception("Reply sending failed.")
        return jsonify({"error": f"YouTube could not send the approved reply: {error}"}), 502
    return jsonify(result)


@pipeline.post("/api/engagement/videos/<video_id>/like")
def like_published_clip(video_id: str):
    try:
        result = engagement.like_own_video(video_id)
    except QuotaExceeded as error:
        return jsonify({"error": str(error), "quota": store.QuotaBudget().snapshot()}), 429
    except PermissionError as error:
        return jsonify({"error": str(error)}), 401
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except Exception as error:
        logger.exception("YouTube rating failed.")
        return jsonify({"error": f"YouTube could not rate this clip: {error}"}), 502
    return jsonify(result)


def start_pipeline_scheduler() -> threading.Thread:
    stop = threading.Event()
    store.requeue_interrupted_source_jobs()
    store.mark_interrupted_uploads_for_review()

    def run() -> None:
        while not stop.is_set():
            try:
                run_scheduler_once()
            except Exception:
                logger.exception("Automation scheduler cycle failed.")
            stop.wait(30)

    scheduler = threading.Thread(target=run, name="clipship-daily-scheduler", daemon=True)
    scheduler.start()
    return scheduler
