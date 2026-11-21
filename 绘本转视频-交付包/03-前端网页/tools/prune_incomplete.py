"""列出并清理「没有生成完成」的绘本目录（data/<id> 下缺 book.json 的那些）。

这些目录来自中断或失败的生成任务：书架不显示它们，但每个都带着原始 PDF 与页图，
占空间很大（一本书约 50–100 MB）。

用法：
    python tools/prune_incomplete.py            # 只列出，不删除（默认）
    python tools/prune_incomplete.py --apply    # 真的删除
    python tools/prune_incomplete.py --apply --keep 5fc0966f0ed9

--keep 可以重复，用目录名前缀匹配；_jobs / _warmup / .vision-cache 永远不动。
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RESERVED = {"_jobs", "_warmup", ".vision-cache"}


def folder_size(path: Path) -> int:
    return sum(file.stat().st_size for file in path.rglob("*") if file.is_file())


def candidates(keep):
    result = []
    for entry in sorted(DATA.iterdir()):
        if not entry.is_dir() or entry.name in RESERVED or entry.is_symlink():
            continue
        if (entry / "book.json").is_file():
            continue
        if any(entry.name.startswith(prefix) for prefix in keep):
            continue
        result.append(entry)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="清理未生成完成的绘本目录（默认仅列出）")
    parser.add_argument("--apply", action="store_true", help="真正删除，而不是只列出")
    parser.add_argument("--keep", action="append", default=[], help="保留的目录名前缀，可重复")
    args = parser.parse_args()

    if not DATA.is_dir():
        raise SystemExit(f"没有数据目录：{DATA}")

    items = candidates(args.keep)
    if not items:
        print("没有未完成的目录，无需清理。")
        return

    total = 0
    print(f"{'目录':<36}{'大小':>10}")
    for entry in items:
        size = folder_size(entry)
        total += size
        files = sum(1 for file in entry.rglob("*") if file.is_file())
        print(f"{entry.name:<36}{size / 1048576:>8.1f} MB  ({files} 个文件)")
    print(f"\n共 {len(items)} 个目录，合计 {total / 1048576:.1f} MB")

    if not args.apply:
        print("\n这是预览。确认无误后加 --apply 真正删除。")
        return

    for entry in items:
        shutil.rmtree(entry)
        print("已删除", entry.name)
    print(f"完成：释放约 {total / 1048576:.1f} MB")


if __name__ == "__main__":
    main()
