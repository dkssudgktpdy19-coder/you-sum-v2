"""텔레그램으로 메시지를 보내고 받습니다."""
import requests

from app.config import load_secrets


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

    def send(self, chat_id, text, buttons=None):
        chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [" "]  # 한 통 최대 4,096자
        result = None
        for i, chunk in enumerate(chunks):
            markup = {"inline_keyboard": buttons} if buttons and i == len(chunks) - 1 else None
            result = self.call("sendMessage", chat_id=chat_id, text=chunk, reply_markup=markup,
                               link_preview_options={"is_disabled": True})
        return result

    def edit(self, chat_id, message_id, text):
        """버튼 달린 메시지의 글을 바꾸고 버튼을 없앱니다."""
        self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text[:4000],
                  link_preview_options={"is_disabled": True})


def notify_owner(text):
    """봇이 아닌 다른 프로그램(수집기 등)에서 나에게 알림을 보낼 때 씁니다."""
    s = load_secrets()
    token, chat = s.get("TELEGRAM_BOT_TOKEN"), s.get("TELEGRAM_ALLOWED_CHAT_ID")
    if not token or not chat:
        return False
    try:
        Telegram(token).send(int(chat), text)
        return True
    except Exception:
        return False
