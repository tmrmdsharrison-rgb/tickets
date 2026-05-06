#!/usr/bin/env python3
"""Scrape Business Weekly concert table rows with ticket statuses to watch.

The script keeps rows whose sale status does not contain excluded keywords,
then follows the in-page anchor from the performer cell to collect detail
fields such as concert time, venue, ticket sale time, and ticket platform.
Discord notifications are limited to targets whose sale dates are within the
next notification window.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urljoin

import requests
from bs4 import BeautifulSoup, NavigableString, Tag


DEFAULT_URL = "https://www.businessweekly.com.tw/style/blog/3019879#XG"
EXCLUDED_STATUSES = {"完售", "熱賣中"}
EXCLUDED_STATUS_KEYWORDS = ("完售", "熱賣中")
WATCH_FIELDS = (
    "performer",
    "concert_date_from_table",
    "location_from_table",
    "sale_status",
    "detail_title",
    "concert_time",
    "concert_location",
    "sale_time",
    "ticket_platform",
    "ticket_platform_links",
)
DISCORD_MESSAGE_LIMIT = 1900
TELEGRAM_MESSAGE_LIMIT = 4000
NOTIFICATION_WINDOW_DAYS = 14
GENERAL_SALE_KEYWORDS = ("正式", "全面", "一般", "開賣", "啟售", "販售")
NON_GENERAL_SALE_KEYWORDS = (
    "登記",
    "抽選",
    "預購",
    "優先購",
    "預售",
    "會員",
    "通知",
    "繳費",
    "付款",
    "結帳",
    "專區",
)


def setup_logger(log_path: Path) -> logging.Logger:
    logger = logging.getLogger("businessweekly_concert_scraper")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    return logger


def clean_text(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def multiline_text(tag: Tag) -> str:
    return "\n".join(
        line for line in (clean_text(part) for part in tag.stripped_strings) if line
    )


def list_item_own_text(tag: Tag) -> str:
    parts = []
    for child in tag.contents:
        if isinstance(child, NavigableString):
            parts.append(str(child))
        elif isinstance(child, Tag) and child.name not in {"ul", "ol"}:
            parts.append(child.get_text(" ", strip=True))
    return clean_text(" ".join(parts))


def fetch_html(url: str, logger: logging.Logger) -> str:
    logger.info("Fetching page: %s", url)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
    }
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or "utf-8"
    logger.info("Fetched %s bytes", len(response.text))
    return response.text


def load_previous_payload(path: Path, logger: logging.Logger) -> dict[str, Any] | None:
    if not path.exists():
        logger.info("No previous output found at %s; treating this run as baseline", path)
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Could not read previous output at %s", path)
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        logger.warning("Previous output at %s has unexpected format", path)
        return None
    return payload


def item_key(item: dict[str, Any]) -> str:
    parts = [
        item.get("performer", ""),
        item.get("detail_url", ""),
        item.get("concert_date_from_table", ""),
        item.get("location_from_table", ""),
    ]
    return " | ".join(clean_text(str(part)) for part in parts if part)


def normalize_for_compare(value: Any) -> Any:
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, list):
        return [normalize_for_compare(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize_for_compare(value[key]) for key in sorted(value)}
    return value


def compare_items(
    previous_items: list[dict[str, Any]], current_items: list[dict[str, Any]]
) -> dict[str, Any]:
    previous_by_key = {item_key(item): item for item in previous_items if item_key(item)}
    current_by_key = {item_key(item): item for item in current_items if item_key(item)}

    added = [current_by_key[key] for key in sorted(current_by_key.keys() - previous_by_key.keys())]
    removed = [
        previous_by_key[key] for key in sorted(previous_by_key.keys() - current_by_key.keys())
    ]
    updated = []
    for key in sorted(previous_by_key.keys() & current_by_key.keys()):
        before = previous_by_key[key]
        after = current_by_key[key]
        field_changes = {}
        for field in WATCH_FIELDS:
            before_value = normalize_for_compare(before.get(field))
            after_value = normalize_for_compare(after.get(field))
            if before_value != after_value:
                field_changes[field] = {"before": before.get(field), "after": after.get(field)}
        if field_changes:
            updated.append({"key": key, "before": before, "after": after, "changes": field_changes})

    return {"added": added, "updated": updated, "removed": removed}


def has_excluded_sale_status(sale_status: str) -> bool:
    normalized = clean_text(sale_status)
    return any(keyword in normalized for keyword in EXCLUDED_STATUS_KEYWORDS)


def first_line(value: Any) -> str:
    if isinstance(value, list):
        if not value:
            return ""
        return ", ".join(
            clean_text(item.get("name", item.get("url", ""))) if isinstance(item, dict) else str(item)
            for item in value
        )
    return clean_text(str(value or "").splitlines()[0])


def summarize_item(item: dict[str, Any]) -> str:
    title = item.get("detail_title") or item.get("performer") or "(未命名)"
    parts = [
        f"日期：{first_line(item.get('concert_date_from_table') or item.get('concert_time'))}",
        f"地點：{first_line(item.get('location_from_table') or item.get('concert_location'))}",
        f"狀態：{first_line(item.get('sale_status'))}",
        f"售票：{first_line(item.get('sale_time'))}",
        f"平台：{first_line(item.get('ticket_platform'))}",
    ]
    detail_url = item.get("detail_url")
    if detail_url:
        parts.append(f"連結：{detail_url}")
    return f"**{title}**\n" + "\n".join(part for part in parts if not part.endswith("："))


def format_field_change(field: str, before: Any, after: Any) -> str:
    labels = {
        "concert_date_from_table": "表格日期",
        "location_from_table": "表格地點",
        "sale_status": "售票狀態",
        "detail_title": "標題",
        "concert_time": "演唱會時間",
        "concert_location": "演唱會地點",
        "sale_time": "售票時間",
        "ticket_platform": "售票平台",
        "ticket_platform_links": "售票連結",
    }
    label = labels.get(field, field)
    return f"- {label}: {first_line(before) or '(空)'} -> {first_line(after) or '(空)'}"


def build_discord_messages(changes: dict[str, Any], source_url: str) -> list[str]:
    added = changes["added"]
    updated = changes["updated"]
    removed = changes["removed"]
    total = len(added) + len(updated) + len(removed)
    if total == 0:
        return []

    header = (
        f"每週演唱會目標更新：{total} 筆不同\n"
        f"新增 {len(added)} / 變更 {len(updated)} / 移除 {len(removed)}\n"
        f"來源：{source_url}"
    )
    sections = [header]

    for item in added:
        sections.append("[新增]\n" + summarize_item(item))

    for update in updated:
        after = update["after"]
        lines = ["[變更]", summarize_item(after), "異動欄位："]
        for field, values in update["changes"].items():
            lines.append(format_field_change(field, values["before"], values["after"]))
        sections.append("\n".join(lines))

    for item in removed:
        sections.append("[移除]\n" + summarize_item(item))

    messages = []
    current = ""
    for section in sections:
        candidate = f"{current}\n\n{section}".strip() if current else section
        if len(candidate) <= DISCORD_MESSAGE_LIMIT:
            current = candidate
            continue
        if current:
            messages.append(current)
        current = section
    if current:
        messages.append(current)
    return messages


def send_discord_messages(
    webhook_url: str, messages: list[str], logger: logging.Logger
) -> None:
    if not messages:
        logger.info("No Discord messages to notify")
        return
    if not webhook_url:
        logger.info("Discord webhook is blank; skipped sending %s message(s)", len(messages))
        return

    for index, message in enumerate(messages, start=1):
        response = requests.post(webhook_url, json={"content": message}, timeout=30)
        response.raise_for_status()
        logger.info("Sent Discord notification %s/%s", index, len(messages))


def send_telegram_messages(
    bot_token: str, chat_id: str, messages: list[str], logger: logging.Logger
) -> None:
    if not messages:
        logger.info("No Telegram messages to notify")
        return
    if not bot_token or not chat_id:
        logger.info("Telegram config is blank; skipped sending %s message(s)", len(messages))
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    for index, message in enumerate(messages, start=1):
        response = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": message[:TELEGRAM_MESSAGE_LIMIT],
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        response.raise_for_status()
        logger.info("Sent Telegram notification %s/%s", index, len(messages))


def parse_sale_dates(text: str, reference_date: date) -> list[date]:
    dates: list[date] = []
    patterns = (
        re.compile(r"(?:(20\d{2})[/-])(\d{1,2})[/-](\d{1,2})"),
        re.compile(r"(?:(20\d{2})年)?(\d{1,2})月(\d{1,2})日"),
        re.compile(r"(?<!\d)(?:(20\d{2})[/-])?(\d{1,2})[/-](\d{1,2})(?!\d)"),
    )

    for pattern in patterns:
        for match in pattern.finditer(text):
            year_text, month_text, day_text = match.groups()
            year = int(year_text) if year_text else reference_date.year
            month = int(month_text)
            day = int(day_text)
            try:
                parsed = date(year, month, day)
            except ValueError:
                continue
            if parsed not in dates:
                dates.append(parsed)
    return dates


def likely_general_sale_line(line: str) -> bool:
    text = clean_text(line)
    if not text:
        return False
    if any(keyword in text for keyword in NON_GENERAL_SALE_KEYWORDS):
        return False
    return any(keyword in text for keyword in GENERAL_SALE_KEYWORDS)


def notification_sale_dates(item: dict[str, Any], reference_date: date) -> list[date]:
    status_dates = parse_sale_dates(str(item.get("sale_status", "")), reference_date)
    if status_dates:
        dates = status_dates
    else:
        sale_time_lines = str(item.get("sale_time", "")).splitlines()
        sale_time_text = "\n".join(
            line for line in sale_time_lines if likely_general_sale_line(line)
        )
        dates = parse_sale_dates(sale_time_text, reference_date)
    window_end = reference_date + timedelta(days=NOTIFICATION_WINDOW_DAYS)
    return [sale_date for sale_date in dates if reference_date <= sale_date <= window_end]


def filter_notification_items(
    items: list[dict[str, Any]], reference_date: date
) -> list[dict[str, Any]]:
    targets = []
    for item in items:
        sale_dates = notification_sale_dates(item, reference_date)
        if not sale_dates:
            continue
        target = dict(item)
        target["notification_sale_dates"] = [
            sale_date.isoformat() for sale_date in sorted(sale_dates)
        ]
        targets.append(target)

    return sorted(
        targets,
        key=lambda item: (
            item["notification_sale_dates"][0],
            item.get("performer", ""),
            item.get("concert_date_from_table", ""),
        ),
    )


def build_upcoming_sale_messages(
    items: list[dict[str, Any]], source_url: str, reference_date: date
) -> list[str]:
    if not items:
        return []

    window_end = reference_date + timedelta(days=NOTIFICATION_WINDOW_DAYS)
    header = (
        f"未來兩週內開賣提醒：{len(items)} 筆\n"
        f"區間：{reference_date.isoformat()} 至 {window_end.isoformat()}\n"
        f"來源：{source_url}"
    )
    sections = [header]
    for item in items:
        sale_dates = ", ".join(item.get("notification_sale_dates", []))
        sections.append(f"[開賣日：{sale_dates}]\n{summarize_item(item)}")

    messages = []
    current = ""
    for section in sections:
        candidate = f"{current}\n\n{section}".strip() if current else section
        if len(candidate) <= DISCORD_MESSAGE_LIMIT:
            current = candidate
            continue
        if current:
            messages.append(current)
        current = section
    if current:
        messages.append(current)
    return messages


def find_anchor_target(soup: BeautifulSoup, fragment: str) -> Tag | None:
    if not fragment:
        return None

    candidates = [fragment, fragment.lstrip("#")]
    decoded = [candidate for candidate in candidates if candidate]
    for candidate in decoded:
        found = soup.find(id=candidate)
        if isinstance(found, Tag):
            return found

    return None


def heading_for_target(target: Tag) -> Tag | None:
    if target.name in {"h2", "h3"}:
        return target
    heading = target.find_parent(["h2", "h3"])
    return heading if isinstance(heading, Tag) else None


def find_detail_heading(
    soup: BeautifulSoup, anchor_href: str, performer: str, logger: logging.Logger
) -> Tag | None:
    fragment = urldefrag(anchor_href or "")[1]
    targets = soup.find_all(id=fragment) if fragment else []
    headings = [heading for target in targets if (heading := heading_for_target(target))]

    if len(headings) == 1:
        return headings[0]
    if len(headings) > 1:
        for heading in headings:
            if performer.lower() in heading.get_text(" ", strip=True).lower():
                return heading
        logger.warning(
            "Found duplicate anchor id=%s, but no heading text matched performer=%s",
            fragment,
            performer,
        )

    target = find_anchor_target(soup, fragment)
    if target:
        heading = heading_for_target(target)
        if heading:
            return heading

    # Some rows have href="#" or an anchor that differs from the display name.
    for heading in soup.find_all(["h2", "h3"]):
        if performer and performer.lower() in heading.get_text(" ", strip=True).lower():
            logger.info("Matched detail heading by performer text: %s", performer)
            return heading
    return None


def iter_detail_nodes(heading: Tag) -> list[Tag]:
    nodes: list[Tag] = []
    for sibling in heading.next_siblings:
        if isinstance(sibling, Tag) and sibling.name in {"h2", "h3"}:
            break
        if isinstance(sibling, Tag):
            nodes.append(sibling)
    return nodes


def get_link_info(tag: Tag, base_url: str) -> list[dict[str, str]]:
    links = []
    for link in tag.find_all("a"):
        label = clean_text(link.get_text(" ", strip=True))
        href = link.get("href")
        if href:
            links.append({"name": label, "url": urljoin(base_url, href)})
    return links


def parse_labeled_items(nodes: list[Tag], base_url: str) -> dict[str, Any]:
    details: dict[str, Any] = {
        "concert_time": "",
        "concert_location": "",
        "sale_time": "",
        "ticket_platform": "",
        "ticket_platform_links": [],
        "raw_detail_items": [],
    }
    field_aliases = {
        "concert_time": ("演唱會時間", "演出時間", "演出日期"),
        "concert_location": ("演唱會地點", "演出地點", "地點"),
        "sale_time": ("售票時間", "開賣時間", "登記抽選時間", "登記時間", "結果公布", "結帳期間"),
        "ticket_platform": ("售票平台", "購票平台", "售票系統"),
    }

    current_field: str | None = None
    current_value: list[str] = []
    current_links: list[dict[str, str]] = []

    def flush() -> None:
        nonlocal current_field, current_value, current_links
        if not current_field:
            return
        value = "\n".join(v for v in current_value if v).strip()
        if value:
            existing = details.get(current_field)
            details[current_field] = f"{existing}\n{value}".strip() if existing else value
        if current_field == "ticket_platform" and current_links:
            details["ticket_platform_links"].extend(current_links)
        current_field = None
        current_value = []
        current_links = []

    for node in nodes:
        for li in node.find_all("li"):
            text = list_item_own_text(li)
            if not text:
                continue
            details["raw_detail_items"].append(text)

            if text.startswith(("票價", "開賣票價", "一般票價", "身障優惠票價")):
                flush()
                continue

            matched_field = None
            matched_label = None
            for field, labels in field_aliases.items():
                for label in labels:
                    if text.startswith(label):
                        matched_field = field
                        matched_label = label
                        break
                if matched_field:
                    break

            if matched_field:
                flush()
                current_field = matched_field
                value = re.sub(rf"^{re.escape(matched_label or '')}\s*[：:]\s*", "", text)
                current_value = [value]
                current_links = get_link_info(li, base_url)
            elif current_field:
                current_value.append(text)
                current_links.extend(get_link_info(li, base_url))

    flush()
    # Deduplicate platform links while preserving order.
    seen = set()
    unique_links = []
    for link in details["ticket_platform_links"]:
        key = (link.get("name"), link.get("url"))
        if key not in seen:
            seen.add(key)
            unique_links.append(link)
    details["ticket_platform_links"] = unique_links
    return details


def parse_table_rows(
    soup: BeautifulSoup, page_url: str, logger: logging.Logger
) -> list[dict[str, Any]]:
    table = soup.find("table")
    if not table:
        raise RuntimeError("找不到頁面中的演唱會表格")

    results: list[dict[str, Any]] = []
    skipped = 0
    for row_index, tr in enumerate(table.find_all("tr")):
        cells = tr.find_all(["td", "th"])
        if len(cells) != 4:
            continue

        headers = [clean_text(cell.get_text(" ", strip=True)) for cell in cells]
        if headers == ["演出日期", "演出者", "地點", "售票狀態"]:
            continue

        concert_date = multiline_text(cells[0])
        performer = clean_text(cells[1].get_text(" ", strip=True))
        location = multiline_text(cells[2])
        sale_status = multiline_text(cells[3])

        if not performer or not sale_status:
            continue
        if has_excluded_sale_status(sale_status):
            skipped += 1
            continue

        performer_link = cells[1].find("a")
        href = performer_link.get("href") if performer_link else ""
        detail_url = urljoin(page_url, href) if href and href != "#" else ""
        heading = find_detail_heading(soup, href or "", performer, logger)

        detail: dict[str, Any] = {
            "detail_title": "",
            "concert_time": "",
            "concert_location": "",
            "sale_time": "",
            "ticket_platform": "",
            "ticket_platform_links": [],
            "raw_detail_items": [],
        }
        if heading:
            detail["detail_title"] = clean_text(heading.get_text(" ", strip=True))
            detail.update(parse_labeled_items(iter_detail_nodes(heading), page_url))
        else:
            logger.warning(
                "No detail section found for row %s performer=%s href=%s",
                row_index,
                performer,
                href,
            )

        record = {
            "performer": performer,
            "concert_date_from_table": concert_date,
            "location_from_table": location,
            "sale_status": sale_status,
            "detail_url": detail_url,
            **detail,
        }
        if not detail.get("sale_time") or not detail.get("ticket_platform"):
            logger.warning(
                "Missing detail fields for performer=%s sale_time=%r ticket_platform=%r",
                performer,
                detail.get("sale_time"),
                detail.get("ticket_platform"),
            )
        results.append(record)

    logger.info(
        "Skipped %s rows with excluded status keywords: %s",
        skipped,
        ", ".join(EXCLUDED_STATUS_KEYWORDS),
    )
    logger.info("Collected %s matching rows", len(results))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape Business Weekly concert rows excluding configured status keywords."
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="Business Weekly article URL")
    parser.add_argument("--output", default="businessweekly_concerts.json", help="JSON output path")
    parser.add_argument("--log", default="businessweekly_concerts.log", help="log output path")
    parser.add_argument(
        "--diff-output",
        default="businessweekly_concerts_changes.json",
        help="JSON path for added/updated/removed rows compared with the previous output",
    )
    parser.add_argument(
        "--discord-webhook",
        default=os.environ.get("DISCORD_WEBHOOK_URL", ""),
        help="Discord webhook URL. Leave blank to skip sending and only write output files.",
    )
    parser.add_argument(
        "--telegram-bot-token",
        default=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        help="Telegram bot token. Leave blank to skip Telegram notifications.",
    )
    parser.add_argument(
        "--telegram-chat-id",
        default=os.environ.get("TELEGRAM_CHAT_ID", ""),
        help="Telegram chat ID. Leave blank to skip Telegram notifications.",
    )
    parser.add_argument(
        "--html",
        default="",
        help="Optional local HTML file. If set, the script parses this file instead of fetching.",
    )
    args = parser.parse_args()

    output_path = Path(args.output)
    diff_output_path = Path(args.diff_output)
    log_path = Path(args.log)
    logger = setup_logger(log_path)

    try:
        previous_payload = load_previous_payload(output_path, logger)
        if args.html:
            logger.info("Reading local HTML: %s", args.html)
            html = Path(args.html).read_text(encoding="utf-8")
        else:
            html = fetch_html(args.url, logger)

        soup = BeautifulSoup(html, "html.parser")
        rows = parse_table_rows(soup, args.url, logger)
        payload = {
            "source_url": args.url,
            "scraped_at": datetime.now().isoformat(timespec="seconds"),
            "excluded_statuses": sorted(EXCLUDED_STATUSES),
            "excluded_status_keywords": list(EXCLUDED_STATUS_KEYWORDS),
            "count": len(rows),
            "items": rows,
        }
        changes = compare_items(
            previous_payload.get("items", []) if previous_payload else [],
            rows,
        )
        change_payload = {
            "source_url": args.url,
            "scraped_at": payload["scraped_at"],
            "previous_scraped_at": previous_payload.get("scraped_at") if previous_payload else None,
            "summary": {
                "added": len(changes["added"]),
                "updated": len(changes["updated"]),
                "removed": len(changes["removed"]),
            },
            **changes,
        }
        output_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("Wrote JSON output: %s", output_path)
        diff_output_path.write_text(
            json.dumps(change_payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("Wrote change output: %s", diff_output_path)

        today = date.today()
        notification_items = filter_notification_items(rows, today)
        messages = build_upcoming_sale_messages(notification_items, args.url, today)
        send_discord_messages(args.discord_webhook, messages, logger)
        send_telegram_messages(
            args.telegram_bot_token,
            args.telegram_chat_id,
            messages,
            logger,
        )
    except Exception:
        logger.exception("Scrape failed")
        raise


if __name__ == "__main__":
    main()
