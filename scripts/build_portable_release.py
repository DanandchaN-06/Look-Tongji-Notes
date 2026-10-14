"""Build a clean Windows x64 portable ZIP from a verified runtime seed ZIP.

Only Python/Playwright runtime files come from the seed. Project code comes
from this checkout. Account configuration, course workspaces, browser profiles,
and installed-app directories are never read.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PREFIX = "Look-Tongji-Manager-"
EXCLUDED_PARTS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "state", "tests", "test"}
EXCLUDED_NAMES = {".env", "config.json", "python.local", "direct_url.json", "auth_session.json", "cookies.json", "login_state.json"}
SOURCE_DIRS = ("console", "scripts", "llmwiki", "skills", "references", "vision-support", "CheatingSheetTemplate")
ROOT_FILES = ("LICENSE", "README.md", "README_ZH.md", "requirements.txt", "SKILL.md")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_target(root: Path, relative: PurePosixPath) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Unsafe ZIP path: {relative}")
    target = root.joinpath(*relative.parts).resolve()
    if root.resolve() not in target.parents:
        raise ValueError(f"ZIP path escapes destination: {relative}")
    return target


def extract_seed(seed_zip: Path, destination: Path) -> None:
    expected_roots = ("/app/runtime/", "/app/browsers/")
    selected = 0
    with zipfile.ZipFile(seed_zip) as archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if name.endswith("/MICROSOFT_REDIST.txt"):
                target = destination / "MICROSOFT_REDIST.txt"
                with archive.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                continue
            marker = next((name.find(root) for root in expected_roots if root in name), -1)
            if marker < 0 or info.is_dir():
                continue
            relative = PurePosixPath(name[marker + 1:])
            if any(part.lower() in EXCLUDED_PARTS for part in relative.parts):
                continue
            if relative.name.lower() in EXCLUDED_NAMES or relative.suffix.lower() in {".pyc", ".pyo"}:
                continue
            target = safe_target(destination, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            selected += 1
    if not selected:
        raise RuntimeError("Runtime seed ZIP did not contain app/runtime or app/browsers")
    required = (
        destination / "app/runtime/python.exe",
        destination / "app/runtime/Lib/site-packages/requests",
        destination / "app/browsers/chromium_headless_shell-1223",
        destination / "MICROSOFT_REDIST.txt",
    )
    for path in required:
        if not path.exists():
            raise RuntimeError(f"Runtime seed is missing required component: {path.relative_to(destination)}")


def excluded_source(relative: Path) -> bool:
    parts = {part.lower() for part in relative.parts}
    name = relative.name.lower()
    if parts & EXCLUDED_PARTS or name in EXCLUDED_NAMES or name.endswith((".pyc", ".pyo")):
        return True
    if relative.parts[:2] == ("console", "frontend"):
        return True
    if relative.parts and relative.parts[0] == "console" and (
        name.startswith("test_") or name in {"browser_smoke.py", "selftest.py"}
    ):
        return True
    return False


def copy_source(project: Path) -> None:
    for directory in SOURCE_DIRS:
        source = ROOT / directory
        if not source.is_dir():
            raise RuntimeError(f"Required source directory is missing: {directory}")
        for path in source.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT)
            if excluded_source(relative):
                continue
            target = project / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    for name in ROOT_FILES:
        source = ROOT / name
        if not source.is_file():
            raise RuntimeError(f"Required source file is missing: {name}")
        shutil.copy2(source, project / name)
    for name in ("MANAGER_GUIDE.md", "MANAGER_RELEASE.md"):
        source = ROOT / "docs" / name
        if not source.is_file():
            raise RuntimeError(f"Required user guide is missing: docs/{name}")
        target = project / "docs" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    shutil.copy2(ROOT / "llmwiki/serve.py", project.parent / "original_serve.py")
    shutil.copy2(ROOT / "console/portable_launcher.py", project.parent / "launcher.py")
    shutil.copy2(ROOT / "console/portable_install_media.py", project.parent / "install_media.py")


def write_entry(root: Path) -> None:
    entry = r"""@echo off
