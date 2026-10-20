"""在服务停止写入后，为附件卷生成并校验可恢复快照。"""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
from pathlib import Path

from app.config import settings


def _manifest(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not root.exists():
        return result
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.is_symlink():
            raise AssertionError(f"附件卷不应包含符号链接：{path.relative_to(root)}")
        relative = path.relative_to(root).as_posix()
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def snapshot(archive_path: Path, manifest_path: Path) -> None:
    root = settings.attachments_root.resolve()
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = _manifest(root)
    with tarfile.open(archive_path, "w:gz") as archive:
        if root.exists():
            for entry in sorted(root.iterdir()):
                archive.add(entry, arcname=entry.name, recursive=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def restore_and_verify(
    archive_path: Path, manifest_path: Path, restore_root: Path
) -> None:
    expected = json.loads(manifest_path.read_text(encoding="utf-8"))
    restore_root.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            candidate = (restore_root / member.name).resolve()
            if (
                candidate != restore_root.resolve()
                and restore_root.resolve() not in candidate.parents
            ):
                raise AssertionError("附件备份包含越界路径")
            if member.issym() or member.islnk():
                raise AssertionError("附件备份不允许恢复符号链接或硬链接")
        archive.extractall(restore_root, filter="data")
    actual = _manifest(restore_root)
    assert actual == expected, "恢复后附件文件清单或 SHA-256 与快照不一致"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("snapshot", "restore-verify"))
    parser.add_argument("archive", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("restore_root", type=Path, nargs="?")
    args = parser.parse_args()
    if args.action == "snapshot":
        snapshot(args.archive, args.manifest)
        print(
            f"附件快照已生成，SHA-256 文件数：{len(json.loads(args.manifest.read_text(encoding='utf-8')))}"
        )
    else:
        if args.restore_root is None:
            parser.error("restore-verify 必须提供恢复目录")
        restore_and_verify(args.archive, args.manifest, args.restore_root)
        print("附件备份恢复后 SHA-256 全量一致")


if __name__ == "__main__":
    main()
