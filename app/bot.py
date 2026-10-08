"""you-sum v2 텔레그램 봇 (채널 관리, 수집·분석 현황, 영상 분석 시험, 아침 보고서)"""
import json
import logging
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app import analyzer, gemini, report, youtube
from app.config import APP_DIR, DATA_DIR, load_secrets, load_settings, save_secret
from app.db import connect, get_meta, now_utc
from app.telegram import Telegram

log = logging.getLogger("yousum")
KST = ZoneInfo("Asia/Seoul")
STARTED_AT = time.time()
HEARTBEAT = DATA_DIR / "heartbeat"
LAST_DEPLOY = DATA_DIR / "deploy" / "last_result"
ANNOUNCED = DATA_DIR / "announced_version"

COMMANDS = [("status", "상태 확인"), ("report", "아침 보고서 보기"), ("channels", "등록 채널 목록"),
            ("recent", "최근 확인한 영상"), ("test", "영상 하나 바로 분석"), ("settings", "현재 설정 보기"),
            ("help", "도움말")]
ALIASES = {
    "/status": "status", "/상태": "status", "상태": "status",
    "/report": "report", "/보고서": "report", "보고서": "report",
    "/channels": "channels", "/채널": "channels", "채널": "channels",
    "/remove": "remove", "/삭제": "remove", "삭제": "remove",
    "/recent": "recent", "/최근": "recent", "최근": "recent",
    "/test": "test", "/시험": "test", "시험": "test",
    "/settings": "settings", "/설정": "settings", "설정": "settings",
    "/help": "help", "/start": "help", "/도움말": "help", "도움말": "help",
}
HELP = (
    "📌 채널 추가: 유튜브 영상·쇼츠·채널 링크나 @핸들을 그대로 보내세요.\n\n"
    "/status (또는 '상태') : 작동 상태와 수집·분석 현황\n"
    "/report (또는 '보고서') : 최근 아침 보고서 다시 보기\n"
    "   /report 목록 · /report 10/07 (그날 영상 보고서) · /report 미리보기\n"
    "/channels (또는 '채널') : 등록 채널 목록\n"
    "/remove 번호 (또는 '삭제 번호') : 채널 삭제\n"
    "/recent (또는 '최근') : 최근 확인한 영상과 롱폼 판정\n"
    "/test 링크 (또는 '시험 링크') : 영상 하나를 바로 분석\n"
    "/settings (또는 '설정') : 현재 설정\n\n"
    "☀️ 아침 보고서는 매일 정해진 시각에 자동으로 옵니다. 영상마다 👍/👎을 누르면 순위에 반영됩니다."
)
ICONS = {"long": "🎬", "short": "✂️", "pending": "⏳", "skipped": "⛔"}


def touch_heartbeat():
    HEARTBEAT.write_text(str(int(time.time())))


def first_line(cmd):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except Exception:
        return "확인 불가"


def version():
    return first_line(["git", "-C", str(APP_DIR), "log", "-1",
                       "--format=%h (%cd)", "--date=format-local:%m/%d %H:%M"])


def memory_text():
    info = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        info[key] = int(value.split()[0])
    total = info["MemTotal"] // 1024
    used = (info["MemTotal"] - info["MemAvailable"]) // 1024
    pct = used * 100 // total
    return f"{used}MB / {total}MB ({pct}%)" + (" ⚠️ 높음" if pct >= 85 else "")


def duration(sec):
    sec = int(sec)
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    return (f"{d}일 " if d else "") + f"{h}시간 {sec // 60}분"


def kst(iso_text):
    if not iso_text:
        return "날짜 모름"
    return datetime.fromisoformat(iso_text).astimezone(KST).strftime("%m/%d %H:%M")


def fmt_subs(n):
    if n is None:
        return "비공개"
    if n >= 10000:
        return f"{n / 10000:.1f}".rstrip("0").rstrip(".") + "만 명"
    return f"{n:,}명"


def fmt_len(sec):
    return f"{sec // 60}분" if sec >= 60 else f"{sec}초"


def threshold():
    return load_settings()["videos"]["longform_min_seconds"]


def active_channels(conn):
    return conn.execute("SELECT channel_id, title, subscribers FROM channels WHERE active=1 "
                        "ORDER BY added_at, rowid").fetchall()


