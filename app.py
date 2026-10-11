from __future__ import annotations

import html
import math
import os
import re
import subprocess
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import imageio_ffmpeg
import yt_dlp
from flask import Flask, jsonify, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

import captioning
from quota_budget import APP_DATA_DIR


OUTPUT_DIR = APP_DATA_DIR / "clips"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.json.ensure_ascii = False
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024 + 512 * 1024
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
executor = ThreadPoolExecutor(max_workers=2)


@app.before_request
def reject_cross_origin_state_changes():
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return None
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if not origin:
        return None
    parsed = urlparse(origin)
    if (
        parsed.scheme != request.scheme
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.netloc.lower() != request.host.lower()
    ):
        return jsonify({"error": "Cross-origin requests cannot change local Clipship state."}), 403
    return None


VTT_TIME = re.compile(
    r"((?:\d{2}:)?\d{2}:\d{2}[.,]\d{3})\s+-->\s+"
    r"((?:\d{2}:)?\d{2}:\d{2}[.,]\d{3})"
)
TAG_RE = re.compile(r"<[^>]+>")
HIGHLIGHT_CUES = {
    "actually", "amazing", "because", "best", "breakthrough", "crazy", "discover",
    "discovered", "easy", "first", "finally", "important", "incredible", "mistake",
    "never", "secret", "surprise", "surprising", "tip", "trick", "unexpected",
    "unbelievable", "wrong",
}


