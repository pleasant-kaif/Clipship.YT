const form = document.querySelector("#analyze-form");
const analyzeButton = document.querySelector("#analyze-button");
const notice = document.querySelector("#notice");
const progressPanel = document.querySelector("#progress-panel");
const progressTitle = document.querySelector("#progress-title");
const progressMessage = document.querySelector("#progress-message");
const results = document.querySelector("#results");
const downloadResults = document.querySelector("#download-results");
const createButton = document.querySelector("#create-button");
const highlightList = document.querySelector("#highlight-list");
const manualEditor = document.querySelector("#manual-editor");
const manualList = document.querySelector("#manual-list");
const selectionCount = document.querySelector("#selection-count");
const selectedHighlights = new Set();
const manualSegments = [];
let analysisJobId = null;
let analysisVideo = null;

function showError(message) {
    notice.textContent = message;
    notice.hidden = false;
}

function clearError() {
    notice.textContent = "";
    notice.hidden = true;
}

function setBusy(busy, title = "Finding the good bits…", message = "This can take a little while.") {
    analyzeButton.disabled = busy;
    document.querySelector("#video-url").disabled = busy;
    document.querySelector("#rights-check").disabled = busy;
    document.querySelectorAll('input[name="mode"]').forEach(input => {
        input.disabled = busy;
    });
    const mode = document.querySelector('input[name="mode"]:checked').value;
    analyzeButton.innerHTML = busy
        ? "Working…"
        : mode === "manual"
            ? 'Get video duration <span aria-hidden="true">↗</span>'
            : mode === "ai"
                ? 'Transcribe &amp; find highlights <span aria-hidden="true">↗</span>'
                : 'Find highlights <span aria-hidden="true">↗</span>';
    progressTitle.textContent = title;
    progressMessage.textContent = message;
    progressPanel.hidden = !busy;
}