chcp 65001 >nul
setlocal
set "PYTHONHOME=%~dp0app\runtime"
set "PYTHONNOUSERSITE=1"
set "PYTHONPATH="
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0app\browsers"
set "MODE=--manager"
if not "%~1"=="" set "MODE="
if not "%~1"=="" goto run
echo.
echo   课程助手
echo   [1] 管理控制台（V2）
echo   [2] 原有阅读网页
echo   [3] 安装字幕转写组件 FFmpeg
echo   [4] 升级旧版并保留设置
echo.
choice /c 1234 /n /m "请选择 1、2、3 或 4："
if errorlevel 4 goto upgrade
if errorlevel 3 goto media
if errorlevel 2 set "MODE=--reader"
:run
start "" "%~dp0app\runtime\pythonw.exe" "%~dp0app\launcher.py" %MODE% %*
exit /b
:media
"%~dp0app\runtime\python.exe" "%~dp0app\install_media.py"
pause
:upgrade
set /p "TARGET_APP=请输入旧版应用目录完整路径（例如 D:\课程助手\应用）："
"%~dp0app\runtime\python.exe" "%~dp0app\project\scripts\upgrade_portable_install.py" --target-app "%TARGET_APP%"
pause
"""
    (root / "启动课程助手.cmd").write_text(entry, encoding="utf-8")


def write_notices(root: Path) -> None:
    license_path = root / "app/runtime/LICENSE.txt"
    python_notice = license_path.read_text(encoding="utf-8", errors="replace") if license_path.is_file() else "See https://docs.python.org/3/license.html"
    redist = (root / "MICROSOFT_REDIST.txt").read_text(encoding="utf-8", errors="replace")
    notices = [
        "Look Tongji Notes\nMIT License; see app/project/LICENSE.",
        "Python\n" + python_notice,
        "Playwright Chromium\nSee https://playwright.dev/docs/browsers and the license files included with the browser distribution.",
        "Microsoft Visual C++ Redistributable\n" + redist + "\nApplication-local runtime files are distributed under Microsoft's applicable terms.",
        "Other Python dependencies\nSee DEPENDENCIES.json and the corresponding license files under app/runtime/Lib/site-packages.",
    ]
    (root / "THIRD_PARTY_NOTICES.txt").write_text("\n\n".join(notices) + "\n", encoding="utf-8")


def write_dependencies(root: Path) -> None:
    site_packages = root / "app/runtime/Lib/site-packages"
    dependencies = []
    for dist in importlib.metadata.distributions(path=[str(site_packages)]):
        metadata = dist.metadata
        dependencies.append({
            "name": metadata.get("Name", "unknown"),
            "version": dist.version,
            "license": metadata.get("License-Expression") or metadata.get("License") or "See distribution license files",
        })
    dependencies.sort(key=lambda item: item["name"].casefold())
    (root / "DEPENDENCIES.json").write_text(json.dumps(dependencies, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_quickstart(root: Path, version: str) -> None:
    content = f"""# 课程助手便携版 {version}

1. 解压整个 ZIP 到有写入权限的位置。
2. 双击 `启动课程助手.cmd`，选择 **1 管理控制台（V2）**。
3. 首次使用时进入“设置”填写自己的同济账号和资料目录。
4. 首次字幕转写需选择入口 **3** 安装 FFmpeg，然后重开管理器。
5. 已生成的原课程阅读网页可从入口 **2** 打开。

本包为 Windows x64 便携版，不需要先安装 Python 或运行 pip。启动器配置在 `app/data`，账号保存在 `app/project/.env`，任务数据库和缓存保存在 `app/project/state`，模型接口设置保存在 `%APPDATA%\\look-tongji`。课程资料写入你在设置中指定的目录。不要分享使用后的这些设置目录或课程资料目录。

笔记由你选择的 Agent 撰写。字幕识别会把音频发送到界面提示的外部识别服务。视觉接口和其他外部服务按需配置。

