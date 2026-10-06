"""매시 5분에 실행: 등록 채널의 새 영상을 찾고 롱폼인지 판정합니다."""
import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app import youtube
from app.config import load_settings
from app.db import connect, get_meta, now_utc, set_meta
from app.telegram import notify_owner

log = logging.getLogger("yousum.collect")
KST = ZoneInfo("Asia/Seoul")
RECHECK_AFTER = timedelta(hours=1)
MAX_CHECKS = 3
SEED_WINDOW = timedelta(days=2)
PER_RUN = 60


def discover(conn, channel_id, seeding):
    items = youtube.fetch_rss(channel_id)
    if items is None:
        items = youtube.recent_from_tab(channel_id)
    now = datetime.now(timezone.utc)
    new = 0
    for it in items:
        if conn.execute("SELECT 1 FROM videos WHERE video_id=?", (it["id"],)).fetchone():
            continue
        kind = "pending"
        if seeding and (not it["published"] or datetime.fromisoformat(it["published"]) < now - SEED_WINDOW):
            kind = "baseline"
        conn.execute("INSERT INTO videos(video_id, channel_id, title, published_at, kind, found_at, next_check_at) "
                     "VALUES(?,?,?,?,?,?,?)",
                     (it["id"], channel_id, it["title"], it["published"], kind, now_utc(), now_utc()))
        new += kind == "pending"
    conn.commit()
    return new


def retry_later(conn, video_id, checks, note):
    if checks >= MAX_CHECKS:
        conn.execute("UPDATE videos SET kind='skipped', checks=?, note=? WHERE video_id=?",
                     (checks, note, video_id))
    else:
        nxt = (datetime.now(timezone.utc) + RECHECK_AFTER).isoformat(timespec="seconds")
        conn.execute("UPDATE videos SET checks=?, note=?, next_check_at=? WHERE video_id=?",
                     (checks, note, nxt, video_id))
    conn.commit()


def classify(conn, threshold):
    due = conn.execute("SELECT video_id, checks FROM videos WHERE kind='pending' AND next_check_at<=? "
                       "ORDER BY found_at LIMIT ?", (now_utc(), PER_RUN)).fetchall()
    longs = shorts = 0
    for i, row in enumerate(due):
        if i:
            time.sleep(5)  # 유튜브에 몰아서 요청하지 않도록
        vid, checks = row["video_id"], row["checks"] + 1
        try:
            d = youtube.video_details(vid)
        except youtube.Blocked:
            raise
        except Exception as e:
            retry_later(conn, vid, checks, f"정보 조회 실패: {str(e)[:150]}")
            continue
        if d["live_status"] in ("is_live", "is_upcoming", "post_live") or d["duration"] <= 0:
            retry_later(conn, vid, checks, "길이가 아직 확정되지 않음 (생방송·처리 중)")
            continue
        kind = "long" if d["duration"] >= threshold else "short"
        conn.execute("UPDATE videos SET kind=?, duration_sec=?, title=COALESCE(?, title), "
                     "published_at=COALESCE(published_at, ?), checks=?, note=NULL WHERE video_id=?",
                     (kind, d["duration"], d["title"], d["published"], checks, vid))
        conn.commit()
        if kind == "long":
            longs += 1
        else:
            shorts += 1
    return longs, shorts


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    conn = connect()
    threshold = load_settings()["videos"]["longform_min_seconds"]
    channels = conn.execute("SELECT channel_id, title, seeded FROM channels WHERE active=1 "
                            "ORDER BY added_at").fetchall()
    found, failed, longs, shorts, problem = 0, [], 0, 0, None
    try:
        for ch in channels:
            try:
                found += discover(conn, ch["channel_id"], seeding=not ch["seeded"])
                if not ch["seeded"]:
                    conn.execute("UPDATE channels SET seeded=1 WHERE channel_id=?", (ch["channel_id"],))
                    conn.commit()
            except youtube.Blocked:
                raise
            except Exception as e:
                log.warning("채널 확인 실패 %s: %s", ch["title"], e)
                failed.append(ch["title"] or ch["channel_id"])
            time.sleep(1)
        longs, shorts = classify(conn, threshold)
    except youtube.Blocked as e:
        problem = "유튜브가 요청을 잠시 막음"
        log.warning("차단 감지: %s", e)
    except Exception:
        problem = "프로그램 오류"
        log.exception("수집 중 오류")
    if not problem and channels and len(failed) == len(channels):
        problem = "모든 채널 확인 실패 (인터넷 문제일 수 있음)"

    stamp = datetime.now(KST).strftime("%m/%d %H:%M")
    summary = f"{stamp} · 새 영상 {found}개 · 롱폼 {longs} / 기준 미달 {shorts}"
    if failed:
        summary += f" · 확인 실패 채널 {len(failed)}개"
    if problem:
        summary += f" · ⚠️ {problem}"
    set_meta(conn, "last_collect", summary)
    log.info(summary)

    streak = int(get_meta(conn, "collect_fail_streak", "0")) + 1 if problem else 0
    set_meta(conn, "collect_fail_streak", streak)
    if streak == 1 and "막음" in (problem or ""):
        notify_owner("⚠️ 유튜브가 새 영상 확인 요청을 잠시 막았습니다. 보통 일시적인 현상이라 "
                     "1시간 뒤 자동으로 다시 시도합니다. 따로 하실 일은 없습니다.")
    elif streak == 3:
        notify_owner(f"🚨 3시간째 새 영상을 확인하지 못하고 있습니다.\n원인: {problem}\n"
                     "/status 결과를 캡처해서 알려 주세요.")


if __name__ == "__main__":
    main()