def validate_youtube_url(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Enter a YouTube video link.")
    url = value.strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    allowed_hosts = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
    if parsed.scheme != "https" or host not in allowed_hosts or not parsed.path.strip("/"):
        raise ValueError("Enter a valid HTTPS link from youtube.com or youtu.be.")
    if parsed.username or parsed.password:
        raise ValueError("YouTube links containing sign-in details are not supported.")
    return url


def parse_timestamp(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    seconds, millis = parts[-1].split(".")
    if len(parts) == 2:
        hours = 0
        minutes, seconds = parts[0], seconds
    else:
        hours, minutes = parts[0], parts[1]
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000


def parse_vtt(content: str) -> list[dict]:
    cues: list[dict] = []
    lines = content.lstrip("\ufeff").splitlines()
    index = 0
    while index < len(lines):
        match = VTT_TIME.search(lines[index])
        index += 1
        if not match:
            continue
        start = parse_timestamp(match.group(1))
        end = parse_timestamp(match.group(2))
        text_lines = []
        while index < len(lines) and lines[index].strip():
            line = lines[index].strip()
            if not line.startswith(("NOTE", "STYLE", "REGION")):
                text_lines.append(line)
            index += 1
        text = html.unescape(TAG_RE.sub("", " ".join(text_lines)))
        text = re.sub(r"\s+", " ", text).strip()
        if text and end > start and (not cues or text != cues[-1]["text"]):
            cues.append({"start": start, "end": end, "text": text})
    return cues


def find_highlights(cues: list[dict], duration: float, limit: int = 5) -> list[dict]:
    if not cues or duration <= 0:
        return []

    cues = sorted(cues, key=lambda cue: cue["start"])
    window = min(35.0, max(18.0, duration / 2))
    step = max(6.0, window / 3)
    starts = [0.0]
    while starts[-1] + window < duration:
        starts.append(starts[-1] + step)
    starts = sorted(set(min(start, max(0.0, duration - window)) for start in starts))

    candidates = []
    first_cue = 0
    for start in starts:
        end = min(duration, start + window)
        while first_cue < len(cues) and cues[first_cue]["end"] <= start:
            first_cue += 1
        cue_index = first_cue
        lines = []
        while cue_index < len(cues) and cues[cue_index]["start"] < end:
            if cues[cue_index]["end"] > start:
                lines.append(cues[cue_index]["text"])
            cue_index += 1
        text = " ".join(lines).strip()
        words = re.findall(r"\b[\w'-]+\b", text.lower())
        if len(words) < 8:
            continue
        cue_hits = sum(word in HIGHLIGHT_CUES for word in words)
        punctuation = min(4, text.count("?") + text.count("!"))
        numbers = min(3, len(re.findall(r"\b\d+(?:\.\d+)?\b", text)))
        density = min(30, len(words) * 0.55)
        score = min(99, round(38 + density + cue_hits * 4 + punctuation * 2 + numbers))
        candidates.append(
            {
                "start": round(start, 2),
                "end": round(end, 2),
                "duration": round(end - start, 1),
                "text": text,
                "score": score,
            }
        )

    selected = []
    for candidate in sorted(candidates, key=lambda item: item["score"], reverse=True):
        if any(
            candidate["start"] < item["end"] and candidate["end"] > item["start"]
            for item in selected
        ):
            continue
        selected.append(candidate)
        if len(selected) >= limit:
            break
    return sorted(selected, key=lambda item: item["start"])


def update_job(job_id: str, **changes) -> None:
    with jobs_lock:
        jobs[job_id].update(changes)


def set_job_error(job_id: str, error: Exception) -> None:
    update_job(job_id, status="error", error=str(error))


def make_ytdlp_options(directory: Path, **extra) -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ignoreerrors": False,
        "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(),
        "outtmpl": str(directory / "%(id)s.%(ext)s"),
        **extra,
    }


def get_video_info(url: str) -> dict:
    with yt_dlp.YoutubeDL(make_ytdlp_options(Path(tempfile.gettempdir()))) as downloader:
        info = downloader.extract_info(url, download=False)
    if not info:
        raise RuntimeError("YouTube did not return video information.")
    return info


def caption_language(info: dict) -> str | None:
    languages = list(info.get("subtitles", {}).keys())
    automatic = list(info.get("automatic_captions", {}).keys())
    available = languages + [language for language in automatic if language not in languages]
    if not available:
        return None
    return next((language for language in available if language.lower().startswith("en")), available[0])


def get_caption_cues(url: str, language: str, directory: Path) -> list[dict]:
    options = make_ytdlp_options(
        directory,
        skip_download=True,
        writesubtitles=True,
        writeautomaticsub=True,
        subtitleslangs=[language],
        subtitlesformat="vtt",
    )
    with yt_dlp.YoutubeDL(options) as downloader:
        downloader.download([url])
    caption_files = list(directory.glob("*.vtt"))
    if not caption_files:
        raise RuntimeError(
            f"YouTube listed {language} captions but did not provide a readable VTT caption file."
        )
    cues = parse_vtt(caption_files[0].read_text(encoding="utf-8"))
    if not cues:
        raise RuntimeError("The downloaded caption track did not contain usable timed text.")
    return cues


def transcribe_audio(url: str, directory: Path) -> list[dict]:
    options = make_ytdlp_options(
        directory,
        format="bestaudio/best",
        postprocessors=[
            {"key": "FFmpegExtractAudio", "preferredcodec": "wav", "preferredquality": "0"}
        ],
    )
    with yt_dlp.YoutubeDL(options) as downloader:
        downloader.download([url])
    audio_files = list(directory.glob("*.wav"))
    if not audio_files:
        raise RuntimeError("The audio download finished without producing a WAV file.")
    return captioning.transcribe_audio_file(audio_files[0])


def analyze_video(job_id: str, url: str, mode: str, has_rights: bool) -> None:
    try:
        with tempfile.TemporaryDirectory(prefix="clipship-analysis-") as temp_dir:
            directory = Path(temp_dir)
            update_job(job_id, status="working", message="Checking video details…")
            info = get_video_info(url)
            duration = float(info.get("duration") or 0)
            if duration <= 0:
                raise RuntimeError("Could not determine the video duration.")
            if duration > 12 * 60 * 60:
                raise RuntimeError("Videos longer than 12 hours are not supported.")

            if mode == "manual":
                highlights = []
                source = "Manual time selection"
            elif mode == "ai":
                update_job(job_id, message="Transcribing audio with the local AI model…")
                cues = transcribe_audio(url, directory)
                source = "Local AI transcription"
            else:
                language = caption_language(info)
                if not language:
                    raise RuntimeError(
                        "This video has no available caption tracks. Try the local AI option instead."
                    )
                update_job(job_id, message=f"Reading {language} captions…")
                cues = get_caption_cues(url, language, directory)
                source = f"YouTube captions ({language})"

            if mode != "manual":
                highlights = find_highlights(cues, duration)
                if not highlights:
                    raise RuntimeError(
                        "There was not enough transcript text to suggest clips. Try the other analysis option."
                    )
            video = {
                "id": info.get("id"),
                "title": info.get("title") or "Untitled video",
                "duration": duration,
                "thumbnail": info.get("thumbnail"),
                "source": source,
                "mode": mode,
                "highlights": highlights,
                "has_rights": has_rights,
            }
            update_job(job_id, status="complete", message="", video=video)
    except Exception as error:
        set_job_error(job_id, error)


def find_downloaded_video(directory: Path) -> Path:
    candidates = [
        path
        for path in directory.iterdir()
        if path.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
        and path.is_file()
    ]
    if not candidates:
        raise RuntimeError("The video download completed without a supported video file.")
    return max(candidates, key=lambda path: path.stat().st_size)


def create_clips(job_id: str, source_job_id: str, segments: list[dict]) -> None:
    created_paths = []
    try:
        with jobs_lock:
            video = jobs[source_job_id]["video"]
        update_job(job_id, status="working", message="Downloading the authorized source video…")
        with tempfile.TemporaryDirectory(prefix="clipship-render-") as temp_dir:
            directory = Path(temp_dir)
            options = make_ytdlp_options(
                directory,
                format="bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                merge_output_format="mp4",
            )
            with yt_dlp.YoutubeDL(options) as downloader:
                downloader.download([jobs[source_job_id]["url"]])
            source = find_downloaded_video(directory)
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            created = []
            total = len(segments)
            for position, segment in enumerate(segments, start=1):
                title = secure_filename(video["title"])[:48] or "youtube-video"
                filename = f"{title}-{int(segment['start'])}-{uuid.uuid4().hex[:8]}.mp4"
                output = OUTPUT_DIR / filename
                update_job(
                    job_id,
                    message=f"Rendering clip {position} of {total}…",
                )
                result = subprocess.run(
                    [
                        ffmpeg,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-ss",
                        str(segment["start"]),
                        "-i",
                        str(source),
                        "-t",
                        str(segment["end"] - segment["start"]),
                        "-c:v",
                        "libx264",
                        "-preset",
                        "veryfast",
                        "-crf",
                        "22",
                        "-c:a",
                        "aac",
                        "-movflags",
                        "+faststart",
                        str(output),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if result.returncode != 0:
                    output.unlink(missing_ok=True)
                    detail = result.stderr.strip() or "FFmpeg returned an unknown error."
                    raise RuntimeError(f"Could not render clip {position}: {detail}")
                created_paths.append(output)
                created.append(
                    {
                        "filename": filename,
                        "title": video["title"],
                        "start": segment["start"],
                        "duration": segment["end"] - segment["start"],
                        "url": f"/api/clips/{filename}",
                    }
                )
        update_job(job_id, status="complete", message="", clips=created)
    except Exception as error:
        for path in created_paths:
            path.unlink(missing_ok=True)
        set_job_error(job_id, error)


import quota_budget
from pipeline import pipeline, start_pipeline_scheduler

quota_budget.initialize()
app.register_blueprint(pipeline)


@app.errorhandler(413)
def request_too_large(_error):
    return jsonify({"error": "Uploaded files must be under 2 GB."}), 413


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/config")
def config():
    try:
        from faster_whisper import WhisperModel as _WhisperModel

        ai_available = _WhisperModel is not None
    except ImportError:
        ai_available = False
    return jsonify({"ai_available": ai_available})


@app.get("/healthz")
def health_check():
    return jsonify({"status": "ok"})


@app.post("/api/analyze")
def start_analysis():
    data = request.get_json(silent=True) or {}
    try:
        url = validate_youtube_url(data.get("url"))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    mode = data.get("mode", "ai")
    if mode not in {"manual", "captions", "ai"}:
        return jsonify({"error": "Choose manual clipping, captions, or local AI analysis."}), 400
    has_rights = data.get("has_rights") is True
    if not has_rights:
        return jsonify(
            {"error": "Confirm that you own this video or have permission to make clips."}
        ), 400
    if mode == "ai":
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return jsonify(
                {
                    "error": "Local AI transcription is not installed. Stop the app and run "
                    "\"run.bat\" again so the required packages are installed."
                }
            ), 400

    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {
            "id": job_id,
            "status": "queued",
            "message": "Starting analysis…",
            "url": url,
            "mode": mode,
            "has_rights": has_rights,
        }
    executor.submit(analyze_video, job_id, url, mode, has_rights)
    return jsonify({"job_id": job_id}), 202


@app.post("/api/clips")
def start_clip_render():
    data = request.get_json(silent=True) or {}
    source_job_id = data.get("analysis_job_id")
    segments = data.get("segments")
    if not isinstance(source_job_id, str) or not isinstance(segments, list) or not segments:
        return jsonify({"error": "Choose at least one clip to create."}), 400
    if len(segments) > 5 or any(not isinstance(segment, dict) for segment in segments):
        return jsonify({"error": "Choose up to five valid clips."}), 400
    with jobs_lock:
        source_job = jobs.get(source_job_id)
        if not source_job or source_job.get("status") != "complete" or "video" not in source_job:
            return jsonify({"error": "Analyze a video before creating clips."}), 400
        duration = source_job["video"]["duration"]
        if source_job_id != source_job.get("id") or not source_job.get("has_rights"):
            return jsonify({"error": "The video is not authorized for clipping."}), 403
        checked_segments = []
        for segment in segments:
            start, end = segment.get("start"), segment.get("end")
            if (
                isinstance(start, bool)
                or not isinstance(start, (int, float))
                or isinstance(end, bool)
                or not isinstance(end, (int, float))
                or not math.isfinite(start)
                or not math.isfinite(end)
                or start < 0
                or end > duration
                or end <= start
                or end - start > 600
            ):
                return jsonify(
                    {"error": "Clip times must be valid, within the video, and no longer than 10 minutes."}
                ), 400
            checked_segments.append({"start": start, "end": end})

    render_job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[render_job_id] = {
            "id": render_job_id,
            "status": "queued",
            "message": "Preparing your clips…",
        }
    executor.submit(create_clips, render_job_id, source_job_id, checked_segments)
    return jsonify({"job_id": render_job_id}), 202


@app.get("/api/jobs/<job_id>")
def job_status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return jsonify({"error": "That job could not be found. Start again to retry."}), 404
        return jsonify(
            {
                key: value
                for key, value in job.items()
                if key not in {"url", "mode", "has_rights"}
            }
        )


@app.get("/api/clips/<path:filename>")
def download_clip(filename: str):
    if Path(filename).name != filename or not filename.lower().endswith(".mp4"):
        return jsonify({"error": "Clip not found."}), 404
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=True)


if __name__ == "__main__":
    start_pipeline_scheduler()
    app.run(
        host=os.environ.get("CLIPSHIP_HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
        threaded=True,
    )
