"""Build news.html and refresh index.html from the item registry.

Reads data/news-registry.json, keeps eligible items and renders them into
a static news.html page using the site's design system. It also injects
the latest headlines and live counts into index.html between
<!-- generated:... --> markers, so the landing page always shows real
data. Run after collect.py (locally or in GitHub Actions). This script
never sends emails.

Usage:
    python3 scripts/build_site.py
"""

import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path

REGISTRY_PATH = Path("data/news-registry.json")
NEWS_PATH = Path("news.html")
INDEX_PATH = Path("index.html")
MAX_ITEMS = 100
HERO_ITEMS = 3

SOURCE_LABELS = {
    "prometheus_blog": "Prometheus Blog",
    "prometheus_releases": "Prometheus Releases",
    "opentelemetry_blog": "OpenTelemetry Blog",
    "opentelemetry_collector_releases": "OpenTelemetry Collector Releases",
    "dynatrace_product_news": "Dynatrace Product News",
    "dynatrace_release_notes": "Dynatrace Release Notes",
}

STYLE = """
    :root {
      --paper: #f4f7fb;
      --surface: #ffffff;
      --ink: #0f172a;
      --muted: #64748b;
      --accent: #2563eb;
      --line: #e2e8f0;
      --radius: 14px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: 'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif;
      background: var(--paper);
      color: var(--ink);
      line-height: 1.6;
      -webkit-font-smoothing: antialiased;
    }
    .container { max-width: 840px; margin: 0 auto; padding: 0 24px; }
    body::before {
      content: "";
      position: fixed;
      top: 0; left: 0;
      width: 100%; height: 100%;
      z-index: 999;
      pointer-events: none;
      opacity: 0.04;
      background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='160' height='160' filter='url(%23n)'/%3E%3C/svg%3E");
    }
    .topbar {
      position: sticky; top: 0; z-index: 10;
      background: rgba(244, 247, 251, 0.92);
      backdrop-filter: blur(6px);
      border-bottom: 1px solid var(--line);
    }
    .topbar-inner { display: flex; align-items: center; justify-content: space-between; height: 64px; }
    .brand {
      display: flex; align-items: center; gap: 10px;
      font-weight: 600; letter-spacing: -0.01em;
      color: inherit; text-decoration: none;
    }
    .brand-dot {
      width: 10px; height: 10px; border-radius: 50%;
      background: var(--accent);
      box-shadow: 0 0 0 4px rgba(37, 99, 235, 0.15);
    }
    .nav-link { font-size: 15px; color: var(--ink); text-decoration: none; font-weight: 500; }
    .nav-link:hover { color: var(--accent); }
    h1 {
      font-family: 'Newsreader', Georgia, serif;
      font-size: clamp(30px, 4vw, 42px);
      line-height: 1.12; letter-spacing: -0.02em;
      margin: 40px 0 10px; font-weight: 600;
    }
    .stats { font-size: 15px; color: var(--muted); margin: 0 0 32px; }
    .item {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 20px 24px;
      margin-bottom: 14px;
    }
    .item h2 {
      margin: 8px 0 4px;
      font-size: 17px;
      line-height: 1.4;
    }
    .item h2 a { color: var(--ink); text-decoration: none; }
    .item h2 a:hover { color: var(--accent); }
    .meta { margin: 0; font-size: 13px; color: var(--muted); }
    .tag {
      display: inline-block; font-size: 12px; font-weight: 600;
      color: var(--accent); background: rgba(37, 99, 235, 0.08);
      padding: 2px 10px; border-radius: 999px;
    }
    .empty {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 32px; color: var(--muted);
    }
    .limit-note { font-size: 13px; color: var(--muted); margin: 8px 0 40px; }
    footer {
      border-top: 1px solid var(--line);
      padding: 28px 0; font-size: 14px; color: var(--muted);
    }
    footer a { color: var(--muted); }
"""


def format_date(iso_string):
    try:
        return datetime.fromisoformat(iso_string).strftime("%b %d, %Y")
    except (TypeError, ValueError):
        return ""


def source_label(source):
    return SOURCE_LABELS.get(source, (source or "").replace("_", " ").title())


def render_item(item):
    tool = html.escape(item.get("tool") or "News")
    title = html.escape(item.get("title") or "Untitled")
    url = html.escape(item.get("canonical_url") or "#", quote=True)
    source = html.escape(source_label(item.get("source")))
    date = html.escape(format_date(item.get("published_at")))
    meta = " · ".join(part for part in (source, date) if part)
    return (
        '      <article class="item">\n'
        f'        <span class="tag">{tool}</span>\n'
        f'        <h2><a href="{url}" target="_blank" rel="noopener">{title}</a></h2>\n'
        f'        <p class="meta">{meta}</p>\n'
        "      </article>"
    )


