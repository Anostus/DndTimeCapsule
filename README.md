# D&D Time Capsule

An RSS feed that surfaces the top Dungeons & Dragons videos from YouTube — from exactly 10 years ago this week.

Every Sunday, a GitHub Action searches YouTube for D&D content uploaded during a 7-day window corresponding to the current week a decade prior, grabs the top 10 videos by view count, and adds them to a rolling RSS feed.

## Feed Rules

- **New items:** 10 videos added per weekly run
- **Expiry:** Items are removed after 30 days
- **Cap:** Maximum 25 items in the feed at any time (oldest dropped first)

## How It Works

1. Calculates a 7-day date window: today minus 10 years, ±3 days
2. Uses `yt-dlp` to search YouTube for D&D-related videos uploaded in that window
3. Filters and ranks results by view count
4. Merges the top 10 into the existing feed, applying expiry and cap rules
5. Commits the updated `docs/feed.xml` back to the repo

## RSS Feed URL

Enable GitHub Pages for the `docs/` folder, then your feed will be at:

```
https://<your-username>.github.io/<repo-name>/feed.xml
```

## Manual Run

You can trigger the workflow manually from the Actions tab in GitHub.

## No API Keys Required

This project uses `yt-dlp` for YouTube search — no YouTube Data API key or any other API key is needed.

## Files

| File | Purpose |
|------|---------|
| `dnd_time_capsule.py` | Main script |
| `data/feed_items.json` | Persistent feed item database (committed by the Action) |
| `docs/feed.xml` | The RSS feed output (serve via GitHub Pages) |
| `.github/workflows/dnd_time_capsule.yml` | Weekly GitHub Action |
