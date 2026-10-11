"""Generate a 30-second animated explainer video for Clipship.YT.

Animated 1920x1080 slides (fade-in, slide-in text, progress bar, stage badges)
+ Windows SAPI TTS narration, muxed with ffmpeg.

Usage: .venv\\Scripts\\python.exe make_video.py
Output: repo_demo/clipship_demo.mp4
"""
import base64
import os
import subprocess
import sys
import wave
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).parent
OUT = ROOT / "repo_demo"
OUT.mkdir(exist_ok=True)
FPS = 30
W, H = 1920, 1080
# Windows default fonts
FONT_BOLD = "C:/Windows/Fonts/segoeuib.ttf"
FONT_REG = "C:/Windows/Fonts/segoeui.ttf"

BG = (13, 17, 23)          # GitHub dark background
FG = (233, 236, 239)       # near-white
ACCENT = (77, 171, 247)    # blue
ACCENT2 = (63, 185, 80)    # green
MUTED = (139, 148, 158)

SLIDES = [
    ("Clipship.YT", "Your 6-stage agentic YouTube automation pipeline", 4.75),
    ("Stage 1 — Trend Scout", "trend_scout.py finds what's hot before anyone else", 3.4),
    ("Stage 2 — Clip Factory", "clip_factory.py cuts the perfect moments automatically", 3.4),
    ("Stage 3 — Captioning", "captioning.py adds perfectly synced captions", 3.4),
    ("Stage 4 — SEO Agent", "seo_agent.py + local AI writes titles and descriptions", 3.4),
    ("Stage 5 — Engagement", "engagement.py handles comments and community growth", 3.4),
    ("Stage 6 — Quota Budget", "quota_budget.py keeps every API call within limits", 3.4),
    ("Star it. Fork it. Ship clips.", "github.com/pleasant-kaif/Clipship.YT", 4.85),
]
TOTAL = sum(s[2] for s in SLIDES)
assert abs(TOTAL - 30.0) < 0.01, f"Total duration {TOTAL}s != 30s"


def font(size, bold=True):
    return ImageFont.truetype(FONT_BOLD if bold else FONT_REG, size)


def frame(t):
    """Draw the frame at global time t (seconds) with cross-slide transitions."""
    # Find current slide and local time
    acc = 0.0
    idx, local = 0, 0.0
    for i, (_, _, dur) in enumerate(SLIDES):
        if t < acc + dur or i == len(SLIDES) - 1:
            idx, local = i, t - acc
            break
        acc += dur

    title, subtitle, dur = SLIDES[idx]
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # Decorative animated diagonal lines
    for k in range(-2, 12):
        x = (k * 260 + int(t * 60)) % (W + 500) - 250
        d.line([(x, H), (x + 320, 0)], fill=(22, 27, 34), width=90)

    # Fade-in + slide-up of the text block during first 0.6s of each slide
    p = min(1.0, local / 0.6)
    ease = 1 - (1 - p) ** 3  # ease-out cubic
    dy = int((1 - ease) * 60)
    alpha = ease

    # Stage badge (slides 1..6)
    if 1 <= idx <= 6:
        bx, by, bs = 120, 100, 130
        d.rounded_rectangle([bx, by, bx + bs, by + bs], 30, fill=ACCENT2)
        bf = font(64)
        d.text((bx + bs / 2, by + bs / 2), str(idx), font=bf, fill=BG, anchor="mm")
        d.text((bx + bs + 30, by + bs / 2), f"of 6", font=font(52, False),
               fill=MUTED, anchor="lm")
        # animated progress ring approximation: growing bar under badge
        d.rounded_rectangle([bx, by + bs + 25, bx + bs + 380, by + bs + 45], 10,
                            fill=(30, 38, 48))
        d.rounded_rectangle([bx, by + bs + 25,
                             bx + int(bs + 380 * min(1.0, (idx - 1 + min(1.0, local / dur)) / 6)),
                             by + bs + 45], 10, fill=ACCENT2)

    # Title
    tf = font(110)
    tw = d.textlength(title, font=tf)
    d.text((W / 2, H / 2 - 80 + dy), title, font=tf, fill=FG, anchor="mm")

    # Accent underline that grows with ease
    uw = int(ease * min(tw, 900))
    d.rounded_rectangle([W / 2 - uw / 2, H / 2 + 20, W / 2 + uw / 2, H / 2 + 32],
                        6, fill=ACCENT)

    # Subtitle
    sf = font(52, bold=False)
    d.text((W / 2, H / 2 + 120 + dy), subtitle, font=sf, fill=MUTED, anchor="mm")

    # Global progress bar at bottom
    prog = t / TOTAL
    d.rectangle([0, H - 14, W, H], fill=(30, 38, 48))
    d.rectangle([0, H - 14, int(W * prog), H], fill=ACCENT)

    # Watermark
    d.text((W - 40, 40), "Clipship.YT", font=font(36), fill=MUTED, anchor="ra")

    # Apply fade to black between slides via overlay
    if local < 0.35 and idx > 0:
        a = 1 - local / 0.35
        img = Image.blend(img, Image.new("RGB", (W, H), (0, 0, 0)), a * 0.8)
    return img


