"""Gemini 무료 등급 호출과 사용량 관리 (하루 한도는 미국 태평양 자정에 초기화)"""
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from app.config import load_secrets, load_settings
from app.db import get_meta, set_meta

API = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"
PT = ZoneInfo("America/Los_Angeles")
NOT_COUNTED = (400, 401, 403, 404)  # 모델까지 가지 못한 요청은 한도를 쓰지 않으므로 세지 않음


class BudgetReached(Exception):
    """오늘 쓰기로 한 무료 한도(70%)를 다 씀"""


class VideoBudgetReached(BudgetReached):
    """영상 직접 분석 시간만 다 씀 (자막 분석은 계속 가능)"""


class AIError(Exception):
    """그 밖의 Gemini 문제"""


def pt_day():
    return datetime.now(PT).strftime("%Y-%m-%d")


def used(conn, model=None):
    if model:
        row = conn.execute("SELECT requests, video_seconds FROM ai_usage WHERE day=? AND model=?",
                           (pt_day(), model)).fetchone()
    else:
        row = conn.execute("SELECT COALESCE(SUM(requests),0) AS requests, "
                           "COALESCE(SUM(video_seconds),0) AS video_seconds FROM ai_usage WHERE day=?",
                           (pt_day(),)).fetchone()
    return (row["requests"], row["video_seconds"]) if row else (0, 0)


def budget(model):
    s = load_settings()
    return int(s["ai"]["daily_requests"].get(model, 0) * s["limits"]["max_usage_ratio"])


def budget_text(conn):
    return " · ".join(f"{m} {used(conn, m)[0]}/{budget(m)}" for m in load_settings()["ai"]["models"])


def _count(conn, model, video_seconds):
    conn.execute("INSERT INTO ai_usage(day, model, requests, video_seconds) VALUES(?,?,1,?) "
                 "ON CONFLICT(day, model) DO UPDATE SET requests=requests+1, "
                 "video_seconds=video_seconds+excluded.video_seconds", (pt_day(), model, video_seconds))
    conn.commit()


def _message(r):
    try:
        return r.json()["error"]["message"][:150]
    except Exception:
        return r.text[:150]


def _parse(data):
    try:
        parts = data["candidates"][0]["content"]["parts"]
        return json.loads("".join(p.get("text", "") for p in parts if not p.get("thought")))
    except (KeyError, IndexError, ValueError) as e:
        reason = (data.get("promptFeedback") or {}).get("blockReason") or \
                 ((data.get("candidates") or [{}])[0]).get("finishReason")
        raise AIError(f"Gemini 응답을 읽지 못함 ({reason})") from e


def generate(conn, parts, schema, video_seconds=0):
    s = load_settings()
    models = s["ai"]["models"]
    key = load_secrets().get("GEMINI_API_KEY")
    if not key:
        raise AIError("Gemini 키가 아직 저장되지 않았습니다 (set_gemini_key.sh 실행 필요)")
    if video_seconds:
        cap = s["ai"]["video_hours_per_day"] * 3600 * s["limits"]["max_usage_ratio"]
        if used(conn)[1] + video_seconds > cap:
            raise VideoBudgetReached("오늘 영상 직접 분석 시간 한도")
    # Gemini 3 계열은 temperature를 기본값으로 두라는 권고가 있어 따로 정하지 않습니다.
    config = {"responseMimeType": "application/json", "responseJsonSchema": schema}
    if video_seconds:
        config["mediaResolution"] = "MEDIA_RESOLUTION_LOW"
    body = {"contents": [{"parts": parts}], "generationConfig": config}

    busy, unavailable = False, []
    for model in models:
        if used(conn, model)[0] >= budget(model) or get_meta(conn, f"ai_exhausted:{pt_day()}:{model}"):
            continue
        for _ in range(2):
            try:
                r = requests.post(API.format(model), headers={"x-goog-api-key": key}, json=body, timeout=300)
            except requests.RequestException as e:
                raise AIError(f"Gemini 연결 실패: {e}") from e
            if r.status_code not in NOT_COUNTED:
                _count(conn, model, video_seconds)
            if r.status_code == 200:
                return model, _parse(r.json())
            if r.status_code == 429:
                flat = r.text.lower().replace(" ", "").replace("_", "")
                if '"limit":0' in flat or "limit:0," in flat or flat.endswith("limit:0"):
                    unavailable.append(f"{model}(무료 등급에서 못 씀)")
                    set_meta(conn, f"ai_exhausted:{pt_day()}:{model}", 1)
                    break
                if "perday" in flat:
                    set_meta(conn, f"ai_exhausted:{pt_day()}:{model}", 1)
                    break
                busy = True
                time.sleep(65)  # 분당 한도: 1분 쉬고 한 번 더
                continue
            if r.status_code in (500, 502, 503, 504):
                busy = True
                time.sleep(30)
                continue
            if r.status_code == 404:
                unavailable.append(f"{model}({_message(r)})")
                break
            raise AIError(f"{model} 오류 {r.status_code}: {_message(r)}")
    if unavailable and len(unavailable) == len(models):
        raise AIError("쓸 수 있는 모델이 없음: " + " / ".join(unavailable))
    if busy:
        raise AIError("Gemini 서버가 잠시 바쁩니다")
    raise BudgetReached("오늘 AI 무료 한도")
