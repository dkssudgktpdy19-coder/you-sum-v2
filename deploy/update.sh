#!/bin/bash
# GitHub에 새 코드가 있으면 반영하고, 새 버전이 시작되지 않으면 이전 버전으로 되돌립니다.
set -uo pipefail
APP=/home/yousum/app
SECRETS=/home/yousum/.config/yousum/secrets.env
DATA=/home/yousum/data
STATE=$DATA/deploy
mkdir -p "$STATE"
exec 9>/run/yousum-update.lock
flock -n 9 || exit 0

as_user() { runuser -u yousum -- "$@"; }
now() { date '+%m/%d %H:%M'; }
secret() { grep -E "^$1=" "$SECRETS" 2>/dev/null | head -1 | cut -d= -f2-; }
notify() {
  local token chat
  token=$(secret TELEGRAM_BOT_TOKEN); chat=$(secret TELEGRAM_ALLOWED_CHAT_ID)
  [ -n "$token" ] && [ -n "$chat" ] || return 0
  curl -s -m 20 "https://api.telegram.org/bot${token}/sendMessage" \
    --data-urlencode "chat_id=${chat}" --data-urlencode "text=$1" > /dev/null || true
}

if ! as_user git -C "$APP" fetch --quiet origin main 2> "$STATE/fetch_error"; then
  n=$(( $(cat "$STATE/fetch_fail" 2>/dev/null || echo 0) + 1 ))
  echo "$n" > "$STATE/fetch_fail"
  [ "$n" -eq 6 ] && notify "⚠️ 30분째 GitHub에서 새 코드를 확인하지 못하고 있습니다. 집 인터넷이나 GitHub 쪽 문제일 수 있습니다. 지금 버전은 계속 작동합니다."
  exit 0
fi
echo 0 > "$STATE/fetch_fail"

OLD=$(as_user git -C "$APP" rev-parse HEAD)
NEW=$(as_user git -C "$APP" rev-parse origin/main)
[ "$OLD" = "$NEW" ] && exit 0
[ "$(cat "$STATE/bad_commit" 2>/dev/null)" = "$NEW" ] && exit 0

deploy() {
  local rev=$1 start hb
  as_user git -C "$APP" reset --hard --quiet "$rev" || return 1
  as_user "$APP/.venv/bin/pip" install --quiet -r "$APP/requirements.txt" || return 1
  cp "$APP"/deploy/yousum-*.service "$APP"/deploy/yousum-*.timer /etc/systemd/system/ && systemctl daemon-reload
  for t in "$APP"/deploy/yousum-*.timer; do systemctl enable --now --quiet "$(basename "$t")"; done
  systemctl restart yousum-bot.service
  start=$(date +%s)
  for _ in $(seq 1 18); do
    sleep 5
    hb=$(stat -c %Y "$DATA/heartbeat" 2>/dev/null || echo 0)
    if [ "$hb" -ge "$start" ] && systemctl is-active --quiet yousum-bot.service; then return 0; fi
  done
  return 1
}

if deploy "$NEW"; then
  rm -f "$STATE/bad_commit"
  echo "$(now) ${NEW:0:7} 반영 성공" > "$STATE/last_result"
else
  echo "$NEW" > "$STATE/bad_commit"
  if deploy "$OLD"; then
    echo "$(now) ${NEW:0:7} 실패 → ${OLD:0:7}로 되돌림" > "$STATE/last_result"
    notify "⚠️ 새 버전(${NEW:0:7})이 제대로 시작되지 않아 이전 버전(${OLD:0:7})으로 되돌렸습니다. 지금도 정상 작동 중입니다. 고친 파일을 Push하면 5분 안에 다시 시도합니다."
  else
    echo "$(now) 되돌리기도 실패" > "$STATE/last_result"
    notify "🚨 새 버전과 이전 버전 모두 시작되지 않습니다. Proxmox에서 yousum 컨테이너를 base_ok 스냅샷으로 되돌린 뒤 알려 주세요."
  fi
fi
