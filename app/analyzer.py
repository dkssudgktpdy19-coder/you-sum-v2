"""자막을 받아 Gemini로 주장 단위 분석을 합니다. 30분마다 대기열을 처리합니다."""
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app import gemini, transcript, youtube
from app.db import connect, get_meta, now_utc, set_meta
from app.telegram import notify_owner

log = logging.getLogger("yousum.analyze")
KST = ZoneInfo("Asia/Seoul")
VIDEO_FALLBACK_MAX_SEC = 2400  # 자막 없는 영상은 40분 이하만 Gemini가 직접 봄
MAX_ATTEMPTS = 3
GAP_SEC = 30
RUN_LIMIT_SEC = 25 * 60
ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|live/|embed/)([\w-]{11})")

CATEGORIES = ["거시", "주식", "부동산", "코인", "부업·창업", "세금·연금", "기타"]
TYPES = ["새 데이터", "실행 방법", "덜 알려진 정보", "다른 관점", "전망", "일반 상식", "마인드셋"]
EVIDENCE = ["수치·출처 있음", "근거 일부", "근거 없음"]
SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "array", "items": {"type": "string", "enum": CATEGORIES}},
        "one_line": {"type": "string"},
        "claims": {"type": "array", "items": {"type": "object", "properties": {
            "time": {"type": "string"},
            "text": {"type": "string"},
            "type": {"type": "string", "enum": TYPES},
            "evidence": {"type": "string", "enum": EVIDENCE}},
            "required": ["time", "text", "type", "evidence"]}},
        "data_points": {"type": "array", "items": {"type": "object", "properties": {
            "what": {"type": "string"}, "value": {"type": "string"},
            "as_of": {"type": "string"}, "source": {"type": "string"}},
            "required": ["what", "value", "as_of", "source"]}},
        "hype": {"type": "integer"},
        "value_score": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["category", "one_line", "claims", "data_points", "hype", "value_score", "reason"],
}
PROMPT = """너는 경제·재테크 유튜브 영상을 분석해, 바쁜 개인 투자자에게 '새롭고 쓸모 있는 것'만 골라 주는 편집자다.
{source_note}

규칙:
1. claims: 핵심 주장을 중요한 순서로 최대 8개. 한 문장씩, 숫자·이름·조건을 넣어 구체적으로. time은 그 주장이 처음 나오는 시점을 [분:초] 또는 [시:분:초]로.
2. type: 새 데이터=최근 발표된 수치·통계·정책 / 실행 방법=시청자가 바로 따라 할 수 있는 구체적 절차 / 덜 알려진 정보=일반 투자자가 잘 모르는 사실 / 다른 관점=통념이나 다수 의견과 다른 주장 / 전망=근거를 댄 예측 / 일반 상식=이미 널리 알려진 내용 / 마인드셋=막연한 태도·동기부여.
3. evidence: 영상 안에서 출처나 수치를 제시했는지로만 판단.
4. data_points: 영상에 나온 구체적 수치만. 기준 시점(as_of)과 출처(source)를 말하지 않았으면 빈 문자열.
5. hype: 과장 정도 0~3. 3=제목이 내용과 다르거나 근거 없는 공포·확신을 조장.
6. value_score 1~10: 새 데이터·실행 방법·덜 알려진 정보·다른 관점이 많을수록 높게, 마인드셋·일반 상식·과장이 많을수록 낮게.
7. reason: 왜 그 점수인지 한두 문장.
8. one_line: 영상 전체를 한 문장으로.
9. 영상에 없는 내용은 지어내지 말 것. 자동 자막의 오타는 문맥으로 바로잡을 것. 모든 글은 한국어.

제목: {title}
채널: {channel}
{body}"""


class NoSubtitles(Exception):
    pass


def video_id_from(text):
    m = ID_RE.search(text or "")
    return m.group(1) if m else None


def mmss(sec):
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def to_sec(t):
    sec = 0
    for n in [int(x) for x in re.findall(r"\d+", t or "")][-3:]:
        sec = sec * 60 + n
    return sec


def to_prompt_text(lines, chunk=30):
    out, start, buf = [], None, []
    for t, s in lines:
        if start is None:
            start = t
        if t - start >= chunk and buf:
            out.append(f"[{mmss(start)}] {' '.join(buf)}")
            start, buf = t, []
        buf.append(s)
    if buf:
        out.append(f"[{mmss(start)}] {' '.join(buf)}")
    return "\n".join(out)