校验文件：`SHA256SUMS.txt`。项目与第三方许可见 `app/project/LICENSE`、`THIRD_PARTY_NOTICES.txt` 和 `DEPENDENCIES.json`。
"""
    (root / "先读我.md").write_text(content, encoding="utf-8")


def scan_package(root: Path) -> None:
    forbidden_names = EXCLUDED_NAMES | {"manager-service.json", "service.json", "server.log", "last-error.txt"}
    user_path = re.compile(
        rb"(?i)(?:[a-z]:[\\/]+users[\\/]+)"
        rb"(?!public[\\/]|default[\\/]|all users[\\/]|<u>|<user>|<username>|%username%)"
        rb"[^\\/\s,.;:()<>\x22'`]+"
    )
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.parts[:2] == ("app", "data") or relative.parts[:3] == ("app", "project", "state"):
            raise RuntimeError(f"User state must not be packaged: {relative}")
        packaged_runtime = relative.parts[:2] in (("app", "runtime"), ("app", "browsers"))
        if (not packaged_runtime and path.name.lower() in forbidden_names) or any(part.lower() in EXCLUDED_PARTS for part in relative.parts):
            raise RuntimeError(f"Private/generated file must not be packaged: {relative}")
        if relative.parts[:2] not in (("app", "runtime"), ("app", "browsers")):
            data = path.read_bytes()
            if b"WorkBuddy" in data or b"C:\\Users\\84379" in data or b"C:/Users/84379" in data:
                raise RuntimeError(f"Local machine identifier found: {relative}")
            if user_path.search(data):
                raise RuntimeError(f"Absolute user profile path found: {relative}")
    if not (root / "app/project/console/web/v2/.vite/manifest.json").is_file():
        raise RuntimeError("V2 built asset manifest is missing")
    if not (root / "app/project/console/web/v2/index.html").is_file():
        raise RuntimeError("V2 built index is missing")


def file_manifest(root: Path, version: str, source_commit: str) -> dict:
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name not in {"PACKAGE_MANIFEST.json", "SHA256SUMS.txt"}:
            files[path.relative_to(root).as_posix()] = sha256(path)
    return {"version": version, "platform": "Windows x64", "source_commit": source_commit, "files": files}


def zip_package(root: Path, archive: Path) -> None:
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as bundle:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                bundle.write(path, f"{root.name}/{path.relative_to(root).as_posix()}")


def verify_archive(root: Path, archive: Path, manifest: dict) -> None:
    prefix = root.name + "/"
    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip():
            raise RuntimeError("ZIP CRC verification failed")
        archived = set(bundle.namelist())
        for relative, expected in manifest["files"].items():
            name = prefix + relative
            if name not in archived:
                raise RuntimeError(f"Archive is missing {relative}")
            digest = hashlib.sha256(bundle.read(name)).hexdigest()
            if digest != expected:
                raise RuntimeError(f"Archive hash mismatch: {relative}")
        package_manifest = json.loads(bundle.read(prefix + "PACKAGE_MANIFEST.json"))
        if package_manifest != manifest:
            raise RuntimeError("Embedded package manifest does not match the build manifest")
        expected_names = {prefix + name for name in manifest["files"]} | {prefix + "PACKAGE_MANIFEST.json"}
        if archived != expected_names:
            raise RuntimeError("Archive contents differ from the package manifest")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-zip", type=Path, required=True, help="Clean previous portable ZIP supplying Python and Playwright runtimes")
    parser.add_argument("--seed-sha256", required=True, help="Expected SHA-256 for the runtime seed")
    parser.add_argument("--output", type=Path, required=True, help="Directory for the package folder, archive, and checksum")
    parser.add_argument("--version", default="0.5.0")
    parser.add_argument("--source-commit", default="working-tree")
    args = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.version):
        parser.error("--version must use MAJOR.MINOR.PATCH")
    seed = args.seed_zip.resolve()
    if not seed.is_file():
        parser.error(f"Runtime seed ZIP not found: {seed}")
    actual_seed_sha = sha256(seed)
    if actual_seed_sha.lower() != args.seed_sha256.lower():
        parser.error(f"Runtime seed SHA-256 mismatch: expected {args.seed_sha256}, got {actual_seed_sha}")
    output = args.output.resolve()
    package = output / f"{PACKAGE_PREFIX}{args.version}-Windows-x64"
    archive = output / f"{package.name}.zip"
    if package.exists() or archive.exists():
        parser.error(f"Output already exists; choose a new clean output directory: {output}")
    output.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="look-tongji-package-") as temporary:
        stage = Path(temporary)
        extract_seed(seed, stage)
        app = package / "app"
        app.mkdir(parents=True)
        shutil.copytree(stage / "app/runtime", app / "runtime")
        shutil.copytree(stage / "app/browsers", app / "browsers")
        shutil.copy2(stage / "MICROSOFT_REDIST.txt", package / "MICROSOFT_REDIST.txt")
    try:
        copy_source(package / "app/project")
        write_entry(package)
        write_quickstart(package, args.version)
        write_notices(package)
        write_dependencies(package)
        scan_package(package)
        manifest = file_manifest(package, args.version, args.source_commit)
        (package / "PACKAGE_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        zip_package(package, archive)
        verify_archive(package, archive, manifest)
        checksum = sha256(archive)
        (output / "SHA256SUMS.txt").write_text(f"{checksum}  {archive.name}\n", encoding="utf-8")
        print(json.dumps({"version": args.version, "archive": str(archive), "bytes": archive.stat().st_size,
                          "archive_sha256": checksum, "files": len(manifest["files"]),
                          "source_commit": args.source_commit}, ensure_ascii=False, indent=2))
    except BaseException:
        shutil.rmtree(package, ignore_errors=True)
        archive.unlink(missing_ok=True)
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
