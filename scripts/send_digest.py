#!/usr/bin/env python3
"""Send the Observability News digest to subscribers.

Reads data/news-registry.json, selects eligible items that were never sent,
renders an HTML and a plain-text digest and delivers it through the
configured transport. Items get sent_at only after the transport confirms
delivery, so a failed run can be retried safely. The status stays "eligible"
on purpose: build_site.py only renders eligible items, and a sent item must
not vanish from the site.

Usage:
    python3 scripts/send_digest.py --dry-run
    python3 scripts/send_digest.py --to you@example.com
    python3 scripts/send_digest.py

Environment:
    DIGEST_SUBSCRIBERS  JSON array of {"email": "...", "tools": ["Grafana"]}.
                        Omit "tools" to receive every tool.
    DIGEST_TRANSPORT    "smtp" or "resend" (default: smtp)
    DIGEST_FROM         sender address (default: observabilitynews@gmail.com)
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD
    RESEND_API_KEY
"""

import argparse
import html
import json
import os
import re
import smtplib
import sys
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

REGISTRY_PATH = Path("data/news-registry.json")
PREVIEW_PATH = Path("digest-preview.html")
SITE_URL = "https://fernandofot.github.io/observability-news"
RECENCY_DAYS = 30
DIGEST_MAX_ITEMS = 20
DIGEST_PER_TOOL = 5
DIGEST_PER_TOOL_DAY = 2
DEFAULT_FROM = "observabilitynews@gmail.com"
UNSUBSCRIBE_MAILTO = "observabilitynews@gmail.com"

MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Observability News</title>
</head>
<body style="margin:0;padding:0;background:#f4f7fb;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f7fb;padding:24px 12px;">
    <tr><td align="center">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;border:1px solid #e2e8f0;border-radius:14px;">
        <tr><td style="padding:28px;">
          <div style="font:600 12px/1.4 Arial,sans-serif;color:#2563eb;text-transform:uppercase;letter-spacing:.1em;">Observability News</div>
          <h1 style="margin:8px 0 4px;font:600 24px/1.2 Georgia,serif;color:#0f172a;">__HEADLINE__</h1>
          <p style="margin:0 0 4px;font:400 14px/1.6 Arial,sans-serif;color:#64748b;">__INTRO__</p>
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0">__BODY__</table>
          <p style="margin:24px 0 0;font:400 14px/1.6 Arial,sans-serif;color:#64748b;">
            <a href="__SITE__/news.html" style="color:#2563eb;font-weight:600;text-decoration:none;">Browse all the news</a>
          </p>
        </td></tr>
      </table>
      <p style="max-width:600px;margin:16px auto 0;font:400 12px/1.6 Arial,sans-serif;color:#94a3b8;text-align:center;">
        You receive this because you subscribed to Observability News.
        <a href="mailto:__UNSUB__?subject=unsubscribe" style="color:#94a3b8;">Unsubscribe</a>.
      </p>
    </td></tr>
  </table>