def status_text(conn):
    free_gb = shutil.disk_usage("/").free / 1024 ** 3
    last = LAST_DEPLOY.read_text(encoding="utf-8").strip() if LAST_DEPLOY.exists() else "기록 없음"
    today0 = (datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0)
              .astimezone(timezone.utc).isoformat(timespec="seconds"))
    counts = {r[0]: r[1] for r in conn.execute(
        "SELECT kind, COUNT(*) FROM videos WHERE published_at>=? GROUP BY kind", (today0,))}
    streak = int(get_meta(conn, "collect_fail_streak", "0"))
    has_key = "있음" if load_secrets().get("GEMINI_API_KEY") else "❌ 없음"
    no_report = f"아직 없음 (매일 {load_settings()['report']['time']} 도착)"
    return "\n".join([
        "✅ 정상 작동 중" if streak == 0 else f"⚠️ 새 영상 확인에 문제가 있습니다 ({streak}회 연속)",
        f"등록 채널: {len(active_channels(conn))}개",
        f"마지막 수집: {get_meta(conn, 'last_collect', '아직 없음 (매시 5분에 실행)')}",
        f"오늘 올라온 영상: 롱폼 {counts.get('long', 0)} · 기준 미달 {counts.get('short', 0)}"
        f" · 확인 대기 {counts.get('pending', 0)}",
        f"마지막 분석: {get_meta(conn, 'last_analyze', '아직 없음 (매시 15분·45분에 실행)')}",
        f"마지막 보고서: {get_meta(conn, 'last_report', no_report)}",
        f"AI 오늘 사용/허용: {gemini.budget_text(conn)} (Gemini 키 {has_key})",
        "",
        f"버전: {version()}",
        f"켜진 지: {duration(time.time() - STARTED_AT)}",
        f"메모리: {memory_text()}",
        f"디스크 남은 공간: {free_gb:.1f}GB",
        f"마지막 업데이트: {last}",
        f"yt-dlp {first_line([sys.executable, '-m', 'yt_dlp', '--version'])} / {first_line(['deno', '--version'])}",
        f"한국 시간: {datetime.now(KST).strftime('%Y-%m-%d %H:%M')}",
    ])


def settings_text():
    s = load_settings()
    return "\n".join([
        "⚙️ 현재 설정",
        f"보고서 시각: 매일 {s['report']['time']} (한국 시간)",
        f"'오늘 꼭 볼 것' 최대: {s['report'].get('must_max', 3)}개",
        f"롱폼 기준: {s['videos']['longform_min_seconds'] // 60}분 이상",
        f"무료 한도 사용 상한: {int(s['limits']['max_usage_ratio'] * 100)}%",
        f"AI 모델 순서: {' → '.join(s['ai']['models'])}",
        "",
        "휴대폰에서 바꾸는 기능은 나중 업데이트에서 붙습니다.",
    ])


def channels_text(conn):
    rows = active_channels(conn)
    if not rows:
        return "등록된 채널이 없습니다. 유튜브 링크를 보내 추가해 보세요."
    lines = [f"📋 등록 채널 {len(rows)}개", ""]
    lines += [f"{i}. {r['title']} · {fmt_subs(r['subscribers'])}" for i, r in enumerate(rows, 1)]
    lines += ["", "삭제하려면: /remove 번호"]
    return "\n".join(lines)


def recent_text(conn):
    rows = conn.execute(
        "SELECT v.title, v.kind, v.duration_sec, v.published_at, v.note, c.title AS ch "
        "FROM videos v LEFT JOIN channels c ON c.channel_id = v.channel_id "
        "WHERE v.kind != 'baseline' ORDER BY COALESCE(v.published_at, v.found_at) DESC LIMIT 15").fetchall()
    if not rows:
        return "아직 확인한 새 영상이 없습니다. 채널을 등록하면 매시 5분에 확인합니다."
    lines = ["🕘 최근 확인한 영상 (최대 15개)",
             "🎬 롱폼 · ✂️ 기준 미달(쇼츠 포함) · ⏳ 길이 확인 대기 · ⛔ 제외", ""]
    for r in rows:
        length = f" ({fmt_len(r['duration_sec'])})" if r["duration_sec"] else ""
        line = f"{ICONS.get(r['kind'], '•')} {kst(r['published_at'])} {r['ch'] or ''} · {(r['title'] or '')[:40]}{length}"
        if r["kind"] in ("pending", "skipped") and r["note"]:
            line += f"\n    └ {r['note']}"
        lines.append(line)
    return "\n".join(lines)


