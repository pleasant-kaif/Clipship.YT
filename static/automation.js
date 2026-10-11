const notice = document.querySelector("#studio-notice");
const dashboard = { videos: [], comments: [], published: [], settings: {}, quota: {} };
let settingsDirty = false;
let trendRefreshTimer = null;
let trendRefreshSequence = 0;
const TREND_SEARCH_TIMEOUT_MS = 10_000;

let noticeTimer = null;
function showNotice(message, error = false) {
    notice.textContent = message;
    notice.classList.toggle("error", error);
    notice.hidden = false;
    window.clearTimeout(noticeTimer);
    noticeTimer = window.setTimeout(() => {
        notice.hidden = true;
    }, error ? 10_000 : 6500);
}

function statusLabel(status) {
    return ({
        trending: "Rights check needed",
        source_uploaded: "Original video ready to upload",
        processing: "Preparing source",
        captioning: "Auto Captioning · Whisper transcription",
        auto_clipping: "Auto Clipping · selecting and rendering",
        pending_approval: "Awaiting your review",
        approved: "Approved · scheduled",
        uploading: "Uploading original video to YouTube",
        published: "Published on YouTube",
        upload_failed: "Upload needs your attention",
        rejected: "Rejected · not scheduled",
        failed: "Clip generation failed",
    })[status] || status;
}

function formatUnits(value) {
    return Number(value || 0).toLocaleString();
}

function makeEmpty(message) {
    const empty = document.createElement("div");
    empty.className = "empty-card";
    empty.textContent = message;
    return empty;
}

async function requestJson(url, options = {}) {
    const response = await fetch(url, options);
    let data;
    try {
        data = await response.json();
    } catch {
        throw new Error(`The local app returned an invalid response (${response.status}).`);
    }
    if (!response.ok) {
        if (data.quota) renderQuota(data.quota);
        throw new Error(data.error || `Request failed (${response.status}).`);
    }
    return data;
}

function renderQuota(quota) {
    dashboard.quota = quota;
    document.querySelector("#quota-date").textContent = `${quota.date} · ${quota.timezone}`;
    document.querySelector("#quota-used").textContent = formatUnits(quota.used);
    document.querySelector("#quota-cap").textContent = formatUnits(quota.daily_cap);
    document.querySelector("#quota-left").textContent =
        `${formatUnits(quota.remaining)} units remaining`;
    const percent = quota.daily_cap
        ? Math.min(100, Math.round(100 * quota.used / quota.daily_cap))
        : 0;
    document.querySelector("#quota-percent").textContent = `${percent}%`;
    const fill = document.querySelector("#quota-fill");
    fill.style.width = `${percent}%`;
    fill.parentElement.setAttribute("aria-valuenow", String(percent));
    document.querySelector("#tier-upload").textContent = formatUnits(quota.upload_reserve);
    document.querySelector("#quota-projection").textContent =
        `${formatUnits(quota.projected_queued_units_today)} units are projected for today's greenlit uploads. ` +
        `${quota.uploads_remaining} additional upload(s) fit today's remaining reserved quota. ` +
        `${quota.estimated_run_out}. ${quota.queued_pipeline_tasks} videos are processing, awaiting review, scheduled, or need attention.`;
    const warnings = document.querySelector("#quota-warnings");
    warnings.replaceChildren();
    quota.warnings.forEach(item => {
        const warning = document.createElement("div");
        warning.className = "quota-warning";
        warning.textContent = item.message;
        warnings.append(warning);
    });
    const overage = document.querySelector("#overage-warning");
    overage.textContent = quota.daily_cap === 10_000
        ? "Ten uploads in one day would need 16,000 units. At this setting Clipship spreads the approved queue across the next two days; request a free Google quota increase if you need more capacity."
        : `This higher cap supports up to ${quota.daily_upload_limit} upload(s) per day after trend and engagement reserves. Google must approve any quota increase before it applies.`;
}

function updateOAuth(status) {
    const badge = document.querySelector("#oauth-badge");
    if (status.youtube_connected) {
        badge.textContent = "YouTube connected";
        badge.className = "connection-badge connected";
    } else {
        badge.textContent = status.oauth_client_uploaded ? "Needs account consent" : "Not connected";
        badge.className = "connection-badge disconnected";
    }
    document.querySelector("#connect-youtube").disabled = !status.oauth_client_uploaded;
    document.querySelector("#disconnect-youtube").hidden = !status.youtube_connected;
}

function renderSettings(settings) {
    dashboard.settings = settings;
    if (settingsDirty) return;
    const categorySelect = document.querySelector("#setting-category");
    const languageSelect = document.querySelector("#setting-language");
    const indiaRegionSelect = document.querySelector("#setting-india-region");
    if (!categorySelect.options.length) {
        settings.categories.forEach(category => {
            const option = document.createElement("option");
            option.value = category.id;
            option.textContent = category.name;
            categorySelect.append(option);
        });
    }
    if (!languageSelect.options.length) {
        settings.trend_languages.forEach(language => {
            const option = document.createElement("option");
            option.value = language.id;
            option.textContent = language.name;
            languageSelect.append(option);
        });
    }
    if (!indiaRegionSelect.options.length) {
        settings.india_regions.forEach(region => {
            const option = document.createElement("option");
            option.value = region.id;
            option.textContent = region.name;
            indiaRegionSelect.append(option);
        });
    }
    document.querySelector("#setting-region").value = settings.region;
    categorySelect.value = settings.category_id;
    languageSelect.value = settings.trend_language;
    indiaRegionSelect.value = settings.india_region;
    document.querySelector("#setting-time").value = settings.schedule_time;
    document.querySelector("#setting-timezone").value = settings.timezone;
    document.querySelector("#setting-clips").value = settings.clips_per_day;
    document.querySelector("#setting-quota").value = settings.quota_cap;
    document.querySelector("#setting-privacy").value = settings.upload_privacy;
    document.querySelector("#setting-auto-scout").checked = settings.auto_scout === "true";
    updateIndiaRegionAvailability();
}

