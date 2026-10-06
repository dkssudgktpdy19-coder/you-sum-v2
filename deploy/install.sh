#!/bin/bash
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "root 계정에서 실행해 주세요."; exit 1; }

APP=/home/yousum/app
CONF=/home/yousum/.config/yousum
SECRETS=$CONF/secrets.env
DATA=/home/yousum/data

echo "[1/5] Deno 설치 (유튜브 자막 도구 yt-dlp가 필요로 합니다)"
if ! command -v deno >/dev/null; then
  curl -fsSL -o /tmp/deno.zip https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip
  unzip -o -q /tmp/deno.zip -d /usr/local/bin && chmod +x /usr/local/bin/deno && rm -f /tmp/deno.zip
fi
deno --version | head -1

echo "[2/5] 파이썬 환경 만들기 (2~3분)"
runuser -u yousum -- python3 -m venv "$APP/.venv"
runuser -u yousum -- "$APP/.venv/bin/pip" install --quiet --upgrade pip
runuser -u yousum -- "$APP/.venv/bin/pip" install --quiet -r "$APP/requirements.txt"

echo "[3/5] 텔레그램 봇 토큰 저장"
runuser -u yousum -- mkdir -p "$CONF" "$DATA/deploy"
chmod 700 "$CONF"
if ! grep -q '^TELEGRAM_BOT_TOKEN=.\+' "$SECRETS" 2>/dev/null; then
  while true; do
    read -r -s -p "BotFather가 준 토큰을 붙여넣고 Enter (화면에 안 보이는 게 정상): " TOKEN; echo
    if curl -s -m 15 "https://api.telegram.org/bot${TOKEN}/getMe" | grep -q '"ok":true'; then break; fi
    echo "토큰이 맞지 않습니다. 토큰 전체를 다시 붙여넣어 주세요."
  done
  printf 'TELEGRAM_BOT_TOKEN=%s\nTELEGRAM_ALLOWED_CHAT_ID=\n' "$TOKEN" > "$SECRETS"
fi
CHAT=$(grep -E '^TELEGRAM_ALLOWED_CHAT_ID=' "$SECRETS" | cut -d= -f2- || true)
CODE=""
if [ -z "$CHAT" ]; then
  CODE=$(shuf -i 100000-999999 -n 1)
  sed -i '/^PAIRING_CODE=/d' "$SECRETS"
  echo "PAIRING_CODE=$CODE" >> "$SECRETS"
fi
chown yousum:yousum "$SECRETS"
chmod 600 "$SECRETS"

echo "[4/5] 서비스 등록 (자동 시작 + 5분마다 업데이트 확인)"
cp "$APP"/deploy/yousum-*.service "$APP"/deploy/yousum-*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --quiet yousum-bot.service yousum-update.timer
systemctl restart yousum-bot.service
systemctl start yousum-update.timer
echo "$(date '+%m/%d %H:%M') 처음 설치" > "$DATA/deploy/last_result"

echo "[5/5] 확인"
sleep 10
if systemctl is-active --quiet yousum-bot.service; then echo "✅ 봇 작동 중"; else echo "❌ 봇이 시작되지 않았습니다. 이 화면을 캡처해 주세요."; journalctl -u yousum-bot -n 20 --no-pager; fi
if [ -n "$CODE" ]; then
  echo
  echo "=============================================="
  echo "  새 봇에게 이 숫자 6자리만 보내세요:  $CODE"
  echo "=============================================="
fi
