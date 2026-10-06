"""SQLite 데이터베이스 파일 하나에 모든 기록을 저장합니다."""
import sqlite3
from datetime import datetime, timezone

from app.config import DATA_DIR

DB_FILE = DATA_DIR / "yousum.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels(
    channel_id  TEXT PRIMARY KEY,
    title       TEXT,
    handle      TEXT,
    subscribers INTEGER,
    active      INTEGER NOT NULL DEFAULT 1,
    seeded      INTEGER NOT NULL DEFAULT 0,
    added_at    TEXT NOT NULL,
    removed_at  TEXT,
    source      TEXT
);
CREATE TABLE IF NOT EXISTS videos(
    video_id      TEXT PRIMARY KEY,
    channel_id    TEXT NOT NULL,
    title         TEXT,
    published_at  TEXT,
    duration_sec  INTEGER,
    kind          TEXT NOT NULL,
    checks        INTEGER NOT NULL DEFAULT 0,
    next_check_at TEXT,
    note          TEXT,
    found_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS videos_published ON videos(published_at);
CREATE INDEX IF NOT EXISTS videos_kind ON videos(kind, next_check_at);
CREATE TABLE IF NOT EXISTS pending_channels(
    channel_id TEXT PRIMARY KEY,
    info       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta(
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS transcripts(
    video_id   TEXT PRIMARY KEY,
    lang       TEXT,
    kind       TEXT,
    lines      TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analyses(
    video_id    TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    source      TEXT,
    model       TEXT,
    result      TEXT,
    error       TEXT,
    next_try_at TEXT,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_usage(
    day           TEXT NOT NULL,
    model         TEXT NOT NULL,
    requests      INTEGER NOT NULL DEFAULT 0,
    video_seconds INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(day, model)
);
"""
# videos.kind 값: pending(길이 확인 대기) / long(롱폼) / short(기준 미달, 쇼츠 포함)
#                 skipped(3번 확인 실패로 제외) / baseline(등록 전에 올라온 지난 영상)
# analyses.status 값: done(완료) / retry(나중에 다시) / failed(3번 실패) / no_transcript(자막 없음)


def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    return conn


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn, key, value):
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    conn.commit()