function updateIndiaRegionAvailability() {
    const isIndia = document.querySelector("#setting-region").value.trim().toUpperCase() === "IN";
    const indiaRegionSelect = document.querySelector("#setting-india-region");
    indiaRegionSelect.disabled = !isIndia;
    if (!isIndia) indiaRegionSelect.value = "all";
}

function makeSourceUploadForm(videoId, buttonText) {
    const form = document.createElement("form");
    form.className = "trend-upload-form";
    form.dataset.videoId = videoId;
    const rights = document.createElement("label");
    rights.className = "rights-confirm";
    const check = document.createElement("input");
    check.type = "checkbox";
    check.name = "rights_confirmed";
    check.value = "true";
    check.required = true;
    const checkText = document.createElement("span");
    checkText.textContent = "I own this video or have the rights to publish it on my YouTube channel.";
    rights.append(check, checkText);
    const picker = document.createElement("label");
    picker.className = "source-picker";
    picker.textContent = "Choose a local video (MP4, MOV, MKV, WebM)";
    const file = document.createElement("input");
    file.type = "file";
    file.name = "source_file";
    file.accept = ".mp4,.mov,.mkv,.webm,.m4v,video/mp4,video/quicktime,video/webm";
    file.required = true;
    picker.append(file);
    const button = document.createElement("button");
    button.type = "submit";
    button.className = "button button-secondary";
    button.textContent = buttonText;
    form.append(rights, picker, button);
    form.addEventListener("submit", event => submitSource(event, form));
    return form;
}

function renderTrending() {
    const grid = document.querySelector("#trending-grid");
    grid.replaceChildren();
    const eligible = dashboard.videos.filter(matchesSelectedTrend);
    const busy = dashboard.videos.filter(video =>
        video.has_local_source &&
        ["source_uploaded", "processing", "captioning", "auto_clipping"].includes(video.status),
    );
    document.querySelector("#trending-count").textContent =
        `${eligible.length} trend${eligible.length === 1 ? "" : "s"} ready to review · ` +
        `${busy.length} local source${busy.length === 1 ? "" : "s"} processing.`;
    if (!eligible.length && !busy.length) {
        grid.append(makeEmpty(
            "No trends loaded yet. Connect YouTube and scout your selected region and category.",
        ));
        return;
    }
    eligible.forEach(video => grid.append(renderTrendCard(video)));
    busy.forEach(video => {
        const item = document.createElement("article");
        item.className = "scheduled-item";
        const image = document.createElement("img");
        image.src = video.thumbnail || "";
        image.alt = "";
        const copy = document.createElement("div");
        copy.className = "scheduled-main";
        const title = document.createElement("strong");
        title.textContent = video.title;
        const details = document.createElement("small");
        details.textContent = video.error || statusLabel(video.status);
        copy.append(title, details);
        if (video.has_source_captions) {
            const captionsLink = document.createElement("a");
            captionsLink.className = "text-button";
            captionsLink.href = video.source_captions_url;
            captionsLink.textContent = "Download full-video captions (.srt)";
            copy.append(captionsLink);
        }
        const status = document.createElement("span");
        status.className = `status-pill status-${video.status}`;
        status.textContent = statusLabel(video.status);
        item.append(image, copy, status);
        grid.append(item);
    });
}

function matchesSelectedTrend(video) {
    const region = document.querySelector("#setting-region").value.toUpperCase();
    const categoryId = document.querySelector("#setting-category").value;
    const language = document.querySelector("#setting-language").value;
    const indiaRegion = document.querySelector("#setting-india-region").value;
    const videoCategoryId = String(video.trend_category_id || video.category_id || "");
    return video.status === "trending" &&
        videoCategoryId === categoryId &&
        (!video.trend_region || video.trend_region === region) &&
        (!video.trend_language || video.trend_language === language) &&
        (!video.india_region || video.india_region === indiaRegion);
}

function hasCurrentTrends() {
    const region = document.querySelector("#setting-region").value.toUpperCase();
    const categoryId = document.querySelector("#setting-category").value;
    const language = document.querySelector("#setting-language").value;
    const indiaRegion = document.querySelector("#setting-india-region").value;
    return dashboard.videos.some(video =>
        video.status === "trending" &&
        String(video.trend_category_id || video.category_id || "") === categoryId &&
        video.trend_region === region &&
        video.trend_language === language &&
        video.india_region === indiaRegion &&
        video.statistics_available &&
        (video.duration_available || ["live", "upcoming"].includes(video.live_status)),
    );
}

