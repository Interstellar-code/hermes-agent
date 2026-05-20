#!/usr/bin/env python3
"""
Fetch a YouTube video transcript and output it as structured JSON.

Usage:
    python fetch_transcript.py <url_or_video_id> [--language en,tr] [--timestamps]

Output (JSON):
    {
        "video_id": "...",
        "language": "en",
        "segments": [{"text": "...", "start": 0.0, "duration": 2.5}, ...],
        "full_text": "complete transcript as plain text",
        "timestamped_text": "00:00 first line\n00:05 second line\n..."
    }

Install dependency:  pip install youtube-transcript-api
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory


CLOAKBROWSER_SMOKE_SCRIPT = r"""
import { launch } from 'cloakbrowser';

const videoId = process.argv[2];
const browser = await launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1280, height: 720 } });
const page = await context.newPage();
let timedtext = null;
page.on('response', async (resp) => {
  if (timedtext) return;
  if (resp.url().includes('/api/timedtext')) {
    try { timedtext = await resp.json(); } catch {}
  }
});
await page.goto(`https://www.youtube.com/watch?v=${videoId}`, { waitUntil: 'networkidle', timeout: 90000 });
await page.waitForTimeout(2000);
await browser.close();
console.log(JSON.stringify(timedtext || null));
"""


def extract_video_id(url_or_id: str) -> str:
    """Extract the 11-character video ID from various YouTube URL formats."""
    url_or_id = url_or_id.strip()
    patterns = [
        r'(?:v=|youtu\.be/|shorts/|embed/|live/)([a-zA-Z0-9_-]{11})',
        r'^([a-zA-Z0-9_-]{11})$',
    ]
    for pattern in patterns:
        match = re.search(pattern, url_or_id)
        if match:
            return match.group(1)
    return url_or_id


def format_timestamp(seconds: float) -> str:
    """Convert seconds to HH:MM:SS or MM:SS format."""
    total = int(seconds)
    h, remainder = divmod(total, 3600)
    m, s = divmod(remainder, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def normalize_segments(segments):
    return [
        {"text": seg["text"], "start": seg["start"], "duration": seg["duration"]}
        for seg in segments
    ]


def parse_pb3_timedtext(payload):
    events = payload.get("events") or []
    segments = []
    for ev in events:
        text = "".join((s.get("utf8") or "") for s in ev.get("segs") or []).strip()
        if not text:
            continue
        start = (ev.get("tStartMs") or 0) / 1000.0
        duration = (ev.get("dDurationMs") or 0) / 1000.0
        segments.append({"text": text, "start": start, "duration": duration})
    return segments


def fetch_transcript_with_cloakbrowser(video_id: str):
    """Try CloakBrowser first via timedtext interception."""
    candidates = [
        os.environ.get("HERMES_NODE_BIN"),
        "/Users/rohits/Library/PhpWebStudy/env/node/bin/node",
        shutil.which("node"),
        "node",
    ]
    node_cmd = next((c for c in candidates if c), None)
    if not node_cmd:
        raise RuntimeError("node not found")

    with TemporaryDirectory(prefix="yt-cloak-") as td:
        script_path = os.path.join(td, "smoke.mjs")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(CLOAKBROWSER_SMOKE_SCRIPT)
        proc = subprocess.run(
            [node_cmd, script_path, video_id],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "CloakBrowser smoke failed")
        raw = proc.stdout.strip()
        if not raw or raw == "null":
            raise RuntimeError("No timedtext response intercepted")
        payload = json.loads(raw)
        return normalize_segments(parse_pb3_timedtext(payload))


def fetch_transcript(video_id: str, languages: list = None):
    """Fetch transcript segments from YouTube.

    Returns a list of dicts with 'text', 'start', and 'duration' keys.
    Compatible with youtube-transcript-api v1.x.
    """
    # 1) Prefer CloakBrowser timedtext interception for bot-gated cases.
    try:
        return fetch_transcript_with_cloakbrowser(video_id)
    except Exception:
        pass

    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        print("Error: youtube-transcript-api not installed. Run: pip install youtube-transcript-api",
              file=sys.stderr)
        sys.exit(1)

    api = YouTubeTranscriptApi()
    if languages:
        result = api.fetch(video_id, languages=languages)
    else:
        result = api.fetch(video_id)

    # v1.x returns FetchedTranscriptSnippet objects; normalize to dicts
    return [
        {"text": seg.text, "start": seg.start, "duration": seg.duration}
        for seg in result
    ]


def main():
    parser = argparse.ArgumentParser(description="Fetch YouTube transcript as JSON")
    parser.add_argument("url", help="YouTube URL or video ID")
    parser.add_argument("--language", "-l", default=None,
                        help="Comma-separated language codes (e.g. en,tr). Default: auto")
    parser.add_argument("--timestamps", "-t", action="store_true",
                        help="Include timestamped text in output")
    parser.add_argument("--text-only", action="store_true",
                        help="Output plain text instead of JSON")
    args = parser.parse_args()

    video_id = extract_video_id(args.url)
    languages = [l.strip() for l in args.language.split(",")] if args.language else None

    try:
        segments = fetch_transcript(video_id, languages)
    except Exception as e:
        error_msg = str(e)
        if "disabled" in error_msg.lower():
            print(json.dumps({"error": "Transcripts are disabled for this video."}))
        elif "no transcript" in error_msg.lower():
            print(json.dumps({"error": f"No transcript found. Try specifying a language with --language."}))
        else:
            print(json.dumps({"error": error_msg}))
        sys.exit(1)

    full_text = " ".join(seg["text"] for seg in segments)
    timestamped = "\n".join(
        f"{format_timestamp(seg['start'])} {seg['text']}" for seg in segments
    )

    if args.text_only:
        print(timestamped if args.timestamps else full_text)
        return

    result = {
        "video_id": video_id,
        "segment_count": len(segments),
        "duration": format_timestamp(segments[-1]["start"] + segments[-1]["duration"]) if segments else "0:00",
        "full_text": full_text,
    }
    if args.timestamps:
        result["timestamped_text"] = timestamped

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
