from __future__ import annotations

import json
import logging
import os
import sqlite3
import time as system_time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


APP_DIR = Path(__file__).resolve().parent
APP_DATA_DIR = Path(os.environ.get("CLIPSHIP_DATA_DIR", APP_DIR)).resolve()
DB_PATH = APP_DATA_DIR / "clipship.db"
UPLOAD_RESERVE = 8_000
ENGAGEMENT_RESERVE = 1_000
TREND_RESERVE = 1_000
PRIORITY_COSTS = {
    "videos.list": 1,
    "videoCategories.list": 1,
    "commentThreads.list": 1,
    "comments.insert": 50,
    "videos.rate": 50,
    "videos.insert": 1_600,
    "search.list": 100,
}
PRIORITY_BUCKETS = {"uploads": "upload", "engagement": "engagement", "trends": "trends"}
DEFAULT_SETTINGS = {
    "timezone": "Asia/Kolkata",
    "quota_cap": "10000",
    "region": "US",
    "category_id": "20",
    "trend_language": "en",
    "india_region": "all",
    "schedule_time": "09:00",
    "clips_per_day": "10",
    "auto_scout": "true",
    "upload_privacy": "public",
}
logger = logging.getLogger(__name__)


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exception_type, exception_value, traceback):
        try:
            return super().__exit__(exception_type, exception_value, traceback)
        finally:
            self.close()


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(
        DB_PATH,
        timeout=30,
        isolation_level=None,
        factory=ClosingConnection,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize() -> None:
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS quota_reservations (
                id TEXT PRIMARY KEY,
                local_day TEXT NOT NULL,
                priority TEXT NOT NULL,
                operation TEXT NOT NULL,
                cost INTEGER NOT NULL,
                task_id TEXT,
                created_at TEXT NOT NULL,
                result TEXT NOT NULL DEFAULT 'reserved'
            );
            CREATE INDEX IF NOT EXISTS quota_reservations_day_idx
                ON quota_reservations(local_day, priority);
            CREATE TABLE IF NOT EXISTS quota_warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                local_day TEXT NOT NULL,
                operation TEXT NOT NULL,
                priority TEXT NOT NULL,
                cost INTEGER NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS api_task_queue (
                task_id TEXT PRIMARY KEY,
                operation TEXT NOT NULL,
                priority TEXT NOT NULL,
                payload TEXT NOT NULL,
                scheduled_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS api_task_queue_schedule_idx
                ON api_task_queue(status, scheduled_at);
            CREATE TABLE IF NOT EXISTS videos (
                video_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                channel_title TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                thumbnail TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL,
                category_id TEXT NOT NULL DEFAULT '',
                duration_seconds REAL NOT NULL DEFAULT 0,
                view_count INTEGER NOT NULL DEFAULT 0,
                rights_confirmed INTEGER NOT NULL DEFAULT 0,
                upload_privacy TEXT NOT NULL DEFAULT '',
                source_path TEXT NOT NULL DEFAULT '',
                clip_path TEXT NOT NULL DEFAULT '',
                caption_text TEXT NOT NULL DEFAULT '',
                start_seconds REAL NOT NULL DEFAULT 0,
                metadata TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'trending',
                scheduled_at TEXT NOT NULL DEFAULT '',
                uploaded_video_id TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trend_search_cache (
                cache_key TEXT PRIMARY KEY,
                results TEXT NOT NULL,
                fetched_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS videos_status_schedule_idx
                ON videos(status, scheduled_at);
            CREATE TABLE IF NOT EXISTS engagement (
                comment_id TEXT PRIMARY KEY,
                video_id TEXT NOT NULL,
                author TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL,
                published_at TEXT NOT NULL DEFAULT '',
                ai_reply TEXT NOT NULL DEFAULT '',
                reply_status TEXT NOT NULL DEFAULT 'draft',
                liked INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS engagement_video_idx
                ON engagement(video_id, created_at);
            CREATE TABLE IF NOT EXISTS video_likes (
                video_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS daily_clip_runs (
                local_day TEXT PRIMARY KEY,
                generated_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS daily_clip_video_runs (
                local_day TEXT NOT NULL,
                video_id TEXT NOT NULL,
                PRIMARY KEY(local_day, video_id)
            );
            """
        )
        video_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(videos)").fetchall()
        }
        if "start_seconds" not in video_columns:
            db.execute(
                "ALTER TABLE videos ADD COLUMN start_seconds REAL NOT NULL DEFAULT 0"
            )
        if "upload_privacy" not in video_columns:
            db.execute(
                "ALTER TABLE videos ADD COLUMN upload_privacy TEXT NOT NULL DEFAULT ''"
            )
        for key, value in DEFAULT_SETTINGS.items():
            db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (key, value))


def get_setting(key: str) -> str:
    with connect() as db:
        row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        raise KeyError(f"Unknown setting: {key}")
    return row["value"]


def get_settings() -> dict[str, str]:
    with connect() as db:
        rows = db.execute("SELECT key, value FROM settings").fetchall()
    return {row["key"]: row["value"] for row in rows}


def set_settings(values: dict[str, str]) -> None:
    with connect() as db:
        db.executemany(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            list(values.items()),
        )


def local_now() -> datetime:
    try:
        zone = ZoneInfo(get_setting("timezone"))
    except ZoneInfoNotFoundError as error:
        raise ValueError("The configured timezone is not a valid IANA timezone.") from error
    return datetime.now(zone)


def local_day() -> date:
    return local_now().date()


def upload_daily_limit(quota_cap: int | None = None) -> int:
    cap = quota_cap if quota_cap is not None else int(get_setting("quota_cap"))
    return max(0, (cap - ENGAGEMENT_RESERVE - TREND_RESERVE) // PRIORITY_COSTS["videos.insert"])


def priority_limit(priority: str, quota_cap: int) -> int:
    if priority == "uploads":
        return max(0, quota_cap - ENGAGEMENT_RESERVE - TREND_RESERVE)
    if priority == "engagement":
        return ENGAGEMENT_RESERVE
    if priority == "trends":
        return TREND_RESERVE
    raise ValueError("Quota priority must be uploads, engagement, or trends.")


@dataclass(frozen=True)
class QuotaReservation:
    reservation_id: str
    cost: int
    local_day: str


class QuotaExceeded(Exception):
    def __init__(self, message: str, *, remaining: int, local_day: str):
        super().__init__(message)
        self.remaining = remaining
        self.local_day = local_day


class QuotaBudget:
    def check(
        self,
        cost: int,
        priority: str,
        operation: str,
        task_id: str | None = None,
    ) -> QuotaReservation:
        if priority not in PRIORITY_BUCKETS:
            raise ValueError("Quota priority must be uploads, engagement, or trends.")
        if isinstance(cost, bool) or not isinstance(cost, int) or cost <= 0:
            raise ValueError("Quota cost must be a positive integer.")
        expected = PRIORITY_COSTS.get(operation)
        if expected is None or expected != cost:
            raise ValueError(f"Unexpected quota cost for {operation}.")

        now = local_now()
        today = now.date().isoformat()
        cap = int(get_setting("quota_cap"))
        reserve = priority_limit(priority, cap)
        reservation_id = uuid.uuid4().hex
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT COALESCE(SUM(cost), 0) AS used FROM quota_reservations WHERE local_day = ?",
                (today,),
            ).fetchone()
            total_used = int(row["used"])
            row = db.execute(
                "SELECT COALESCE(SUM(cost), 0) AS used FROM quota_reservations "
                "WHERE local_day = ? AND priority = ?",
                (today, priority),
            ).fetchone()
            priority_used = int(row["used"])
            remaining = max(0, cap - total_used)
            tier_remaining = max(0, reserve - priority_used)
            if cost > remaining or cost > tier_remaining:
                message = (
                    f"Quota limit reached for the {priority} tier. "
                    f"{tier_remaining} {priority}-tier units and {remaining} total units remain; "
                    f"{operation} requires {cost}."
                )
                db.execute(
                    "INSERT INTO quota_warnings(local_day, operation, priority, cost, message, created_at) "
                    "VALUES(?, ?, ?, ?, ?, ?)",
                    (today, operation, priority, cost, message, now.isoformat()),
                )
                if task_id:
                    retry_at = datetime.combine(
                        now.date() + timedelta(days=1),
                        time.fromisoformat(get_setting("schedule_time")),
                        tzinfo=ZoneInfo(get_setting("timezone")),
                    ).isoformat()
                    try:
                        db.execute(
                            "UPDATE videos SET scheduled_at = ?, updated_at = ? "
                            "WHERE video_id = ? AND status IN ('approved', 'queued')",
                            (
                                retry_at,
                                now.isoformat(),
                                task_id,
                            ),
                        )
                        db.execute(
                            "UPDATE api_task_queue SET status='queued', scheduled_at=?, "
                            "error=?, updated_at=? WHERE task_id=?",
                            (retry_at, message, now.isoformat(), task_id),
                        )
                    except sqlite3.Error:
                        logger.exception("Could not defer quota-limited task %s", task_id)
                message += " This action has been queued for the next scheduled day."
                db.commit()
                logger.warning("%s", message)
                raise QuotaExceeded(message, remaining=remaining, local_day=today)
            db.execute(
                "INSERT INTO quota_reservations"
                "(id, local_day, priority, operation, cost, task_id, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    reservation_id,
                    today,
                    priority,
                    operation,
                    cost,
                    task_id,
                    now.isoformat(),
                ),
            )
            db.commit()
        return QuotaReservation(reservation_id, cost, today)

    @staticmethod
    def finish(reservation: QuotaReservation, result: str) -> None:
        if result not in {"succeeded", "failed"}:
            raise ValueError("A quota result must be succeeded or failed.")
        with connect() as db:
            db.execute(
                "UPDATE quota_reservations SET result = ? WHERE id = ?",
                (result, reservation.reservation_id),
            )

    def snapshot(self) -> dict:
        now = local_now()
        today = now.date().isoformat()
        settings = get_settings()
        cap = int(settings["quota_cap"])
        daily_limit = upload_daily_limit(cap)
        with connect() as db:
            usage = db.execute(
                "SELECT priority, SUM(cost) AS used FROM quota_reservations "
                "WHERE local_day = ? GROUP BY priority",
                (today,),
            ).fetchall()
            queued_api_tasks = db.execute(
                "SELECT COUNT(*) AS tasks FROM api_task_queue "
                "WHERE status = 'queued' AND substr(scheduled_at, 1, 10) = ?",
                (today,),
            ).fetchone()["tasks"]
            queued_api_units = sum(
                PRIORITY_COSTS[row["operation"]]
                for row in db.execute(
                    "SELECT operation FROM api_task_queue "
                    "WHERE status = 'queued' AND substr(scheduled_at, 1, 10) = ?",
                    (today,),
                ).fetchall()
                if row["operation"] in PRIORITY_COSTS
            )
            projected = db.execute(
                "SELECT COUNT(*) AS tasks FROM videos "
                "WHERE status IN ('approved', 'queued') AND scheduled_at != '' "
                "AND substr(scheduled_at, 1, 10) = ?",
                (today,),
            ).fetchone()["tasks"]
            scheduled_uploads = db.execute(
                "SELECT COUNT(*) AS tasks FROM videos "
                "WHERE status IN ('approved', 'queued') AND scheduled_at != '' "
                "AND substr(scheduled_at, 1, 10) = ?",
                (today,),
            ).fetchone()["tasks"]
            queued = db.execute(
                "SELECT COUNT(*) AS tasks FROM videos WHERE status IN "
                "('trending', 'source_uploaded', 'processing', 'pending_approval')"
            ).fetchone()["tasks"]
            warnings = db.execute(
                "SELECT message, created_at FROM quota_warnings WHERE local_day = ? "
                "ORDER BY id DESC LIMIT 10",
                (today,),
            ).fetchall()
        used_by_tier = {row["priority"]: int(row["used"]) for row in usage}
        total_used = sum(used_by_tier.values())
        projected_cost = int(projected) * PRIORITY_COSTS["videos.insert"] + queued_api_units
        upload_remaining = max(0, priority_limit("uploads", cap) - used_by_tier.get("uploads", 0))
        estimated_uploads_remaining = min(
            upload_remaining // PRIORITY_COSTS["videos.insert"],
            max(0, cap - total_used) // PRIORITY_COSTS["videos.insert"],
        )
        estimated_uploads_remaining = max(
            0, estimated_uploads_remaining - int(scheduled_uploads)
        )
        schedule_start = datetime.combine(
            now.date(),
            time.fromisoformat(settings["schedule_time"]),
            tzinfo=ZoneInfo(settings["timezone"]),
        )
        if schedule_start <= now:
            schedule_start += timedelta(days=1)
        slots_already_used = (
            used_by_tier.get("uploads", 0) // PRIORITY_COSTS["videos.insert"]
            + int(scheduled_uploads)
        )
        estimated_exhaustion = schedule_start + timedelta(
            minutes=30
            * max(0, slots_already_used + estimated_uploads_remaining - 1)
        )
        next_upload_time = schedule_start
        if not estimated_uploads_remaining and schedule_start.date() == now.date():
            next_upload_time += timedelta(days=1)
        return {
            "date": today,
            "timezone": settings["timezone"],
            "daily_cap": cap,
            "used": total_used,
            "remaining": max(0, cap - total_used),
            "upload_reserve": priority_limit("uploads", cap),
            "engagement_reserve": ENGAGEMENT_RESERVE,
            "trends_reserve": TREND_RESERVE,
            "used_by_tier": used_by_tier,
            "daily_upload_limit": daily_limit,
            "uploads_remaining": estimated_uploads_remaining,
            "projected_queued_units_today": projected_cost,
            "projected_usage_today": total_used + projected_cost,
            "queued_pipeline_tasks": int(queued) + int(queued_api_tasks),
            "estimated_run_out": (
                f"Reserved upload allowance projects full by "
                f"{estimated_exhaustion.strftime('%Y-%m-%d %H:%M %Z')}"
                if estimated_uploads_remaining > 0
                else "Today's upload allowance is reserved; approved extras are scheduled "
                f"for the next available day at {next_upload_time.strftime('%Y-%m-%d %H:%M %Z')}."
            ),
            "warnings": [dict(row) for row in warnings],
        }


def queue_api_task(
    task_id: str,
    operation: str,
    priority: str,
    payload: dict,
    *,
    status: str = "running",
    scheduled_at: str | None = None,
    error: str = "",
) -> None:
    now = local_now()
    scheduled = scheduled_at or now.isoformat()
    with connect() as db:
        db.execute(
            "INSERT INTO api_task_queue(task_id, operation, priority, payload, scheduled_at, "
            "status, error, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET scheduled_at=excluded.scheduled_at, "
            "status=excluded.status, error=excluded.error, updated_at=excluded.updated_at",
            (
                task_id,
                operation,
                priority,
                json.dumps(payload),
                scheduled,
                status,
                error,
                now.isoformat(),
                now.isoformat(),
            ),
        )


def finish_api_task(task_id: str, status: str, error: str = "") -> None:
    if status not in {"completed", "failed", "queued"}:
        raise ValueError("Invalid API task status.")
    with connect() as db:
        db.execute(
            "UPDATE api_task_queue SET status=?, error=?, updated_at=? WHERE task_id=?",
            (status, error[:1000], local_now().isoformat(), task_id),
        )


def due_api_tasks(limit: int = 25) -> list[dict]:
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM api_task_queue WHERE status = 'queued' AND scheduled_at <= ? "
            "ORDER BY scheduled_at LIMIT ?",
            (local_now().isoformat(), limit),
        ).fetchall()
    tasks = []
    for row in rows:
        task = dict(row)
        task["payload"] = json.loads(task["payload"])
        tasks.append(task)
    return tasks


def add_video(video: dict) -> None:
    now = local_now().isoformat()
    with connect() as db:
        db.execute(
            "INSERT INTO videos(video_id, title, channel_title, description, thumbnail, url, "
            "category_id, duration_seconds, view_count, metadata, status, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'trending', ?, ?) "
            "ON CONFLICT(video_id) DO UPDATE SET title=excluded.title, "
            "channel_title=excluded.channel_title, description=excluded.description, "
            "thumbnail=excluded.thumbnail, url=excluded.url, category_id=excluded.category_id, "
            "duration_seconds=excluded.duration_seconds, view_count=excluded.view_count, "
            "metadata=excluded.metadata, updated_at=excluded.updated_at "
            "WHERE videos.status IN ('trending', 'source_uploaded', 'processing')",
            (
                video["video_id"],
                video["title"],
                video.get("channel_title", ""),
                video.get("description", ""),
                video.get("thumbnail", ""),
                video["url"],
                str(video.get("category_id", "")),
                float(video.get("duration_seconds") or 0),
                int(video.get("view_count") or 0),
                video.get("metadata", "{}"),
                now,
                now,
            ),
        )


def get_video(video_id: str) -> dict | None:
    with connect() as db:
        row = db.execute("SELECT * FROM videos WHERE video_id = ?", (video_id,)).fetchone()
    return dict(row) if row else None


def list_videos(limit: int = 200) -> list[dict]:
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM videos ORDER BY CASE status "
            "WHEN 'pending_approval' THEN 0 WHEN 'trending' THEN 1 "
            "WHEN 'source_uploaded' THEN 2 WHEN 'approved' THEN 3 ELSE 4 END, "
            "updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_trend_search_cache(cache_key: str, max_age_seconds: int) -> list[dict] | None:
    with connect() as db:
        row = db.execute(
            "SELECT results, fetched_at FROM trend_search_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if row is None:
            return None
        if system_time.time() - row["fetched_at"] >= max_age_seconds:
            db.execute("DELETE FROM trend_search_cache WHERE cache_key = ?", (cache_key,))
            return None
    results = json.loads(row["results"])
    if not isinstance(results, list) or any(not isinstance(item, dict) for item in results):
        raise ValueError("Stored trend search cache has an invalid format.")
    return results


def set_trend_search_cache(cache_key: str, results: list[dict]) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO trend_search_cache(cache_key, results, fetched_at) VALUES(?, ?, ?) "
            "ON CONFLICT(cache_key) DO UPDATE SET results=excluded.results, "
            "fetched_at=excluded.fetched_at",
            (cache_key, json.dumps(results), system_time.time()),
        )


def update_video(video_id: str, **changes) -> None:
    allowed = {
        "rights_confirmed",
        "upload_privacy",
        "source_path",
        "clip_path",
        "caption_text",
        "start_seconds",
        "status",
        "scheduled_at",
        "uploaded_video_id",
        "error",
    }
    if not changes or set(changes) - allowed:
        raise ValueError("Unsupported video update.")
    changes["updated_at"] = local_now().isoformat()
    query = ", ".join(f"{name} = ?" for name in changes)
    with connect() as db:
        cursor = db.execute(
            f"UPDATE videos SET {query} WHERE video_id = ?",
            (*changes.values(), video_id),
        )
        if cursor.rowcount != 1:
            raise KeyError(video_id)


def claim_source_for_processing(video_id: str | None = None) -> dict | None:
    now = local_now().isoformat()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if video_id:
            row = db.execute(
                "SELECT * FROM videos WHERE video_id = ? AND status = 'source_uploaded'",
                (video_id,),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT * FROM videos WHERE status = 'source_uploaded' "
                "ORDER BY created_at LIMIT 1"
            ).fetchone()
        if not row:
            db.rollback()
            return None
        db.execute(
            "UPDATE videos SET status = 'processing', error = '', updated_at = ? "
            "WHERE video_id = ? AND status = 'source_uploaded'",
            (now, row["video_id"]),
        )
        db.commit()
    result = dict(row)
    result["status"] = "processing"
    return result


def reserve_daily_clip(video_id: str, target: int) -> bool:
    today = local_day().isoformat()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        already_reserved = db.execute(
            "SELECT 1 FROM daily_clip_video_runs WHERE local_day = ? AND video_id = ?",
            (today, video_id),
        ).fetchone()
        if already_reserved:
            db.rollback()
            return False
        db.execute(
            "INSERT OR IGNORE INTO daily_clip_runs(local_day, generated_count) VALUES(?, 0)",
            (today,),
        )
        count = db.execute(
            "SELECT generated_count FROM daily_clip_runs WHERE local_day = ?", (today,)
        ).fetchone()["generated_count"]
        if count >= target:
            db.rollback()
            return False
        db.execute(
            "INSERT INTO daily_clip_video_runs(local_day, video_id) VALUES(?, ?)",
            (today, video_id),
        )
        db.execute(
            "UPDATE daily_clip_runs SET generated_count = generated_count + 1 WHERE local_day = ?",
            (today,),
        )
        db.commit()
        return True


def create_engagement_draft(item: dict) -> None:
    now = local_now().isoformat()
    with connect() as db:
        db.execute(
            "INSERT INTO engagement(comment_id, video_id, author, text, published_at, "
            "created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(comment_id) DO NOTHING",
            (
                item["comment_id"],
                item["video_id"],
                item.get("author", ""),
                item["text"],
                item.get("published_at", ""),
                now,
                now,
            ),
        )


def list_engagement(limit: int = 100) -> list[dict]:
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM engagement ORDER BY published_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


def update_engagement(comment_id: str, **changes) -> None:
    allowed = {"ai_reply", "reply_status", "liked"}
    if not changes or set(changes) - allowed:
        raise ValueError("Unsupported engagement update.")
    changes["updated_at"] = local_now().isoformat()
    query = ", ".join(f"{name} = ?" for name in changes)
    with connect() as db:
        cursor = db.execute(
            f"UPDATE engagement SET {query} WHERE comment_id = ?",
            (*changes.values(), comment_id),
        )
        if cursor.rowcount != 1:
            raise KeyError(comment_id)


def claim_reply(comment_id: str) -> dict | None:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT e.*, v.uploaded_video_id, v.status AS video_status FROM engagement e "
            "JOIN videos v ON v.uploaded_video_id = e.video_id "
            "WHERE e.comment_id = ?",
            (comment_id,),
        ).fetchone()
        if (
            not row
            or row["video_status"] != "published"
            or row["reply_status"] not in {"draft", "queued"}
        ):
            db.rollback()
            return None
        db.execute(
            "UPDATE engagement SET reply_status = 'sending', updated_at = ? "
            "WHERE comment_id = ? AND reply_status IN ('draft', 'queued')",
            (local_now().isoformat(), comment_id),
        )
        db.commit()
    return dict(row)


def record_video_like(video_id: str) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO video_likes(video_id, created_at) VALUES(?, ?) "
            "ON CONFLICT(video_id) DO NOTHING",
            (video_id, local_now().isoformat()),
        )


def is_video_liked(video_id: str) -> bool:
    with connect() as db:
        row = db.execute("SELECT 1 FROM video_likes WHERE video_id = ?", (video_id,)).fetchone()
    return row is not None


def initialize_task_database() -> None:
    initialize()
