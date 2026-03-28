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
"""

import json
import logging
import subprocess
import sys
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

# Search queries to try (we'll take the top results across these)
SEARCH_QUERIES = [
    "Dungeons and Dragons",
    "D&D tabletop",
    "D&D campaign",
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
    The window runs from 3 days before to 4 days after the anniversary date
    so we get a full week.
    """
    today = datetime.now(timezone.utc).date()
    try:
        anniversary = today.replace(year=today.year - YEARS_AGO)
    except ValueError:
        # Handle Feb 29 → Feb 28
        anniversary = today.replace(year=today.year - YEARS_AGO, day=28)

    start = anniversary - timedelta(days=3)
    end = anniversary + timedelta(days=4)  # exclusive upper bound for search
    return (
        datetime(start.year, start.month, start.day, tzinfo=timezone.utc),
        datetime(end.year, end.month, end.day, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# YouTube search via yt-dlp
# ---------------------------------------------------------------------------


def search_youtube(query: str, start: datetime, end: datetime, max_results: int = 10) -> list[dict]:
    """
    Use yt-dlp to search YouTube and return video metadata.
    We use ytsearch to get candidates, then filter by upload date.
    """
    # yt-dlp's daterange filter uses YYYYMMDD format
    date_start = start.strftime("%Y%m%d")
    date_end = end.strftime("%Y%m%d")

    # Request more than we need since we'll filter by date
    search_term = f"ytsearch50:{query}"

    cmd = [
        "yt-dlp",
        "--dump-json",
        "--flat-playlist",
        "--no-download",
        "--no-warnings",
        search_term,
    ]

    log.info("Searching YouTube: %s (window %s to %s)", query, date_start, date_end)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        log.error("yt-dlp search timed out for query: %s", query)
        return []

    if result.returncode != 0:
        log.warning("yt-dlp returned code %d for query: %s", result.returncode, query)
        log.warning("stderr: %s", result.stderr[:500] if result.stderr else "(none)")

    videos = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        # flat-playlist gives us limited info; we need the upload_date
        # If upload_date is present in flat mode, use it; otherwise we'll
        # do a second pass for promising candidates
        upload_date = data.get("upload_date", "")

        video = {
            "id": data.get("id", ""),
            "title": data.get("title", "(no title)"),
            "url": data.get("url") or data.get("webpage_url") or f"https://www.youtube.com/watch?v={data.get('id', '')}",
            "upload_date": upload_date,
            "description": (data.get("description") or "")[:300],
            "channel": data.get("channel") or data.get("uploader") or "",
            "view_count": data.get("view_count") or 0,
            "duration": data.get("duration") or 0,
        }

        if video["id"]:
            videos.append(video)

    log.info("  Got %d raw results for '%s'", len(videos), query)
    return videos


def fetch_video_details(video_ids: list[str], start: datetime, end: datetime) -> list[dict]:
    """
    Fetch full metadata for specific video IDs to get accurate upload dates.
    Filter to only those within our date window.
    """
    date_start = start.strftime("%Y%m%d")
    date_end = end.strftime("%Y%m%d")

    urls = [f"https://www.youtube.com/watch?v={vid}" for vid in video_ids]

    cmd = [
        "yt-dlp",
        "--dump-json",
        "--no-download",
        "--no-warnings",
        "--dateafter", date_start,
        "--datebefore", date_end,
    ] + urls

    log.info("Fetching details for %d videos (filtering %s to %s)...",
             len(video_ids), date_start, date_end)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        log.error("yt-dlp detail fetch timed out")
        return []

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
            "url": data.get("webpage_url") or f"https://www.youtube.com/watch?v={data.get('id', '')}",
            "upload_date": data.get("upload_date", ""),
            "description": (data.get("description") or "")[:300],
            "channel": data.get("channel") or data.get("uploader") or "",
            "view_count": data.get("view_count") or 0,
            "duration": data.get("duration") or 0,
            "thumbnail": data.get("thumbnail") or "",
        }

        videos.append(video)

    log.info("  %d videos matched the date window", len(videos))
    return videos


def find_top_videos(start: datetime, end: datetime) -> list[dict]:
    """
    Search across multiple queries, deduplicate, fetch details with date
    filtering, and return the top 10 by view count.
    """
    # Phase 1: Collect candidate video IDs from flat search
    candidate_ids = {}
    for query in SEARCH_QUERIES:
        results = search_youtube(query, start, end)
        for v in results:
            vid = v["id"]
            if vid not in candidate_ids:
                candidate_ids[vid] = v

    log.info("Total unique candidates across all queries: %d", len(candidate_ids))

    if not candidate_ids:
        log.warning("No candidates found at all.")
        return []

    # Phase 2: Fetch full details with date filtering
    # Process in batches to avoid overwhelming yt-dlp
    all_ids = list(candidate_ids.keys())
    detailed = []
    batch_size = 25

    for i in range(0, len(all_ids), batch_size):
        batch = all_ids[i:i + batch_size]
        batch_results = fetch_video_details(batch, start, end)
        detailed.extend(batch_results)

    if not detailed:
        # Fallback: if detail fetching found nothing (maybe date filtering
        # was too strict or yt-dlp couldn't get dates), use flat results
        # that have upload_date in the right range
        date_start = start.strftime("%Y%m%d")
        date_end = end.strftime("%Y%m%d")
        log.info("Detail fetch returned nothing; falling back to flat results with date check")
        for v in candidate_ids.values():
            ud = v.get("upload_date", "")
            if ud and date_start <= ud <= date_end:
                detailed.append(v)

    # Sort by view count descending and take top N
    detailed.sort(key=lambda v: v.get("view_count", 0), reverse=True)
    top = detailed[:VIDEOS_PER_RUN]

    log.info("Top %d videos selected:", len(top))
    for v in top:
        log.info("  [%s views] %s — %s",
                 f"{v.get('view_count', 0):,}", v["title"], v["url"])

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

    # Build lookup of existing IDs
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

        # Use the added_at timestamp as pubDate so feed readers sort correctly
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
