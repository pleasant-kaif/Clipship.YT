from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import captioning


class CaptioningAgentTests(unittest.TestCase):
    def test_full_source_srt_preserves_timestamps(self):
        segments = [
            {"start": 1.25, "end": 2.75, "text": " First caption "},
            {"start": 3672, "end": 3674.5, "text": "Later in the long video."},
        ]

        subtitles = captioning.build_srt(segments)

        self.assertIn("00:00:01,250 --> 00:00:02,750", subtitles)
        self.assertIn("01:01:12,000 --> 01:01:14,500", subtitles)
        self.assertIn("First caption", subtitles)

    def test_clip_srt_is_trimmed_and_rebased_to_clip_time(self):
        segments = [
            {"start": 58, "end": 62, "text": "Crosses the clip start."},
            {"start": 120, "end": 123, "text": "Outside the clip."},
        ]

        subtitles = captioning.build_srt(segments, clip_start=60, clip_duration=60)

        self.assertIn("00:00:00,000 --> 00:00:02,000", subtitles)
        self.assertIn("Crosses the clip start.", subtitles)
        self.assertNotIn("Outside the clip.", subtitles)

    def test_agent_writes_downloadable_full_source_srt(self):
        source = Path("authorized-source.mp4")
        segments = [{"start": 0, "end": 2, "text": "A complete sentence."}]
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(captioning, "CAPTIONS_DIR", Path(directory)),
                patch.object(
                    captioning,
                    "transcribe_audio_file",
                    return_value=segments,
                ) as transcribe,
            ):
                result = captioning.generate_source_captions(source)

            transcribe.assert_called_once_with(source)
            subtitle_path = Path(result["subtitle_path"])
            self.assertEqual(subtitle_path.parent, Path(directory))
            self.assertEqual(subtitle_path.read_text(encoding="utf-8"), result["subtitle_text"])
            self.assertEqual(result["segments"], segments)