function formatVideoDuration(seconds) {
    const total = Math.floor(Number(seconds));
    if (!Number.isFinite(total) || total <= 0) return "";
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const remainder = total % 60;
    return hours
        ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`
        : `${minutes}:${String(remainder).padStart(2, "0")}`;
}

function renderTrendCard(video) {
    const card = document.createElement("article");
    card.className = "trending-card";
    const thumb = document.createElement("img");
    thumb.className = "trend-thumb";
    thumb.src = video.thumbnail || "";
    thumb.alt = "";
    thumb.loading = "lazy";
    const body = document.createElement("div");
    body.className = "trend-card-body";
    const title = document.createElement("h3");
    title.textContent = video.title;
    const meta = document.createElement("p");
    meta.className = "trend-meta";
    const details = [];
    if (video.channel_title) details.push(video.channel_title);
    const duration = formatVideoDuration(video.duration_seconds);
    if (duration) {
        details.push(duration);
    } else if (video.live_status === "live") {
        details.push("Live");
    } else if (video.live_status === "upcoming") {
        details.push("Upcoming live stream");
    } else {
        details.push("Duration unavailable");
    }
    meta.textContent = details.join(" · ");
    const stats = document.createElement("p");
    stats.className = "trend-stats";
    const formatCount = count =>
        count === null || count === undefined ? "Not public" : formatUnits(count);
    stats.textContent =
        `${formatCount(video.view_count)} views · ` +
        `${formatCount(video.comment_count)} comments · ` +
        `${formatCount(video.like_count)} likes`;
    const languageHint = document.createElement("p");
    languageHint.className = "trend-meta";
    languageHint.textContent = video.language_match
        ? `${video.trend_language_name || "Selected language"} · best-effort text match`
        : `${video.trend_language_name || "Selected language"} · language not identified from text`;
    if (video.india_region_name && video.india_region !== "all") {
        const regionHint = document.createElement("p");
        regionHint.className = "trend-meta";
        const matches = video.india_region_matches || [];
        regionHint.textContent = matches.length
            ? `${video.india_region_name} keyword match: ${matches.join(", ")}`
            : `${video.india_region_name} search · location not confirmed in video text`;
        body.append(title, meta, stats, languageHint, regionHint);
    } else {
        body.append(title, meta, stats, languageHint);
    }
    const link = document.createElement("a");
    link.className = "trend-video-link";
    link.href = video.url || `https://www.youtube.com/watch?v=${encodeURIComponent(video.video_id)}`;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = link.href;
    link.setAttribute("aria-label", `Open video on YouTube: ${link.href}`);
    const source = document.createElement("p");
    source.className = "rights-warning-small";
    source.textContent =
        "Import your authorized local video and upload the original unchanged. No duration minimum; YouTube's own upload limits still apply.";
    body.append(link, source);
    body.append(makeSourceUploadForm(video.video_id, "Import & publish original video →"));
    card.append(thumb, body);
    return card;
}

const PIPELINE_STEPS = [
    "Import",
    "SEO Description",
    "YouTube upload",
    "Published",
];
const PIPELINE_STATUSES = new Set([
    "source_uploaded", "processing", "captioning", "auto_clipping",
    "uploading", "pending_approval", "approved", "upload_failed", "published",
    "failed", "rejected",
]);

function pipelineStepIndex(video) {
    if (video.upload_mode === "direct") {
        if (video.status === "published") return 3;
        if (video.seo_status === "ready") return 2;
        if (["uploading", "upload_failed"].includes(video.status)) return 1;
        return 0;
    }
    switch (video.status) {
        case "source_uploaded":
        case "processing":
            return 0;
        case "captioning":
            return 1;
        case "auto_clipping":
            return 2;
        case "pending_approval":
        case "rejected":
            return 3;
        case "approved":
            return video.seo_status === "writing" ? 5 : 4;
        case "upload_failed":
            return video.seo_status === "ready" ? 6 : 5;
        case "published":
            return 7;
        case "failed":
            return /at least 60 seconds|60 seconds long/i.test(video.error || "") ||
                video.has_source_captions ? 2 : 1;
        default:
            return 0;
    }
}

function formatPipelineTime(value) {
    if (!value) return "Time not recorded";
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return value;
    try {
        return new Intl.DateTimeFormat(undefined, {
            dateStyle: "medium",
            timeStyle: "short",
            timeZone: dashboard.settings.timezone,
        }).format(date);
    } catch {
        return date.toLocaleString();
    }
}

function scrollToPipelineCard(sectionId, videoId) {
    const section = document.querySelector(`#${sectionId}`);
    const card = Array.from(section?.querySelectorAll("[data-video-id]") || [])
        .find(item => item.dataset.videoId === videoId);
    (card || section)?.scrollIntoView({ behavior: "smooth", block: "center" });
}

