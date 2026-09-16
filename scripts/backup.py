"""备份 comfyweb 数据:SQLite 数据库(在线备份,服务运行中也可执行)+ .env。

用法: python scripts/backup.py [目标目录]      # 目标目录默认 ~/comfyweb-backup
保留最近 30 份,更早的自动删除。图片本体在 ComfyUI 主机输出目录,不在备份范围。
"""
import shutil
import sqlite3
import sys
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
KEEP = 30

dest = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "comfyweb-backup"
dest.mkdir(parents=True, exist_ok=True)
stamp = date.today().isoformat()

db = BASE / "data" / "comfyweb.db"
if db.exists():
    target = dest / f"comfyweb-{stamp}.db"
    src = sqlite3.connect(db)
    out = sqlite3.connect(target)
    with out:
        src.backup(out)
    out.close()
    src.close()
    print(f"数据库 → {target} ({target.stat().st_size // 1024} KB)")
else:
    print(f"跳过数据库(不存在: {db})")

envf = BASE / ".env"
if envf.exists():
    copy = dest / f"env-{stamp}"
    shutil.copy2(envf, copy)
    copy.chmod(0o600)  # 内含网关密钥,仅属主可读
    print(f".env → {copy}")

backups = sorted(dest.glob("comfyweb-*.db"))
for old in backups[:-KEEP]:
    old.unlink(missing_ok=True)
    print(f"清理过期备份: {old.name}")
print(f"完成,当前保留 {min(len(backups), KEEP)} 份数据库备份于 {dest}")
