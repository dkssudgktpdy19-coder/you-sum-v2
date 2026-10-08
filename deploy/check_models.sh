#!/bin/bash
[ "$(id -u)" -eq 0 ] || { echo "root 계정에서 실행해 주세요."; exit 1; }
KEY=$(grep -E '^GEMINI_API_KEY=' /home/yousum/.config/yousum/secrets.env | cut -d= -f2-)
[ -n "$KEY" ] || { echo "Gemini 키가 없습니다. set_gemini_key.sh를 먼저 실행하세요."; exit 1; }
echo "모델별 시험 중… (모델당 최대 1분)"
for M in gemini-3.8-flash gemini-3.7-flash gemini-3.6-flash gemini-3.5-flash gemini-3.5-flash-lite gemini-3.1-flash-lite; do
  CODE=$(curl -s -m 60 -o /tmp/gm.json -w '%{http_code}' -H "x-goog-api-key: $KEY" \
    -H 'Content-Type: application/json' -d '{"contents":[{"parts":[{"text":"1+1은? 숫자만"}]}]}' \
    "https://generativelanguage.googleapis.com/v1beta/models/$M:generateContent")
  MSG=$(grep -o '"message": *"[^"]*' /tmp/gm.json 2>/dev/null | head -1 | sed 's/.*"message": *"//' | cut -c1-70)
  case $CODE in
    200) echo "✅ $M : 사용 가능" ;;
    429) if grep -q 'limit: 0' /tmp/gm.json; then echo "⛔ $M : 무료 등급에서 못 씀"; else echo "⏸ $M : 지금 한도 초과 (모델은 쓸 수 있음)"; fi ;;
    404) echo "⛔ $M : 이 계정에서 못 씀 ($MSG)" ;;
    *)   echo "❓ $M : $CODE $MSG" ;;
  esac
  sleep 5
done
rm -f /tmp/gm.json