function renderPipeline() {
    const grid = document.querySelector("#pipeline-grid");
    const summary = document.querySelector("#pipeline-summary");
    grid.replaceChildren();
    summary.replaceChildren();
    const videos = dashboard.videos.filter(video =>
        PIPELINE_STATUSES.has(video.status) || video.has_local_source,
    );
    videos.sort((left, right) => {
        const leftPriority = left.status === "rejected" || left.status === "published" ? 1 : 0;
        const rightPriority = right.status === "rejected" || right.status === "published" ? 1 : 0;
        return leftPriority - rightPriority ||
            String(right.updated_at || "").localeCompare(String(left.updated_at || ""));
    });
    const counts = [
        ["Processing", ["processing", "captioning", "auto_clipping"]],
        ["Ready to upload", ["source_uploaded"]],
        ["Uploading", ["uploading"]],
        ["Needs review", ["pending_approval"]],
        ["Scheduled", ["approved"]],
        ["Published", ["published"]],
        ["Needs attention", ["failed", "upload_failed"]],
    ];
    counts.forEach(([label, statuses]) => {
        const count = videos.filter(video => statuses.includes(video.status)).length;
        const badge = document.createElement("span");
        badge.className = "pipeline-count";
        badge.textContent = `${label}: ${count}`;
        summary.append(badge);
    });
    document.querySelector("#pipeline-updated").textContent =
        `Live · refreshed ${new Date().toLocaleTimeString()}`;

    if (!videos.length) {
        grid.append(makeEmpty(
            "No local sources or scheduled clips yet. Choose authorized local footage from a trend to start processing.",
        ));
        return;
    }

    videos.forEach(video => {
        const card = document.createElement("article");
        card.className = `pipeline-card pipeline-${video.status}`;
        card.dataset.videoId = video.video_id;
        const heading = document.createElement("div");
        heading.className = "pipeline-card-heading";
        const title = document.createElement("h3");
        title.textContent = video.title;
        const badge = document.createElement("span");
        badge.className = `status-pill status-${video.status}`;
        badge.textContent = statusLabel(video.status);
        heading.append(title, badge);

        const timeline = document.createElement("ol");
        timeline.className = "pipeline-steps";
        const activeIndex = pipelineStepIndex(video);
        if (video.upload_mode === "direct") {
            timeline.classList.add("pipeline-steps-direct");
        }
        const steps = video.upload_mode === "direct"
            ? PIPELINE_STEPS
            : ["Source ready", "Auto Captioning", "Auto Clipping", "Review", "Scheduled", "SEO Description", "YouTube upload", "Published"];
        steps.forEach((step, index) => {
            const item = document.createElement("li");
            item.className = index < activeIndex
                ? "complete"
                : index === activeIndex
                    ? video.status === "failed" || video.status === "upload_failed"
                        ? "failed"
                        : "current"
                    : "";
            const label = document.createElement("span");
            label.textContent = step;
            item.append(label);
            timeline.append(item);
        });

        const details = document.createElement("p");
        details.className = "pipeline-detail";
        if (video.upload_mode === "direct" && video.status === "source_uploaded") {
            details.textContent = video.error ||
                "Original video is ready to upload. Confirm rights were checked when importing.";
        } else if (video.status === "uploading") {
            details.textContent = video.seo_status === "writing"
                ? "SEO Description Agent is using local AI to prepare video-specific, trend-aware copy before publishing."
                : video.upload_mode === "direct"
                ? "Uploading your original video to YouTube. Large files can take several minutes."
                : "Uploading your approved clip to YouTube.";
        } else if (video.status === "approved") {
            details.textContent = video.seo_status === "writing"
                ? "SEO Description Agent is preparing video-specific, trend-aware copy before upload."
                : `Upload scheduled for ${formatPipelineTime(video.scheduled_at)} ` +
                    `(${video.upload_privacy || dashboard.settings.upload_privacy} visibility). ` +
                    "The SEO Description Agent prepares the description before upload; Clipship must be running and YouTube connected.";
        } else if (video.status === "published") {
            details.textContent = `Published ${video.published_url || "to YouTube"}.`;
        } else if (video.status === "upload_failed" || video.status === "failed") {
            details.textContent = video.error || "The last step failed without a reported reason.";
        } else {
            details.textContent = statusLabel(video.status);
        }
        if (video.seo_status === "ready" && video.seo_description) {
            const seoDraft = document.createElement("details");
            seoDraft.className = "pipeline-seo-description";
            const summary = document.createElement("summary");
            summary.textContent = "SEO description drafted by local AI";
            const description = document.createElement("p");
            description.textContent = video.seo_description;
            seoDraft.append(summary, description);
            card.append(heading, timeline, details, seoDraft);
        } else {
            card.append(heading, timeline, details);
        }
        const updated = document.createElement("small");
        updated.className = "pipeline-updated";
        updated.textContent = `Last updated ${formatPipelineTime(video.updated_at)}`;
        card.append(updated);

        if (
            video.upload_mode === "direct" &&
            video.status === "source_uploaded" &&
            video.has_local_source
        ) {
            const actions = document.createElement("div");
            actions.className = "pipeline-actions";
            const publish = document.createElement("button");
            publish.type = "button";
            publish.className = "button button-primary";
            publish.textContent = "Publish saved original video now →";
            publish.addEventListener("click", async () => {
                publish.disabled = true;
                try {
                    await requestJson(
                        `/api/pipeline/videos/${encodeURIComponent(video.video_id)}/publish-source`,
                        { method: "POST" },
                    );
                    showNotice("Original video upload started. Follow its progress here.");
                    await loadDashboard();
                } catch (error) {
                    showNotice(error.message, true);
                    publish.disabled = false;
                }
            });
            actions.append(publish);
            card.append(actions);
        } else if (
            video.status === "failed" &&
            video.has_local_source &&
            !video.uploaded_video_id
        ) {
            const actions = document.createElement("div");
            actions.className = "pipeline-actions";
            const explanation = document.createElement("p");
            explanation.className = "pipeline-guidance";
            explanation.textContent =
                "This source can be uploaded unchanged. Clipship no longer requires a replacement file or minimum video duration.";
            const publish = document.createElement("button");
            publish.type = "button";
            publish.className = "button button-primary";
            publish.textContent = "Publish saved original video now →";
            publish.addEventListener("click", async () => {
                publish.disabled = true;
                try {
                    await requestJson(
                        `/api/pipeline/videos/${encodeURIComponent(video.video_id)}/publish-source`,
                        { method: "POST" },
                    );
                    showNotice("Original video upload started. Follow its progress here.");
                    await loadDashboard();
                } catch (error) {
                    showNotice(error.message, true);
                    publish.disabled = false;
                }
            });
            actions.append(explanation, publish);
            card.append(actions);
        } else if (video.status === "pending_approval") {
            const review = document.createElement("button");
            review.type = "button";
            review.className = "text-button";
            review.textContent = "Review and approve this clip ↓";
            review.addEventListener("click", () =>
                scrollToPipelineCard("review-grid", video.video_id),
            );
            card.append(review);
        } else if (["approved", "upload_failed"].includes(video.status)) {
            const schedule = document.createElement("button");
            schedule.type = "button";
            schedule.className = "text-button";
            schedule.textContent = video.status === "upload_failed"
                ? "Open upload retry options ↓"
                : "View upload queue ↓";
            schedule.addEventListener("click", () =>
                scrollToPipelineCard("scheduled-list", video.video_id),
            );
            card.append(schedule);
        } else if (video.status === "published" && video.published_url) {
            const open = document.createElement("a");
            open.className = "text-button";
            open.href = video.published_url;
            open.target = "_blank";
            open.rel = "noopener noreferrer";
            open.textContent = "Open published video ↗";
            card.append(open);
        }
        grid.append(card);
    });
}