def save(conn, vid, status, **f):
    conn.execute(
        "INSERT INTO analyses(video_id, status, attempts, source, model, result, error, next_try_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(video_id) DO UPDATE SET status=excluded.status, "
        "attempts=excluded.attempts, source=excluded.source, model=excluded.model, result=excluded.result, "
        "error=excluded.error, next_try_at=excluded.next_try_at, updated_at=excluded.updated_at",
        (vid, status, f.get("attempts", 0), f.get("source"), f.get("model"), f.get("result"),
         f.get("error"), f.get("next_try_at"), now_utc()))
    conn.commit()


def analyze_video(conn, vid):
    v = conn.execute("SELECT v.title, v.duration_sec, c.title AS ch FROM videos v "
                     "LEFT JOIN channels c ON c.channel_id=v.channel_id WHERE v.video_id=?", (vid,)).fetchone()
    cached = conn.execute("SELECT kind, lines FROM transcripts WHERE video_id=?", (vid,)).fetchone()
    meta = {}
    if cached:
        kind, lines = cached["kind"], json.loads(cached["lines"])
    else:
        t = transcript.fetch(vid)
        meta, kind, lines = t["meta"], t["kind"], t["lines"]
        if lines:
            conn.execute("INSERT OR REPLACE INTO transcripts VALUES(?,?,?,?,?)",
                         (vid, t["lang"], kind, json.dumps(lines, ensure_ascii=False), now_utc()))
            conn.commit()
    title = (v and v["title"]) or meta.get("title") or vid
    channel = (v and v["ch"]) or meta.get("channel") or ""
    duration = (v and v["duration_sec"]) or meta.get("duration") or 0

    if lines:
        source = "사람이 단 자막" if kind == "manual" else "자동 자막"
        prompt = PROMPT.format(source_note="아래는 영상 자막이다. 각 줄 앞의 [ ]는 그 내용이 나오는 시점이다.",
                               title=title, channel=channel, body="자막:\n" + to_prompt_text(lines))
        model, result = gemini.generate(conn, [{"text": prompt}], SCHEMA, label=vid)
    else:
        if not duration or duration > VIDEO_FALLBACK_MAX_SEC:
            raise NoSubtitles("한국어 자막이 없고, 영상이 길어 직접 분석도 하지 않았습니다")
        source = "영상 직접 분석"
        prompt = PROMPT.format(source_note="첨부한 영상을 직접 보고 들은 내용으로 분석하라.",
                               title=title, channel=channel, body="")
        parts = [{"fileData": {"fileUri": f"https://www.youtube.com/watch?v={vid}"}}, {"text": prompt}]
        model, result = gemini.generate(conn, parts, SCHEMA, video_seconds=duration, label=vid)

    result["_meta"] = {"title": title, "channel": channel, "duration": duration}
    save(conn, vid, "done", source=source, model=model, result=json.dumps(result, ensure_ascii=False))
    return {"video_id": vid, "source": source, "model": model, "result": result}


def format_result(rec):
    r, vid = rec["result"], rec["video_id"]
    m = r.get("_meta", {})
    lines = [f"🧪 분석 결과 · {rec['source']} · {rec['model']}", m.get("title", ""),
             f"{m.get('channel', '')} · {(m.get('duration') or 0) // 60}분", "",
             f"가치 {r.get('value_score')}/10 · 과장 {r.get('hype')}/3 · {', '.join(r.get('category', []))}",
             f"근거: {r.get('reason', '')}", f"한 줄: {r.get('one_line', '')}", "", "📌 핵심 주장"]
    for c in r.get("claims", []):
        sec = to_sec(c.get("time"))
        lines.append(f"[{mmss(sec)}] {c.get('type')} · {c.get('evidence')}\n{c.get('text')}\n"
                     f"https://youtu.be/{vid}?t={sec}")
    if r.get("data_points"):
        lines += ["", "📊 영상에 나온 수치"]
        for d in r["data_points"]:
            extra = ", ".join(x for x in (d.get("as_of"), d.get("source")) if x)
            lines.append(f"• {d.get('what')}: {d.get('value')}" + (f" ({extra})" if extra else ""))
    return "\n".join(lines)


