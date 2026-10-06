"""유튜브에서 채널·영상 정보를 가져옵니다. API 키 없이 집 인터넷으로 직접 확인합니다."""
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests
import yt_dlp

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
URL_RE = re.compile(r"(?:https?://)?(?:www\.|m\.)?(?:youtube\.com|youtu\.be)/\S+", re.I)
CHANNEL_PATH = re.compile(
    r"youtube\.com/(@[^/?#\s]+|channel/UC[\w-]{22}|c/[^/?#\s]+|user/[^/?#\s]+)", re.I)
BLOCK_SIGNS = ("confirm you", "not a bot", "429", "too many requests")


class Blocked(Exception):
    """유튜브가 요청을 잠시 막은 상태"""


def _extract(url, **extra):
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
            "ignore_no_formats_error": True, "socket_timeout": 30, "retries": 2}
    opts.update(extra)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False) or {}
    except Exception as e:
        if any(sign in str(e).lower() for sign in BLOCK_SIGNS):
            raise Blocked(str(e)) from e
        raise


def find_link(text):
    """메시지에서 유튜브 링크나 @핸들을 찾습니다. 없으면 None."""
    text = text.strip()
    if re.fullmatch(r"@[^\s/]+", text):
        return "https://www.youtube.com/" + text
    m = URL_RE.search(text)
    if not m:
        return None
    url = m.group(0)
    return url if url.lower().startswith("http") else "https://" + url


def fetch_rss(channel_id):
    """채널 RSS에서 최근 영상 15개를 가져옵니다. 실패하면 None."""
    try:
        r = requests.get("https://www.youtube.com/feeds/videos.xml",
                         params={"channel_id": channel_id}, headers={"User-Agent": UA}, timeout=20)
        if r.status_code != 200:
            return None
        root = ET.fromstring(r.content)
    except Exception:
        return None
    items = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", namespaces=NS)
        if vid:
            items.append({"id": vid, "title": e.findtext("a:title", namespaces=NS),
                          "published": e.findtext("a:published", namespaces=NS)})
    return items


def recent_from_tab(channel_id, n=10):
    """RSS가 안 될 때 대신 쓰는 경로: 채널의 '동영상' 탭 목록 (올린 날짜는 없음)."""
    tab = _extract(f"https://www.youtube.com/channel/{channel_id}/videos",
                   extract_flat="in_playlist", playlistend=n, noplaylist=False)
    return [{"id": e["id"], "title": e.get("title"), "published": None}
            for e in (tab.get("entries") or []) if e and e.get("id")]


def video_details(video_id):
    info = _extract(f"https://www.youtube.com/watch?v={video_id}")
    ts = info.get("timestamp") or info.get("release_timestamp")
    return {
        "title": info.get("title"),
        "duration": int(info.get("duration") or 0),
        "live_status": info.get("live_status"),
        "published": datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None,
    }


def resolve_channel(link, threshold):
    """어떤 링크든 채널을 찾아 등록 확인 카드에 필요한 정보를 돌려줍니다."""
    m = CHANNEL_PATH.search(link)
    if m:
        base = "https://www.youtube.com/" + m.group(1)
    else:  # 영상, 쇼츠, 라이브 링크
        cid = _extract(link).get("channel_id")
        if not cid:
            raise ValueError("링크에서 채널을 찾지 못함")
        base = f"https://www.youtube.com/channel/{cid}"

    tab = _extract(base + "/videos", extract_flat="in_playlist", playlistend=10, noplaylist=False)
    cid = tab.get("channel_id")
    if not cid or not cid.startswith("UC"):
        raise ValueError("채널 ID를 확인하지 못함")
    entries = [e for e in (tab.get("entries") or []) if e][:10]
    longs = [int(e["duration"]) for e in entries if e.get("duration") and e["duration"] >= threshold]

    dates = [datetime.fromisoformat(r["published"]) for r in (fetch_rss(cid) or []) if r.get("published")]
    month_ago = datetime.now(timezone.utc) - timedelta(days=30)
    handle = tab.get("uploader_id") or ""
    return {
        "channel_id": cid,
        "title": tab.get("channel") or tab.get("uploader") or tab.get("title") or cid,
        "handle": handle if handle.startswith("@") else None,
        "subscribers": tab.get("channel_follower_count"),
        "recent": len(entries),
        "long": len(longs),
        "avg_min": round(sum(longs) / len(longs) / 60) if longs else None,
        "uploads_30d": sum(1 for d in dates if d >= month_ago) if dates else None,
        "rss_total": len(dates),
        "last_upload": max(dates).isoformat(timespec="seconds") if dates else None,
    }