function renderReviews() {
    const grid = document.querySelector("#review-grid");
    grid.replaceChildren();
    const reviews = dashboard.videos.filter(video => video.status === "pending_approval");
    document.querySelector("#review-count").textContent =
        `${reviews.length} awaiting review`;
    if (!reviews.length) {
        grid.append(makeEmpty(
            "No finished clips are waiting for approval. Current processing stages and any errors appear in the pipeline tracker above.",
        ));
        return;
    }
    reviews.forEach(video => {
        const card = document.createElement("article");
        card.className = "review-card";
        card.dataset.videoId = video.video_id;
        const preview = document.createElement("video");
        preview.src = video.preview_url;
        preview.controls = true;
        preview.preload = "metadata";
        preview.playsInline = true;
        const body = document.createElement("div");
        body.className = "review-card-body";
        const header = document.createElement("div");
        header.className = "review-card-header";
        const title = document.createElement("h3");
        title.textContent = video.title;
        const badge = document.createElement("span");
        badge.className = `status-pill status-${video.status}`;
        badge.textContent = statusLabel(video.status);
        header.append(title, badge);
        const duration = document.createElement("p");
        duration.textContent =
            `1:00 · 9:16 vertical · ${formatTime(video.start_seconds || 0)} start · Local Whisper captions`;
        const captions = document.createElement("details");
        const captionSummary = document.createElement("summary");
        captionSummary.className = "text-button";
        captionSummary.textContent = "Review burned-in captions";
        const captionText = document.createElement("pre");
        captionText.className = "review-caption";
        captionText.textContent = video.caption_text || "No caption transcript stored.";
        captions.append(captionSummary, captionText);
        if (video.has_source_captions) {
            const sourceCaptions = document.createElement("a");
            sourceCaptions.className = "text-button";
            sourceCaptions.href = video.source_captions_url;
            sourceCaptions.textContent = "Download full-video captions (.srt)";
            body.append(sourceCaptions);
        }
        const consent = document.createElement("label");
        consent.className = "rights-confirm";
        const check = document.createElement("input");
        check.type = "checkbox";
        check.required = true;
        const consentText = document.createElement("span");
        consentText.textContent =
            "I reviewed this clip and confirm I own or can publish its video, audio, and captions.";
        consent.append(check, consentText);
        const actions = document.createElement("div");
        actions.className = "approval-actions";
        const approve = document.createElement("button");
        approve.type = "button";
        approve.className = "button button-primary";
        approve.textContent = "Approve & schedule upload";
        approve.disabled = !video.rights_confirmed;
        check.addEventListener("change", () => {
            approve.disabled = !check.checked || !video.rights_confirmed;
        });
        approve.addEventListener("click", () => approveVideo(video.video_id));
        const reject = document.createElement("button");
        reject.type = "button";
        reject.className = "text-button";
        reject.textContent = "Reject";
        reject.addEventListener("click", () => rejectVideo(video.video_id));
        actions.append(approve, reject);
        body.append(header, duration, captions, consent, actions);
        card.append(preview, body);
        grid.append(card);
    });
}

function renderScheduled() {
    const list = document.querySelector("#scheduled-list");
    list.replaceChildren();
    const videos = dashboard.videos.filter(video =>
        ["approved", "published", "upload_failed"].includes(video.status),
    );
    if (!videos.length) {
        list.append(makeEmpty("No clips are scheduled or published yet."));
        return;
    }
    videos.forEach(video => {
        const item = document.createElement("article");
        item.className = "scheduled-item";
        item.dataset.videoId = video.video_id;
        const image = document.createElement("img");
        image.src = video.thumbnail || "";
        image.alt = "";
        const main = document.createElement("div");
        main.className = "scheduled-main";
        const title = document.createElement("strong");
        title.textContent = video.title;
        const date = document.createElement("small");
        date.textContent = video.status === "published"
            ? `Published · ${video.published_url}`
            : video.status === "upload_failed"
                ? `Upload failed: ${video.error || "YouTube did not confirm the upload."} Check your channel before retrying.`
                : `Scheduled ${formatPipelineTime(video.scheduled_at)} · ${video.upload_privacy || dashboard.settings.upload_privacy} visibility`;
        main.append(title, date);
        const actions = document.createElement("div");
        actions.className = "scheduled-actions";
        const status = document.createElement("span");
        status.className = `status-pill status-${video.status}`;
        status.textContent = statusLabel(video.status);
        actions.append(status);
        if (video.status === "published") {
            const like = document.createElement("button");
            like.type = "button";
            like.className = "text-button";
            like.textContent = "Like this video · 50 units";
            like.addEventListener("click", () => likeVideo(video.video_id));
            actions.append(like);
            const open = document.createElement("a");
            open.className = "text-button";
            open.href = video.published_url;
            open.target = "_blank";
            open.rel = "noopener noreferrer";
            open.textContent = "Open ↗";
            actions.append(open);
        } else if (video.status === "upload_failed") {
            const retry = document.createElement("button");
            retry.type = "button";
            retry.className = "text-button";
            retry.textContent = "Verify channel, then retry · 1,600 units";
            retry.addEventListener("click", async () => {
                if (!window.confirm(
                    "Check your YouTube Studio first to confirm this video was not uploaded. " +
                    "Retrying can create a duplicate if YouTube accepted the earlier request.",
                )) return;
                try {
                    const result = await requestJson(
                        `/api/pipeline/videos/${encodeURIComponent(video.video_id)}/retry-upload`,
                        { method: "POST" },
                    );
                    showNotice(result.direct_upload
                        ? "Retry upload started. Check YouTube Studio if the previous attempt may have been accepted."
                        : `Retry approved and scheduled for ${result.date}, slot ${result.slot}.`);
                    await loadDashboard();
                } catch (error) {
                    showNotice(error.message, true);
                }
            });
            actions.append(retry);
        }
        item.append(image, main, actions);
        list.append(item);
    });
}

