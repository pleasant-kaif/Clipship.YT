# Clipship.YT

A local-first YouTube clipping and channel-automation studio for videos you own or have permission to use. Set clip times yourself, find highlight suggestions with captions or on-device Whisper, or use the optional trend, vertical-clip, review, publishing, and engagement workflow.

## Start on Windows

1. Install [Python 3.10 or newer](https://www.python.org/downloads/).
2. Double-click `run.bat` in this folder. The first launch creates a Python environment and installs the app's dependencies.
3. Open [http://127.0.0.1:5000](http://127.0.0.1:5000) in your browser.
4. Use the manual clipper at `/` to paste a YouTube video link and set clip times or review local-AI highlight suggestions.
5. Open the automation studio at [http://127.0.0.1:5000/automation](http://127.0.0.1:5000/automation) to configure trend scouting, generate captioned vertical clips, and manage approvals.

Keep the command window open while using the app. Press **Ctrl+C** in that window to stop the server.

Run the local checks with `.venv\Scripts\python.exe -m unittest discover -s tests -v`.

## Run with Docker

Install Docker Desktop, then run this folder from PowerShell:

```powershell
Copy-Item .env.example .env
docker compose up --build
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000). The Compose port is bound to localhost only. Stop the app with **Ctrl+C**; start it again with `docker compose up -d`. Use `docker compose logs -f clipship` to inspect logs and `docker compose down` to stop the container. The `clipship-data` Docker volume retains the SQLite database, uploaded OAuth client/token files, local source videos, generated clips, and downloaded Whisper models across rebuilds.

The image includes FFmpeg with subtitle/font support and runs the web server as a non-root user. First-time local transcription downloads the Whisper `base` model into the persistent volume and can take several minutes. To use Ollama installed on the host, make sure it is running and accessible to Docker, then set `OLLAMA_BASE_URL` (the default is `http://host.docker.internal:11434`) before starting Compose. The container does not download Ollama models or publish ports for Ollama.

To use a different local web port, set `$env:CLIPSHIP_PORT = "5001"` before `docker compose up --build`; OAuth's redirect URI remains `http://127.0.0.1:5000/api/youtube/callback`, so use the default port when connecting Google unless you also update the redirect URI configuration in `youtube_api.py`.

## Automation studio setup

The YouTube API and local processing components are free to use, but Google requires a Cloud project and your own OAuth consent:

1. In Google Cloud Console, create/select a project, enable **YouTube Data API v3**, and configure the OAuth consent screen.
2. Create a **Web application** OAuth client and add `http://127.0.0.1:5000/api/youtube/callback` as an authorized redirect URI. Download its JSON file.
3. In the studio, upload that OAuth JSON and select **Connect YouTube account**. The local token grants access to manage channel videos and comments. Revoke access in your Google Account and use **Disconnect** to remove the token from this computer.
4. Choose a two-letter country/region, YouTube category, search language (Hinglish, English, Hindi, or Urdu), and (for India) an Indian zone (North, South, West, or East), plus the daily schedule, timezone, clip-generation target, and upload privacy. Country and category are sent as exact YouTube search filters. Language uses YouTube's relevance-language hint and local text scoring; Indian zones are best-effort title/description keyword matches only. Neither verifies spoken language or creator location, and regional non-matches may still appear. Changing any filter automatically searches again and saves the selection. Matching automatic searches reuse results for 15 minutes at no quota cost; a fresh search costs 100 YouTube API quota units, plus 1 unit for the separate public-statistics/duration lookup when results are found (up to 101 total). The search is bounded by a 10-second limit. Trend cards show public view, like, and comment counts; YouTube may hide likes or comments for individual videos. Uploads default to **private**.
5. The SEO Description and Engagement agents use standalone Ollama only. Install Ollama, start its local service, and run `ollama pull llama3`. On Windows, Clipship defaults to `http://127.0.0.1:11434` with model `llama3`; Docker uses the Ollama host endpoint configured through `OLLAMA_BASE_URL` (default `http://host.docker.internal:11434`). No FreeLLMAPI bridge or remote LLM endpoint is used by the Windows app. The SEO Description Agent runs immediately before upload, uses clip captions when available plus trend metadata, and refuses to upload with generic fallback text if generation fails.

Configure Google Cloud quota increases in Google, then update Clipship's cap only after Google approves them. The default is **10,000 units/day**, split into an 8,000-unit upload tier, a 1,000-unit engagement tier, and a 1,000-unit trend tier. Uploads cost 1,600 units each, so five fit in the default upload tier; approvals beyond that are scheduled on later days. Ten uploads in a day need 16,000 units before any other API use. Clipship does not assume that changing its local cap changes Google's limit.

The quota ledger is stored in `clipship.db` (or the persistent Docker data volume), resets by the configured IANA timezone, and reserves the fixed cost of each API request before sending it. The trend tier reserves 1,000 units (up to nine complete fresh 101-unit searches per day, with cache hits using zero; an empty search uses 100 units because there are no video details to fetch). Exhausted-tier actions are queued for the next scheduled day. The `/quota` and `/api/quota` endpoints and the studio dashboard report usage, remaining units, queued costs, and projected upload capacity. The daily scheduler runs only while the app is open. It searches once per day when enabled and uploads due, already-approved generated clips only. For direct publishing, choose a local video, confirm you own it or have publishing rights, and Clipship immediately uploads the original video unchanged to your connected YouTube channel; this direct path does not require Auto Captioning, Auto Clipping, review approval, or a minimum duration. Clipship accepts source files up to 2 GB, and YouTube's own format, duration, account, and policy limits still apply. Keep the app running and YouTube connected until the upload completes. Generated clips that are awaiting review still require approval and are scheduled for the next available configured clip-upload time. The studio's pipeline tracker shows each source's current stage, errors, last update, and YouTube publication link, and refreshes every ten seconds. On restart, interrupted captioning/clipping jobs are requeued; an interrupted direct upload is marked for attention so you can check YouTube Studio before retrying and avoid duplicates.

### Clip creation and approvals

YouTube's Data API can discover trending video **metadata**, not provide reusable source footage. For a trend, provide a local MP4, MOV, MKV, WebM, or M4V file that you own or have permission to publish. The dashboard's **Import & publish original video** action uploads that local file unchanged, at its original duration, after your explicit rights confirmation. No trending link is downloaded. The separate manual clipper can process an authorized local source into a vertical highlight; its generated clips still go through captioning and review before scheduled publishing.

Watch each generated preview and confirm publishing rights before approving. Only greenlit generated clips can enter the scheduled queue. Direct original-video uploads begin immediately when you confirm rights and use the configured upload visibility; public uploads are public. A failed or interrupted upload is **not** retried automatically because YouTube might have accepted a request without returning confirmation; check YouTube Studio before explicitly requesting a retry.

Comment checks, likes, and replies are restricted to clips published by this Clipship channel. Ollama produces an editable draft; sending a reply or liking a video always requires a separate user action. Clipship does not bulk-like or bulk-reply. A YouTube like applies to the video, not an individual comment.

The six pipeline stages are implemented as cooperating local modules, not six always-running background AI processes:

| Stage | What activates it |
| --- | --- |
| Trend Scout | Filter changes, an explicit fresh search, or the daily scheduled scout |
| Auto Clipping | Starts after an authorized source is fetched or uploaded; selects a one-minute highlight and renders the vertical clip |
| Auto Captioning | Runs Whisper on the same authorized long-form source, saves a downloadable full-video SRT, and supplies timed captions to Auto Clipping for burn-in |
| SEO Description | At publish time, uses local Ollama to write an accurate, search-aware description from available captions and trend context; it avoids unsupported claims and does not promise virality |
| Engagement | Explicit comment sync; Ollama drafts replies on request; likes/replies require a separate user action |
| Approval & Publish | A user reviews and approves a rendered clip; only approved clips are uploaded at their scheduled time |

This trigger-based behavior keeps quota use bounded and preserves the review and anti-spam safeguards. Uploaded footage visibly moves through **Auto Captioning**, **Auto Clipping**, and **SEO Description** states; the generated SEO description is shown in its pipeline card. Direct original uploads skip clipping/review but still run the SEO Description Agent before YouTube receives the video. For those direct uploads, no transcript is available unless it was generated separately, so the agent treats trend metadata as keyword context and avoids claiming unverified details about the local video. Full-video SRT captions can be downloaded during or after processing. A stage shown idle in the UI has no eligible work or has not been explicitly triggered.

## Analysis options

- **YouTube captions** is an alternative to local AI and uses an available caption track for the video. It is the quickest option. If captions are unavailable, use local AI transcription.
- **Local AI transcription** is enabled by default and transcribes the video's audio on your computer. The launcher installs its requirements. The first transcription downloads the Whisper `base` model and may take a few minutes. It uses CPU processing; no AI API key is used or needed for transcription or highlight suggestions.
- **Set times manually** gets the video duration, then lets you create up to five clips by entering start and end times in seconds.

Highlight suggestions are starting points ranked using transcript cues, not an AI judgment that a moment is objectively interesting. Review the text and timestamps before making clips.

## Privacy and usage

The Windows launcher listens only on `127.0.0.1`; Docker publishes its port on `127.0.0.1` only by default. Its database, OAuth credentials, supplied automation sources, and rendered clips stay in ignored local files/folders or the Docker data volume; the browser loads trend thumbnails directly from YouTube. Automation uses the YouTube Data API with your OAuth account, and local Whisper/Ollama processing uses your computer or the configured local Ollama service. The app does not accept sign-in cookies, bypass access controls, or remove DRM.

## Before publishing this repository

- Never add `.env` files, OAuth client/token JSON, the SQLite database, local video files, generated clips, or downloaded model data. The repository ignore rules exclude these local items.
- Create your own Google OAuth client and connect your own YouTube account after setup; never publish account passwords, API keys, or OAuth tokens.
- Review and select a license before making the repository public. No license is included, so reuse is not granted by default.
- GitHub Actions runs the Python test suite on Python 3.11 and 3.12 and builds the Docker image on pushes and pull requests.
- Docker Compose builds the image locally; it does not publish images or connect the project to a GitHub repository automatically.

Only clip videos you own or have permission to use, and follow YouTube's terms and applicable copyright law. Some videos may not be available to download or process; those restrictions are not bypassed.

## Troubleshooting

- **The page will not load:** Make sure the command window says the app is running at `http://127.0.0.1:5000`, and that another app is not already using port 5000.
- **A video cannot be analyzed:** Check that the link is public and that the video has captions or spoken audio. Videos restricted by YouTube cannot be processed by this app.
- **Clip rendering fails:** Run `run.bat` again to ensure the standard dependencies are installed. The app bundles an FFmpeg executable through `imageio-ffmpeg`.
- **Local AI is unavailable:** Stop the app, run `run.bat` again to install the required packages, then restart it.
- **Timezone data is unavailable:** Run `run.bat` again so its `tzdata` dependency is installed.
- **Ollama is unavailable:** Start Ollama, run `ollama pull llama3`, and select **Check** next to the Ollama status in the studio.
- **YouTube says quota exceeded:** Review the `/quota` dashboard. Clipship queues quota-limited approved actions for a later scheduled day; quota increases must be approved in Google Cloud.
- **Search results don't match the selected language or Indian zone exactly:** YouTube's `relevanceLanguage` and region keywords are ranking hints, not verified spoken languages or creator locations. Hinglish uses Hindi as the closest relevance-language hint; regional text matches are surfaced when a state/territory name appears in the video title, description, or channel name.
