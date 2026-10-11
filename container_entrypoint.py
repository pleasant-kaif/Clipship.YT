from __future__ import annotations

import os

from waitress import serve

from app import app
from pipeline import start_pipeline_scheduler


start_pipeline_scheduler()
serve(
    app,
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "5000")),
    threads=8,
    max_request_body_size=2 * 1024 * 1024 * 1024 + 512 * 1024,
)