function renderEngagement() {
    const grid = document.querySelector("#engagement-grid");
    grid.replaceChildren();
    const published = new Map(
        dashboard.published.map(video => [video.uploaded_video_id, video]),
    );
    if (!published.size) {
        grid.append(makeEmpty("Connect your channel and publish an approved clip to manage comments."));
        return;
    }
    if (!dashboard.comments.length) {
        grid.append(makeEmpty("No new top-level comments yet. Refresh to check for comments on your published clips."));
        return;
    }
    dashboard.comments.forEach(comment => {
        const video = published.get(comment.video_id);
        if (!video) return;
        const item = document.createElement("article");
        item.className = "engagement-item";
        const top = document.createElement("div");
        top.className = "engagement-top";
        const author = document.createElement("strong");
        author.textContent = `${comment.author} on ${video.title}`;
        const status = document.createElement("small");
        status.textContent = comment.reply_status === "sent" ? "Replied" : "New · review before sending";
        if (comment.reply_status === "queued") {
            status.textContent = "Approved reply queued for next available quota day";
        }
        top.append(author, status);
        const text = document.createElement("p");
        text.className = "engagement-comment";
        text.textContent = comment.text;
        item.append(top, text);
        if (comment.reply_status === "queued") {
            const queued = document.createElement("p");
            queued.className = "queued-reply";
            queued.textContent =
                `Your reviewed reply will be sent automatically after quota resets at the next scheduled run: ${comment.ai_reply}`;
            item.append(queued);
        } else if (comment.reply_status !== "sent") {
            const textarea = document.createElement("textarea");
            textarea.className = "engagement-reply";
            textarea.maxLength = 500;
            textarea.placeholder = comment.ai_reply || "Draft a reply with local Ollama, or write your own…";
            textarea.value = comment.ai_reply || "";
            const actions = document.createElement("div");
            actions.className = "engagement-actions";
            const draft = document.createElement("button");
            draft.type = "button";
            draft.className = "button button-secondary";
            draft.textContent = "Draft on local Ollama";
            draft.addEventListener("click", async () => {
                draft.disabled = true;
                try {
                    const result = await requestJson(`/api/engagement/${encodeURIComponent(comment.comment_id)}/draft`, { method: "POST" });
                    textarea.value = result.reply;
                    showNotice("Local AI reply drafted. Review and edit it before sending.");
                } catch (error) {
                    showNotice(error.message, true);
                } finally {
                    draft.disabled = false;
                }
            });
            const send = document.createElement("button");
            send.type = "button";
            send.className = "button button-primary";
            send.textContent = "Review & send reply · 50 units";
            send.addEventListener("click", async () => {
                send.disabled = true;
                try {
                    await requestJson(`/api/engagement/${encodeURIComponent(comment.comment_id)}/reply`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ reply: textarea.value }),
                    });
                    showNotice("Your approved reply was sent.");
                    await loadDashboard();
                } catch (error) {
                    showNotice(error.message, true);
                    send.disabled = false;
                }
            });
            actions.append(draft, send);
            item.append(textarea, actions);
        }
        grid.append(item);
    });
}

