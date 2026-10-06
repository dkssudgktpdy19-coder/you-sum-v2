"""한국어 자막을 받습니다. 사람이 단 자막을 먼저, 없으면 자동 자막을 씁니다."""
import json
import tempfile
from pathlib import Path

import yt_dlp

from app.youtube import BLOCK_SIGNS, Blocked

BASE = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
        "ignore_no_formats_error": True, "socket_timeout": 30, "retries": 2}


def _pick(info):
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    for lang in manual:
        if lang == "ko" or lang.startswith("ko-"):
            return lang, "manual"
    for lang in ("ko-orig", "ko"):
        if lang in auto:
            return lang, "auto"
    return None, None


def parse_json3(raw):
    """[[시작 초, 문장], ...] 형태로 바꿉니다."""
    out = []
    for ev in json.loads(raw).get("events", []):
        segs = ev.get("segs")
        if not segs:
            continue
        text = "".join(s.get("utf8", "") for s in segs).replace("\n", " ").strip()
        if text:
            out.append([int(ev.get("tStartMs", 0)) // 1000, text])
    return out


def fetch(video_id):
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with yt_dlp.YoutubeDL(BASE) as ydl:
            info = ydl.extract_info(url, download=False) or {}
        meta = {"title": info.get("title"), "channel": info.get("channel") or info.get("uploader"),
                "duration": int(info.get("duration") or 0)}
        lang, kind = _pick(info)
        if not lang:
            return {"meta": meta, "lang": None, "kind": None, "lines": []}
        with tempfile.TemporaryDirectory() as tmp:
            opts = dict(BASE, writesubtitles=(kind == "manual"), writeautomaticsub=(kind == "auto"),
                        subtitleslangs=[lang], subtitlesformat="json3",
                        outtmpl=str(Path(tmp) / "%(id)s.%(ext)s"))
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.process_ie_result(info, download=True)
            files = list(Path(tmp).glob("*.json3"))
            if not files:
                raise RuntimeError("자막 파일을 받지 못함")
            lines = parse_json3(files[0].read_text(encoding="utf-8"))
    except Blocked:
        raise
    except Exception as e:
        if any(sign in str(e).lower() for sign in BLOCK_SIGNS):
            raise Blocked(str(e)) from e
        raise
    return {"meta": meta, "lang": lang, "kind": kind, "lines": lines}
