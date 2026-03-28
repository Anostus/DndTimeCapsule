#!/usr/bin/env python3
"""
D&D Time Capsule
================
Searches YouTube for the top D&D-related videos from a 7-day window
corresponding to today's date 10 years ago. Adds the top 10 results
to a rolling RSS feed.

Feed rules:
  - Items expire after 30 days
  - Maximum 25 items (oldest removed first when exceeded)

Runs once a week via GitHub Actions.

Search strategy:
  Uses YouTube's own before:/after: search operators via yt-dlp URL
  extraction, so YouTube itself filters to the correct date range.
  Example URL that yt-dlp processes:
    https://www.youtube.com/results?search_query=D%26D+after%3A2016-03-25+before%3A2016-04-01
"""

import json
import logging
import subprocess
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from feedgen.feed import FeedGenerator
import feedparser

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OUTPUT_FILE = "docs/feed.xml"
FEED_DB_PATH = "data/feed_items.json"  # persistent store of current items
MAX_ITEMS = 25
RETENTION_DAYS = 30
VIDEOS_PER_RUN = 10
YEARS_AGO = 10

# Search queries — each will be combined with date operators
SEARCH_QUERIES = [
    "D&D",
    "Dungeons and Dragons",
    "DnD 5e",
]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("dnd_time_capsule")

# ---------------------------------------------------------------------------
# Date window calculation
# ---------------------------------------------------------------------------


