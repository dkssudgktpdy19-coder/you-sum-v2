"""아침 보고서: 전날 영상을 최근 보고서와 실제로 비교해 순위를 정하고 텔레그램으로 보냅니다.
10분마다 실행되며, settings.toml [report] time 이 지나면 하루 한 번 보냅니다."""
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app import gemini
from app.analyzer import mmss, to_sec
from app.config import load_secrets, load_settings
from app.db import connect, get_meta, now_utc, set_meta
from app.telegram import Telegram, notify_owner

log = logging.getLogger("yousum.report")
KST = ZoneInfo("Asia/Seoul")
WEEKDAY = "월화수목금토일"
PAST_DAYS = 7            # 새로움 비교에 쓰는 지난 보고서 기간
BACKLOG_DAYS = 7         # 아직 보고서에 안 나온 영상을 찾는 기간
ANALYZE_WINDOW_DAYS = 3  # analyzer가 다루는 기간. 이보다 오래 대기 중이면 '분석 못 함'으로 정리
MAX_PAST_LINES = 300
MAX_TASTE_LINES = 30
RETRY_MIN = 50           # 정해진 시각 뒤 50분까지는 Gemini가 바쁘면 10분 뒤 다시 시도
MSG_LIMIT = 3500
GOOD_TYPES = ["새 데이터", "실행 방법", "덜 알려진 정보", "다른 관점", "전망"]
TIERS = ["must", "rest", "demoted"]

SCHEMA = {
    "type": "object",
    "properties": {
        "videos": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"},
            "novelty": {"type": "integer"},
            "overlap": {"type": "string"},
            "contrast": {"type": "string"},
            "tier": {"type": "string", "enum": TIERS},
            "reason": {"type": "string"}},
            "required": ["id", "novelty", "overlap", "contrast", "tier", "reason"]}},
        "briefing": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "time": {"type": "string"}, "text": {"type": "string"}},
            "required": ["id", "time", "text"]}},
    },
    "required": ["videos", "briefing"],
}
PROMPT = """너는 바쁜 개인 투자자에게 보낼 경제·재테크 유튜브 아침 보고서의 순서를 정하는 편집장이다.
[오늘 후보]의 각 영상을 [최근 보고서에 이미 나온 주장], 그리고 오늘 다른 후보들과 실제로 비교해서 판단하라.
감으로 '새롭다'고 하지 말고, 겹치면 무엇과 겹치는지 overlap에 구체적으로 적어라. 겹치지 않으면 빈 문자열.

규칙:
- 올릴 것: 최신 수치·데이터·발표, 바로 따라 할 수 있는 구체적 방법, 덜 알려진 정보, 다른 영상과 다른 관점이나 반대 의견
- 내릴 것: 막연한 마인드셋, 널리 알려진 내용, 최근 보고서나 오늘 다른 후보와 겹치는 얘기, 근거 없는 과장·낚시성 제목, 채널 자체 상품·서비스 홍보
- novelty 0~3: 3=거의 안 겹침 / 2=일부 겹침 / 1=대부분 겹침 / 0=거의 같은 얘기
- contrast: 다른 후보나 최근 주장과 반대되는 의견이면 누구의 어떤 주장과 어떻게 다른지 한 문장. 아니면 빈 문자열.
- tier: must=오늘 꼭 볼 것(최대 {must_max}개, 정말 볼 가치가 있을 때만, 0개도 괜찮음) / rest=나머지 / demoted=뒤로 밀 것
- reason: 왜 그 순위인지 한두 문장. 무엇이 새롭거나 무엇과 겹치는지 비교 결과를 꼭 넣을 것.
- videos에는 오늘 후보 전부를 빠짐없이 넣고, id는 V1, V2처럼 그대로 쓸 것.
- briefing: 오늘 후보에 나온 새 수치·발표 중 투자자가 알아야 할 것 최대 8개. 같은 수치는 한 번만, 최근 보고서에 이미 나온 수치는 빼고, 수치와 기준 시점을 넣어 한 문장으로. id는 그 수치가 나온 영상, time은 그 영상 안의 시점.
{taste}
- 모든 글은 한국어.

[최근 보고서에 이미 나온 주장]
{past}

[오늘 후보]
{cands}"""