def card_text(info, limit):
    lines = ["📺 이 채널을 등록할까요?", "", f"이름: {info['title']}"]
    if info.get("handle"):
        lines.append(f"핸들: {info['handle']}")
    lines.append(f"구독자: {fmt_subs(info.get('subscribers'))}")
    if info["recent"]:
        s = f"최근 영상 {info['recent']}개 중 롱폼({limit // 60}분 이상) {info['long']}개"
        if info.get("avg_min"):
            s += f", 롱폼 평균 {info['avg_min']}분"
        lines.append(s)
    if info.get("uploads_30d") is not None:
        n = info["uploads_30d"]
        more = " 이상" if n >= 15 else ""
        lines.append(f"최근 30일 업로드: {n}개{more} (쇼츠 포함), 마지막 {kst(info['last_upload'])}")
    if info["recent"] and info["long"] == 0:
        lines.append("\n⚠️ 최근 롱폼이 없어 보고서에 거의 나오지 않을 수 있습니다.")
    lines.append(f"\nhttps://www.youtube.com/channel/{info['channel_id']}")
    return "\n".join(lines)


def handle_link(tg, conn, chat_id, link):
    tg.send(chat_id, "🔍 채널 정보를 확인하는 중입니다… (10~30초)")
    limit = threshold()
    try:
        info = youtube.resolve_channel(link, limit)
    except youtube.Blocked:
        tg.send(chat_id, "⚠️ 유튜브가 잠시 요청을 막았습니다. 10~20분 뒤에 다시 보내 주세요.")
        return
    except Exception as e:
        log.warning("채널 확인 실패 %s: %s", link, e)
        tg.send(chat_id, "❌ 이 링크에서 채널을 찾지 못했습니다.\n링크가 맞는지, 비공개·삭제된 영상은 아닌지 확인해 주세요.")
        return
    cid = info["channel_id"]
    row = conn.execute("SELECT active FROM channels WHERE channel_id=?", (cid,)).fetchone()
    if row and row["active"]:
        tg.send(chat_id, f"ℹ️ '{info['title']}'은(는) 이미 등록된 채널입니다.")
        return
    conn.execute("INSERT OR REPLACE INTO pending_channels VALUES(?,?,?)",
                 (cid, json.dumps(info, ensure_ascii=False), now_utc()))
    conn.commit()
    tg.send(chat_id, card_text(info, limit),
            buttons=[[{"text": "✅ 등록", "callback_data": f"add:{cid}"},
                      {"text": "취소", "callback_data": "cancel"}]])


def handle_test(tg, conn, chat_id, arg):
    vid = analyzer.video_id_from(arg)
    if not vid:
        tg.send(chat_id, "분석할 영상 링크를 함께 보내 주세요.\n예: /test https://youtu.be/영상ID")
        return
    tg.send(chat_id, "🧪 자막을 받아 분석하는 중입니다… (1~5분)")
    try:
        tg.send(chat_id, analyzer.format_result(analyzer.analyze_video(conn, vid)))
    except youtube.Blocked:
        tg.send(chat_id, "⚠️ 유튜브가 잠시 자막 요청을 막았습니다. 20~30분 뒤에 다시 해 주세요.")
    except analyzer.NoSubtitles as e:
        tg.send(chat_id, f"ℹ️ {e}")
    except gemini.BudgetReached:
        tg.send(chat_id, "⏸ 오늘 쓰기로 한 AI 무료 한도를 다 썼습니다. 오후 4~5시 이후 다시 해 주세요.")
    except gemini.Busy as e:
        tg.send(chat_id, "⏳ 구글 Gemini 서버가 지금 붐빕니다 (프로그램 고장 아님).\n"
                         f"{e}\n5~10분 뒤 다시 /test 해 주세요. 자동 분석은 알아서 다시 시도합니다.")
    except gemini.AIError as e:
        tg.send(chat_id, f"❌ AI 분석 실패: {e}\n이 메시지를 캡처해 알려 주세요.")
    except Exception as e:
        log.exception("시험 분석 실패 %s", vid)
        tg.send(chat_id, f"❌ 분석 중 문제가 생겼습니다: {str(e)[:200]}\n이 메시지를 캡처해 알려 주세요.")


