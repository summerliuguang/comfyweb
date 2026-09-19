"""备份 comfyweb 数据:SQLite 数据库(在线备份,服务运行中也可执行)+ .env。

用法: python scripts/backup.py [目标目录]      # 目标目录默认 ~/comfyweb-backup
保留最近 30 份,更早的自动删除。
若 ~/servershare 已挂载(CIFS/NAS),把当日快照镜像到 ~/servershare/comfyui/comfyweb/
并在 NAS 侧同样保留 30 份——挂载缺失时跳过,不影响本地备份。
图片本体在 ComfyUI 主机输出目录,不在备份范围。
"""
import shutil
import sqlite3
import sys
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
KEEP = 30
NAS_MOUNT = Path.home() / "servershare"
NAS_DEST = NAS_MOUNT / "comfyui" / "comfyweb"


def is_mounted() -> bool:
    """servershare 是否真的处于挂载状态(防 automount 未触发时写到本地磁盘)。"""
    want = str(NAS_MOUNT)
    try:
        with open("/proc/mounts", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == want:
                    return True
    except OSError:
        pass
    return False


def rotate(dest: Path):
    backups = sorted(dest.glob("comfyweb-*.db"))
    for old in backups[:-KEEP]:
        old.unlink(missing_ok=True)
        print(f"清理过期备份: {old.name}")


def backup_to(dest: Path, with_integrity_check: bool = False) -> Path | None:
    """当日数据库快照写入 dest(内容不可变,当天重跑直接覆盖)。返回库文件路径。

    注意:SQLite 备份 API 只能在本地盘执行(CIFS 上会因 SMB 字节范围锁卡死),
    NAS 目标请走 copy_snapshot() 字节拷贝。
    """
    db = BASE / "data" / "comfyweb.db"
    if not db.exists():
        print(f"跳过数据库(不存在: {db})")
        return None
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / f"comfyweb-{date.today().isoformat()}.db"
    src = sqlite3.connect(db)
    out = sqlite3.connect(target)
    with out:
        src.backup(out)
    out.close()
    src.close()
    print(f"数据库 → {target} ({target.stat().st_size // 1024} KB)")
    if with_integrity_check:
        chk = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
        chk.close()
        print(f"完整性校验: {ok}" + ("" if ok == "ok" else "  ← 异常!"))
    return target


def copy_snapshot(local_db: Path, dest: Path):
    """把本地已生成的快照字节拷贝到 dest 并校验 md5 一致(不在这台机器上跑 SQLite)。"""
    import hashlib

    dest.mkdir(parents=True, exist_ok=True)
    target = dest / local_db.name
    shutil.copyfile(local_db, target)
    # 快照是单文件成品;清掉可能存在的陈旧 WAL/SHM(若有人曾用 SQLite 直接打开过
    # NAS 副本),否则正常打开会尝试从旧 WAL 恢复,损坏新副本
    for suffix in ("-wal", "-shm"):
        (dest / (local_db.name + suffix)).unlink(missing_ok=True)
    h = lambda p: hashlib.md5(Path(p).read_bytes()).hexdigest()
    if h(local_db) != h(target):
        target.unlink(missing_ok=True)
        raise OSError(f"拷贝后校验不一致: {target}")
    print(f"数据库 → {target} ({target.stat().st_size // 1024} KB,md5 一致)")


def main():
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "comfyweb-backup"

    # 本地备份
    db_path = backup_to(dest, with_integrity_check=True)
    rotate(dest)
    envf = BASE / ".env"
    if envf.exists():
        copy = dest / f"env-{date.today().isoformat()}"
        shutil.copy2(envf, copy)
        copy.chmod(0o600)  # 内含网关密钥,仅属主可读(本地文件有效;CIFS 由挂载参数决定)
        print(f".env → {copy}")

    # NAS 镜像(可选:servershare 已挂载时;只做文件字节拷贝,SQLite 不碰 CIFS)
    if is_mounted():
        try:
            if db_path:
                copy_snapshot(db_path, NAS_DEST)
                rotate(NAS_DEST)
            if envf.exists():
                shutil.copy2(envf, NAS_DEST / f"env-{date.today().isoformat()}")
            for extra in ("tags", "workflows"):  # tag 字典/旧工作流:小体积,顺带镜像
                src_dir = BASE / "data" / extra
                if src_dir.is_dir():
                    shutil.copytree(src_dir, NAS_DEST / extra, dirs_exist_ok=True)
            print(f"NAS 镜像完成: {NAS_DEST}")
        except OSError as e:
            print(f"NAS 镜像失败(本地备份不受影响): {e}")
    else:
        print("servershare 未挂载,跳过 NAS 镜像")

    print("备份完成")


if __name__ == "__main__":
    main()
