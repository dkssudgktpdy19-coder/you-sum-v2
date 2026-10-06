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
        return r.json()["error"]["message"][:200]
    except Exception:
        return r.text[:200]


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
    key = load_secrets().get("GEMINI_API_KEY")
    if not key:
        raise AIError("Gemini 키가 아직 저장되지 않았습니다 (set_gemini_key.sh 실행 필요)")
    if video_seconds:
        cap = s["ai"]["video_hours_per_day"] * 3600 * s["limits"]["max_usage_ratio"]
        if used(conn)[1] + video_seconds > cap:
            raise VideoBudgetReached("오늘 영상 직접 분석 시간 한도")
    config = {"responseMimeType": "application/json", "responseJsonSchema": schema, "temperature": 0.2}
    if video_seconds:
        config["mediaResolution"] = "MEDIA_RESOLUTION_LOW"
    body = {"contents": [{"parts": parts}], "generationConfig": config}

    busy, missing = False, []
    for model in s["ai"]["models"]:
        if used(conn, model)[0] >= budget(model) or get_meta(conn, f"ai_exhausted:{pt_day()}:{model}"):
            continue
        for _ in range(2):
            _count(conn, model, video_seconds)
            try:
                r = requests.post(API.format(model), headers={"x-goog-api-key": key}, json=body, timeout=300)
            except requests.RequestException as e:
                raise AIError(f"Gemini 연결 실패: {e}") from e
            if r.status_code == 200:
                return model, _parse(r.json())
            if r.status_code == 429:
                if "perday" in r.text.lower().replace(" ", "").replace("_", ""):
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
                missing.append(model)
                break
            raise AIError(f"{model} 오류 {r.status_code}: {_message(r)}")
    if missing and len(missing) == len(s["ai"]["models"]):
        raise AIError(f"모델 이름을 찾을 수 없음: {', '.join(missing)}")
    if busy:
        raise AIError("Gemini 서버가 잠시 바쁩니다")
    raise BudgetReached("오늘 AI 무료 한도")
