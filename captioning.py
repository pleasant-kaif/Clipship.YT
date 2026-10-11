from __future__ import annotations

import re
import threading
import uuid
from pathlib import Path

from quota_budget import APP_DATA_DIR


CAPTIONS_DIR = APP_DATA_DIR / "captions"
CAPTIONS_DIR.mkdir(parents=True, exist_ok=True)
MODEL_LOCK = threading.Lock()
_model = None


def local_whisper():
    global _model
    with MODEL_LOCK:
        if _model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as error:
                raise RuntimeError(
                    "faster-whisper is missing. Stop Clipship and launch run.bat to install it."
                ) from error
            _model = WhisperModel("base", device="cpu", compute_type="int8")
        return _model


def transcribe_audio_file(path: str | Path) -> list[dict]:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError("The source audio/video file is missing.")
    transcribed, _ = local_whisper().transcribe(str(source), vad_filter=True)
    segments = [
        {"start": item.start, "end": item.end, "text": item.text.strip()}
        for item in transcribed
        if item.text.strip()
    ]
    if not segments:
        raise RuntimeError("Whisper did not detect speech in this source.")
    return segments


def timestamp(seconds: float) -> str:
    value = max(0, round(seconds * 1000))
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds_part, milliseconds = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds_part:02},{milliseconds:03}"


def build_srt(
    segments: list[dict],
    clip_start: float = 0.0,
    clip_duration: float | None = None,
) -> str:
    captions = []
    clip_end = clip_start + clip_duration if clip_duration is not None else float("inf")
    index = 1
    for segment in segments:
        start = max(float(segment["start"]), clip_start)
        end = min(float(segment["end"]), clip_end)
        text = re.sub(r"\s+", " ", str(segment["text"])).strip()
        if end <= start or not text:
            continue
        caption_lines = [text[offset : offset + 42] for offset in range(0, len(text), 42)]
        captions.append(
            f"{index}\n{timestamp(start - clip_start)} --> {timestamp(end - clip_start)}\n"
            + "\n".join(caption_lines)
        )
        index += 1
    return "\n\n".join(captions) + ("\n" if captions else "")


def generate_source_captions(source_path: str | Path) -> dict:
    segments = transcribe_audio_file(source_path)
    subtitle_text = build_srt(segments)
    if not subtitle_text.strip():
        raise RuntimeError("Whisper returned no timed captions for this source.")
    CAPTIONS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = CAPTIONS_DIR / f"{uuid.uuid4().hex}.srt"
    output_path.write_text(subtitle_text, encoding="utf-8")
    return {
        "segments": segments,
        "subtitle_text": subtitle_text,
        "subtitle_path": str(output_path),
    }
