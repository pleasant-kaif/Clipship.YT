import json
import io
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
from zoneinfo import ZoneInfo

import app as clipship_app
import pipeline as pipeline_module
import quota_budget as store
import seo_agent
import trend_scout
import youtube_api
from quota_budget import QuotaBudget, QuotaExceeded, local_now


class IsolatedAutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path_patch = patch.object(store, "DB_PATH", Path(self.temp_dir.name) / "test.db")
        self.db_path_patch.start()
        store.initialize()
        self.client = clipship_app.app.test_client()

    def tearDown(self):
        self.db_path_patch.stop()
        self.temp_dir.cleanup()

    def add_video(self, video_id="trend-1", *, status="pending_approval"):
        path = Path(self.temp_dir.name) / f"{video_id}.mp4"
        path.write_bytes(b"authorized fixture video")
        store.add_video(
            {
                "video_id": video_id,
                "title": f"Clip {video_id}",
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "duration_seconds": 120,
                "category_id": "20",
            }
        )
        store.update_video(
            video_id,
            rights_confirmed=1,
            clip_path=str(path),
            caption_text="Verified fixture captions.",
            status=status,
            start_seconds=12,
        )
        return path

    def test_quota_reservation_tracks_fixed_cost_and_priority(self):
        reservation = QuotaBudget().check(1, "trends", "videos.list")
        QuotaBudget.finish(reservation, "succeeded")
        snapshot = QuotaBudget().snapshot()
        self.assertEqual(snapshot["used"], 1)
        self.assertEqual(snapshot["used_by_tier"]["trends"], 1)
        self.assertEqual(snapshot["daily_upload_limit"], 5)

    def test_quota_exhaustion_defers_the_action_to_next_scheduled_day(self):
        budget = QuotaBudget()
        for index in range(20):
            reservation = budget.check(50, "engagement", "comments.insert")
            budget.finish(reservation, "succeeded")
        task_id = "reply-test"
        store.queue_api_task(
            task_id,
            "comments.insert",
            "engagement",
            {"kind": "reply", "comment_id": "comment-1", "reply": "Thank you."},
        )
        with self.assertRaises(QuotaExceeded):
            budget.check(50, "engagement", "comments.insert", task_id=task_id)
        with store.connect() as db:
            queued = db.execute(
                "SELECT status, scheduled_at FROM api_task_queue WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        self.assertEqual(queued["status"], "queued")
        self.assertGreater(queued["scheduled_at"][:10], local_now().date().isoformat())
        self.assertEqual(budget.snapshot()["used_by_tier"]["engagement"], 1000)

    def test_daily_clip_limit_counts_distinct_video_once(self):
        self.assertTrue(store.reserve_daily_clip("one", 1))
        self.assertFalse(store.reserve_daily_clip("one", 1))
        self.assertTrue(store.daily_clip_reserved_today("one"))
        self.assertFalse(store.daily_clip_reserved_today("two"))
        self.assertFalse(store.reserve_daily_clip("two", 1))
        next_day = local_now().date() + timedelta(days=1)
        with patch.object(store, "local_day", return_value=next_day):
            self.assertTrue(store.reserve_daily_clip("one", 1))

    def test_startup_requeues_interrupted_local_processing(self):
        source = Path(self.temp_dir.name) / "interrupted-source.mp4"
        source.write_bytes(b"authorized fixture video")
        store.add_video(
            {
                "video_id": "interrupted-source",
                "title": "Interrupted source",
                "url": "https://www.youtube.com/watch?v=interrupted-source",
            }
        )
        store.update_video(
            "interrupted-source",
            source_path=str(source),
            rights_confirmed=1,
            status="captioning",
        )

        self.assertEqual(store.requeue_interrupted_source_jobs(), 1)
        stored = store.get_video("interrupted-source")
        self.assertEqual(stored["status"], "source_uploaded")
        self.assertIn("restarted", stored["error"])

    def test_dashboard_lists_pipeline_videos_before_more_than_200_trends(self):
        for index in range(205):
            store.add_video(
                {
                    "video_id": f"trend-{index}",
                    "title": f"Trend {index}",
                    "url": f"https://www.youtube.com/watch?v=trend-{index}",
                }
            )
        self.add_video("important-review", status="pending_approval")

        dashboard = pipeline_module.dashboard_state()
        visible_ids = {video["video_id"] for video in dashboard["videos"]}

        self.assertIn("important-review", visible_ids)
        self.assertLessEqual(
            sum(video["status"] == "trending" for video in dashboard["videos"]),
            200,
        )

    def test_scheduler_retries_source_after_clip_quota_was_reserved(self):
        source = Path(self.temp_dir.name) / "queued-source.mp4"
        source.write_bytes(b"authorized fixture video")
        store.add_video(
            {
                "video_id": "queued-source",
                "title": "Queued source",
                "url": "https://www.youtube.com/watch?v=queued-source",
            }
        )
        store.update_video(
            "queued-source",
            source_path=str(source),
            rights_confirmed=1,
            status="source_uploaded",
            error="Previous worker was interrupted.",
        )
        self.assertTrue(store.reserve_daily_clip("queued-source", 10))

        with patch.object(pipeline_module.pipeline_executor, "submit") as submit:
            result = pipeline_module.run_scheduler_once()

        submit.assert_called_once_with(
            pipeline_module.process_uploaded_source, "queued-source"
        )
        self.assertEqual(result["status"], "complete")

    def test_scheduler_does_not_send_direct_upload_to_captioning_or_clipping(self):
        source = Path(self.temp_dir.name) / "direct-source.mp4"
        source.write_bytes(b"authorized fixture video")
        store.add_video(
            {
                "video_id": "direct-ready",
                "title": "Direct upload ready",
                "url": "https://www.youtube.com/watch?v=direct-ready",
            }
        )
        store.update_video(
            "direct-ready",
            source_path=str(source),
            rights_confirmed=1,
            upload_mode="direct",
            status="source_uploaded",
        )

        with patch.object(pipeline_module.pipeline_executor, "submit") as submit:
            pipeline_module.run_scheduler_once()

        submit.assert_not_called()
        self.assertEqual(store.get_video("direct-ready")["status"], "source_uploaded")

    def test_failed_short_source_can_publish_original_without_replacement(self):
        source = Path(self.temp_dir.name) / "short-source.mp4"
        source.write_bytes(b"short source")
        store.add_video(
            {
                "video_id": "replace-source",
                "title": "Replace source",
                "url": "https://www.youtube.com/watch?v=replace-source",
            }
        )
        store.update_video(
            "replace-source",
            source_path=str(source),
            rights_confirmed=1,
            status="failed",
            error="Only source videos at least 60 seconds long can make one-minute clips.",
        )
        with (
            patch.object(
                pipeline_module.youtube_api,
                "credentials_status",
                return_value={"youtube_connected": True, "oauth_client_uploaded": True},
            ),
            patch.object(pipeline_module.pipeline_executor, "submit") as submit,
        ):
            response = self.client.post(
                "/api/pipeline/videos/replace-source/publish-source",
            )

        self.assertEqual(response.status_code, 202)
        stored = store.get_video("replace-source")
        self.assertEqual(stored["status"], "uploading")
        self.assertEqual(stored["error"], "")
        self.assertEqual(stored["source_path"], str(source))
        self.assertEqual(stored["upload_mode"], "direct")
        submit.assert_called_once_with(
            pipeline_module.process_original_video_upload, "replace-source"
        )

    def test_trend_scout_uses_exact_search_then_details_lookup(self):
        search_request = Mock()
        search_request.execute.return_value = {
            "items": [
                {
                    "id": {"videoId": "abc"},
                    "snippet": {
                        "title": "ट्रेंडिंग वीडियो",
                        "channelTitle": "Channel",
                        "thumbnails": {"high": {"url": "https://example.test/thumb.jpg"}},
                        "publishedAt": "2026-01-01T00:00:00Z",
                    },
                }
            ]
        }
        details_request = Mock()
        details_request.execute.return_value = {
            "items": [
                {
                    "id": "abc",
                    "snippet": {"liveBroadcastContent": "none"},
                    "contentDetails": {"duration": "PT2M3S"},
                    "statistics": {
                        "viewCount": "1200",
                        "likeCount": "89",
                        "commentCount": "12",
                    },
                }
            ]
        }
        service = Mock()
        service.search.return_value.list.return_value = search_request
        service.videos.return_value.list.return_value = details_request
        with patch.object(
            trend_scout,
            "youtube_service",
            return_value=service,
        ) as get_service:
            videos = trend_scout.fetch_trending(
                "CA", "20", language="hi", task_id="test-trend"
            )
        get_service.assert_called_once_with(timeout=4)
        service.search.return_value.list.assert_called_once_with(
            part="snippet",
            type="video",
            q="trending",
            regionCode="CA",
            videoCategoryId="20",
            relevanceLanguage="hi",
            maxResults=50,
        )
        service.videos.return_value.list.assert_called_once_with(
            part="snippet,contentDetails,statistics",
            id="abc",
            maxResults=1,
        )
        self.assertEqual(videos[0]["duration_seconds"], 123)
        metadata = json.loads(videos[0]["metadata"])
        self.assertEqual(metadata["language"], "hi")
        self.assertTrue(metadata["duration_available"])
        self.assertTrue(metadata["language_match"])
        self.assertEqual(metadata["view_count"], 1200)
        self.assertEqual(metadata["like_count"], 89)
        self.assertEqual(metadata["comment_count"], 12)
        self.assertEqual(
            QuotaBudget().snapshot()["used_by_tier"]["trends"], 101
        )

    def test_hinglish_matching_is_best_effort_and_keeps_details_lookup(self):
        search_request = Mock()
        search_request.execute.return_value = {
            "items": [
                {
                    "id": {"videoId": "hinglish-video"},
                    "snippet": {
                        "title": "Bhai, yeh amazing video hai",
                        "channelTitle": "Channel",
                    },
                },
                {
                    "id": {"videoId": "english-video"},
                    "snippet": {"title": "An ordinary gaming stream"},
                },
            ]
        }
        details_request = Mock()
        details_request.execute.return_value = {
            "items": [
                {
                    "id": "hinglish-video",
                    "contentDetails": {"duration": "PT1M"},
                    "statistics": {"viewCount": "100"},
                },
                {
                    "id": "english-video",
                    "contentDetails": {"duration": "PT1M"},
                    "statistics": {"viewCount": "500"},
                },
            ]
        }
        service = Mock()
        service.search.return_value.list.return_value = search_request
        service.videos.return_value.list.return_value = details_request
        with patch.object(trend_scout, "youtube_service", return_value=service):
            videos = trend_scout.fetch_trending(
                "IN", "23", language="hinglish"
            )

        service.search.return_value.list.assert_called_once_with(
            part="snippet",
            type="video",
            q="trending",
            regionCode="IN",
            videoCategoryId="23",
            relevanceLanguage="hi",
            maxResults=50,
        )
        self.assertEqual(videos[0]["video_id"], "hinglish-video")
        self.assertEqual(QuotaBudget().snapshot()["used_by_tier"]["trends"], 101)

    def test_trend_scout_reuses_results_without_spending_quota(self):
        search_request = Mock()
        search_request.execute.return_value = {
            "items": [
                {
                    "id": {"videoId": "cached-trend"},
                    "snippet": {"title": "Cached trend", "channelTitle": "Channel"},
                }
            ]
        }
        details_request = Mock()
        details_request.execute.return_value = {
            "items": [
                {
                    "id": "cached-trend",
                    "contentDetails": {"duration": "PT2M"},
                    "statistics": {"viewCount": "100"},
                }
            ]
        }
        service = Mock()
        service.search.return_value.list.return_value = search_request
        service.videos.return_value.list.return_value = details_request
        with patch.object(trend_scout, "youtube_service", return_value=service):
            initial_results, initial_cached = trend_scout.fetch_trending_with_status(
                "CA", "20", language="en"
            )
            cached_results, cached = trend_scout.fetch_trending_with_status(
                "CA", "20", language="en"
            )

        self.assertFalse(initial_cached)
        self.assertTrue(cached)
        self.assertEqual(cached_results, initial_results)
        service.search.return_value.list.assert_called_once()
        service.videos.return_value.list.assert_called_once()
        self.assertEqual(QuotaBudget().snapshot()["used_by_tier"]["trends"], 101)

    def test_force_refresh_bypasses_trend_cache(self):
        request = Mock()
        request.execute.return_value = {"items": []}
        service = Mock()
        service.search.return_value.list.return_value = request
        with patch.object(trend_scout, "youtube_service", return_value=service):
            trend_scout.fetch_trending("CA", "20", language="en")
            results, cached = trend_scout.fetch_trending_with_status(
                "CA", "20", language="en", force_refresh=True
            )

        self.assertEqual(results, [])
        self.assertFalse(cached)
        self.assertEqual(service.search.return_value.list.call_count, 2)
        self.assertEqual(service.videos.return_value.list.call_count, 0)
        self.assertEqual(QuotaBudget().snapshot()["used_by_tier"]["trends"], 200)

    def test_automation_routes_load_with_quota_dashboard(self):
        self.assertEqual(self.client.get("/automation").status_code, 200)
        response = self.client.get("/api/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["quota"]["daily_cap"], 10_000)
        self.assertEqual(
            [language["id"] for language in response.json["trend_languages"]],
            ["hinglish", "en", "hi", "ur"],
        )
        self.assertEqual(
            [region["id"] for region in response.json["india_regions"]],
            ["all", "north", "south", "west", "east"],
        )
        self.assertEqual(self.client.get("/quota").status_code, 200)

    def test_ollama_status_uses_configured_service_url(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"models": [{"name": "llama3:latest"}]}'
        with (
            patch.object(
                pipeline_module.engagement,
                "OLLAMA_BASE_URL",
                "http://127.0.0.1:11434",
            ),
            patch.object(pipeline_module.engagement, "LOCAL_AI_MODEL", "llama3"),
            patch.object(
                pipeline_module.engagement.urllib.request,
                "urlopen",
                return_value=response,
            ) as urlopen,
        ):
            status = pipeline_module.engagement.ollama_status()

        self.assertTrue(status["model_installed"])
        self.assertEqual(status["configured_model"], "llama3")
        self.assertEqual(status["service_name"], "Ollama")
        self.assertEqual(
            urlopen.call_args.args[0].full_url,
            "http://127.0.0.1:11434/api/tags",
        )

    def test_scout_route_uses_and_saves_selected_region_and_category(self):
        trend = {
            "video_id": "trend-ca",
            "title": "Canadian trend",
            "url": "https://www.youtube.com/watch?v=trend-ca",
            "category_id": "20",
            "metadata": json.dumps({"region": "CA", "language": "ur"}),
        }

        def save_trend(*_args, **_kwargs):
            store.add_video(trend)
            return [trend], False

        with patch.object(
            trend_scout,
            "fetch_trending_with_status",
            side_effect=save_trend,
        ) as fetch:
            response = self.client.post(
                "/api/trends/scout",
                json={
                    "region": "CA",
                    "category_id": "20",
                    "trend_language": "ur",
                    "india_region": "all",
                    "force_refresh": True,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(fetch.call_args.args[:2], ("CA", "20"))
        self.assertEqual(fetch.call_args.kwargs["language"], "ur")
        self.assertTrue(fetch.call_args.kwargs["force_refresh"])
        self.assertEqual(store.get_setting("region"), "CA")
        self.assertEqual(store.get_setting("category_id"), "20")
        self.assertEqual(store.get_setting("trend_language"), "ur")
        self.assertEqual(len(response.json["videos"]), 1)
        self.assertFalse(response.json["cached"])
        self.assertEqual(response.json["quota_cost"], 101)
        self.assertEqual(response.json["videos"][0]["trend_region"], "CA")
        self.assertEqual(response.json["videos"][0]["trend_category_id"], "20")
        self.assertEqual(response.json["videos"][0]["trend_language"], "ur")
        self.assertEqual(store.get_setting("india_region"), "all")

    def test_north_india_trends_locally_match_state_names(self):
        request = Mock()
        request.execute.return_value = {
            "items": [
                {
                    "id": {"videoId": "delhi-video"},
                    "snippet": {
                        "title": "Delhi food and culture",
                        "description": "A tour around the city.",
                        "channelTitle": "Local channel",
                    },
                }
            ]
        }
        details_request = Mock()
        details_request.execute.return_value = {
            "items": [
                {
                    "id": "delhi-video",
                    "contentDetails": {"duration": "PT1M8S"},
                    "statistics": {
                        "viewCount": "1500",
                        "likeCount": "75",
                        "commentCount": "8",
                    },
                }
            ]
        }
        service = Mock()
        service.search.return_value.list.return_value = request
        service.videos.return_value.list.return_value = details_request
        with patch.object(trend_scout, "youtube_service", return_value=service):
            videos = trend_scout.fetch_trending(
                "IN", "23", language="hi", india_region="north"
            )

        service.search.return_value.list.assert_called_once_with(
            part="snippet",
            type="video",
            q="trending",
            regionCode="IN",
            videoCategoryId="23",
            relevanceLanguage="hi",
            maxResults=50,
        )
        service.videos.return_value.list.assert_called_once_with(
            part="snippet,contentDetails,statistics",
            id="delhi-video",
            maxResults=1,
        )
        metadata = json.loads(videos[0]["metadata"])
        self.assertEqual(metadata["india_region"], "north")
        self.assertEqual(videos[0]["duration_seconds"], 68)
        self.assertTrue(metadata["duration_available"])
        self.assertIn("Delhi", metadata["india_region_matches"])
        self.assertEqual(QuotaBudget().snapshot()["used_by_tier"]["trends"], 101)

    def test_indian_zone_requires_india_country_code(self):
        response = self.client.put(
            "/api/settings",
            json={"region": "US", "india_region": "south"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("India (IN)", response.json["error"])

    def test_scout_route_returns_gateway_timeout_for_slow_youtube_request(self):
        with patch.object(
            trend_scout,
            "fetch_trending_with_status",
            side_effect=TimeoutError("request timed out"),
        ):
            response = self.client.post(
                "/api/trends/scout",
                json={"region": "CA", "category_id": "20"},
            )

        self.assertEqual(response.status_code, 504)
        self.assertIn("10-second limit", response.json["error"])

    def test_settings_reject_unsupported_trend_language(self):
        response = self.client.put(
            "/api/settings",
            json={"trend_language": "fr"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(store.get_setting("trend_language"), "en")

    def test_trend_json_includes_filter_metadata_for_dashboard(self):
        store.add_video(
            {
                "video_id": "trend-ca",
                "title": "Canadian trend",
                "url": "https://www.youtube.com/watch?v=trend-ca",
                "category_id": "20",
                "metadata": json.dumps({"region": "CA"}),
            }
        )

        video = pipeline_module.video_for_json(store.get_video("trend-ca"))

        self.assertEqual(video["trend_region"], "CA")
        self.assertEqual(video["trend_category_id"], "20")
        self.assertIsNone(video["like_count"])
        self.assertIsNone(video["comment_count"])
        self.assertFalse(video["duration_available"])

    def test_trend_json_marks_duration_from_video_details_available(self):
        store.add_video(
            {
                "video_id": "trend-duration",
                "title": "Exact duration video",
                "url": "https://www.youtube.com/watch?v=trend-duration",
                "category_id": "20",
                "duration_seconds": 123,
                "metadata": json.dumps(
                    {"region": "IN", "duration_available": True}
                ),
            }
        )

        video = pipeline_module.video_for_json(store.get_video("trend-duration"))

        self.assertEqual(video["duration_seconds"], 123)
        self.assertTrue(video["duration_available"])

    def test_cross_origin_post_cannot_change_local_settings(self):
        old_region = store.get_setting("region")
        response = self.client.put(
            "/api/settings",
            json={"region": "CA"},
            headers={"Origin": "https://untrusted.example"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(store.get_setting("region"), old_region)

    def test_oauth_requires_web_client_and_saves_secret_outside_api_responses(self):
        secret_path = Path(self.temp_dir.name) / "oauth.json"
        client_secret = json.dumps(
            {
                "web": {
                    "client_id": "test-client",
                    "client_secret": "not-a-real-secret",
                    "redirect_uris": [youtube_api.CALLBACK_URL],
                }
            }
        ).encode()
        with patch.object(youtube_api, "CLIENT_SECRET_PATH", secret_path):
            youtube_api.save_uploaded_client_secret(client_secret)
            self.assertTrue(secret_path.is_file())
            with self.assertRaisesRegex(ValueError, "Web application"):
                youtube_api.save_uploaded_client_secret(
                    json.dumps(
                        {
                            "installed": {
                                "client_id": "desktop-client",
                                "client_secret": "desktop-secret",
                            }
                        }
                    ).encode()
                )

    def test_oauth_pkce_verifier_is_persisted_and_consumed_during_callback(self):
        authorization_flow = Mock()
        authorization_flow.code_verifier = "local-pkce-verifier"
        authorization_flow.authorization_url.side_effect = lambda **kwargs: (
            "https://accounts.google.com/oauth",
            kwargs["state"],
        )
        token_flow = Mock()
        token_flow.credentials.refresh_token = "refresh-token"
        token_flow.credentials.to_json.return_value = '{"refresh_token":"refresh-token"}'
        token_path = Path(self.temp_dir.name) / "token.json"
        with (
            patch.object(
                youtube_api,
                "create_oauth_flow",
                side_effect=[authorization_flow, token_flow],
            ),
            patch.object(youtube_api, "TOKEN_PATH", token_path),
        ):
            url = youtube_api.oauth_authorization_url()
            self.assertIn("https://accounts.google.com/oauth", url)
            oauth_state = store.get_setting("oauth_state")
            self.assertEqual(store.get_setting("oauth_code_verifier"), "local-pkce-verifier")
            youtube_api.finish_oauth_callback("authorization-code", oauth_state)

        self.assertEqual(token_flow.code_verifier, "local-pkce-verifier")
        token_flow.fetch_token.assert_called_once_with(code="authorization-code")
        self.assertTrue(token_path.is_file())
        self.assertEqual(store.get_setting("oauth_state"), "")
        with self.assertRaises(KeyError):
            store.get_setting("oauth_code_verifier")

    def test_only_a_reviewed_finished_clip_can_be_approved(self):
        path = self.add_video()
        store.update_video("trend-1", status="trending")
        response = self.client.post(
            "/api/pipeline/videos/trend-1/approve",
            json={"approved": True},
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(path.is_file())

    def test_approved_clip_is_scheduled_privately_and_never_published_inline(self):
        self.add_video()
        with patch.object(pipeline_module, "youtube_service") as service:
            response = self.client.post(
                "/api/pipeline/videos/trend-1/approve",
                json={"approved": True},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "approved")
        self.assertEqual(response.json["date"] >= local_now().date().isoformat(), True)
        service.assert_not_called()
        self.assertEqual(store.get_video("trend-1")["status"], "approved")

    def test_approval_returns_the_actual_scheduled_time_and_visibility(self):
        self.add_video()
        response = self.client.post(
            "/api/pipeline/videos/trend-1/approve",
            json={"approved": True},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["scheduled_at"], store.get_video("trend-1")["scheduled_at"])
        self.assertEqual(response.json["timezone"], "Asia/Kolkata")
        self.assertEqual(response.json["privacy"], "private")

    def test_approval_after_configured_time_moves_to_next_day(self):
        self.add_video()
        store.set_settings({"schedule_time": "15:40"})
        now = datetime(2026, 10, 10, 15, 45, tzinfo=ZoneInfo("Asia/Kolkata"))
        with patch.object(pipeline_module, "local_now", return_value=now):
            response = self.client.post(
                "/api/pipeline/videos/trend-1/approve",
                json={"approved": True},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json["scheduled_at"],
            "2026-10-11T15:40:00+05:30",
        )

    def test_upload_uses_visibility_confirmed_at_approval(self):
        self.add_video()
        approved = self.client.post(
            "/api/pipeline/videos/trend-1/approve",
            json={"approved": True},
        )
        self.assertEqual(approved.status_code, 200)
        store.set_settings({"upload_privacy": "public"})
        request = Mock()
        request.execute.return_value = {"id": "youtube-id"}
        service = Mock()
        service.videos.return_value.insert.return_value = request
        media_file_upload = Mock()
        with (
            patch.object(pipeline_module, "youtube_service", return_value=service),
            patch.object(
                pipeline_module.seo_agent,
                "write_description",
                return_value="A search-aware description based on the clip transcript.",
            ) as write_description,
            patch(
                "googleapiclient.http.MediaFileUpload",
                return_value=media_file_upload,
            ),
        ):
            result = pipeline_module.upload_greenlit_video(store.get_video("trend-1"))
        self.assertEqual(result["uploaded_video_id"], "youtube-id")
        body = service.videos.return_value.insert.call_args.kwargs["body"]
        self.assertEqual(body["status"]["privacyStatus"], "private")
        self.assertEqual(
            body["snippet"]["description"],
            "A search-aware description based on the clip transcript.",
        )
        write_description.assert_called_once_with("trend-1")

    def test_seo_agent_generates_and_persists_local_description(self):
        self.add_video(video_id="seo-video", status="approved")
        store.update_video(
            "seo-video",
            caption_text="A creator explains how to repair a bicycle chain.",
        )
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(
            {"response": "Learn practical bicycle chain repair steps with this clear guide. #BikeRepair"}
        ).encode("utf-8")
        with (
            patch.object(seo_agent.engagement, "OLLAMA_BASE_URL", "http://ollama:11434"),
            patch.object(seo_agent.engagement, "LOCAL_AI_MODEL", "llama3"),
            patch("seo_agent.urllib.request.urlopen", return_value=response) as urlopen,
        ):
            description = seo_agent.write_description("seo-video")

        self.assertEqual(
            description,
            "Learn practical bicycle chain repair steps with this clear guide. #BikeRepair",
        )
        stored = store.get_video("seo-video")
        self.assertEqual(stored["seo_description"], description)
        self.assertEqual(stored["seo_status"], "ready")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://ollama:11434/api/generate")
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "llama3")
        self.assertIn("bicycle chain", payload["prompt"])
        self.assertIn("trend-aware", payload["prompt"])

    def test_seo_agent_failure_blocks_fallback_description(self):
        self.add_video(video_id="seo-offline", status="approved")
        with (
            patch.object(seo_agent.engagement, "OLLAMA_BASE_URL", "http://ollama:11434"),
            patch.object(seo_agent.engagement, "LOCAL_AI_MODEL", "llama3"),
            patch(
                "seo_agent.urllib.request.urlopen",
                side_effect=seo_agent.urllib.error.URLError("offline"),
            ),
        ):
            with self.assertRaisesRegex(ConnectionError, "SEO Description Agent"):
                seo_agent.write_description("seo-offline")

        stored = store.get_video("seo-offline")
        self.assertEqual(stored["seo_status"], "failed")
        self.assertEqual(stored["seo_description"], "")

    def test_source_upload_requires_explicit_rights_confirmation(self):
        store.add_video(
            {
                "video_id": "source-1",
                "title": "A trend",
                "url": "https://www.youtube.com/watch?v=source-1",
                "duration_seconds": 300,
            }
        )
        response = self.client.post(
            "/api/pipeline/videos/source-1/source",
            data={"source_file": (io.BytesIO(b"test video"), "source.mp4")},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("permission", response.json["error"])

    def test_authorized_local_source_is_imported_without_duration_or_clip_queue(self):
        store.add_video(
            {
                "video_id": "source-2",
                "title": "A trend",
                "url": "https://www.youtube.com/watch?v=source-2",
                "duration_seconds": 30,
            }
        )
        sources = Path(self.temp_dir.name) / "sources"
        with (
            patch.object(pipeline_module, "SOURCE_DIR", sources),
            patch.object(
                pipeline_module.youtube_api,
                "credentials_status",
                return_value={"youtube_connected": False, "oauth_client_uploaded": False},
            ),
            patch.object(pipeline_module.pipeline_executor, "submit") as submit,
        ):
            response = self.client.post(
                "/api/pipeline/videos/source-2/source",
                data={
                    "rights_confirmed": "true",
                    "source_file": (io.BytesIO(b"authorized fixture"), "source.mp4"),
                },
            )
        self.assertEqual(response.status_code, 202)
        self.assertTrue(store.get_video("source-2")["rights_confirmed"])
        self.assertEqual(store.get_video("source-2")["status"], "source_uploaded")
        self.assertEqual(store.get_video("source-2")["upload_mode"], "direct")
        self.assertTrue(Path(store.get_video("source-2")["source_path"]).is_file())
        self.assertIn("Connect your YouTube account", store.get_video("source-2")["error"])
        submit.assert_not_called()

    def test_imported_local_video_starts_direct_upload_without_duration_probe(self):
        store.add_video(
            {
                "video_id": "direct-source",
                "title": "Original long video",
                "url": "https://www.youtube.com/watch?v=direct-source",
            }
        )
        sources = Path(self.temp_dir.name) / "sources"
        with (
            patch.object(pipeline_module, "SOURCE_DIR", sources),
            patch.object(
                pipeline_module.youtube_api,
                "credentials_status",
                return_value={"youtube_connected": True, "oauth_client_uploaded": True},
            ),
            patch.object(pipeline_module.pipeline_executor, "submit") as submit,
        ):
            response = self.client.post(
                "/api/pipeline/videos/direct-source/source",
                data={
                    "rights_confirmed": "true",
                    "source_file": (io.BytesIO(b"video of arbitrary duration"), "original.mp4"),
                },
            )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json["direct_upload"])
        self.assertEqual(store.get_video("direct-source")["status"], "uploading")
        self.assertEqual(store.get_video("direct-source")["upload_mode"], "direct")
        with store.connect() as db:
            clip_reservations = db.execute(
                "SELECT COUNT(*) FROM daily_clip_video_runs"
            ).fetchone()[0]
        self.assertEqual(clip_reservations, 0)
        submit.assert_called_once_with(
            pipeline_module.process_original_video_upload, "direct-source"
        )

    def test_direct_upload_publishes_original_file_with_its_duration_and_title(self):
        source = Path(self.temp_dir.name) / "original.mp4"
        source.write_bytes(b"short original video")
        store.add_video(
            {
                "video_id": "direct-upload",
                "title": "Original video",
                "url": "https://www.youtube.com/watch?v=direct-upload",
                "category_id": "20",
            }
        )
        store.update_video(
            "direct-upload",
            source_path=str(source),
            rights_confirmed=1,
            upload_mode="direct",
            status="uploading",
        )
        request = Mock()
        request.execute.return_value = {"id": "youtube-original"}
        service = Mock()
        service.videos.return_value.insert.return_value = request
        media = Mock()
        with (
            patch.object(pipeline_module, "youtube_service", return_value=service),
            patch.object(
                pipeline_module.seo_agent,
                "write_description",
                return_value="Original video's trend-aware description.",
            ),
            patch(
                "googleapiclient.http.MediaFileUpload",
                return_value=media,
            ) as upload,
        ):
            result = pipeline_module.upload_original_source("direct-upload")

        self.assertEqual(result["uploaded_video_id"], "youtube-original")
        upload.assert_called_once_with(
            str(source),
            mimetype="video/mp4",
            resumable=False,
        )
        body = service.videos.return_value.insert.call_args.kwargs["body"]
        self.assertEqual(body["snippet"]["title"], "Original video")
        self.assertEqual(
            body["snippet"]["description"],
            "Original video's trend-aware description.",
        )
        self.assertNotIn("#Shorts", body["snippet"]["title"])
        stored = store.get_video("direct-upload")
        self.assertEqual(stored["status"], "published")
        self.assertEqual(stored["uploaded_video_id"], "youtube-original")

    def test_auto_captioning_then_auto_clipping_complete_before_review(self):
        source = Path(self.temp_dir.name) / "long-source.mp4"
        source.write_bytes(b"authorized long-form source")
        captions = Path(self.temp_dir.name) / "long-source.srt"
        captions.write_text("1\n00:00:00,000 --> 00:00:02,000\nTranscript.\n", encoding="utf-8")
        clip = Path(self.temp_dir.name) / "vertical-clip.mp4"
        segments = [{"start": 0, "end": 2, "text": "Transcript."}]
        store.add_video(
            {
                "video_id": "agent-source",
                "title": "Authorized long-form source",
                "url": "https://www.youtube.com/watch?v=agent-source",
                "duration_seconds": 3600,
            }
        )
        store.update_video(
            "agent-source",
            source_path=str(source),
            rights_confirmed=1,
            status="source_uploaded",
        )
        stages = []

        def run_captioning(source_path):
            self.assertEqual(store.get_video("agent-source")["status"], "captioning")
            self.assertEqual(source_path, str(source))
            stages.append("captioning")
            return {
                "segments": segments,
                "subtitle_text": captions.read_text(encoding="utf-8"),
                "subtitle_path": str(captions),
            }

        def run_clipping(video, transcript):
            self.assertEqual(video["video_id"], "agent-source")
            self.assertEqual(transcript, segments)
            self.assertEqual(store.get_video("agent-source")["status"], "auto_clipping")
            stages.append("auto_clipping")
            clip.write_bytes(b"rendered clip")
            return {
                "video_id": "agent-source",
                "clip_path": str(clip),
                "captions": "Burned-in clip captions.",
                "start": 30,
            }

        with (
            patch.object(
                pipeline_module.captioning,
                "generate_source_captions",
                side_effect=run_captioning,
            ),
            patch.object(
                pipeline_module.clip_factory,
                "generate_clip",
                side_effect=run_clipping,
            ),
        ):
            result = pipeline_module.clip_factory.create_pipeline_clip("agent-source")

        stored = store.get_video("agent-source")
        dashboard_video = pipeline_module.video_for_json(stored)
        response = self.client.get("/api/pipeline/videos/agent-source/captions")
        self.assertEqual(stages, ["captioning", "auto_clipping"])
        self.assertEqual(result["video_id"], "agent-source")
        self.assertEqual(stored["status"], "pending_approval")
        self.assertEqual(stored["source_caption_path"], str(captions))
        self.assertTrue(dashboard_video["has_source_captions"])
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Transcript.", response.data)
        response.close()

    def test_unapproved_clip_cannot_be_uploaded(self):
        path = self.add_video(status="pending_approval")
        with patch.object(pipeline_module, "youtube_service") as service:
            with self.assertRaises(ValueError):
                pipeline_module.upload_greenlit_video(store.get_video("trend-1"))
        service.assert_not_called()
        self.assertTrue(path.is_file())

    def test_daily_approved_upload_overage_is_spread_across_days(self):
        for index in range(6):
            self.add_video(f"overage-{index}")
        responses = [
            self.client.post(
                f"/api/pipeline/videos/overage-{index}/approve",
                json={"approved": True},
            )
            for index in range(6)
        ]
        self.assertTrue(all(response.status_code == 200 for response in responses))
        dates = [response.json["date"] for response in responses]
        self.assertEqual(dates.count(min(dates)), 5)
        self.assertEqual(len(set(dates)), 2)
        self.assertEqual(QuotaBudget().snapshot()["daily_upload_limit"], 5)

    def test_deferred_trend_tasks_can_be_decoded_for_scheduler(self):
        store.queue_api_task(
            "trend-task",
            "search.list",
            "trends",
            {
                "kind": "trend_scout",
                "region": "US",
                "category_id": "20",
                "language": "hinglish",
            },
            scheduled_at=(local_now() - timedelta(minutes=1)).isoformat(),
            status="queued",
        )
        tasks = store.due_api_tasks()
        self.assertEqual(tasks[0]["payload"]["kind"], "trend_scout")
        self.assertEqual(json.loads(json.dumps(tasks[0]["payload"]))["region"], "US")
        self.assertEqual(tasks[0]["payload"]["language"], "hinglish")

    def test_scheduler_retries_due_actions_from_local_queue(self):
        store.queue_api_task(
            "comment-task",
            "commentThreads.list",
            "engagement",
            {"kind": "comment_sync", "video_id": "uploaded-1"},
            scheduled_at=(local_now() - timedelta(minutes=1)).isoformat(),
            status="queued",
        )
        result = {"errors": [], "scouted": 0, "status": "idle"}
        with (
            patch.object(
                pipeline_module.engagement,
                "published_videos",
                return_value=[{"uploaded_video_id": "uploaded-1"}],
            ),
            patch.object(
                pipeline_module.engagement,
                "sync_video_comments",
                return_value=2,
            ) as sync,
        ):
            pipeline_module.retry_deferred_api_tasks(result)
        sync.assert_called_once_with("uploaded-1", task_id="comment-task")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(store.due_api_tasks(), [])


if __name__ == "__main__":
    unittest.main()
