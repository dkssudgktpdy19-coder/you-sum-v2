"""Gemini 무료 등급 호출과 사용량 관리 (하루 한도는 미국 태평양 자정에 초기화)"""
import json
import random
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from app.config import load_secrets, load_settings
from app.db import get_meta, now_utc, set_meta

# 스트리밍: 답을 조금씩 받으므로 긴 자막도 '응답 없음'으로 끊기지 않음
API = "https://generativelanguage.googleapis.com/v1beta/models/{}:streamGenerateContent?alt=sse"
PT = ZoneInfo("America/Los_Angeles")
NOT_COUNTED = (400, 401, 403, 404)       # 모델까지 못 간 요청은 한도에서 빼고 셈
TRANSIENT = (429, 500, 502, 503, 504)    # 잠깐 바쁨
COOL_MIN = {"busy": 3, "rpm": 1, "timeout": 10, "network": 2}  # 상황별 쉬는 시간(분)
ROUNDS = 2                 # 모든 모델을 최대 2바퀴 시도
ROUND_WAIT = (20, 40)      # 바퀴 사이 대기 (초, 무작위)
DEADLINE_SEC = 300         # 영상 하나당 최대 5분
MIN_GAP_SEC = 13           # 같은 모델 요청 간격 (분당 한도 보호)
CONNECT_TIMEOUT = 15
IDLE_TIMEOUT = 150         # 이 시간 동안 데이터가 전혀 안 오면 응답 없음
MAX_OUTPUT_TOKENS = 8192
THINKING_LEVEL = "low"
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


def _cool(conn, model, kind):
    until = datetime.now(timezone.utc) + timedelta(minutes=COOL_MIN[kind])
    set_meta(conn, f"ai_cool:{model}", until.isoformat(timespec="seconds"))


def _cooling(conn, model):
    until = get_meta(conn, f"ai_cool:{model}")
    return bool(until) and until > now_utc()


def _message(text):
    try:
        data = json.loads(text)
        if isinstance(data, list):          # 스트리밍 주소는 오류를 [ {...} ] 로 줄 때가 있음
            data = data[0]
        return data["error"]["message"][:150]
    except Exception:
        return (text or "")[:150]


class _StreamFail(Exception):
    def __init__(self, outcome, detail):
        super().__init__(detail)
        self.outcome = outcome