def get_search_window() -> tuple[datetime, datetime]:
    """
    Return (start, end) for a 7-day window centred on today-minus-10-years.
    The window runs from 3 days before to 4 days after the anniversary date.
    """
    today = datetime.now(timezone.utc).date()
    try:
        anniversary = today.replace(year=today.year - YEARS_AGO)
    except ValueError:
        # Handle Feb 29 → Feb 28
        anniversary = today.replace(year=today.year - YEARS_AGO, day=28)

    start = anniversary - timedelta(days=3)
    end = anniversary + timedelta(days=4)
    return (
        datetime(start.year, start.month, start.day, tzinfo=timezone.utc),
        datetime(end.year, end.month, end.day, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# YouTube search via yt-dlp + YouTube date operators
# ---------------------------------------------------------------------------


def build_youtube_search_url(query: str, after_date: str, before_date: str) -> str:
    """
    Build a YouTube search URL with before:/after: date operators.
    YouTube recognizes these operators in the search query itself.

    after_date / before_date should be YYYY-MM-DD strings.
    """
    full_query = f"{query} after:{after_date} before:{before_date}"
    encoded = urllib.parse.quote(full_query)
    return f"https://www.youtube.com/results?search_query={encoded}"


def search_youtube_by_url(search_url: str, max_results: int = 30) -> list[dict]:
    """
    Use yt-dlp to extract video entries from a YouTube search results URL.
    Returns a list of video dicts with metadata.
    """
    cmd = [
        "yt-dlp",
        "--dump-json",
        "--flat-playlist",
        "--no-download",
        "--no-warnings",
        "--playlist-end", str(max_results),
        search_url,
    ]

    log.info("  yt-dlp extracting: %s", search_url)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        log.error("yt-dlp timed out for URL: %s", search_url)
        return []

    if result.returncode != 0:
        log.warning("yt-dlp returned code %d", result.returncode)
        if result.stderr:
            log.warning("stderr: %s", result.stderr[:500])

    videos = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        video = {
            "id": data.get("id", ""),
            "title": data.get("title", "(no title)"),
            "url": data.get("webpage_url") or data.get("url") or "",
            "upload_date": data.get("upload_date", ""),
            "description": (data.get("description") or "")[:300],
            "channel": data.get("channel") or data.get("uploader") or "",
            "view_count": data.get("view_count") or 0,
            "duration": data.get("duration") or 0,
            "thumbnail": data.get("thumbnail") or "",
        }

        # Ensure we have a proper URL
        if video["id"] and not video["url"]:
            video["url"] = f"https://www.youtube.com/watch?v={video['id']}"

        if video["id"]:
            videos.append(video)

    return videos


def enrich_videos(video_ids: list[str]) -> dict[str, dict]:
    """
    Fetch full metadata (view_count, description, thumbnail, upload_date)
    for a list of video IDs. No date filtering — we trust YouTube's search
    already filtered by date. Returns a dict keyed by video ID.
    """
    if not video_ids:
        return {}

    urls = [f"https://www.youtube.com/watch?v={vid}" for vid in video_ids]

    cmd = [
        "yt-dlp",
        "--dump-json",
        "--no-download",
        "--no-warnings",
        "--ignore-errors",
    ] + urls

    log.info("Enriching %d videos with full metadata...", len(video_ids))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        log.error("yt-dlp enrichment timed out")
        return {}

    enriched = {}
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        vid = data.get("id", "")
        if vid:
            enriched[vid] = {
                "id": vid,
                "title": data.get("title", "(no title)"),
                "url": data.get("webpage_url") or f"https://www.youtube.com/watch?v={vid}",
                "upload_date": data.get("upload_date", ""),
                "description": (data.get("description") or "")[:300],
                "channel": data.get("channel") or data.get("uploader") or "",
                "view_count": data.get("view_count") or 0,
                "duration": data.get("duration") or 0,
                "thumbnail": data.get("thumbnail") or "",
            }

    log.info("  Enriched %d / %d videos", len(enriched), len(video_ids))
    return enriched


def find_top_videos(start: datetime, end: datetime) -> list[dict]:
    """
    Search YouTube with date operators, deduplicate across queries,
    enrich with full metadata, and return the top 10 by view count.
    """
    after_str = start.strftime("%Y-%m-%d")
    before_str = end.strftime("%Y-%m-%d")

    # Phase 1: Collect candidate video IDs via YouTube search URLs
    candidates = {}  # id -> video dict
    for query in SEARCH_QUERIES:
        url = build_youtube_search_url(query, after_str, before_str)
        log.info("Searching: '%s' (%s to %s)", query, after_str, before_str)
        results = search_youtube_by_url(url, max_results=30)
        log.info("  Got %d results for '%s'", len(results), query)
        for v in results:
            vid = v["id"]
            if vid not in candidates:
                candidates[vid] = v

    log.info("Total unique candidates: %d", len(candidates))

    if not candidates:
        log.warning("No candidates found.")
        return []

    # Phase 2: Enrich with full metadata (view_count, etc.)
    # Flat-playlist mode often lacks view_count, so we fetch full details.
    # Process in batches.
    all_ids = list(candidates.keys())
    enriched = {}
    batch_size = 20

    for i in range(0, len(all_ids), batch_size):
        batch = all_ids[i:i + batch_size]
        batch_enriched = enrich_videos(batch)
        enriched.update(batch_enriched)

    # Merge: prefer enriched data, fall back to flat data
    final = []
    for vid, flat in candidates.items():
        if vid in enriched:
            final.append(enriched[vid])
        else:
            # Use flat data as-is (may lack view_count)
            final.append(flat)

    # Sort by view count descending, take top N
    final.sort(key=lambda v: v.get("view_count", 0), reverse=True)
    top = final[:VIDEOS_PER_RUN]

    log.info("Top %d videos selected:", len(top))
    for v in top:
        views = v.get("view_count", 0)
        log.info("  [%s views] %s — %s",
                 f"{views:,}" if views else "?", v["title"], v["url"])

    return top


# ---------------------------------------------------------------------------
# Feed item persistence (JSON)
# ---------------------------------------------------------------------------


def load_feed_items() -> list[dict]:
    """Load the persistent list of feed items."""
    path = Path(FEED_DB_PATH)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning("Corrupt feed DB; starting fresh.")
    return []


def save_feed_items(items: list[dict]) -> None:
    """Save the persistent list of feed items."""
    path = Path(FEED_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, indent=2))


def merge_items(existing: list[dict], new_videos: list[dict]) -> list[dict]:
    """
    Merge new videos into the existing feed items, applying:
      1. Deduplication by video ID
      2. Expiry: remove items older than RETENTION_DAYS
      3. Cap at MAX_ITEMS (oldest removed first)
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=RETENTION_DAYS)
    cutoff_iso = cutoff.isoformat()

    seen_ids = set()
    merged = []

    # Keep existing items that haven't expired
    for item in existing:
        added = item.get("added_at", "")
        if added and added < cutoff_iso:
            log.info("Expiring old item: %s", item.get("title", "?"))
            continue
        seen_ids.add(item.get("video_id", ""))
        merged.append(item)

    # Add new videos
    now_iso = now.isoformat()
    for v in new_videos:
        vid = v.get("id", "")
        if vid in seen_ids:
            log.info("Skipping duplicate: %s", v.get("title", "?"))
            continue
        seen_ids.add(vid)

        # Convert upload_date (YYYYMMDD) to a display date
        ud = v.get("upload_date", "")
        if len(ud) == 8:
            pub_date = f"{ud[:4]}-{ud[4:6]}-{ud[6:8]}"
        else:
            pub_date = ud

        merged.append({
            "video_id": vid,
            "title": v.get("title", "(no title)"),
            "url": v.get("url", ""),
            "channel": v.get("channel", ""),
            "description": v.get("description", ""),
            "view_count": v.get("view_count", 0),
            "upload_date": pub_date,
            "thumbnail": v.get("thumbnail", ""),
            "added_at": now_iso,
        })

    # Sort by added_at descending (newest first)
    merged.sort(key=lambda x: x.get("added_at", ""), reverse=True)

    # Cap at MAX_ITEMS (drop oldest)
    if len(merged) > MAX_ITEMS:
        dropped = merged[MAX_ITEMS:]
        for d in dropped:
            log.info("Dropping overflow item: %s", d.get("title", "?"))
        merged = merged[:MAX_ITEMS]

    return merged


# ---------------------------------------------------------------------------
# RSS feed generation
# ---------------------------------------------------------------------------


def generate_feed(items: list[dict]) -> None:
    """Build the RSS feed XML from the current item list."""
    fg = FeedGenerator()
    fg.title("D&D Time Capsule")
    fg.link(href="https://github.com")  # Update to your repo URL
    fg.description(
        "Top D&D videos from YouTube, 10 years ago this week. "
        "A weekly time capsule of the tabletop RPG community."
    )
    fg.language("en")
    fg.lastBuildDate(datetime.now(timezone.utc))

    for item in items:
        fe = fg.add_entry()
        fe.title(item.get("title", "(no title)"))

        url = item.get("url", "")
        fe.link(href=url)
        fe.id(url)

        # Build a rich description
        channel = item.get("channel", "")
        views = item.get("view_count", 0)
        upload_date = item.get("upload_date", "")
        desc = item.get("description", "")
        thumbnail = item.get("thumbnail", "")

        desc_parts = []
        if thumbnail:
            desc_parts.append(f'<p><img src="{thumbnail}" alt="{item.get("title", "")}" width="480" /></p>')
        if channel:
            desc_parts.append(f"<p><strong>Channel:</strong> {channel}</p>")
        if upload_date:
            desc_parts.append(f"<p><strong>Originally uploaded:</strong> {upload_date}</p>")
        if views:
            desc_parts.append(f"<p><strong>Views:</strong> {views:,}</p>")
        if desc:
            desc_parts.append(f"<p>{desc}</p>")

        fe.description("\n".join(desc_parts))

        # Use the added_at timestamp as pubDate
        added_at = item.get("added_at", "")
        if added_at:
            try:
                dt = datetime.fromisoformat(added_at)
                fe.pubDate(dt)
            except ValueError:
                fe.pubDate(datetime.now(timezone.utc))

        fe.category(term="D&D", label="Dungeons & Dragons")

    # Write output
    output_path = Path(OUTPUT_FILE)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fg.rss_file(str(output_path))
    log.info("Wrote %d entries to %s", len(items), OUTPUT_FILE)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    log.info("=" * 60)
    log.info("D&D Time Capsule — run started at %s", datetime.now(timezone.utc).isoformat())
    log.info("=" * 60)

    # Calculate the search window
    start, end = get_search_window()
    log.info("Search window: %s to %s (10 years ago this week)",
             start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))

    # Search YouTube
    top_videos = find_top_videos(start, end)

    if not top_videos:
        log.warning("No videos found for this window. Feed will still be refreshed (expiry only).")

    # Load existing feed items
    existing = load_feed_items()
    log.info("Existing feed items: %d", len(existing))

    # Merge new + existing, apply expiry and cap
    merged = merge_items(existing, top_videos)
    log.info("Feed items after merge: %d", len(merged))

    # Save updated items DB
    save_feed_items(merged)

    # Generate RSS feed
    generate_feed(merged)

    log.info("Done. Added %d new videos this run.", len(top_videos))


if __name__ == "__main__":
    main()