function formatTime(seconds) {
    const value = Math.floor(seconds);
    return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, "0")}`;
}

function renderDashboard() {
    renderQuota(dashboard.quota);
    renderSettings({
        ...dashboard.settings,
        categories: dashboard.categories,
        trend_languages: dashboard.trendLanguages,
        india_regions: dashboard.indiaRegions,
    });
    updateOAuth(dashboard.youtube);
    renderTrending();
    renderPipeline();
    renderReviews();
    renderScheduled();
    renderEngagement();
}

async function loadDashboard(showErrors = false, expectedTrendRefresh = null) {
    try {
        const state = await requestJson("/api/dashboard");
        if (expectedTrendRefresh !== null && expectedTrendRefresh !== trendRefreshSequence) return;
        Object.assign(dashboard, {
            videos: state.videos,
            comments: state.engagement.comments,
            published: state.engagement.published_videos,
            settings: state.settings,
            quota: state.quota,
            youtube: state.youtube,
            categories: state.categories,
            trendLanguages: state.trend_languages,
            indiaRegions: state.india_regions,
        });
        renderDashboard();
        if (showErrors) showNotice("Dashboard refreshed.");
    } catch (error) {
        if (showErrors) showNotice(error.message, true);
    }
}

async function loadOllamaStatus() {
    const badge = document.querySelector("#ollama-badge");
    const checkBtn = document.querySelector("#refresh-ollama");
    const startBtn = document.querySelector("#start-ollama");
    checkBtn.disabled = true;
    try {
        const status = await requestJson("/api/ollama/status");
        // Update badge text and colour
        if (!status.available) {
            badge.textContent = "Ollama is not running";
            badge.className = "connection-badge disconnected";
        } else if (!status.model_installed) {
            badge.textContent = `Ollama running · ${status.configured_model} not installed`;
            badge.className = "connection-badge disconnected";
            showNotice(
                `Ollama is running but the model "${status.configured_model}" is not installed. ` +
                `Open a terminal and run: ollama pull ${status.configured_model}`,
                true,
            );
        } else {
            badge.textContent = `Ollama · ${status.configured_model} ready`;
            badge.className = "connection-badge connected";
        }
        // Show/hide the Start button
        if (startBtn) {
            startBtn.hidden = !status.can_start;
        }
    } catch (error) {
        badge.textContent = "Ollama status unavailable";
        badge.className = "connection-badge disconnected";
        showNotice(error.message, true);
    } finally {
        checkBtn.disabled = false;
    }
}

async function startOllama() {
    const badge = document.querySelector("#ollama-badge");
    const startBtn = document.querySelector("#start-ollama");
    const checkBtn = document.querySelector("#refresh-ollama");
    startBtn.disabled = true;
    checkBtn.disabled = true;
    badge.textContent = "Starting Ollama…";
    badge.className = "connection-badge";
    try {
        const controller = new AbortController();
        const timeout = window.setTimeout(() => controller.abort(), 13_000);
        let result;
        try {
            result = await requestJson("/api/ollama/start", {
                method: "POST",
                signal: controller.signal,
            });
        } finally {
            window.clearTimeout(timeout);
        }
        showNotice(result.message || (result.available ? "Ollama started." : "Ollama did not respond in time."), !result.available);
    } catch (error) {
        showNotice(
            error.name === "AbortError"
                ? "Ollama start request timed out. It may still be loading — click Check in a few seconds."
                : error.message,
            true,
        );
    } finally {
        startBtn.disabled = false;
        checkBtn.disabled = false;
        // Refresh the badge regardless of outcome
        await loadOllamaStatus();
    }
}

document.querySelector("#refresh-ollama").addEventListener("click", loadOllamaStatus);
document.querySelector("#start-ollama")?.addEventListener("click", startOllama);

async function submitSource(event, form) {
    event.preventDefault();
    const data = new FormData(form);
    const file = data.get("source_file");
    if (!file || !file.size) {
        showNotice("Choose a local source video file.", true);
        return;
    }
    if (file.size > 2 * 1024 * 1024 * 1024) {
        showNotice("Source files must be under 2 GB.", true);
        return;
    }
    const button = form.querySelector("button");
    const originalButtonText = button.textContent;
    button.disabled = true;
    button.textContent = "Importing video…";
    try {
        const videoId = form.dataset.videoId;
        const result = await requestJson(`/api/pipeline/videos/${encodeURIComponent(videoId)}/source`, {
            method: "POST",
            body: data,
        });
        showNotice(result.direct_upload
            ? "Original video imported. Uploading it to YouTube unchanged; no minimum duration or replacement source is required."
            : "Original video imported. Connect YouTube, then publish the saved video from the pipeline tracker.");
        await loadDashboard();
    } catch (error) {
        showNotice(error.message, true);
        button.disabled = false;
        button.textContent = originalButtonText;
    }
}

async function scoutNow(expectedTrendRefresh = ++trendRefreshSequence, automatic = false) {
    const button = document.querySelector("#run-scout");
    const region = document.querySelector("#setting-region").value.toUpperCase();
    const categoryId = document.querySelector("#setting-category").value;
    const language = document.querySelector("#setting-language").value;
    const indiaRegion = document.querySelector("#setting-india-region").value;
    if (!/^[A-Z]{2}$/.test(region)) {
        if (expectedTrendRefresh === trendRefreshSequence) {
            showNotice("Country/region must be a two-letter country code.", true);
        }
        return;
    }
    if (!automatic) {
        button.disabled = true;
        button.textContent = "Searching trends (language match is best-effort)…";
    } else {
        showNotice(`Searching ${region} trends; language and Indian-region matches are best-effort…`);
    }
    const controller = new AbortController();
    const timeoutId = window.setTimeout(
        () => controller.abort(),
        TREND_SEARCH_TIMEOUT_MS,
    );
    try {
        const result = await requestJson("/api/trends/scout", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            signal: controller.signal,
            body: JSON.stringify({
                region,
                category_id: categoryId,
                trend_language: language,
                india_region: indiaRegion,
                force_refresh: !automatic,
            }),
        });
        if (expectedTrendRefresh !== trendRefreshSequence) return;
        renderQuota(result.quota);
        dashboard.settings.region = region;
        dashboard.settings.category_id = categoryId;
        dashboard.settings.trend_language = language;
        dashboard.settings.india_region = indiaRegion;
        const quotaNote = result.cached
            ? " Reused results cached for up to 15 minutes (0 API units)."
            : ` Fresh search used ${result.quota_cost} YouTube API quota units.`;
        showNotice(
            `Found ${result.videos.length} ${document.querySelector("#setting-language").selectedOptions[0].textContent} ` +
            `video results for ${region} (language/region matching is best-effort).${quotaNote}`,
        );
        const resultIds = new Set(result.videos.map(video => video.video_id));
        dashboard.videos = dashboard.videos.filter(video =>
            video.status !== "trending" ||
            (!matchesSelectedTrend(video) && !resultIds.has(video.video_id)),
        );
        const existingIds = new Set(dashboard.videos.map(video => video.video_id));
        dashboard.videos.push(
            ...result.videos.filter(video => !existingIds.has(video.video_id)),
        );
        renderTrending();
    } catch (error) {
        if (expectedTrendRefresh === trendRefreshSequence) {
            showNotice(
                error.name === "AbortError"
                    ? "Trending search exceeded the 10-second limit. Try again shortly."
                    : error.message,
                true,
            );
        }
    } finally {
        window.clearTimeout(timeoutId);
        if (expectedTrendRefresh === trendRefreshSequence && !automatic) {
            button.disabled = false;
            button.textContent = "Search videos now · up to 101 units →";
        }
    }
}

function scheduleTrendRefresh() {
    window.clearTimeout(trendRefreshTimer);
    const sequence = ++trendRefreshSequence;
    const button = document.querySelector("#run-scout");
    button.disabled = false;
    button.textContent = "Search videos now · up to 101 units →";
    trendRefreshTimer = window.setTimeout(() => scoutNow(sequence, true), 350);
}

async function approveVideo(videoId) {
    try {
        const result = await requestJson(`/api/pipeline/videos/${encodeURIComponent(videoId)}/approve`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ approved: true }),
        });
        showNotice(
            `Approved and scheduled for ${formatPipelineTime(result.scheduled_at)} ` +
            `(${result.timezone}, slot ${result.slot}, ${result.privacy} visibility). ` +
            "Clipship must be running and YouTube connected at that time.",
        );
        await loadDashboard();
    } catch (error) {
        showNotice(error.message, true);
    }
}

async function rejectVideo(videoId) {
    try {
        await requestJson(`/api/pipeline/videos/${encodeURIComponent(videoId)}/approve`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ approved: false }),
        });
        showNotice("Clip rejected. It will not be uploaded.");
        await loadDashboard();
    } catch (error) {
        showNotice(error.message, true);
    }
}

async function startConnection() {
    try {
        const response = await requestJson("/api/youtube/auth/start", { method: "POST" });
        window.location.assign(response.authorization_url);
    } catch (error) {
        showNotice(error.message, true);
    }
}

document.querySelector("#oauth-file-form").addEventListener("submit", async event => {
    event.preventDefault();
    const input = document.querySelector("#oauth-file");
    if (!input.files[0]) {
        showNotice("Choose the Google OAuth client-secret JSON file first.", true);
        return;
    }
    const form = new FormData();
    form.append("client_secret", input.files[0]);
    try {
        await requestJson("/api/youtube/client-secret", { method: "POST", body: form });
        showNotice("Google OAuth client saved locally. Now connect your YouTube account.");
        await loadDashboard();
    } catch (error) {
        showNotice(error.message, true);
    }
});

document.querySelector("#connect-youtube").addEventListener("click", startConnection);

document.querySelector("#disconnect-youtube").addEventListener("click", async () => {
    if (!window.confirm("Disconnect the saved YouTube refresh token from this computer?")) return;
    try {
        await requestJson("/api/youtube/disconnect", { method: "POST" });
        showNotice("YouTube account disconnected.");
        await loadDashboard();
    } catch (error) {
        showNotice(error.message, true);
    }
});

document.querySelector("#settings-form").addEventListener("submit", async event => {
    event.preventDefault();
    const settings = {
        region: document.querySelector("#setting-region").value,
        category_id: document.querySelector("#setting-category").value,
        trend_language: document.querySelector("#setting-language").value,
        india_region: document.querySelector("#setting-india-region").value,
        schedule_time: document.querySelector("#setting-time").value,
        timezone: document.querySelector("#setting-timezone").value,
        clips_per_day: Number(document.querySelector("#setting-clips").value),
        quota_cap: Number(document.querySelector("#setting-quota").value),
        upload_privacy: document.querySelector("#setting-privacy").value,
        auto_scout: document.querySelector("#setting-auto-scout").checked,
    };
    try {
        const result = await requestJson("/api/settings", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(settings),
        });
        dashboard.settings = result.settings;
        settingsDirty = false;
        renderQuota(result.quota);
        showNotice("Automation schedule and quota budget saved.");
        await loadDashboard();
    } catch (error) {
        showNotice(error.message, true);
    }
});
document.querySelector("#settings-form").addEventListener("input", () => {
    settingsDirty = true;
});
document.querySelector("#settings-form").addEventListener("change", () => {
    settingsDirty = true;
});

document.querySelector("#setting-region").addEventListener("change", scheduleTrendRefresh);
document.querySelector("#setting-region").addEventListener("input", updateIndiaRegionAvailability);
document.querySelector("#setting-category").addEventListener("change", scheduleTrendRefresh);
document.querySelector("#setting-language").addEventListener("change", scheduleTrendRefresh);
document.querySelector("#setting-india-region").addEventListener("change", scheduleTrendRefresh);
document.querySelector("#run-scout").addEventListener("click", scoutNow);
document.querySelector("#refresh-dashboard").addEventListener("click", () => loadDashboard(true));
document.querySelector("#sync-comments").addEventListener("click", async event => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
        const result = await requestJson("/api/engagement/sync", { method: "POST" });
        showNotice(`Checked ${result.videos_checked} of your published clips. ${result.comments_seen} comment thread(s) loaded.`);
        await loadDashboard();
    } catch (error) {
        showNotice(error.message, true);
    } finally {
        button.disabled = false;
    }
});

async function likeVideo(videoId) {
    try {
        await requestJson(`/api/engagement/videos/${encodeURIComponent(videoId)}/like`, {
            method: "POST",
        });
        showNotice("Liked your YouTube video. 50 quota units used.");
        await loadDashboard();
    } catch (error) {
        showNotice(error.message, true);
    }
}

const query = new URLSearchParams(window.location.search);
if (query.get("youtube_connected") === "1") {
    window.history.replaceState({}, "", "/automation");
    showNotice("YouTube account connected successfully.");
}
async function initializeDashboard() {
    await loadDashboard();
    loadOllamaStatus();
    if (dashboard.youtube?.youtube_connected && !hasCurrentTrends()) {
        scoutNow(++trendRefreshSequence, true);
    }
}

initializeDashboard();
window.setInterval(() => loadDashboard(), 10000);
