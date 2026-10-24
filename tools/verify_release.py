"""发布包验收：zip 结构 / 元数据 / 文案与凭据扫描 / SHA256。

在仓库根目录执行：

    python tools/verify_release.py
    python tools/verify_release.py --skip-import

默认尝试从 ``ASTRBOT_SRC`` / ``ASTRBOT_ROOT`` / 常见相对目录找到 AstrBot 源码，
找到时额外做一次“干净解压后 import main/web_api”的验证；找不到则跳过（不影响结构检查）。
只依赖标准库。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import sys
import tempfile
import zipfile
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PLUGIN_NAME = "astrbot_plugin_bili_scroll"
RELEASE = PLUGIN_DIR / "release"

FORBIDDEN = re.compile(
    r"dsh-bili-scroll|c1overzz|humanizer|mnemoria|bilibili-mcp|AGI_Ananas", re.IGNORECASE
)
USER_VISIBLE = [
    "README.md",
    "metadata.yaml",
    "_conf_schema.json",
    "CHANGELOG.md",
    "pages/bili-panel/index.html",
]
ABSOLUTE = re.compile(r"[A-Za-z]:\\|/Users/|/home/[^/]+/")
SECRET = re.compile(
    r"(?:sessdata|bili_jct)\"?\s*[:=]\s*\"[A-Za-z0-9%*_\-]{16,}\""
    r"|(?:password_hash|pbkdf2_password|jwt_secret|api_key)\"?\s*[:=]\s*\"[^\"]{8,}\"",
    re.IGNORECASE,
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def png_size(data: bytes) -> tuple[int, int]:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return (0, 0)
    width, height = struct.unpack(">II", data[16:24])
    return (width, height)


def find_astrbot_source() -> str:
    for env_name in ("ASTRBOT_SRC", "ASTRBOT_ROOT"):
        value = os.environ.get(env_name)
        if value and (Path(value) / "astrbot").is_dir():
            return value
    for candidate in (
        Path.cwd() / "AstrBot",
        Path.cwd().parent / "AstrBot",
        PLUGIN_DIR.parent / "AstrBot",
    ):
        if (candidate / "astrbot").is_dir():
            return str(candidate)
    return ""


def version_key(path: Path) -> tuple[int, int, int]:
    match = re.search(r"-(\d+)\.(\d+)\.(\d+)\.zip$", path.name)
    return tuple(int(part) for part in match.groups()) if match else (-1, -1, -1)


def main() -> None:
    parser = argparse.ArgumentParser(description="验收 AstrBot 插件发布包")
    parser.add_argument("--skip-import", action="store_true", help="跳过 AstrBot 导入验证")
    parser.add_argument("--zip", default="", help="指定要验收的 zip 路径（默认取版本号最大的包）")
    args = parser.parse_args()

    zips = sorted(RELEASE.glob(f"{PLUGIN_NAME}-*.zip"), key=version_key)
    if not zips:
        raise SystemExit("没有找到发布 zip，请先运行 tools/build_release.py")
    zip_path = Path(args.zip) if args.zip else zips[-1]
    if not zip_path.is_file():
        raise SystemExit(f"发布包不存在：{zip_path}")
    print("zip=", zip_path.name, "size=", zip_path.stat().st_size)

    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
    print("entries=", len(names))
    bad = [n for n in names if "__pycache__" in n or n.endswith(".pyc") or "/data/" in n]
    print("bad_entries=", bad)
    print("top_dirs=", sorted({n.split("/")[0] for n in names}))
    for required in (
        "main.py",
        "bili_client.py",
        "logic.py",
        "vision.py",
        "store.py",
        "web_api.py",
        "metadata.yaml",
        "_conf_schema.json",
        "logo.png",
        "LICENSE",
        "NOTICE.md",
        "CHANGELOG.md",
        "manifest.json",
        "pages/bili-panel/index.html",
    ):
        print("has", required, "=", f"{PLUGIN_NAME}/{required}" in names)

    # SHA256 与同名 .sha256 是否一致
    expected = ""
    sha_file = zip_path.with_suffix(".zip.sha256")
    if sha_file.exists():
        expected = sha_file.read_text(encoding="utf-8").split()[0].strip()
    digest = sha256(zip_path)
    print("sha256_match=", (not expected) or expected == digest, "sha256=", digest)

    workdir = Path(tempfile.mkdtemp(prefix="bili-release-verify-"))
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(workdir)
    plugin_dir = workdir / PLUGIN_NAME

    meta_text = (plugin_dir / "metadata.yaml").read_text(encoding="utf-8")
    meta = {
        key: (re.search(rf"^{key}:\s*(.+)$", meta_text, re.MULTILINE) or [None, ""])[1].strip()
        for key in ("name", "version", "author", "display_name", "repo")
    }
    print("metadata:", meta)
    schema = json.loads((plugin_dir / "_conf_schema.json").read_text(encoding="utf-8-sig"))
    print("schema_keys=", len(schema))

    hits = []
    for rel in USER_VISIBLE:
        text = (plugin_dir / rel).read_text(encoding="utf-8")
        if FORBIDDEN.search(text):
            hits.append(rel)
    print("forbidden_name_hits=", hits)

    leak, secrets = [], []
    for path in plugin_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(plugin_dir).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if ABSOLUTE.search(text):
            leak.append(rel)
        if SECRET.search(text):
            secrets.append(rel)
    print("absolute_path_hits=", leak)
    print("secret_hits=", secrets)

    width, height = png_size((plugin_dir / "logo.png").read_bytes())
    print("logo=", (width, height))

    source = "" if args.skip_import else find_astrbot_source()
    if source:
        sys.path.insert(0, str(workdir))
        sys.path.insert(0, source)
        os.environ.setdefault(
            "ASTRBOT_ROOT", str(Path(tempfile.mkdtemp(prefix="bili-astrbot-root-")))
        )
        import importlib

        main_mod = importlib.import_module(f"{PLUGIN_NAME}.main")
        web_mod = importlib.import_module(f"{PLUGIN_NAME}.web_api")
        print("import_ok main=", bool(main_mod.BiliScrollPlugin), "routes=", len(web_mod.ROUTE_SPECS))
    else:
        print("import_ok= skipped（未找到 AstrBot 源码）")

    problems = bad + hits + leak + secrets
    print("verify_done", "problems=", problems)
    if problems:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
