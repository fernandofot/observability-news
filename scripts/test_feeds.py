import ssl
import certifi
import json
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

CONFIG_PATH = Path("config/sources.json")


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
                return href
            return (child.text or "").strip()
    return ""


def parse_entries(xml_content):
    root = ET.fromstring(xml_content)
    entries = []

    for element in root.iter():
        if local_name(element.tag) not in {"item", "entry"}:
            continue

        entries.append({
            "title": child_text(element, {"title"}),
            "link": entry_link(element),
            "date": child_text(
                element,
                {"pubDate", "published", "updated", "date"}
            ),
        })

    return entries


def fetch_feed(source):
    request = urllib.request.Request(
        source["url"],
        headers={"User-Agent": "ObservabilityNewsFeedCheck/1.0"},
    )

    with urllib.request.urlopen(request, timeout=30, context=ssl.create_default_context(cafile=certifi.where()),) as response:
        return parse_entries(response.read())


def main():
    if not CONFIG_PATH.is_file():
        print(f"Config file not found: {CONFIG_PATH}")
        return 1

    with CONFIG_PATH.open(encoding="utf-8") as file:
        config = json.load(file)

    failures = 0

    for source in config["sources"]:
        if not source.get("enabled", False):
            continue

        print(f"\n{source['id']} — {source['url']}")

        try:
            entries = fetch_feed(source)
            print(f"Fetched {len(entries)} entries")

            for entry in entries[:3]:
                print(f"- {entry['title'] or '(no title)'}")
                print(f"  {entry['date'] or '(no date)'}")
                print(f"  {entry['link'] or '(no link)'}")

        except Exception as error:
            failures += 1
            print(f"ERROR: {error}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())