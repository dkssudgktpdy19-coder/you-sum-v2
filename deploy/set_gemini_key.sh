#!/bin/bash
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "root 계정에서 실행해 주세요."; exit 1; }
SECRETS=/home/yousum/.config/yousum/secrets.env

while true; do
  read -r -s -p "Gemini API 키를 붙여넣고 Enter (화면에 안 보이는 게 정상): " KEY; echo
  RESP=$(curl -s -m 20 -H "x-goog-api-key: ${KEY}" "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200")
  if echo "$RESP" | grep -q '"models"'; then break; fi
  echo "키가 맞지 않습니다. 키 전체를 다시 붙여넣어 주세요."
done
sed -i '/^GEMINI_API_KEY=/d' "$SECRETS"
echo "GEMINI_API_KEY=$KEY" >> "$SECRETS"
chown yousum:yousum "$SECRETS"
chmod 600 "$SECRETS"
echo "✅ 키 저장 완료"
echo "이 키로 쓸 수 있는 Flash 계열 모델:"
echo "$RESP" | grep -o '"name": *"models/[^"]*flash[^"]*"' | sed 's/.*models\///; s/"$//' | sort -u
