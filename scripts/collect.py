"""Collect observability news from official feeds into the item registry.

Reads config/sources.json, fetches every enabled feed, normalizes entries,
applies the MVP editorial filters and merges new items into
data/news-registry.json. This script never sends emails.

Usage:
    python3 scripts/collect.py             # collect and update the registry
    python3 scripts/collect.py --dry-run   # print a summary, write nothing
"""

import json
import re
import ssl
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import certifi

CONFIG_PATH = Path("config/sources.json")
REGISTRY_PATH = Path("data/news-registry.json")

PRERELEASE_PATTERN = re.compile(
    r"[-.](rc|alpha|beta|nightly|preview)", re.IGNORECASE
)
COMPONENT_PATTERN = re.compile(r"^cmd/")
ANNOUNCEMENT_PATTERN = re.compile(
    r"(announc|releas|generally available|now available|launch|introducing)",
    re.IGNORECASE,
)
COMMUNITY_PATTERN = re.compile(
    r"(working group|election|governance|survey|kubecon|humans of|"
    r"community|meetup|podcast|webinar|meeting|office hours)",
    re.IGNORECASE,
)

EXCLUDED_PRERELEASE = "prerelease_or_nightly"
EXCLUDED_COMPONENT = "component_release"
EXCLUDED_NOT_ANNOUNCEMENT = "not_a_product_announcement"
EXCLUDED_COMMUNITY = "community_content"


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def child_text(element, names):
    for child in element:
        if local_name(child.tag) in names:
            return (child.text or "").strip()
    return ""


def entry_link(element):
    for child in element:
        if local_name(child.tag) == "link":
            href = child.attrib.get("href")
            if href:
                return href.strip()
            return (child.text or "").strip()
    return ""


def parse_entries(xml_content):
    root = ET.fromstring(xml_content)
    entries = []

    for element in root.iter():
        if local_name(element.tag) not in {"item", "entry"}:
            continue

        entries.append({
            "entry_id": child_text(element, {"id", "guid"}),
            "title": child_text(element, {"title"}),
            "link": entry_link(element),
            "date": child_text(element, {"pubDate", "published", "updated", "date"}),
        })

    return entries


def fetch_feed(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "ObservabilityNewsCollector/1.0"},
    )

    with urllib.request.urlopen(
        request,
        timeout=30,
        context=ssl.create_default_context(cafile=certifi.where()),
    ) as response:
        return parse_entries(response.read())


def normalize_date(raw_date):
    if not raw_date:
        return None

    parsed = None

    try:
        parsed = parsedate_to_datetime(raw_date)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
        except ValueError:
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc).isoformat()


def normalize_url(url):
    if not url:
        return ""

    parts = urlsplit(url.strip())
    query = "&".join(
        part for part in parts.query.split("&")
        if part and not part.lower().startswith(("utm_",))
    )

    return urlunsplit((
        parts.scheme.lower(),
        parts.netloc.lower(),
        parts.path.rstrip("/"),
        query,
        "",
    ))


def apply_filters(source, entry, item):
    if source["type"] == "releases":
        if COMPONENT_PATTERN.match(entry["title"]):
            item["status"] = "excluded"
            item["exclusion_reason"] = EXCLUDED_COMPONENT
            return

        release_text = f"{entry['title']} {entry['link']} {entry['entry_id']}"

        if PRERELEASE_PATTERN.search(release_text):
            item["status"] = "excluded"
            item["exclusion_reason"] = EXCLUDED_PRERELEASE
            return

        return

    # Blog sources: the MVP digest only carries product announcements.
    if COMMUNITY_PATTERN.search(entry["title"]):
        item["status"] = "excluded"
        item["exclusion_reason"] = EXCLUDED_COMMUNITY
        return

    if not ANNOUNCEMENT_PATTERN.search(entry["title"]):
        item["status"] = "excluded"
        item["exclusion_reason"] = EXCLUDED_NOT_ANNOUNCEMENT


def build_item(source, entry, seen_at):
    item = {
        "source": source["id"],
        "source_url": source["url"],
        "source_item_id": entry["entry_id"] or None,
        "title": entry["title"],
        "published_at": normalize_date(entry["date"]),
        "tool": source["tool"],
        "canonical_url": normalize_url(entry["link"]),
        "status": "eligible",
        "exclusion_reason": None,
        "first_seen_at": seen_at,
        "sent_at": None,
        "related_urls": [],
    }

    apply_filters(source, entry, item)
    return item


def is_duplicate(existing, source_id, entry_id_value, canonical_url):
    if existing["source"] != source_id:
        return False

    if entry_id_value and existing.get("source_item_id") == entry_id_value:
        return True

    return bool(canonical_url) and canonical_url == existing.get("canonical_url", "")


def main():
    dry_run = "--dry-run" in sys.argv
    now = datetime.now(timezone.utc).isoformat()

    if not CONFIG_PATH.is_file() or not REGISTRY_PATH.is_file():
        print("Missing config/sources.json or data/news-registry.json")
        return 1

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    items = registry.get("items", [])

    for source in config["sources"]:
        if not source.get("enabled", False):
            continue

        try:
            entries = fetch_feed(source["url"])
        except Exception as error:
            print(f"{source['id']}: FETCH ERROR: {error}")
            continue

        stats = {"fetched": len(entries), "new": 0, "eligible": 0, "excluded": 0, "duplicate": 0}

        for entry in entries:
            entry_id_value = entry["entry_id"] or entry["link"]
            canonical_url = normalize_url(entry["link"])

            if any(
                is_duplicate(existing, source["id"], entry_id_value, canonical_url)
                for existing in items
            ):
                stats["duplicate"] += 1
                continue

            item = build_item(source, entry, now)
            items.append(item)
            stats["new"] += 1

            if item["status"] == "eligible":
                stats["eligible"] += 1
            else:
                stats["excluded"] += 1

        print(
            f"{source['id']}: fetched={stats['fetched']} new={stats['new']} "
            f"eligible={stats['eligible']} excluded={stats['excluded']} "
            f"duplicate={stats['duplicate']}"
        )

    if dry_run:
        print("Dry run: registry not updated.")
        return 0

    registry["items"] = items
    registry["last_successful_run"] = now
    REGISTRY_PATH.write_text(
        json.dumps(registry, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Registry updated: {len(items)} items total.")

    return 0


if __name__ == "__main__":
    sys.exit(main())