def inject(page, name, replacement):
    """Replace the content between the generated markers for `name`,
    keeping the markers themselves in place.

    Matching is whitespace-tolerant: copy-paste can introduce extra
    spaces or non-breaking spaces that look identical but break an
    exact string comparison, so we match with a regex instead.
    """
    start_re = re.compile(
        r"<!--\s*generated\s*:\s*" + re.escape(name) + r"\s*:\s*start\s*-->"
    )
    end_re = re.compile(
        r"<!--\s*generated\s*:\s*" + re.escape(name) + r"\s*:\s*end\s*-->"
    )
    start_match = start_re.search(page)
    if not start_match:
        print(f"Warning: markers for '{name}' not found in index.html, skipped.")
        return page
    end_match = end_re.search(page, start_match.end())
    if not end_match:
        print(f"Warning: end marker for '{name}' not found in index.html, skipped.")
        return page
    return page[: start_match.end()] + replacement + page[end_match.start() :]


def render_hero_item(item):
    tool = html.escape(item.get("tool") or "News")
    title = html.escape(item.get("title") or "Untitled")
    url = html.escape(item.get("canonical_url") or "#", quote=True)
    return (
        '        <div class="preview-item">\n'
        f'          <span class="tag">{tool}</span>\n'
        f'          <h4><a href="{url}" target="_blank" rel="noopener">{title}</a></h4>\n'
        "        </div>"
    )


def update_index(items):
    if not INDEX_PATH.exists():
        print("index.html not found, skipped.")
        return

    page = INDEX_PATH.read_text(encoding="utf-8")
    total = len(items)
    hero = items[:HERO_ITEMS]

    updated = datetime.now(timezone.utc).strftime("%B %d, %Y")
    page = inject(page, "updated", updated)

    if hero:
        preview = "\n".join(render_hero_item(item) for item in hero)
    else:
        preview = (
            '        <div class="preview-item">\n'
            '          <span class="tag">Observability News</span>\n'
            '          <h4>No items yet</h4>\n'
            '          <p>Collection runs twice each morning.</p>\n'
            "        </div>"
        )
    page = inject(page, "preview", preview)

    if hero:
        note = (
            f'The latest {len(hero)} of {total} items · '
            '<a href="news.html">browse all the news</a>'
        )
    else:
        note = "No items collected yet · check back after the next scheduled run."
    page = inject(page, "preview-note", note)

    page = inject(
        page,
        "stats",
        f"Tracking {total} announcements and releases · "
        "updated daily from official feeds · collected twice each morning · "
        "more sources coming.",
    )

    INDEX_PATH.write_text(page, encoding="utf-8")
    print(f"index.html updated: hero shows {len(hero)} latest items of {total}.")


def main():
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    items = [i for i in registry.get("items", []) if i.get("status") == "eligible"]
    items.sort(key=lambda i: i.get("published_at") or "", reverse=True)

    total = len(items)
    shown = items[:MAX_ITEMS]
    generated = datetime.now(timezone.utc).strftime("%B %d, %Y")

    if shown:
        items_html = "\n".join(render_item(item) for item in shown)
        limit_note = (
            f'      <p class="limit-note">Showing the {MAX_ITEMS} most recent of {total} items.</p>'
            if total > MAX_ITEMS
            else ""
        )
    else:
        items_html = (
            '      <p class="empty">No eligible items yet. '
            'Collection runs twice each morning.</p>'
        )
        limit_note = ""

    stats = f"{total} announcements and releases · Updated {generated}"

    head = (
        '<!DOCTYPE html>\n'
        '<html lang="en">\n'
        '<head>\n'
        '  <meta charset="UTF-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        '  <title>Observability News · Latest announcements and releases</title>\n'
        '  <meta name="description" content="Product announcements and releases from Prometheus, OpenTelemetry, and Dynatrace, collected daily from official feeds.">\n'
        '  <link rel="preconnect" href="https://fonts.googleapis.com">\n'
        '  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
        '  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Newsreader:opsz,wght@6..72,500;6..72,600&display=swap" rel="stylesheet">\n'
        '  <style>' + STYLE + '  </style>\n'
        '</head>\n'
        '<body>\n'
        '\n'
        '  <header class="topbar">\n'
        '    <div class="container topbar-inner">\n'
        '      <a class="brand" href="index.html"><span class="brand-dot"></span>Observability News</a>\n'
        '      <a class="nav-link" href="index.html">About the project</a>\n'
        '    </div>\n'
        '  </header>\n'
        '\n'
        '  <main class="container">\n'
        '    <h1>Latest announcements and releases</h1>\n'
    )

    stats_html = f'    <p class="stats">{html.escape(stats)}</p>\n'

    tail = (
        limit_note
        + '  </main>\n'
        '\n'
        '  <footer>\n'
        '    <div class="container">\n'
        '      <span>© 2026 Observability News · Daily observability curation · </span>'
        '<a href="index.html">About the project</a>\n'
        '    </div>\n'
        '  </footer>\n'
        '\n'
        '</body>\n'
        '</html>\n'
    )

    NEWS_PATH.write_text(head + stats_html + items_html + "\n" + tail, encoding="utf-8")
    print(f"news.html written: {total} eligible items, {len(shown)} shown.")

    update_index(items)


if __name__ == "__main__":
    main()