QUEUE_WHERE = ("FROM videos v LEFT JOIN analyses a ON a.video_id=v.video_id "
               "WHERE v.kind='long' AND v.published_at>=? "
               "AND (a.video_id IS NULL OR (a.status='retry' AND a.next_try_at<=?))")


def notify_once(conn, key, text):
    if not get_meta(conn, key):
        notify_owner(text)
        set_meta(conn, key, 1)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    conn = connect()
    since = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds")
    started, done, skipped, fails, busy, stop, last_err = time.time(), 0, 0, 0, 0, None, ""
    today = datetime.now(KST).strftime("%Y-%m-%d")

    while time.time() - started < RUN_LIMIT_SEC - gemini.DEADLINE_SEC - GAP_SEC:
        row = conn.execute("SELECT v.video_id, COALESCE(a.attempts,0) AS attempts " + QUEUE_WHERE +
                           " ORDER BY v.published_at LIMIT 1", (since, now_utc())).fetchone()
        if not row:
            break
        vid, attempts = row["video_id"], row["attempts"]
        later = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
        try:
            rec = analyze_video(conn, vid)
            done, fails, busy = done + 1, 0, 0
            log.info("완료 %d번째 %s %s %s", done, vid, rec["model"],
                     rec["result"].get("_meta", {}).get("title", "")[:40])
        except gemini.VideoBudgetReached:
            six = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat(timespec="seconds")
            save(conn, vid, "retry", attempts=attempts, error="영상 직접 분석 시간 한도", next_try_at=six)
        except gemini.BudgetReached:
            stop = "budget"
            break
        except gemini.Busy as e:
            half = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(timespec="seconds")
            last_err = str(e)[:150]
            save(conn, vid, "retry", attempts=attempts, error=last_err, next_try_at=half)
            log.info("미룸 %s %s", vid, last_err)
            busy += 1
            if busy >= 2:
                stop = "busy"
                break
        except youtube.Blocked as e:
            stop, last_err = "blocked", str(e)[:150]
            break
        except NoSubtitles as e:
            save(conn, vid, "no_transcript", attempts=attempts, error=str(e))
            skipped += 1
        except Exception as e:
            log.exception("분석 실패 %s", vid)
            attempts, fails, last_err = attempts + 1, fails + 1, str(e)[:200]
            save(conn, vid, "failed" if attempts >= MAX_ATTEMPTS else "retry",
                 attempts=attempts, error=last_err, next_try_at=later)
            if fails >= 3:
                stop = "error"
                break
        time.sleep(GAP_SEC)

    left = conn.execute("SELECT COUNT(*) " + QUEUE_WHERE, (since, now_utc())).fetchone()[0]
    stamp = datetime.now(KST).strftime("%m/%d %H:%M")
    summary = f"{stamp} · 분석 {done}개 · 자막 없음 {skipped}개 · 대기 {left}개"
    if stop:
        summary += {"budget": " · ⏸ AI 한도", "blocked": " · ⚠️ 자막 요청 막힘",
                    "error": " · ⚠️ 오류 반복", "busy": " · Gemini 혼잡, 30분 뒤 재시도"}[stop]
    set_meta(conn, "last_analyze", summary)
    log.info(summary)
    if stop == "budget":
        notify_once(conn, f"notified_budget:{gemini.pt_day()}",
                    f"⏸ 오늘 쓰기로 한 AI 무료 한도(70%)를 다 썼습니다.\n남은 롱폼 {left}개는 한도가 새로 풀리는 "
                    "오후 4~5시 이후 이어서 분석합니다. 따로 하실 일은 없습니다.")
    elif stop == "blocked":
        notify_once(conn, f"notified_blocked:{today}",
                    "⚠️ 유튜브가 자막 요청을 잠시 막았습니다. 30분 뒤 자동으로 다시 시도합니다.\n"
                    "하루 넘게 계속되면 /status를 캡처해 알려 주세요.")
    elif stop == "error":
        notify_once(conn, f"notified_error:{today}",
                    f"🚨 영상 분석이 연속 3번 실패했습니다.\n마지막 오류: {last_err}\n"
                    "이 메시지를 캡처해서 알려 주세요.")


if __name__ == "__main__":
    main()
