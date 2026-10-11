from __future__ import annotations

import re
import subprocess
import tempfile
import uuid
from pathlib import Path

import imageio_ffmpeg

import captioning
from quota_budget import APP_DATA_DIR, claim_source_for_processing, get_video, update_video


SOURCE_DIR = APP_DATA_DIR / "sources"
CLIP_DIR = APP_DATA_DIR / "clips"
SOURCE_DIR.mkdir(parents=True, exist_ok=True)
CLIP_DIR.mkdir(parents=True, exist_ok=True)
CLIP_SECONDS = 60.0
CLIP_CUES = {
    "actually", "amazing", "because", "best", "breakthrough", "crazy", "discovered",
    "important", "incredible", "mistake", "never", "secret", "surprise", "surprising",
    "tip", "trick", "unexpected", "unbelievable", "wow",
}


def rank_one_minute_highlight(segments: list[dict], video_duration: float) -> float:
    maximum_start = video_duration - CLIP_SECONDS
    if maximum_start < 0:
        raise ValueError("The source video must be at least 60 seconds long.")
    starts = {0.0, maximum_start}
    start = 0.0
    while start < maximum_start:
        starts.add(start)
        start += 10
    for segment in segments:
        candidate = min(maximum_start, max(0.0, float(segment["start"]) - 12))
        starts.add(round(candidate, 2))

    candidates = []
    sorted_segments = sorted(segments, key=lambda cue: cue["start"])
    cue_index = 0
    for start in sorted(starts):
        end = start + CLIP_SECONDS
        while cue_index < len(sorted_segments) and sorted_segments[cue_index]["end"] <= start:
            cue_index += 1
        i = cue_index
        text_parts = []
        while i < len(sorted_segments) and sorted_segments[i]["start"] < end:
            if sorted_segments[i]["end"] > start:
                text_parts.append(sorted_segments[i]["text"])
            i += 1
        text = " ".join(text_parts)
        words = re.findall(r"\b[\w'-]+\b", text.lower())
        if len(words) < 8:
            continue
        energy = sum(word in CLIP_CUES for word in words)
        density = min(35, len(words) * 0.3)
        sentence_ends = min(5, text.count("!") + text.count("?"))
        candidates.append((energy * 5 + density + sentence_ends * 2, start))
    return max(candidates, default=(0.0, min(maximum_start, max(0.0, video_duration * 0.4))))[1]


def generate_clip(video: dict, segments: list[dict] | None = None) -> dict:
    source = Path(video["source_path"])
    if not source.is_file():
        raise FileNotFoundError("The uploaded source video is missing. Upload it again.")
    probe = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-hide_banner",
            "-i",
            str(source),
            "-t",
            "0",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    duration_match = re.search(
        r"Duration:\s*(\d{2,}):(\d{2}):(\d{2}(?:\.\d+)?)",
        probe.stderr,
    )
    if not duration_match:
        raise ValueError("FFmpeg could not read a valid duration from the local source video.")
    duration = (
        int(duration_match.group(1)) * 3600
        + int(duration_match.group(2)) * 60
        + float(duration_match.group(3))
    )
    if duration < CLIP_SECONDS:
        raise ValueError("Only source videos at least 60 seconds long can make one-minute clips.")

    if segments is None:
        segments = captioning.transcribe_audio_file(source)
    if not segments:
        raise RuntimeError("Whisper did not detect speech in this video to caption.")
    start = rank_one_minute_highlight(segments, duration)
    caption_text = captioning.build_srt(segments, start, CLIP_SECONDS)
    if not caption_text.strip():
        raise RuntimeError("No captions overlapped the selected one-minute moment.")

    filename = f"{uuid.uuid4().hex}.mp4"
    final_path = CLIP_DIR / filename
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    subtitle_style = (
        "FontName=Arial,FontSize=20,PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H80000000,BackColour=&H66000000,BorderStyle=3,"
        "Outline=2,Shadow=1,Alignment=2,MarginV=120"
    )
    filter_graph = (
        "scale=720:1280:force_original_aspect_ratio=increase,"
        "crop=720:1280,subtitles=captions.srt:"
        f"force_style='{subtitle_style}'"
    )
    with tempfile.TemporaryDirectory(prefix="clipship-vertical-") as directory:
        workdir = Path(directory)
        (workdir / "captions.srt").write_text(caption_text, encoding="utf-8")
        result = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                str(start),
                "-i",
                str(source),
                "-t",
                str(CLIP_SECONDS),
                "-vf",
                filter_graph,
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "22",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                str(final_path),
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=workdir,
        )
    if result.returncode != 0:
        final_path.unlink(missing_ok=True)
        raise RuntimeError(result.stderr.strip() or "FFmpeg could not render the vertical clip.")
    clip = get_video(video["video_id"])
    return {
        "video_id": video["video_id"],
        "clip_path": str(final_path),
        "clip_url": f"/api/pipeline/videos/{video['video_id']}/preview",
        "title": f"{video['title'][:85]} #Shorts",
        "start": start,
        "duration": CLIP_SECONDS,
        "captions": caption_text,
        "preview_source": clip["source_path"] if clip else "",
    }


def create_pipeline_clip(video_id: str) -> dict:
    video = claim_source_for_processing(video_id)
    if not video:
        raise ValueError("This source is not queued for processing.")
    try:
        update_video(video_id, status="captioning")
        caption_result = captioning.generate_source_captions(video["source_path"])
        update_video(
            video_id,
            source_caption_path=caption_result["subtitle_path"],
            status="auto_clipping",
        )
        result = generate_clip(video, caption_result["segments"])
        update_video(
            video_id,
            clip_path=result["clip_path"],
            caption_text=result["captions"],
            start_seconds=result["start"],
            status="pending_approval",
            error="",
        )
        return result
    except Exception as error:
        update_video(video_id, status="failed", error=str(error))
        raise
