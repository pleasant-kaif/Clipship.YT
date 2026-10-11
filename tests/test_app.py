import unittest
from unittest.mock import patch

import app as clipship_app
from app import find_highlights, parse_vtt, validate_youtube_url


class YouTubeUrlTests(unittest.TestCase):
    def test_accepts_supported_youtube_links(self):
        self.assertEqual(
            validate_youtube_url(" https://www.youtube.com/watch?v=abc123 "),
            "https://www.youtube.com/watch?v=abc123",
        )
        self.assertEqual(
            validate_youtube_url("https://youtu.be/abc123"),
            "https://youtu.be/abc123",
        )

    def test_rejects_non_youtube_hosts_and_non_https_links(self):
        for url in (
            "http://youtube.com/watch?v=abc",
            "https://youtube.com.example.org/watch?v=abc",
            "https://example.org/watch?v=abc",
            "https://user:pass@youtube.com/watch?v=abc",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_youtube_url(url)


class CaptionTests(unittest.TestCase):
    def test_parses_cues_and_removes_caption_markup(self):
        content = (
            "WEBVTT\n\n"
            "00:00:01.000 --> 00:00:03.250\n"
            "<v Speaker>Here is a <b>surprising</b> discovery!</v>\n\n"
            "00:00:04.000 --> 00:00:05.000\n"
            "This is the next part.\n"
        )
        cues = parse_vtt(content)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0], {
            "start": 1.0,
            "end": 3.25,
            "text": "Here is a surprising discovery!",
        })

    def test_parses_short_webvtt_timestamps(self):
        cues = parse_vtt("WEBVTT\n\n00:01.250 --> 00:03.500\nA short caption.\n")
        self.assertEqual(cues[0]["start"], 1.25)
        self.assertEqual(cues[0]["end"], 3.5)

    def test_returns_non_overlapping_ranked_highlights(self):
        cues = [
            {"start": second, "end": second + 1, "text": text}
            for second, text in [
                (1, "Here is an ordinary introduction to this video."),
                (8, "This surprising discovery is amazing and important!"),
                (16, "Because of this secret trick you can finally do it."),
                (41, "Actually this unexpected breakthrough changes everything!"),
                (52, "The best way to avoid this common mistake is practice."),
            ]
        ]
        highlights = find_highlights(cues, 75)
        self.assertGreaterEqual(len(highlights), 1)
        self.assertEqual(highlights, sorted(highlights, key=lambda item: item["start"]))
        for first, second in zip(highlights, highlights[1:]):
            self.assertLessEqual(first["end"], second["start"])


class ClipApiTests(unittest.TestCase):
    def setUp(self):
        self.client = clipship_app.app.test_client()
        with clipship_app.jobs_lock:
            clipship_app.jobs.clear()

    def test_health_endpoint_returns_ok(self):
        response = self.client.get("/healthz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"status": "ok"})

    def test_manual_analysis_requires_rights_confirmation(self):
        response = self.client.post(
            "/api/analyze",
            json={"url": "https://youtu.be/abc123", "mode": "manual"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("permission", response.json["error"])

    def test_manual_analysis_queues_without_transcription(self):
        with patch.object(clipship_app.executor, "submit") as submit:
            response = self.client.post(
                "/api/analyze",
                json={
                    "url": "https://youtu.be/abc123",
                    "mode": "manual",
                    "has_rights": True,
                },
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(submit.call_args.args[3], "manual")

    def test_highlight_analysis_defaults_to_local_ai(self):
        with patch.object(clipship_app.executor, "submit") as submit:
            response = self.client.post(
                "/api/analyze",
                json={
                    "url": "https://youtu.be/abc123",
                    "has_rights": True,
                },
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(submit.call_args.args[3], "ai")

    def test_local_ai_is_available_in_the_app_environment(self):
        response = self.client.get("/api/config")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["ai_available"])

    def test_clip_ranges_must_stay_within_video(self):
        with clipship_app.jobs_lock:
            clipship_app.jobs["analysis"] = {
                "id": "analysis",
                "status": "complete",
                "has_rights": True,
                "url": "https://youtu.be/abc123",
                "video": {"duration": 1200, "highlights": []},
            }
        for segment in (
            {"start": -1, "end": 12},
            {"start": 15, "end": 1201},
            {"start": 20, "end": 20},
            {"start": True, "end": 30},
            {"start": 1, "end": 602},
        ):
            with self.subTest(segment=segment):
                response = self.client.post(
                    "/api/clips",
                    json={"analysis_job_id": "analysis", "segments": [segment]},
                )
                self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
