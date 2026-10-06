"""you-sum v2 텔레그램 봇 (1차 묶음: 기기 등록, 상태 확인, 설정 보기)"""
import logging
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from app.config import APP_DIR, DATA_DIR, load_secrets, load_settings, save_secret

log = logging.getLogger("yousum")
KST = ZoneInfo("Asia/Seoul")
STARTED_AT = time.time()
HEARTBEAT = DATA_DIR / "heartbeat"
LAST_DEPLOY = DATA_DIR / "deploy" / "last_result"
ANNOUNCED = DATA_DIR / "announced_version"

COMMANDS = [("status", "상태 확인"), ("settings", "현재 설정 보기"), ("help", "도움말")]
ALIASES = {
    "/status": "status", "/상태": "status", "상태": "status",
    "/settings": "settings", "/설정": "settings", "설정": "settings",
    "/help": "help", "/start": "help", "/도움말": "help", "도움말": "help",
}
HELP = (
    "사용할 수 있는 명령\n"
    "/status (또는 '상태') : 작동 상태, 메모리, 마지막 업데이트\n"
    "/settings (또는 '설정') : 현재 설정\n\n"
    "영상 링크로 채널 추가하기와 아침 보고서는 다음 업데이트에서 붙습니다."
)


class Telegram:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}"

    def call(self, method, http_timeout=30, **params):
        params = {k: v for k, v in params.items() if v is not None}
        resp = requests.post(f"{self.base}/{method}", json=params, timeout=http_timeout)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"{method} 실패: {data.get('error_code')} {data.get('description')}")
        return data["result"]

    def send(self, chat_id, text):
        for start in range(0, len(text), 4000):  # 텔레그램 한 통 최대 4,096자
            self.call("sendMessage", chat_id=chat_id, text=text[start:start + 4000],
                      link_preview_options={"is_disabled": True})


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


def status_text():
    free_gb = shutil.disk_usage("/").free / 1024 ** 3
    last = LAST_DEPLOY.read_text(encoding="utf-8").strip() if LAST_DEPLOY.exists() else "기록 없음"
    return "\n".join([
        "✅ 정상 작동 중",
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
        f"롱폼 기준: {s['videos']['longform_min_seconds'] // 60}분 이상",
        f"무료 한도 사용 상한: {int(s['limits']['max_usage_ratio'] * 100)}%",
        "",
        "휴대폰에서 바꾸는 기능은 다음 업데이트에서 붙습니다.",
    ])


def handle(tg, state, update):
    msg = update.get("message") or {}
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

    key = ALIASES.get(text.split()[0].split("@")[0].lower())
    if key == "status":
        tg.send(chat_id, status_text())
    elif key == "settings":
        tg.send(chat_id, settings_text())
    else:
        tg.send(chat_id, HELP)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
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
                              allowed_updates=["message"])
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
                handle(tg, state, update)
            except Exception:
                log.exception("메시지 처리 실패")
                chat = (update.get("message") or {}).get("chat", {}).get("id")
                if chat and chat == state["allowed"]:
                    try:
                        tg.send(chat, "⚠️ 처리 중 문제가 생겼습니다. 잠시 뒤 다시 보내 주세요. "
                                      "계속되면 /status 결과를 캡처해 알려 주세요.")
                    except Exception:
                        pass


if __name__ == "__main__":
    main()
