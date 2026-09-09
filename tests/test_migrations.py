"""旧库结构升级测试:在子进程里构造带 UNIQUE filename 的旧 workflows 表,
跑 init_db 验证重建后约束消失、数据完整(模板入库迁移同路径)。"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SCRIPT = r"""
import sqlite3, sys
data_dir = sys.argv[1]
db = sqlite3.connect(data_dir + "/comfyweb.db")
db.executescript('''
CREATE TABLE workflows(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  filename TEXT NOT NULL UNIQUE,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
''')
db.execute("INSERT INTO workflows(name, filename) VALUES('旧模板', 'wf_1.json')")
db.commit(); db.close()

sys.path.insert(0, %r)
import db as appdb
appdb.init_db()
appdb.set_workflow_template(1, {"version": 1, "name": "旧模板", "params": []})

conn = sqlite3.connect(data_dir + "/comfyweb.db")
sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='workflows'").fetchone()[0]
row = conn.execute("SELECT id, name, filename, enabled FROM workflows WHERE id=1").fetchone()
tpl = conn.execute("SELECT template_json FROM workflows WHERE id=1").fetchone()[0]
assert "UNIQUE" not in sql, "UNIQUE 约束应已移除"
assert row == (1, "旧模板", "wf_1.json", 1), row
assert '"version": 1' in tpl, tpl
print("migration-ok")
"""


class WorkflowTableRebuild(unittest.TestCase):
    def test_unique_filename_table_rebuilt(self):
        with tempfile.TemporaryDirectory(prefix="comfyweb-mig-") as tmp:
            r = subprocess.run(
                [sys.executable, "-c", SCRIPT % str(ROOT), tmp],
                capture_output=True, text=True, timeout=60,
                env={**os.environ, "COMFYWEB_DATA_DIR": tmp})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("migration-ok", r.stdout)


if __name__ == "__main__":
    unittest.main()