function formatTime(seconds) {
    const rounded = Math.floor(seconds);
    const hours = Math.floor(rounded / 3600);
    const minutes = Math.floor((rounded % 3600) / 60);
    const remaining = rounded % 60;
    return hours
        ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remaining).padStart(2, "0")}`
        : `${minutes}:${String(remaining).padStart(2, "0")}`;
}

function formatDuration(seconds) {
    const minutes = Math.floor(seconds / 60);
    const remaining = Math.round(seconds % 60);
    return minutes ? `${minutes}m ${remaining}s` : `${remaining}s`;
}

function updateSelection() {
    const count = analysisVideo?.mode === "manual" ? manualSegments.length : selectedHighlights.size;
    selectionCount.textContent = `${count} clip${count === 1 ? "" : "s"} selected`;
    createButton.disabled = count === 0;
}

function renderHighlights(video) {
    analysisVideo = video;
    selectedHighlights.clear();
    manualSegments.length = 0;
    updateSelection();
    document.querySelector("#video-title").textContent = video.title;
    document.querySelector("#video-meta").textContent =
        `${formatDuration(video.duration)} · ${video.source}`;
    const thumbnail = document.querySelector("#video-thumbnail");
    if (video.thumbnail) {
        thumbnail.src = video.thumbnail;
        thumbnail.hidden = false;
    } else {
        thumbnail.hidden = true;
    }
    highlightList.replaceChildren();
    manualList.replaceChildren();
    manualEditor.hidden = video.mode !== "manual";
    highlightList.hidden = video.mode === "manual";
    if (video.mode === "manual") {
        document.querySelector("#suggestion-description").textContent =
            "Set the start and end times for each clip. Add up to five.";
    } else {
        document.querySelector("#suggestion-description").textContent =
            `${video.highlights.length} promising places to start, based on ${video.source.toLowerCase()}. Pick the ones you like.`;
        video.highlights.forEach((highlight, index) => {
            const card = document.createElement("label");
            card.className = "highlight-card";
            card.innerHTML = `
                <input type="checkbox" aria-label="Select clip at ${formatTime(highlight.start)}">
                <span class="clip-check" aria-hidden="true"></span>
                <span class="clip-time">${formatTime(highlight.start)}–${formatTime(highlight.end)}</span>
                <span class="clip-copy"><p></p><small>${formatDuration(highlight.duration)} · Suggested moment</small></span>
                <span class="clip-score"><strong>${highlight.score}</strong><span>MATCH</span></span>`;
            card.querySelector(".clip-copy p").textContent = highlight.text;
            const checkbox = card.querySelector("input");
            checkbox.addEventListener("change", () => {
                if (checkbox.checked) {
                    if (selectedHighlights.size >= 5) {
                        checkbox.checked = false;
                        showError("You can create up to five clips at a time.");
                        return;
                    }
                    selectedHighlights.add(index);
                    card.classList.add("selected");
                } else {
                    selectedHighlights.delete(index);
                    card.classList.remove("selected");
                }
                clearError();
                updateSelection();
            });
            highlightList.append(card);
        });
    }
    createButton.textContent = video.mode === "manual"
        ? "Create these clips →"
        : "Create selected clips →";
    results.hidden = false;
    results.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function renderManualSegments() {
    manualList.replaceChildren();
    manualSegments.forEach((segment, index) => {
        const row = document.createElement("div");
        row.className = "manual-range";
        const label = document.createElement("span");
        label.textContent = `Clip ${index + 1} · ${formatTime(segment.start)}–${formatTime(segment.end)} · ${formatDuration(segment.end - segment.start)}`;
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "text-button";
        remove.textContent = "Remove";
        remove.setAttribute("aria-label", `Remove clip ${index + 1}`);
        remove.addEventListener("click", () => {
            manualSegments.splice(index, 1);
            renderManualSegments();
            updateSelection();
        });
        row.append(label, remove);
        manualList.append(row);
    });
}

async function requestJson(url, options) {
    const response = await fetch(url, options);
    const body = await response.json();
    if (!response.ok) {
        throw new Error(body.error || "Something went wrong. Please try again.");
    }
    return body;
}

async function watchJob(jobId, onComplete) {
    while (true) {
        const job = await requestJson(`/api/jobs/${encodeURIComponent(jobId)}`);
        if (job.status === "error") {
            throw new Error(job.error || "The job could not be completed.");
        }
        if (job.status === "complete") {
            return onComplete(job);
        }
        if (job.status === "working" || job.status === "queued") {
            progressMessage.textContent = job.message || "Working on it…";
        }
        await new Promise(resolve => window.setTimeout(resolve, 1300));
    }
}

form.addEventListener("submit", async event => {
    event.preventDefault();
    clearError();
    results.hidden = true;
    downloadResults.hidden = true;
    analysisJobId = null;
    setBusy(true);
    try {
        const mode = document.querySelector('input[name="mode"]:checked').value;
        const job = await requestJson("/api/analyze", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                url: document.querySelector("#video-url").value,
                mode,
                has_rights: document.querySelector("#rights-check").checked,
            }),
        });
        analysisJobId = job.job_id;
        await watchJob(job.job_id, completed => renderHighlights(completed.video));
    } catch (error) {
        showError(error.message);
    } finally {
        setBusy(false);
    }
});

createButton.addEventListener("click", async () => {
    if (!analysisJobId || createButton.disabled) return;
    clearError();
    setBusy(true, "Making your clips…", "Your selected moments are being cut and saved.");
    createButton.disabled = true;
    try {
        const job = await requestJson("/api/clips", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                analysis_job_id: analysisJobId,
                segments: analysisVideo.mode === "manual"
                    ? manualSegments
                    : [...selectedHighlights].map(index => ({
                        start: analysisVideo.highlights[index].start,
                        end: analysisVideo.highlights[index].end,
                    })),
            }),
        });
        await watchJob(job.job_id, completed => {
            const downloadList = document.querySelector("#download-list");
            downloadList.replaceChildren();
            completed.clips.forEach(clip => {
                const item = document.createElement("div");
                item.className = "download-item";
                const details = document.createElement("div");
                const name = document.createElement("strong");
                name.textContent = `${clip.title} · ${formatTime(clip.start)}`;
                const duration = document.createElement("small");
                duration.textContent = `${formatDuration(clip.duration)} · MP4`;
                details.append(name, duration);
                const link = document.createElement("a");
                link.className = "download-link";
                link.href = clip.url;
                link.download = clip.filename;
                link.textContent = "Download ↓";
                item.append(details, link);
                downloadList.append(item);
            });
            results.hidden = true;
            downloadResults.hidden = false;
            downloadResults.scrollIntoView({ behavior: "smooth", block: "nearest" });
        });
    } catch (error) {
        showError(error.message);
        createButton.disabled = false;
    } finally {
        setBusy(false);
        updateSelection();
    }
});

document.querySelector("#clear-results").addEventListener("click", () => {
    analysisJobId = null;
    analysisVideo = null;
    selectedHighlights.clear();
    manualSegments.length = 0;
    results.hidden = true;
    downloadResults.hidden = true;
    document.querySelector("#video-url").value = "";
    clearError();
    updateSelection();
    document.querySelector("#video-url").focus();
});

document.querySelector("#add-manual-clip").addEventListener("click", () => {
    clearError();
    const start = Number(document.querySelector("#manual-start").value);
    const end = Number(document.querySelector("#manual-end").value);
    if (!Number.isFinite(start) || !Number.isFinite(end)) {
        showError("Enter a start and end time in seconds.");
        return;
    }
    if (start < 0 || end <= start || end > analysisVideo.duration) {
        showError(`Clip times must be in order and within the ${formatDuration(analysisVideo.duration)} video.`);
        return;
    }
    if (end - start > 600) {
        showError("Each clip can be up to 10 minutes long.");
        return;
    }
    if (manualSegments.length >= 5) {
        showError("You can add up to five clips at a time.");
        return;
    }
    manualSegments.push({ start, end });
    renderManualSegments();
    updateSelection();
    document.querySelector("#manual-start").value = "";
    document.querySelector("#manual-end").value = "";
});

document.querySelector("#make-more").addEventListener("click", () => {
    downloadResults.hidden = true;
    results.hidden = false;
});

document.querySelectorAll('input[name="mode"]').forEach(input => {
    input.addEventListener("change", () => {
        document.querySelectorAll(".mode-option").forEach(option => option.classList.remove("selected"));
        input.closest(".mode-option").classList.add("selected");
        if (analysisVideo) {
            analysisJobId = null;
            analysisVideo = null;
            selectedHighlights.clear();
            manualSegments.length = 0;
            results.hidden = true;
            downloadResults.hidden = true;
            updateSelection();
        }
        setBusy(false);
    });
});

async function loadConfig() {
    try {
        const config = await requestJson("/api/config");
        const aiDescription = document.querySelector("#ai-description");
        const aiNote = document.querySelector("#ai-note");
        if (config.ai_available) {
            aiDescription.textContent = "Enabled · on-device CPU transcription";
        } else {
            aiDescription.textContent = "Required · app setup needed";
            aiNote.hidden = false;
        }
    } catch (error) {
        showError(error.message);
    }
}

setBusy(false);
loadConfig();
