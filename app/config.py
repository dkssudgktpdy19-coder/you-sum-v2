"""설정과 비밀 정보를 읽고 씁니다. 비밀 정보는 GitHub가 아닌 컨테이너 안에만 있습니다."""
import os
import tomllib
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path("/home/yousum/data")
SECRETS_FILE = Path("/home/yousum/.config/yousum/secrets.env")


def load_settings():
    with open(APP_DIR / "config" / "settings.toml", "rb") as f:
        return tomllib.load(f)


def load_secrets():
    secrets = {}
    if SECRETS_FILE.exists():
        for line in SECRETS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            secrets[key.strip()] = value.strip()
    return secrets


def save_secret(key, value):
    """value가 None이면 해당 줄을 지웁니다."""
    lines = SECRETS_FILE.read_text(encoding="utf-8").splitlines() if SECRETS_FILE.exists() else []
    out, found = [], False
    for line in lines:
        if line.split("=", 1)[0].strip() == key:
            found = True
            if value is not None:
                out.append(f"{key}={value}")
        else:
            out.append(line)
    if not found and value is not None:
        out.append(f"{key}={value}")
    tmp = SECRETS_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(SECRETS_FILE)
