"""一次性迁移:output/ 全量收编 + 替身升级为全画质 + 本地缓冲清理 + 快照。

在服务停止状态下运行(避免与归档线程竞争索引):
  shared-venv/bin/python scripts/migrate_library.py
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import library  # noqa: E402
from comfy_client import ComfyError, client  # noqa: E402

OUT = library.output_dir()


def upgrade_from_output():
    """已入索引但 library 副本劣于 output 同名文件的,用 output 原图替换(画质升级)。"""
    upgraded = 0
    conn = library._lib_connect()
    rows = conn.execute("SELECT filename, path, size FROM files").fetchall()
    conn.close()
    for r in rows:
        out_file = OUT / r["filename"]
        lib_file = library.library_dir() / r["path"]
        try:
            if not out_file.is_file():
                continue
            if out_file.stat().st_size <= r["size"]:
                continue
            lib_file.unlink(missing_ok=True)
            shutil.move(str(out_file), str(lib_file))
            c = library._lib_connect()
            c.execute("UPDATE files SET size=? WHERE filename=?",
                      (lib_file.stat().st_size, r["filename"]))
            c.commit()
            c.close()
            upgraded += 1
        except OSError as e:
            print(f"升级失败 {r['filename']}: {e}")
    return upgraded


def main():
    moved, skipped = library.collect_once(limit=5000)
    print(f"收编 output:移动 {moved},跳过(已入索引) {skipped}")

    upgraded = upgrade_from_output()
    print(f"画质升级(用 output 原图替换替身):{upgraded} 张")

    # 本地缓冲里已被 library 更优副本覆盖的替身,立即清理(读路径落 library)
    pruned = 0
    for p in (db.DATA_DIR / "images").rglob("*"):
        if not p.is_file() or p.suffix == ".part":
            continue
        row = library.indexed(p.name)
        if row:
            dest = library.library_dir() / row["path"]
            try:
                if dest.exists() and dest.stat().st_size >= p.stat().st_size:
                    p.unlink()
                    pruned += 1
            except OSError:
                pass
    print(f"本地缓冲清理:{pruned} 张(library 已有更优副本)")

    n = library.snapshot()
    print(f"索引快照推送 NAS: {'ok' if n else 'skip'}")
    st = library.status()
    print(f"索引 {st['indexed']} 张(未分类 {st['unclassified']}),output 待收编 {st['pending']}")


if __name__ == "__main__":
    main()