def _utc(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _day0(dt):
    return dt.astimezone(KST).replace(hour=0, minute=0, second=0, microsecond=0)


def _label(d):
    return f"{d:%m/%d}({WEEKDAY[d.weekday()]})"


def _yt(vid, sec=None):
    return f"https://youtu.be/{vid}" + (f"?t={sec}" if sec else "")


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


def must_max():
    return int(load_settings()["report"].get("must_max", 3))


def report_time(now):
    hh, mm = (int(x) for x in load_settings()["report"]["time"].split(":"))
    return now.replace(hour=hh, minute=mm, second=0, microsecond=0)


# ---------- 재료 모으기 ----------

def _collect(conn, start, end):
    since = _utc(end - timedelta(days=BACKLOG_DAYS))
    stale = _utc(datetime.now(timezone.utc) - timedelta(days=ANALYZE_WINDOW_DAYS))
    start_s = _utc(start)
    rows = conn.execute(
        "SELECT v.video_id, v.title, v.published_at, v.duration_sec, c.title AS ch, "
        "a.status, a.result, a.error FROM videos v "
        "LEFT JOIN analyses a ON a.video_id=v.video_id "
        "LEFT JOIN channels c ON c.channel_id=v.channel_id "
        "LEFT JOIN report_items r ON r.video_id=v.video_id "
        "WHERE v.kind='long' AND r.video_id IS NULL AND v.published_at>=? AND v.published_at<? "
        "ORDER BY v.published_at", (since, _utc(end))).fetchall()
    done, broken, waiting = [], [], []
    for row in rows:
        it = {"vid": row["video_id"], "title": row["title"] or row["video_id"], "ch": row["ch"] or "",
              "minutes": (row["duration_sec"] or 0) // 60, "old": row["published_at"] < start_s}
        if row["status"] == "done" and row["result"]:
            r = json.loads(row["result"])
            m = r.get("_meta", {})
            it.update(r=r, title=m.get("title") or it["title"], ch=m.get("channel") or it["ch"])
            done.append(it)
        elif row["status"] in ("failed", "no_transcript") or row["published_at"] < stale:
            it["why"] = ("자막이 없어 분석 못 함" if row["status"] == "no_transcript"
                         else f"분석 실패: {(row['error'] or '시간 초과')[:60]}")
            broken.append(it)
        else:
            waiting.append(it)
    return done, broken, waiting


def _past_lines(conn, end):
    since = (_day0(end) - timedelta(days=PAST_DAYS)).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT r.report_day, a.result FROM report_items r JOIN analyses a ON a.video_id=r.video_id "
        "WHERE r.report_day>=? AND a.status='done' ORDER BY r.report_day DESC", (since,)).fetchall()
    out = []
    for row in rows:
        res = json.loads(row["result"])
        ch = res.get("_meta", {}).get("channel", "")
        for c in res.get("claims", []):
            out.append(f"- ({row['report_day'][5:]}, {ch}) {(c.get('text') or '')[:120]}")
            if len(out) >= MAX_PAST_LINES:
                return "\n".join(out)
    return "\n".join(out) or "(아직 없음: 첫 보고서이므로 오늘 후보끼리만 비교)"


def _taste_lines(conn):
    rows = conn.execute(
        "SELECT f.vote, a.result FROM feedback f JOIN analyses a ON a.video_id=f.video_id "
        "WHERE a.status='done' ORDER BY f.at DESC LIMIT ?", (MAX_TASTE_LINES,)).fetchall()
    lines = []
    for row in rows:
        r = json.loads(row["result"])
        m = r.get("_meta", {})
        lines.append(f"{'👍' if row['vote'] > 0 else '👎'} [{', '.join(r.get('category', []))}] "
                     f"{(m.get('title') or '')[:50]} — {(r.get('one_line') or '')[:80]}")
    if not lines:
        return ""
    return ("- 사용자 취향 기록(👍 좋아함 / 👎 별로). 주제·유형이 비슷한 영상은 이 기록을 참고해 올리거나 내려라:\n"
            + "\n".join(lines))


def _cand_text(items):
    out = []
    for it in items:
        r = it["r"]
        lines = [f"{it['key']} | 채널: {it['ch']} | 제목: {it['title']} | {it['minutes']}분 | "
                 f"개별 평가: 가치 {r.get('value_score')}/10, 과장 {r.get('hype')}/3 | "
                 f"분류: {', '.join(r.get('category', []))}",
                 f"한 줄: {r.get('one_line', '')}", "주장:"]
        lines += [f"- [{c.get('time')}] ({c.get('type')}, {c.get('evidence')}) {c.get('text')}"
                  for c in r.get("claims", [])]
        if r.get("data_points"):
            lines.append("수치:")
            lines += [f"- {d.get('what')}: {d.get('value')} ({d.get('as_of') or '시점 미상'}, "
                      f"{d.get('source') or '출처 미상'})" for d in r["data_points"]]
        out.append("\n".join(lines))
    return "\n\n".join(out)


# ---------- 판단과 정렬 ----------

def _judge(conn, items, end, label):
    prompt = PROMPT.format(must_max=must_max(), taste=_taste_lines(conn),
                           past=_past_lines(conn, end), cands=_cand_text(items))
    _, res = gemini.generate(conn, [{"text": prompt}], SCHEMA, label=label)
    verdicts = {str(v.get("id", "")).strip(): v for v in res.get("videos", []) if isinstance(v, dict)}
    return verdicts, res.get("briefing", [])


def _rank(items, verdicts):
    for it in items:
        r, v = it["r"], verdicts.get(it["key"])
        value, hype = _int(r.get("value_score")), _int(r.get("hype"))
        if v:
            it["novelty"] = max(0, min(3, _int(v.get("novelty"))))
            it["tier"] = v.get("tier") if v.get("tier") in TIERS else "rest"
            it["why"] = v.get("reason") or r.get("reason", "")
            it["contrast"] = v.get("contrast") or ""
        else:  # 비교 분석이 없을 때: 영상별 점수로만
            it["novelty"], it["contrast"] = None, ""
            it["tier"] = "demoted" if value <= 4 or hype >= 2 else ("must" if value >= 8 else "rest")
            it["why"] = r.get("reason", "")
        it["score"] = value + 1.5 * (it["novelty"] or 0) - hype + (1 if it["contrast"] else 0)
    items.sort(key=lambda x: (TIERS.index(x["tier"]), -x["score"]))
    limit, n = must_max(), 0
    for it in items:
        if it["tier"] == "must":
            n += 1
            if n > limit:
                it["tier"] = "rest"
    items.sort(key=lambda x: (TIERS.index(x["tier"]), -x["score"]))


# ---------- 메시지 만들기 ----------

def _fb_row(vid):
    return [{"text": "👍 좋아요", "callback_data": f"fb:u:{vid}"},
            {"text": "👎 별로", "callback_data": f"fb:d:{vid}"}]


def _fb_grid(entries):
    rows, row = [], []
    for num, vid in entries:
        row += [{"text": f"{num} 👍", "callback_data": f"fb:u:{vid}"},
                {"text": f"{num} 👎", "callback_data": f"fb:d:{vid}"}]
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def _pack(title, blocks):
    """(글, 번호, 영상ID) 묶음을 길이에 맞게 나누고, 메시지마다 해당 영상 버튼을 붙입니다."""
    msgs, text, entries, count = [], title, [], 0
    for body, num, vid in blocks:
        if count and len(text) + len(body) + 2 > MSG_LIMIT:
            msgs.append({"text": text, "buttons": _fb_grid(entries) or None})
            text, entries, count = title + " (이어서)", [], 0
        text += "\n\n" + body
        count += 1
        if vid:
            entries.append((num, vid))
    if count:
        msgs.append({"text": text, "buttons": _fb_grid(entries) or None})
    return msgs


def _tags(it):
    r = it["r"]
    s = f"{it['ch']} · {it['minutes']}분 · 가치 {r.get('value_score')}/10"
    if it["novelty"] is not None:
        s += f" · 새로움 {it['novelty']}/3"
    if _int(r.get("hype")) >= 2:
        s += f" · 과장 {r.get('hype')}/3"
    return s + (" · 지난 영상" if it["old"] else "")


def _must_text(num, it):
    r = it["r"]
    lines = [f"⭐ {num}. {it['title']}", _tags(it), f"💡 {it['why']}"]
    if it["contrast"]:
        lines.append(f"↔️ 다른 관점: {it['contrast']}")
    lines += [f"한 줄: {r.get('one_line', '')}", ""]
    claims = r.get("claims", [])
    good = [c for c in claims if c.get("type") in GOOD_TYPES]
    for c in (good or claims)[:4]:
        sec = to_sec(c.get("time"))
        lines.append(f"• [{mmss(sec)}] {c.get('type')} · {c.get('text')}\n  {_yt(it['vid'], sec)}")
    lines.append(f"\n🎬 {_yt(it['vid'])}")
    return "\n".join(lines)


def _briefing_lines(items, briefing, keymap):
    lines = []
    for b in briefing[:8]:
        it = keymap.get(str(b.get("id", "")).strip())
        if not it:
            continue
        sec = to_sec(b.get("time"))
        lines.append(f"• {b.get('text')}\n  └ {it['ch']} [{mmss(sec)}] {_yt(it['vid'], sec)}")
    if lines:
        return lines
    for it in items:  # 비교 분석이 없을 때: 영상에 나온 수치를 그대로
        if it["tier"] == "demoted":
            continue
        for d in it["r"].get("data_points", [])[:2]:
            extra = ", ".join(x for x in (d.get("as_of"), d.get("source")) if x)
            lines.append(f"• {d.get('what')}: {d.get('value')}" + (f" ({extra})" if extra else "")
                         + f"\n  └ {it['ch']} {_yt(it['vid'])}")
        if len(lines) >= 8:
            break
    return lines[:8]


def build(conn, start, end, label, title, allow_wait=False):
    """보고서 메시지 목록, 보고서에 넣은 영상 기록, 방식(ai/basic)을 돌려줍니다."""
    done, broken, waiting = _collect(conn, start, end)
    for i, it in enumerate(done, 1):
        it["key"] = f"V{i}"
    notes, mode, verdicts, briefing = [], "basic", {}, []
    if done:
        try:
            verdicts, briefing = _judge(conn, done, end, label)
            mode = "ai"
        except gemini.Busy:
            if allow_wait:
                raise
            notes.append("⚠️ Gemini가 바빠 '최근 내용과 비교'를 못 하고 영상별 점수로만 정렬했습니다.")
        except gemini.BudgetReached:
            notes.append("⚠️ 오늘 AI 한도를 다 써서 '최근 내용과 비교'를 못 하고 영상별 점수로만 정렬했습니다.")
        except gemini.AIError as e:
            notes.append(f"⚠️ 비교 분석 실패({str(e)[:80]}) · 영상별 점수로만 정렬했습니다.")
    _rank(done, verdicts)
    keymap = {it["key"]: it for it in done}
    must = [it for it in done if it["tier"] == "must"]
    rest = [it for it in done if it["tier"] == "rest"]
    demoted = [it for it in done if it["tier"] == "demoted"]

    header = [f"☀️ {title}"]
    if done or broken:
        header.append(f"롱폼 {len(done)}개 분석 · ⭐ 꼭 볼 것 {len(must)} · 📝 나머지 {len(rest)} · "
                      f"🔻 뒤로 밀림 {len(demoted)}" + (f" · ⚠️ 분석 못 함 {len(broken)}" if broken else ""))
    else:
        header.append("보고서에 넣을 새 롱폼이 없습니다.")
    if waiting:
        header.append(f"⏳ 아직 분석 중인 영상 {len(waiting)}개는 다음 보고서에 넣습니다.")
    if mode == "ai":
        header.append("새로움은 최근 7일 보고서·오늘 다른 영상과 비교해 판단했습니다.")
    header += notes
    if done and not must:
        header.append("오늘은 '꼭 볼 것'으로 고를 만한 영상이 없었습니다.")
    if done:
        header.append("\n버튼 👍/👎로 알려 주시면 다음 보고서 순위에 반영합니다.")
    msgs = [{"text": "\n".join(header)}]

    num = 0
    for it in must:
        num += 1
        msgs.append({"text": _must_text(num, it), "buttons": [_fb_row(it["vid"])]})
    lines = _briefing_lines(done, briefing, keymap)
    if lines:
        msgs.append({"text": "📊 새 데이터 브리핑\n\n" + "\n".join(lines)})
    blocks = []
    for it in rest:
        num += 1
        blocks.append((f"{num}. {it['title']}\n   {_tags(it)}\n   {it['r'].get('one_line', '')}\n"
                       f"   💡 {it['why']}\n   {_yt(it['vid'])}", num, it["vid"]))
    msgs += _pack("📝 나머지 한 줄 요약", blocks)
    blocks = []
    for it in demoted:
        num += 1
        blocks.append((f"{num}. {it['title']}\n   {_tags(it)}\n   🔻 {it['why']}\n   {_yt(it['vid'])}",
                       num, it["vid"]))
    for it in broken:
        num += 1
        blocks.append((f"{num}. {it['title']}\n   {it['ch']} · ⚠️ {it['why']}\n   {_yt(it['vid'])}", num, None))
    msgs += _pack("🔻 뒤로 밀린 영상 (판단이 틀렸다면 👍로 알려 주세요)", blocks)

    marks = [(it["vid"], it["tier"], it["novelty"], (it["why"] or "")[:300]) for it in done]
    marks += [(it["vid"], "broken", None, it["why"]) for it in broken]
    return msgs, marks, mode


# ---------- 보내기·저장·조회 ----------

def deliver(tg, chat_id, msgs):
    for i, m in enumerate(msgs):
        if i:
            time.sleep(1)  # 텔레그램 전송 속도 제한 보호
        tg.send(chat_id, m["text"], buttons=m.get("buttons"))


def commit(conn, day, msgs, marks, mode):
    conn.execute("INSERT OR REPLACE INTO reports(day, created_at, mode, messages) VALUES(?,?,?,?)",
                 (day, now_utc(), mode, json.dumps(msgs, ensure_ascii=False)))
    conn.executemany("INSERT OR IGNORE INTO report_items(video_id, report_day, tier, novelty, reason) "
                     "VALUES(?,?,?,?,?)", [(v, day, t, n, w) for v, t, n, w in marks])
    conn.commit()


def preview(conn, tg, chat_id):
    now = datetime.now(KST)
    msgs, _, _ = build(conn, now - timedelta(hours=24), now, label="report:preview",
                       title="미리보기 · 아직 보고서에 안 나온 영상 (저장 안 함)")
    deliver(tg, chat_id, msgs)


def saved(conn, day):
    row = conn.execute("SELECT messages FROM reports WHERE day=?", (day,)).fetchone()
    return json.loads(row["messages"]) if row else None


def latest_day(conn):
    row = conn.execute("SELECT day FROM reports ORDER BY day DESC LIMIT 1").fetchone()
    return row["day"] if row else None


def recent_days(conn, n=14):
    return conn.execute(
        "SELECT r.day, r.mode, (SELECT COUNT(*) FROM report_items i WHERE i.report_day=r.day) AS n "
        "FROM reports r ORDER BY r.day DESC LIMIT ?", (n,)).fetchall()


def parse_day(text):
    now = datetime.now(KST)
    for fmt in ("%Y-%m-%d", "%m/%d", "%m-%d", "%m.%d"):
        try:
            d = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt != "%Y-%m-%d":
            d = d.replace(year=now.year)
            if d.date() > now.date():
                d = d.replace(year=now.year - 1)
        return d.strftime("%Y-%m-%d")
    return None


def record_feedback(conn, data):
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[1] not in ("u", "d"):
        return None
    vote, vid = (1 if parts[1] == "u" else -1), parts[2]
    conn.execute("INSERT INTO feedback(video_id, vote, at) VALUES(?,?,?) ON CONFLICT(video_id) "
                 "DO UPDATE SET vote=excluded.vote, at=excluded.at", (vid, vote, now_utc()))
    conn.commit()
    row = conn.execute("SELECT title FROM videos WHERE video_id=?", (vid,)).fetchone()
    name = ((row and row["title"]) or vid)[:30]
    return f"{'👍 좋아요' if vote > 0 else '👎 별로'} 기록: {name}"


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    conn = connect()
    now = datetime.now(KST)
    target = report_time(now)
    if now < target:
        return
    today0 = _day0(now)
    y0 = today0 - timedelta(days=1)
    day = y0.strftime("%Y-%m-%d")
    if conn.execute("SELECT 1 FROM reports WHERE day=?", (day,)).fetchone():
        return
    s = load_secrets()
    token, chat = s.get("TELEGRAM_BOT_TOKEN"), s.get("TELEGRAM_ALLOWED_CHAT_ID")
    if not token or not chat:
        log.error("텔레그램 설정이 없어 보고서를 보낼 수 없습니다.")
        return
    try:
        msgs, marks, mode = build(conn, y0, today0, label=f"report:{day}",
                                  title=f"{_label(today0)} 아침 보고서 · {_label(y0)} 영상",
                                  allow_wait=now < target + timedelta(minutes=RETRY_MIN))
        deliver(Telegram(token), int(chat), msgs)
    except gemini.Busy as e:
        log.info("Gemini 혼잡, 10분 뒤 다시 시도: %s", e)
        return
    except Exception as e:
        log.exception("보고서 실패")
        key = f"notified_report_error:{day}"
        if not get_meta(conn, key):
            notify_owner(f"🚨 아침 보고서를 만들지 못했습니다.\n오류: {str(e)[:200]}\n"
                         "10분마다 자동으로 다시 시도합니다. 계속되면 이 메시지를 캡처해 알려 주세요.")
            set_meta(conn, key, 1)
        return
    commit(conn, day, msgs, marks, mode)
    set_meta(conn, "last_report", f"{now:%m/%d %H:%M} · {_label(y0)} 영상 {len(marks)}개 · "
                                  f"{'비교 분석' if mode == 'ai' else '기본 정렬'}")
    log.info("보고서 전송 완료 %s (%d개, %s)", day, len(marks), mode)


if __name__ == "__main__":
    main()
