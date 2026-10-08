"""Gemini 무료 등급 호출과 사용량 관리 (하루 한도는 미국 태평양 자정에 초기화)"""
import json
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from app.config import load_secrets, load_settings
from app.db import get_meta, now_utc, set_meta

API = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"
PT = ZoneInfo("America/Los_Angeles")
NOT_COUNTED = (400, 401, 403, 404)  # 모델까지 가지 못한 요청은 한도를 쓰지 않으므로 세지 않음
TRANSIENT = (429, 500, 502, 503, 504)  # 잠깐 바쁨
COOLDOWN_MIN = 20          # 바쁘거나 응답이 없던 모델은 20분 쉬게 함
MAX_FAILS_PER_CALL = 2     # 한 영상에서 실패 요청은 최대 2번까지만
MIN_GAP_SEC = 13           # 같은 모델에 요청 사이 최소 간격 (분당 5회 한도 보호)
TIMEOUT = (10, 240)        # (접속, 응답) 최대 대기 초
THINKING_LEVEL = "low"     # 생각 단계: 문서상 사용 중인 6개 모델 모두 low 지원
_last_call = {}


class BudgetReached(Exception):
    """오늘 쓰기로 한 무료 한도(70%)를 다 씀"""


class VideoBudgetReached(BudgetReached):
    """영상 직접 분석 시간만 다 씀 (자막 분석은 계속 가능)"""


class AIError(Exception):
    """그 밖의 Gemini 문제"""


class Busy(AIError):
    """모델들이 잠시 바쁨: 영상은 실패로 세지 않고 나중에 다시"""


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


def request_stats(conn):
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(outcome='ok'),0) AS ok, "
        "COUNT(DISTINCT CASE WHEN outcome='ok' THEN label END) AS vids "
        "FROM ai_requests WHERE day=?", (pt_day(),)).fetchone()
    n, ok, vids = row["n"], row["ok"], row["vids"]
    if not n:
        return ""
    text = f" · 요청 {n}회 중 실패 {n - ok}회"
    if vids:
        text += f" · 영상당 {n / vids:.1f}회"
    return text


def budget_text(conn):
    base = " · ".join(f"{m} {used(conn, m)[0]}/{budget(m)}" for m in load_settings()["ai"]["models"])
    return base + request_stats(conn)


def _count(conn, model, video_seconds):
    conn.execute("INSERT INTO ai_usage(day, model, requests, video_seconds) VALUES(?,?,1,?) "
                 "ON CONFLICT(day, model) DO UPDATE SET requests=requests+1, "
                 "video_seconds=video_seconds+excluded.video_seconds", (pt_day(), model, video_seconds))
    conn.commit()


def _record(conn, model, label, status, outcome, detail="", usage=None, finish=None, ms=0):
    u = usage or {}
    conn.execute(
        "INSERT INTO ai_requests(at, day, model, label, status, outcome, detail, prompt_tokens, "
        "output_tokens, thought_tokens, finish, ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (now_utc(), pt_day(), model, label, status, outcome, (detail or "")[:200],
         u.get("promptTokenCount"), u.get("candidatesTokenCount"), u.get("thoughtsTokenCount"),
         finish, ms))
    conn.commit()


def _cool(conn, model):
    until = (datetime.now(timezone.utc) + timedelta(minutes=COOLDOWN_MIN)).isoformat(timespec="seconds")
    set_meta(conn, f"ai_cool:{model}", until)


def _cooling(conn, model):
    until = get_meta(conn, f"ai_cool:{model}")
    return bool(until) and until > now_utc()


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


def generate(conn, parts, schema, video_seconds=0, label=None):
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
    config = {"responseMimeType": "application/json", "responseJsonSchema": schema,
              "thinkingConfig": {"thinkingLevel": THINKING_LEVEL}}
    if video_seconds:
        config["mediaResolution"] = "MEDIA_RESOLUTION_LOW"
    body = {"contents": [{"parts": parts}], "generationConfig": config}

    fails, busy, unavailable = 0, [], []
    for model in models:
        if used(conn, model)[0] >= budget(model) or get_meta(conn, f"ai_exhausted:{pt_day()}:{model}"):
            continue
        if _cooling(conn, model):
            busy.append(f"{model}(쉬는 중)")
            continue
        if fails >= MAX_FAILS_PER_CALL:
            break
        wait = MIN_GAP_SEC - (time.time() - _last_call.get(model, 0))
        if wait > 0:
            time.sleep(wait)
        t0 = time.time()
        try:
            r = requests.post(API.format(model), headers={"x-goog-api-key": key}, json=body, timeout=TIMEOUT)
        except requests.Timeout as e:
            # 요청은 이미 모델에 도착했을 수 있으므로 한도 사용으로 셉니다.
            ms = int((time.time() - t0) * 1000)
            _last_call[model] = time.time()
            _count(conn, model, video_seconds)
            _record(conn, model, label, None, "timeout", str(e), ms=ms)
            _cool(conn, model)
            fails += 1
            busy.append(f"{model} 응답 없음({ms // 1000}초)")
            continue
        except requests.RequestException as e:
            _record(conn, model, label, None, "network", str(e))
            raise AIError(f"Gemini 연결 실패: {e}") from e
        _last_call[model] = time.time()
        ms = int((time.time() - t0) * 1000)
        if r.status_code not in NOT_COUNTED:
            _count(conn, model, video_seconds)

        if r.status_code == 200:
            data = r.json()
            finish = ((data.get("candidates") or [{}])[0]).get("finishReason")
            usage = data.get("usageMetadata")
            try:
                result = _parse(data)
            except AIError as e:
                _record(conn, model, label, 200, "parse_fail", str(e), usage, finish, ms)
                raise
            _record(conn, model, label, 200, "ok", "", usage, finish, ms)
            return model, result

        msg = _message(r)
        if r.status_code == 429:
            flat = r.text.lower().replace(" ", "").replace("_", "")
            if '"limit":0' in flat or "limit:0," in flat or flat.endswith("limit:0"):
                _record(conn, model, label, 429, "no_free", msg, ms=ms)
                unavailable.append(f"{model}(무료 등급에서 못 씀)")
                set_meta(conn, f"ai_exhausted:{pt_day()}:{model}", 1)
                continue
            if "perday" in flat:
                _record(conn, model, label, 429, "day_limit", msg, ms=ms)
                set_meta(conn, f"ai_exhausted:{pt_day()}:{model}", 1)
                continue
        if r.status_code in TRANSIENT:
            _record(conn, model, label, r.status_code, "busy", msg, ms=ms)
            _cool(conn, model)
            fails += 1
            busy.append(f"{model} {r.status_code}")
            continue
        if r.status_code == 404:
            _record(conn, model, label, 404, "unavailable", msg, ms=ms)
            unavailable.append(f"{model}({msg})")
            continue
        _record(conn, model, label, r.status_code, "error", msg, ms=ms)
        raise AIError(f"{model} 오류 {r.status_code}: {msg}")

    if unavailable and len(unavailable) == len(models):
        raise AIError("쓸 수 있는 모델이 없음: " + " / ".join(unavailable))
    if busy:
        raise Busy("Gemini가 잠시 바쁩니다: " + ", ".join(busy))
    raise BudgetReached("오늘 AI 무료 한도")