def handle_report(tg, conn, chat_id, arg):
    a = arg.strip().lower()
    if a in ("미리보기", "지금", "now", "preview"):
        tg.send(chat_id, "🧾 아직 보고서에 안 나온 영상으로 미리보기를 만듭니다… (1~5분)\n"
                         "저장하지 않으니 다음 아침 보고서에는 그대로 나옵니다.")
        try:
            report.preview(conn, tg, chat_id)
        except Exception as e:
            log.exception("미리보기 실패")
            tg.send(chat_id, f"❌ 미리보기를 만들지 못했습니다: {str(e)[:200]}\n이 메시지를 캡처해 알려 주세요.")
        return
    if a in ("목록", "list"):
        rows = report.recent_days(conn)
        if not rows:
            tg.send(chat_id, "아직 보낸 보고서가 없습니다.")
            return
        lines = ["🗂 지난 보고서 (영상 날짜 기준)", ""]
        lines += [f"• {r['day'][5:].replace('-', '/')} 영상 {r['n']}개 · "
                  f"{'비교 분석' if r['mode'] == 'ai' else '기본 정렬'}" for r in rows]
        lines += ["", "다시 보기: /report 10/07"]
        tg.send(chat_id, "\n".join(lines))
        return
    day = report.parse_day(a) if a else report.latest_day(conn)
    if not day:
        if a:
            tg.send(chat_id, "날짜를 이해하지 못했습니다. 예: /report 10/07")
        else:
            tg.send(chat_id, f"아직 보낸 보고서가 없습니다. 매일 {load_settings()['report']['time']}에 도착합니다.\n"
                             "미리 보려면: /report 미리보기")
        return
    msgs = report.saved(conn, day)
    if not msgs:
        tg.send(chat_id, f"{day} 영상 보고서가 없습니다. /report 목록 으로 확인해 보세요.")
        return
    report.deliver(tg, chat_id, msgs)


def handle_remove(tg, conn, chat_id, arg):
    rows = active_channels(conn)
    if not arg.isdigit() or not 1 <= int(arg) <= len(rows):
        tg.send(chat_id, "삭제할 채널 번호를 함께 보내 주세요. 예: /remove 3\n번호는 /channels 에서 볼 수 있습니다.")
        return
    ch = rows[int(arg) - 1]
    tg.send(chat_id, f"🗑 '{ch['title']}' 채널을 삭제할까요?\n지금까지 모은 기록은 남고, 새 영상만 더 이상 확인하지 않습니다.",
            buttons=[[{"text": "삭제", "callback_data": f"del:{ch['channel_id']}"},
                      {"text": "취소", "callback_data": "cancel"}]])


def handle_callback(tg, conn, state, cq):
    msg = cq.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    data = cq.get("data") or ""
    allowed = state["allowed"] is not None and chat_id == state["allowed"]
    toast = report.record_feedback(conn, data) if allowed and data.startswith("fb:") else None
    try:
        tg.call("answerCallbackQuery", callback_query_id=cq["id"], text=toast)
    except Exception:
        pass
    if not allowed or data.startswith("fb:"):
        return
    mid = msg.get("message_id")
    if data == "cancel":
        tg.edit(chat_id, mid, (msg.get("text") or "") + "\n\n→ 취소했습니다.")
        return
    action, _, cid = data.partition(":")
    if action == "add":
        row = conn.execute("SELECT info FROM pending_channels WHERE channel_id=?", (cid,)).fetchone()
        if not row:
            tg.edit(chat_id, mid, "이 확인 카드는 이미 처리됐습니다. 필요하면 링크를 다시 보내 주세요.")
            return
        info = json.loads(row["info"])
        conn.execute(
            "INSERT INTO channels(channel_id, title, handle, subscribers, active, seeded, added_at, source) "
            "VALUES(?,?,?,?,1,0,?,'link') ON CONFLICT(channel_id) DO UPDATE SET title=excluded.title, "
            "handle=excluded.handle, subscribers=excluded.subscribers, active=1, seeded=0, removed_at=NULL",
            (cid, info["title"], info.get("handle"), info.get("subscribers"), now_utc()))
        conn.execute("DELETE FROM pending_channels WHERE channel_id=?", (cid,))
        conn.commit()
        tg.edit(chat_id, mid, f"✅ '{info['title']}' 등록 완료 (지금 {len(active_channels(conn))}개 채널)\n"
                              "다음 정각 5분부터 새 영상을 확인합니다. 최근 2일 안의 영상부터 다룹니다.")
    elif action == "del":
        title_row = conn.execute("SELECT title FROM channels WHERE channel_id=?", (cid,)).fetchone()
        cur = conn.execute("UPDATE channels SET active=0, removed_at=? WHERE channel_id=? AND active=1",
                           (now_utc(), cid))
        conn.commit()
        title = title_row["title"] if title_row else cid
        tg.edit(chat_id, mid, f"🗑 '{title}' 삭제했습니다." if cur.rowcount else "이미 삭제된 채널입니다.")


