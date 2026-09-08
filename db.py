"""SQLite 存储:设置、工作流模板元数据、生成任务与图片记录。"""
import sqlite3
import threading
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
WF_DIR = DATA_DIR / "workflows"
DB_PATH = DATA_DIR / "comfyweb.db"

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS workflows(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  filename TEXT NOT NULL UNIQUE,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  prompt_id TEXT NOT NULL DEFAULT '',
  workflow_id INTEGER,
  workflow_name TEXT NOT NULL DEFAULT '',
  prompt_text TEXT NOT NULL DEFAULT '',
  seed INTEGER,
  params_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'queued',
  progress REAL NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  count INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS images(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  filename TEXT NOT NULL,
  subfolder TEXT NOT NULL DEFAULT '',
  type TEXT NOT NULL DEFAULT 'output',
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_images_task ON images(task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);
"""


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    WF_DIR.mkdir(parents=True, exist_ok=True)
    db = get_db()
    db.executescript(SCHEMA)
    # 同一任务的同名图片只留一条(WS 落库与轮询对账可能并发写入),再建唯一索引兜底
    db.execute(
        "DELETE FROM images WHERE id NOT IN "
        "(SELECT MIN(id) FROM images GROUP BY task_id, filename, subfolder)")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_images_unique "
               "ON images(task_id, filename, subfolder)")
    db.commit()


def get_db():
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def query(sql, args=()):
    return get_db().execute(sql, args).fetchall()


def query_one(sql, args=()):
    return get_db().execute(sql, args).fetchone()


def execute(sql, args=()):
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    return cur


def get_setting(key, default=""):
    row = query_one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key, value):
    execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
