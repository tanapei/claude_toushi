"""
日本の金融ニュースをRSSフィードから取得するモジュール。
標準ライブラリのみ使用（外部依存なし）。

直近18時間以内のビジネス・経済ニュースを複数ソースから収集する。
"""
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import List, Dict
from urllib.request import urlopen, Request
from urllib.error import URLError

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

NEWS_FEEDS = [
    {"name": "NHK 経済",          "url": "https://www3.nhk.or.jp/rss/news/cat5.xml"},
    {"name": "Yahoo Japan ビジネス","url": "https://news.yahoo.co.jp/rss/topics/business.xml"},
    {"name": "Yahoo Japan 経済",   "url": "https://news.yahoo.co.jp/rss/topics/economy.xml"},
]

_UA = "Mozilla/5.0 (compatible; NewsBot/1.0)"
_TIMEOUT = 15


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _parse_date(date_str: str) -> datetime:
    """RFC2822形式の日時をUTC awareなdatetimeに変換する。"""
    try:
        dt = parsedate_to_datetime(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return datetime.now(timezone.utc)


def _fetch_feed(url: str) -> List[ET.Element]:
    """URLからRSS/Atomフィードを取得してitemリストを返す。"""
    req = Request(url, headers={"User-Agent": _UA})
    with urlopen(req, timeout=_TIMEOUT) as resp:
        content = resp.read()
    root = ET.fromstring(content)
    # RSS 2.0: channel/item, Atom: {namespace}entry
    items = root.findall(".//item")
    if not items:
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        items = root.findall(".//atom:entry", ns)
    return items


def _get_text(el: ET.Element, *tags: str) -> str:
    """複数の候補タグから最初に見つかったテキストを返す。"""
    for tag in tags:
        child = el.find(tag)
        if child is not None and child.text:
            return child.text.strip()
    return ""


def fetch_recent_news(hours: int = 18, max_per_feed: int = 12) -> List[Dict]:
    """
    直近 `hours` 時間以内のニュースを複数RSSから収集する。

    Returns:
        List of {"title": str, "summary": str, "source": str, "published": str}
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    results: List[Dict] = []
    seen: set = set()

    for feed_info in NEWS_FEEDS:
        try:
            items = _fetch_feed(feed_info["url"])
            count = 0
            for item in items:
                if count >= max_per_feed:
                    break

                title = _strip_html(_get_text(item, "title"))
                if not title or title in seen:
                    continue

                pub_raw = _get_text(item, "pubDate", "published", "updated")
                pub = _parse_date(pub_raw) if pub_raw else datetime.now(timezone.utc)
                if pub < cutoff:
                    continue

                summary = _strip_html(
                    _get_text(item, "description", "summary", "content")
                )[:250]

                results.append({
                    "title":     title,
                    "summary":   summary,
                    "source":    feed_info["name"],
                    "published": pub.astimezone(JST).strftime("%m/%d %H:%M"),
                })
                seen.add(title)
                count += 1

            logger.debug(f"[news] {feed_info['name']}: {count}件取得")

        except URLError as e:
            logger.warning(f"[news] {feed_info['name']} 接続失敗: {e}")
        except Exception as e:
            logger.warning(f"[news] {feed_info['name']} 取得失敗: {e}")

    results.sort(key=lambda x: x["published"], reverse=True)
    return results[:30]