def handle_message(tg, conn, state, msg):
    chat_id = msg.get("chat", {}).get("id")
    text = (msg.get("text") or "").strip()
    if not chat_id or not text:
        return

    if state["allowed"] is None:  # 등록 전: 6자리 코드를 보낸 사람만 등록
        if state["pairing_code"] and text == state["pairing_code"]:
            save_secret("TELEGRAM_ALLOWED_CHAT_ID", str(chat_id))
            save_secret("PAIRING_CODE", None)
            state["allowed"], state["pairing_code"] = chat_id, None
            tg.send(chat_id, "🔐 등록 완료. 이제 이 대화방의 명령만 받습니다.\n\n" + HELP)
        else:
            log.info("등록 전 메시지 무시 (chat_id=%s)", chat_id)
        return

    if chat_id != state["allowed"]:
        log.warning("허용되지 않은 대화방 무시 (chat_id=%s)", chat_id)
        return

    parts = text.split(maxsplit=1)
    key = ALIASES.get(parts[0].split("@")[0].lower())
    arg = parts[1].strip() if len(parts) > 1 else ""
    if key == "status":
        tg.send(chat_id, status_text(conn))
    elif key == "report":
        handle_report(tg, conn, chat_id, arg)
    elif key == "channels":
        tg.send(chat_id, channels_text(conn))
    elif key == "remove":
        handle_remove(tg, conn, chat_id, arg)
    elif key == "recent":
        tg.send(chat_id, recent_text(conn))
    elif key == "test":
        handle_test(tg, conn, chat_id, arg)
    elif key == "settings":
        tg.send(chat_id, settings_text())
    elif key == "help":
        tg.send(chat_id, HELP)
    else:
        link = youtube.find_link(text)
        tg.send(chat_id, HELP) if not link else handle_link(tg, conn, chat_id, link)


def handle(tg, conn, state, update):
    if "callback_query" in update:
        handle_callback(tg, conn, state, update["callback_query"])
    elif "message" in update:
        handle_message(tg, conn, state, update["message"])


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect()
    secrets = load_secrets()
    token = secrets.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        log.error("봇 토큰이 없습니다. install.sh를 다시 실행하세요.")
        sys.exit(1)
    allowed = secrets.get("TELEGRAM_ALLOWED_CHAT_ID", "")
    state = {"allowed": int(allowed) if allowed else None,
             "pairing_code": secrets.get("PAIRING_CODE") or None}
    tg = Telegram(token)

    failures = 0
    while True:
        try:
            me = tg.call("getMe")
            break
        except Exception as e:
            failures += 1
            log.warning("텔레그램 연결 실패 %s회: %s", failures, e)
            time.sleep(min(300, 10 * failures))
    touch_heartbeat()
    log.info("봇 시작: @%s", me.get("username"))

    try:
        tg.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
    except Exception as e:
        log.warning("명령 메뉴 등록 실패: %s", e)

    current = version()
    if state["allowed"] and (not ANNOUNCED.exists() or ANNOUNCED.read_text() != current):
        try:
            tg.send(state["allowed"], f"🔄 새 버전 반영: {current}")
            ANNOUNCED.write_text(current)
        except Exception as e:
            log.warning("시작 알림 실패: %s", e)

    offset, failures = None, 0
    while True:
        try:
            updates = tg.call("getUpdates", http_timeout=70, offset=offset, timeout=50,
                              allowed_updates=["message", "callback_query"])
            failures = 0
            touch_heartbeat()
        except Exception as e:
            failures += 1
            if "409" in str(e):
                log.error("같은 봇 토큰을 다른 프로그램이 쓰고 있습니다.")
            log.warning("메시지 확인 실패 %s회: %s", failures, e)
            time.sleep(min(300, 5 * 2 ** min(failures, 6)))
            continue
        for update in updates:
            offset = update["update_id"] + 1
            try:
                handle(tg, conn, state, update)
            except Exception:
                log.exception("메시지 처리 실패")
                src = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
                chat = src.get("chat", {}).get("id")
                if chat and chat == state["allowed"]:
                    try:
                        tg.send(chat, "⚠️ 처리 중 문제가 생겼습니다. 잠시 뒤 다시 보내 주세요. "
                                      "계속되면 /status 결과를 캡처해 알려 주세요.")
                    except Exception:
                        pass


if __name__ == "__main__":
    main()