def _read_stream(r, started):
    """SSE 응답을 끝까지 읽어 (본문 글자, 종료 이유, 사용량)을 돌려줌"""
    texts, finish, usage, block = [], None, None, None
    for raw in r.iter_lines():
        if time.time() - started > DEADLINE_SEC:
            raise _StreamFail("timeout", "전체 시간 초과")
        if not raw or not raw.startswith(b"data:"):
            continue
        chunk = json.loads(raw[5:].strip().decode("utf-8"))
        if "error" in chunk:
            raise _StreamFail("busy", chunk["error"].get("message", "")[:150])
        usage = chunk.get("usageMetadata") or usage
        block = (chunk.get("promptFeedback") or {}).get("blockReason") or block
        for cand in chunk.get("candidates") or []:
            finish = cand.get("finishReason") or finish
            for p in (cand.get("content") or {}).get("parts") or []:
                if not p.get("thought"):
                    texts.append(p.get("text", ""))
    return "".join(texts), finish or block, usage


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
    config = {"responseMimeType": "application/json", "responseJsonSchema": schema,
              "maxOutputTokens": MAX_OUTPUT_TOKENS,
              "thinkingConfig": {"thinkingLevel": THINKING_LEVEL}}
    if video_seconds:
        config["mediaResolution"] = "MEDIA_RESOLUTION_LOW"
    body = {"contents": [{"parts": parts}], "generationConfig": config}

    started = time.time()
    pre_cooled = {m for m in models if _cooling(conn, m)}   # 이번 호출 전부터 쉬던 모델
    dead = set()                                             # 오늘은 못 쓰는 모델
    busy, unavailable, last_err = [], [], ""

    for rnd in range(ROUNDS):
        if rnd:
            if time.time() - started > DEADLINE_SEC - 90:
                break
            time.sleep(random.uniform(*ROUND_WAIT))
        for model in models:
            if model in dead:
                continue
            if used(conn, model)[0] >= budget(model) or get_meta(conn, f"ai_exhausted:{pt_day()}:{model}"):
                dead.add(model)
                continue
            if rnd == 0 and model in pre_cooled:
                busy.append(f"{model}(쉬는 중)")
                continue
            if time.time() - started > DEADLINE_SEC - 30:
                break
            wait = MIN_GAP_SEC - (time.time() - _last_call.get(model, 0))
            if wait > 0:
                time.sleep(wait)

            t0 = time.time()
            ms = lambda: int((time.time() - t0) * 1000)
            try:
                r = requests.post(API.format(model), headers={"x-goog-api-key": key}, json=body,
                                  timeout=(CONNECT_TIMEOUT, IDLE_TIMEOUT), stream=True)
            except (requests.ConnectTimeout, requests.ConnectionError) as e:
                # 접속 단계 실패: 모델까지 안 갔으므로 한도에서 빼지 않음
                _record(conn, model, label, None, "network", str(e), ms=ms())
                _cool(conn, model, "network")
                busy.append(f"{model} 연결 실패")
                continue
            except requests.Timeout as e:
                _last_call[model] = time.time()
                _count(conn, model, video_seconds)
                _record(conn, model, label, None, "timeout", str(e), ms=ms())
                _cool(conn, model, "timeout")
                busy.append(f"{model} 응답 없음({ms() // 1000}초)")
                continue
            _last_call[model] = time.time()
            if r.status_code not in NOT_COUNTED:
                _count(conn, model, video_seconds)

            if r.status_code == 200:
                try:
                    with r:
                        text, finish, usage = _read_stream(r, started)
                except _StreamFail as e:
                    _record(conn, model, label, 200, e.outcome, str(e), ms=ms())
                    _cool(conn, model, "busy" if e.outcome == "busy" else "timeout")
                    busy.append(f"{model} {e.outcome}")
                    continue
                except (requests.RequestException, ValueError) as e:
                    # 스트리밍 중 끊김 (읽기 시간 초과도 여기로 옴)
                    _record(conn, model, label, 200, "timeout", str(e), ms=ms())
                    _cool(conn, model, "timeout")
                    busy.append(f"{model} 응답 끊김")
                    continue
                try:
                    result = json.loads(text)
                except ValueError:
                    detail = f"응답을 읽지 못함 ({finish})"
                    _record(conn, model, label, 200, "parse_fail", detail, usage, finish, ms())
                    last_err = f"{model} {detail}"
                    continue          # 다른 모델로 다시 시도
                _record(conn, model, label, 200, "ok", "", usage, finish, ms())
                return model, result

            msg = _message(r.text)
            r.close()
            if r.status_code == 429:
                flat = msg.lower().replace(" ", "").replace("_", "") + r.text.lower().replace(" ", "")
                if '"limit":0' in flat or "limit:0," in flat:
                    _record(conn, model, label, 429, "no_free", msg, ms=ms())
                    unavailable.append(f"{model}(무료 등급에서 못 씀)")
                    set_meta(conn, f"ai_exhausted:{pt_day()}:{model}", 1)
                    dead.add(model)
                    continue
                if "perday" in flat:
                    _record(conn, model, label, 429, "day_limit", msg, ms=ms())
                    set_meta(conn, f"ai_exhausted:{pt_day()}:{model}", 1)
                    dead.add(model)
                    continue
                _record(conn, model, label, 429, "busy", msg, ms=ms())
                _cool(conn, model, "rpm")
                busy.append(f"{model} 429")
                continue
            if r.status_code in TRANSIENT:
                _record(conn, model, label, r.status_code, "busy", msg, ms=ms())
                _cool(conn, model, "busy")
                busy.append(f"{model} {r.status_code}")
                continue
            if r.status_code == 404:
                _record(conn, model, label, 404, "unavailable", msg, ms=ms())
                unavailable.append(f"{model}({msg})")
                dead.add(model)
                continue
            _record(conn, model, label, r.status_code, "error", msg, ms=ms())
            raise AIError(f"{model} 오류 {r.status_code}: {msg}")

    if unavailable and len(unavailable) == len(models):
        raise AIError("쓸 수 있는 모델이 없음: " + " / ".join(unavailable))
    if busy:
        raise Busy("Gemini가 잠시 바쁩니다: " + ", ".join(dict.fromkeys(busy)))
    if last_err:
        raise AIError(last_err)
    raise BudgetReached("오늘 AI 무료 한도")