</body>
</html>"""


def parse_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_date(value):
    parsed = parse_datetime(value)
    if parsed is None:
        return ""
    return f"{MONTHS[parsed.month - 1]} {parsed.day}, {parsed.year}"


BARE_VERSION = re.compile(r"^v?\d+(?:\.\d+)*(?:\s*/\s*\d{4}-\d{2}-\d{2})?$")


def display_title(item):
    """Prefix bare version titles with the tool name.

    Feeds such as Prometheus and the Collector publish titles that are only a
    version string, sometimes with the release date appended. On their own they
    read as noise in an email, so "3.16.0 / 2026-11-04" becomes
    "Prometheus 3.16.0".
    """
    tool = item.get("tool") or "Unknown"
    title = (item.get("title") or "").strip()
    if not title:
        return "Untitled"
    if BARE_VERSION.match(title):
        version = title.split("/")[0].strip()
        return f"{tool} {version}"
    return title


def detect_indent(raw):
    for line in raw.splitlines()[1:]:
        stripped = line.lstrip(" ")
        if stripped and len(line) > len(stripped):
            return len(line) - len(stripped)
    return 2


def select_items(items, now):
    cutoff = now - timedelta(days=RECENCY_DAYS)
    fresh = []
    for item in items:
        if item.get("status") != "eligible" or item.get("sent_at"):
            continue
        published = parse_datetime(item.get("published_at"))
        if published is None or published < cutoff:
            continue
        fresh.append((published, item))
    fresh.sort(key=lambda pair: pair[0], reverse=True)

    per_tool = Counter()
    per_tool_day = Counter()
    chosen = []
    for published, item in fresh:
        tool = item.get("tool") or "Unknown"
        if per_tool[tool] >= DIGEST_PER_TOOL:
            continue
        day = (tool, published.date())
        if per_tool_day[day] >= DIGEST_PER_TOOL_DAY:
            continue
        chosen.append(item)
        per_tool[tool] += 1
        per_tool_day[day] += 1
        if len(chosen) >= DIGEST_MAX_ITEMS:
            break
    return chosen


def render_html(items, now):
    blocks = []
    for item in items:
        title = html.escape(display_title(item))
        url = html.escape(item.get("canonical_url") or item.get("source_url") or SITE_URL)
        tool = html.escape(item.get("tool") or "Unknown")
        date = format_date(item.get("published_at"))
        blocks.append(
            '<tr><td style="padding:16px 0;border-bottom:1px solid #e2e8f0;">'
            f'<div style="font:600 11px/1.4 Arial,sans-serif;color:#2563eb;'
            f'text-transform:uppercase;letter-spacing:.08em;">{tool} · {date}</div>'
            f'<a href="{url}" style="display:block;margin-top:6px;font:600 16px/1.45 '
            f'Georgia,serif;color:#0f172a;text-decoration:none;">{title}</a>'
            '</td></tr>'
        )
    headline = f"{len(items)} new items"
    intro = "Announcements and releases from official observability feeds, collected daily."
    return (
        HTML_TEMPLATE.replace("__HEADLINE__", headline)
        .replace("__INTRO__", intro)
        .replace("__BODY__", "".join(blocks))
        .replace("__SITE__", SITE_URL)
        .replace("__UNSUB__", UNSUBSCRIBE_MAILTO)
    )


def render_text(items, now):
    lines = ["OBSERVABILITY NEWS", ""]
    for item in items:
        tool = item.get("tool") or "Unknown"
        date = format_date(item.get("published_at"))
        url = item.get("canonical_url") or item.get("source_url") or SITE_URL
        lines.append(f"{tool} · {date}")
        lines.append(display_title(item))
        lines.append(url)
        lines.append("")
    lines.append(f"Browse all the news: {SITE_URL}/news.html")
    lines.append(f"Unsubscribe: mailto:{UNSUBSCRIBE_MAILTO}?subject=unsubscribe")
    return "\n".join(lines)


def build_message(items, now, recipient):
    sender = os.environ.get("DIGEST_FROM", DEFAULT_FROM)
    message = EmailMessage()
    message["From"] = f"Observability News <{sender}>"
    message["To"] = recipient
    message["Subject"] = f"Observability News · {len(items)} new items"
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid()
    message["List-Unsubscribe"] = f"<mailto:{UNSUBSCRIBE_MAILTO}?subject=unsubscribe>"
    message.set_content(render_text(items, now))
    message.add_alternative(render_html(items, now), subtype="html")
    return message


def load_subscribers():
    raw = os.environ.get("DIGEST_SUBSCRIBERS", "").strip()
    if not raw:
        return []
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("DIGEST_SUBSCRIBERS must be a JSON array")
    subscribers = []
    for entry in data:
        if isinstance(entry, str):
            subscribers.append({"email": entry, "tools": None})
        elif isinstance(entry, dict) and entry.get("email"):
            tools = entry.get("tools")
            subscribers.append({"email": entry["email"], "tools": set(tools) if tools else None})
        else:
            raise ValueError(f"Invalid subscriber entry: {entry!r}")
    return subscribers


def deliver_smtp(pairs):
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    failures = []
    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls()
        server.login(user, password)
        for message, recipient in pairs:
            try:
                server.send_message(message, from_addr=message["From"], to_addrs=[recipient])
            except smtplib.SMTPException as exc:
                failures.append((recipient, str(exc)))
    return failures


def deliver_resend(pairs):
    api_key = os.environ["RESEND_API_KEY"]
    failures = []
    for message, recipient in pairs:
        payload = json.dumps({
            "from": message["From"],
            "to": [recipient],
            "subject": message["Subject"],
            "html": message.get_body(("html",)).get_content(),
            "text": message.get_body(("plain",)).get_content(),
        }).encode("utf-8")
        request = urllib.request.Request(
            "https://api.resend.com/emails",
            data=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:200]
            failures.append((recipient, f"HTTP {exc.code}: {body}"))
        except urllib.error.URLError as exc:
            failures.append((recipient, str(exc.reason)))
    return failures


def deliver(pairs):
    transport = os.environ.get("DIGEST_TRANSPORT", "smtp").strip().lower()
    if transport == "smtp":
        return deliver_smtp(pairs)
    if transport == "resend":
        return deliver_resend(pairs)
    raise ValueError(f"Unknown DIGEST_TRANSPORT: {transport}")


def save_registry(registry, items, indent):
    if isinstance(registry, dict):
        registry["items"] = items
        payload = registry
    else:
        payload = items
    REGISTRY_PATH.write_text(
        json.dumps(payload, indent=indent, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="write digest-preview.html and send nothing")
    parser.add_argument("--to", help="send a single test copy to this address and mark nothing as sent")
    parser.add_argument("--limit", type=int, help="cap the number of items in the digest")
    args = parser.parse_args()

    if not REGISTRY_PATH.exists():
        print(f"Registry not found: {REGISTRY_PATH}", file=sys.stderr)
        return 1

    raw = REGISTRY_PATH.read_text(encoding="utf-8")
    registry = json.loads(raw)
    items = registry["items"] if isinstance(registry, dict) else registry
    now = datetime.now(timezone.utc)

    chosen = select_items(items, now)
    if args.limit:
        chosen = chosen[: args.limit]

    if not chosen:
        print("No unsent eligible items in the window. Nothing to send.")
        return 0

    counts = Counter(item.get("tool") or "Unknown" for item in chosen)
    print(f"{len(chosen)} items selected")
    for tool, count in counts.most_common():
        print(f"  {tool:28} {count:3}")

    if args.dry_run:
        PREVIEW_PATH.write_text(render_html(chosen, now), encoding="utf-8")
        print(f"dry run: wrote {PREVIEW_PATH}, nothing sent")
        return 0

    if args.to:
        pairs = [(build_message(chosen, now, args.to), args.to)]
        mark_sent = False
    else:
        subscribers = load_subscribers()
        if not subscribers:
            print("No subscribers configured. Nothing to send.")
            return 0
        pairs = []
        for subscriber in subscribers:
            selected = chosen
            if subscriber["tools"]:
                selected = [i for i in chosen if (i.get("tool") or "Unknown") in subscriber["tools"]]
            if not selected:
                continue
            pairs.append((build_message(selected, now, subscriber["email"]), subscriber["email"]))
        if not pairs:
            print("No subscriber matches the selected items. Nothing to send.")
            return 0
        mark_sent = True

    failures = deliver(pairs)
    sent = len(pairs) - len(failures)
    print(f"delivered {sent} of {len(pairs)}")

    for recipient, reason in failures:
        print(f"  failed {recipient}: {reason}", file=sys.stderr)

    if failures:
        print("Some deliveries failed, nothing marked as sent. Safe to retry.", file=sys.stderr)
        return 1

    if mark_sent:
        stamp = now.isoformat()
        for item in chosen:
            item["sent_at"] = stamp
        save_registry(registry, items, detect_indent(raw))
        print(f"marked {len(chosen)} items with sent_at")

    return 0


if __name__ == "__main__":
    sys.exit(main())