def make_tts(text, path):
    """Narrate one slide with Windows SAPI via PowerShell (base64-encoded command)."""
    ps_text = text.replace("'", "''")
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.Rate = 1; $s.SetOutputToWaveFile('{path}'); "
        f"$s.Speak('{ps_text}'); $s.Dispose()"
    )
    enc = base64.b64encode(ps.encode("utf-16-le")).decode()
    subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", enc], check=True)


def wav_len(path):
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def main():
    ffmpeg = None
    for p in [
        os.environ.get("FFMPEG", ""),
        r"C:\Users\Mohdk\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.2-full_build\bin\ffmpeg.exe",
    ]:
        if p and Path(p).exists():
            ffmpeg = p
            break
    if not ffmpeg:
        sys.exit("ffmpeg not found")

    # 1. Narration per slide
    narr_texts = [
        "Clipship dot Y T. Your six stage agentic, YouTube automation pipeline.",
        "Stage one. Trend scout finds what's hot before anyone else.",
        "Stage two. Clip factory cuts the perfect moments, automatically.",
        "Stage three. Captioning adds perfectly synced captions.",
        "Stage four. S E O agent with local AI writes titles and descriptions.",
        "Stage five. Engagement handles comments and community growth.",
        "Stage six. Quota budget keeps every API call within limits.",
        "Star it, fork it, and ship clips. Clipship dot Y T on GitHub.",
    ]
    wavs = []
    for i, txt in enumerate(narr_texts):
        w = OUT / f"narr_{i}.wav"
        make_tts(txt, str(w))
        print(f"[TTS] slide {i}: {wav_len(w):.1f}s")
        wavs.append(w)

    # 2. Render frames
    frames_dir = OUT / "frames"
    frames_dir.mkdir(exist_ok=True)
    nframes = int(TOTAL * FPS)
    print(f"[Render] {nframes} frames...")
    for n in range(nframes):
        t = n / FPS
        f = frame(t)
        f.save(frames_dir / f"f_{n:05d}.png")
        if n % 60 == 0:
            print(f"  frame {n}/{nframes}")

    # 3. Per-slide: scale TTS to slide duration, concat
    print("[Audio] assembling narration track...")
    aargs = []
    for i, (w, (_, _, dur)) in enumerate(zip(wavs, SLIDES)):
        out = OUT / f"adj_{i}.wav"
        # atempo keeps pitch; pad silence to exactly dur seconds
        subprocess.run([ffmpeg, "-y", "-i", str(w), "-af",
                        f"atempo=1.15,apad=whole_dur={dur}", "-ar", "44100",
                        "-ac", "2", str(out)], check=True, capture_output=True)
        aargs += ["-i", str(out)]
    n_in = len(aargs) // 2
    filter_complex = "".join(f"[{i}:a]" for i in range(n_in)) + f"concat=n={n_in}:v=0:a=1[out]"
    subprocess.run([ffmpeg, "-y", *aargs, "-filter_complex", filter_complex,
                    "-map", "[out]", str(OUT / "narration.wav")],
                   check=True, capture_output=True)

    # 4. Mux
    print("[Mux] final video...")
    subprocess.run([ffmpeg, "-y", "-framerate", str(FPS), "-i",
                    str(frames_dir / "f_%05d.png"),
                    "-i", str(OUT / "narration.wav"),
                    "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                    "-shortest", str(OUT / "clipship_demo.mp4")],
                   check=True, capture_output=True)
    size = (OUT / "clipship_demo.mp4").stat().st_size / 1e6
    print(f"DONE: repo_demo/clipship_demo.mp4 ({size:.1f} MB, {TOTAL}s)")


if __name__ == "__main__":
    main()
