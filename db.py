"""SQLite 存储:设置、工作流模板元数据、生成任务与图片记录。"""
import json
import os
import sqlite3
import threading
from pathlib import Path

BASE = Path(__file__).resolve().parent
# 测试通过 COMFYWEB_DATA_DIR 指到临时目录,避免污染生产数据
DATA_DIR = Path(os.environ.get("COMFYWEB_DATA_DIR") or BASE / "data")
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
  filename TEXT NOT NULL DEFAULT '',
  template_json TEXT,
  source TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  fav INTEGER NOT NULL DEFAULT 0,
  scene TEXT NOT NULL DEFAULT '',
  base_model TEXT NOT NULL DEFAULT '',
  purpose TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  prompt_id TEXT NOT NULL DEFAULT '',
  workflow_id INTEGER,
  workflow_name TEXT NOT NULL DEFAULT '',
  prompt_text TEXT NOT NULL DEFAULT '',
  seed INTEGER,
  model TEXT NOT NULL DEFAULT '',
  lora TEXT NOT NULL DEFAULT '',
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
CREATE TABLE IF NOT EXISTS model_meta(
  folder TEXT NOT NULL,
  filename TEXT NOT NULL,
  civ_id INTEGER,
  civ_name TEXT DEFAULT '',
  base_model TEXT DEFAULT '',
  trained_words TEXT NOT NULL DEFAULT '[]',
  cover TEXT DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  PRIMARY KEY(folder, filename)
);
-- Civitai 本地库:拉取过的搜索页与模型卡片/详情落库,页面加载直接读本地
CREATE TABLE IF NOT EXISTS civ_models(
  civ_id INTEGER PRIMARY KEY,
  type TEXT NOT NULL DEFAULT '',
  card_json TEXT NOT NULL DEFAULT '{}',
  detail_json TEXT,
  fetched_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS civ_searches(
  key TEXT PRIMARY KEY,
  ids TEXT NOT NULL DEFAULT '[]',
  next_cursor TEXT NOT NULL DEFAULT '',
  fetched_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_images_task ON images(task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);
"""


def _backfill_task_models(db):
    """历史任务回填 model/lora(从 params_json 里按标签提取),只处理一次。"""
    rows = db.execute(
        "SELECT id, params_json FROM tasks WHERE model='' AND lora='' "
        "AND params_json!='[]'").fetchall()
    for r in rows:
        try:
            items = json.loads(r["params_json"] or "[]")
        except ValueError:
            continue
        model, lora = "", ""
        for it in items if isinstance(items, list) else []:
            lab = str(it.get("label", ""))
            val = str(it.get("value", ""))
            if not val or val == "None" or ".safetensors" not in val and ".ckpt" not in val:
                continue
            low = lab.lower()
            if "lora" in low:
                lora = f"{lora}, {val}" if lora else val
            elif not model and ("模型" in lab or "checkpoint" in low):
                model = val
        if model or lora:
            db.execute("UPDATE tasks SET model=?, lora=? WHERE id=?",
                       (model[:200], lora[:300], r["id"]))


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    WF_DIR.mkdir(parents=True, exist_ok=True)
    db = get_db()
    db.executescript(SCHEMA)
    # 旧库升级:workflows 增加 template_json 列(模板改存数据库)
    cols = {r[1] for r in db.execute("PRAGMA table_info(workflows)")}
    if "template_json" not in cols:
        db.execute("ALTER TABLE workflows ADD COLUMN template_json TEXT")
    # 旧表 filename 列带 UNIQUE 约束,模板入库后多行会共用空值,重建为普通列
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='workflows'").fetchone()
    if row and "UNIQUE" in (row[0] or ""):
        db.execute("ALTER TABLE workflows RENAME TO workflows_old")
        db.executescript(SCHEMA)
        db.execute(
            "INSERT INTO workflows(id, name, filename, template_json, enabled, created_at) "
            "SELECT id, name, filename, template_json, enabled, created_at FROM workflows_old")
        db.execute("DROP TABLE workflows_old")
    # 增量列:模板导入来源 / 画廊筛选用的主模型与 LoRA / 收藏与分类(底模/场景/用途)
    cols = {r[1] for r in db.execute("PRAGMA table_info(workflows)")}
    if "source" not in cols:
        db.execute("ALTER TABLE workflows ADD COLUMN source TEXT NOT NULL DEFAULT ''")
    for col in ("fav", "scene", "base_model", "purpose"):
        if col not in cols:
            ddl = "INTEGER NOT NULL DEFAULT 0" if col == "fav" else "TEXT NOT NULL DEFAULT ''"
            db.execute(f"ALTER TABLE workflows ADD COLUMN {col} {ddl}")
    tcols = {r[1] for r in db.execute("PRAGMA table_info(tasks)")}
    if "model" not in tcols:
        db.execute("ALTER TABLE tasks ADD COLUMN model TEXT NOT NULL DEFAULT ''")
    if "lora" not in tcols:
        db.execute("ALTER TABLE tasks ADD COLUMN lora TEXT NOT NULL DEFAULT ''")
    _backfill_task_models(db)
    # filename 仅为迁移保留,统一按行 id 命名
    db.execute("UPDATE workflows SET filename='wf_'||id||'.json' WHERE filename=''")
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


def update_workflow_meta(wid, name=None, template_json=None, filename=None):
    """兼容辅助:优先写 template_json;filename 仅为旧库迁移保留。"""
    if template_json is not None:
        execute("UPDATE workflows SET template_json=? WHERE id=?", (template_json, int(wid)))
    if name is not None:
        execute("UPDATE workflows SET name=? WHERE id=?", (name, int(wid)))
    if filename is not None:
        execute("UPDATE workflows SET filename=? WHERE id=?", (filename, int(wid)))


def get_workflow_template(wid):
    row = query_one("SELECT template_json FROM workflows WHERE id=?", (int(wid),))
    if row and row["template_json"]:
        return json.loads(row["template_json"])
    return None


def set_workflow_template(wid, tpl):
    execute("UPDATE workflows SET template_json=? WHERE id=?",
            (json.dumps(tpl, ensure_ascii=False), int(wid)))


def delete_workflow(wid):
    execute("DELETE FROM workflows WHERE id=?", (int(wid),))
