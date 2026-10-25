"""构建可发布的插件包（release 目录 + zip + SHA256 + 发布前检查）。

在仓库根目录执行：

    python tools/build_release.py
    python tools/build_release.py --force

只读插件源码，只写 ``release/`` 目录（已 gitignore）；不会改 live 部署、不会 commit/push。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import zipfile
from pathlib import Path

# 仓库根目录 = 本文件上两级（tools/ 的父目录）
PLUGIN_DIR = Path(__file__).resolve().parents[1]
PLUGIN_NAME = "astrbot_plugin_bili_scroll"
RELEASE_ROOT = PLUGIN_DIR / "release"

# 打进发布包的顶层文件 / 目录（白名单，避免 data、tests、缓存混入）
INCLUDE_FILES = [
    "main.py",
    "bili_client.py",
    "logic.py",
    "vision.py",
    "store.py",
    "web_api.py",
    "_conf_schema.json",
    "metadata.yaml",
    "requirements.txt",
    "README.md",
    "LICENSE",
    "NOTICE.md",
    "CHANGELOG.md",
    "logo.png",
]
INCLUDE_DIRS = ["pages"]

# 对外可见文件里不允许出现的其它项目名（NOTICE / LICENSE 属法律文件，单独放行）
USER_VISIBLE = [
    "README.md",
    "metadata.yaml",
    "_conf_schema.json",
    "CHANGELOG.md",
    "pages/bili-panel/index.html",
]
FORBIDDEN_NAMES = re.compile(
    r"dsh-bili-scroll|c1overzz|humanizer|mnemoria|bilibili-mcp|AGI_Ananas",
    re.IGNORECASE,
)
# 本机绝对路径（发布包里不应该出现）
ABSOLUTE_PATH = re.compile(r"[A-Za-z]:\\|/Users/|/home/[^/]+/")
# 疑似凭据残留
SECRET_LIKE = re.compile(
    r"(?:sessdata|bili_jct)\"?\s*[:=]\s*\"[A-Za-z0-9%*_\-]{16,}\""
    r"|(?:password_hash|pbkdf2_password|jwt_secret|api_key)\"?\s*[:=]\s*\"[^\"]{8,}\"",
    re.IGNORECASE,
)


def read_version() -> str:
    text = (PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8")
    match = re.search(r"^version:\s*([0-9A-Za-z.\-]+)\s*$", text, re.MULTILINE)
    if not match:
        raise SystemExit("metadata.yaml 里找不到 version")
    return match.group(1)


def collect_files() -> list[Path]:
    files: list[Path] = []
    for name in INCLUDE_FILES:
        path = PLUGIN_DIR / name
        if not path.is_file():
            raise SystemExit(f"缺少发布必需文件：{path}")
        files.append(path)
    for dir_name in INCLUDE_DIRS:
        directory = PLUGIN_DIR / dir_name
        if not directory.is_dir():
            raise SystemExit(f"缺少发布必需目录：{directory}")
        files.extend(sorted(p for p in directory.rglob("*") if p.is_file()))
    return files


def scan_files(files: list[Path]) -> list[str]:
    problems: list[str] = []
    for path in files:
        rel = path.relative_to(PLUGIN_DIR).as_posix()
        if "__pycache__" in rel or rel.endswith((".pyc", ".pyo")):
            problems.append(f"缓存文件混入：{rel}")
        if rel.startswith("data/"):
            problems.append(f"运行数据混入：{rel}")
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if rel in USER_VISIBLE and FORBIDDEN_NAMES.search(text):
            problems.append(f"对外文件含其它项目名：{rel}")
        if ABSOLUTE_PATH.search(text):
            problems.append(f"含本机绝对路径：{rel}")
        if SECRET_LIKE.search(text):
            problems.append(f"疑似凭据残留：{rel}")
    return problems


def copy_into_staging(files: list[Path], staging_plugin: Path) -> None:
    for path in files:
        rel = path.relative_to(PLUGIN_DIR)
        target = staging_plugin / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def build_zip(source_dir: Path, arc_root: str, zip_path: Path) -> None:
    """把 source_dir 打成 zip，内部顶层目录为 arc_root/。"""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(source_dir.rglob("*")):
            if path.is_file():
                archive.write(path, f"{arc_root}/{path.relative_to(source_dir).as_posix()}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="构建 AstrBot 插件发布包")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的 release 目录/zip")
    args = parser.parse_args()

    version = read_version()
    release_name = f"{PLUGIN_NAME}-{version}"
    out_dir = RELEASE_ROOT / release_name
    staging_root = RELEASE_ROOT / ".staging"
    zip_path = RELEASE_ROOT / f"{release_name}.zip"
    sha_path = RELEASE_ROOT / f"{release_name}.zip.sha256"
    RELEASE_ROOT.mkdir(parents=True, exist_ok=True)

    if out_dir.exists() or zip_path.exists():
        if not args.force:
            raise SystemExit(f"{out_dir} 已存在（用 --force 覆盖）")
        shutil.rmtree(out_dir, ignore_errors=True)
        zip_path.unlink(missing_ok=True)
        sha_path.unlink(missing_ok=True)
    if staging_root.exists():
        shutil.rmtree(staging_root, ignore_errors=True)

    files = collect_files()
    staging_plugin = staging_root / PLUGIN_NAME
    copy_into_staging(files, staging_plugin)

    problems = scan_files(files)
    if problems:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise SystemExit("发布前检查失败：\n- " + "\n- ".join(problems))

    # 发布树单独做一次语法编译（pyc 写临时文件，不进发布树）
    import py_compile

    tmp_pyc = RELEASE_ROOT / ".tmp_compile.pyc"
    try:
        for path in sorted(staging_plugin.rglob("*.py")):
            py_compile.compile(str(path), doraise=True, cfile=str(tmp_pyc))
    finally:
        tmp_pyc.unlink(missing_ok=True)

    out_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staging_plugin), str(out_dir))
    shutil.rmtree(staging_root, ignore_errors=True)

    manifest = [
        {"path": path.relative_to(PLUGIN_DIR).as_posix(), "size": path.stat().st_size}
        for path in files
    ]
    (out_dir / "manifest.json").write_text(
        json.dumps(
            {"name": PLUGIN_NAME, "version": version, "files": manifest},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    build_zip(out_dir, PLUGIN_NAME, zip_path)

    digest = sha256(zip_path)
    sha_path.write_text(f"{digest}  {zip_path.name}\n", encoding="utf-8")

    print(f"release: {out_dir}")
    print(f"zip    : {zip_path} ({zip_path.stat().st_size} B)")
    print(f"sha256 : {digest}")
    print(f"files  : {len(files)}")
    for item in manifest:
        print(f"  {item['size']:>8}  {item['path']}")


if __name__ == "__main__":
    main()
