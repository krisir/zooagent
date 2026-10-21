#!/usr/bin/env python3
"""把 zooagent 打成桌面安装包：macOS 与 Windows 用同一份脚本。

一次构建做三件事：

1. 前端 `pnpm build`（Vite 产物落在 `apps/desktop/dist`）；
2. PyInstaller 把 Python 后端打成单文件 sidecar，并把前端一起嵌进去，
   按 Tauri 的约定命名成 `binaries/zooagent-backend-<target-triple>`；
3. `tauri build` 编译 Rust 壳并出安装包（macOS：`.app` / `.dmg`；Windows：NSIS / MSI）。

跨平台打包不是交叉编译：Python 的 PyInstaller 只能在目标系统上出可执行文件，
Windows 的 NSIS/MSI 也只能在 Windows 上生成。所以这个脚本在不同系统上各跑一次即可，
Android 风格的一台机器出两端包不在支持范围内（见文档 "打包"）。

用法：

    scripts/build/build-app.sh                  # macOS，默认出 .app
    scripts/build/build-app.sh --dmg            # macOS，额外出 .dmg
    scripts\\build\\build-app.ps1                 # Windows，默认出 NSIS 安装器
    scripts\\build\\build-app.ps1 -Bundles msi    # Windows，出 MSI

常用开关见 `--help`。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import NoReturn

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
DESKTOP_DIR = REPO_ROOT / "apps" / "desktop"
TAURI_DIR = DESKTOP_DIR / "src-tauri"
FRONTEND_DIST = DESKTOP_DIR / "dist"
SIDECAR_DIR = TAURI_DIR / "binaries"
SIDECAR_NAME = "zooagent-backend"

#: 安装包后缀，用来在构建完成后清点产物。
BUNDLE_SUFFIXES = {".app", ".dmg", ".exe", ".msi", ".AppImage", ".deb"}

PLATFORM = (
    "macos" if sys.platform == "darwin" else "windows" if os.name == "nt" else "linux"
)
EXE_SUFFIX = ".exe" if os.name == "nt" else ""


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------
def log(message: str) -> None:
    print(f"==> {message}", flush=True)


def fail(message: str) -> NoReturn:
    print(f"\n构建失败：{message}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def ensure_utf8_console() -> None:
    """Windows 控制台默认是 GBK，中文日志会直接抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # pragma: no cover - 老终端
            pass


def prepare_path() -> None:
    """尽量让 cargo / uv / pnpm 在裸 shell 里也能找到（GUI 启动的终端很常见）。"""
    candidates = [
        Path.home() / ".cargo" / "bin",
        Path.home() / ".local" / "bin",
        Path.home() / ".local" / "share" / "pnpm",
        Path(os.environ.get("PNPM_HOME", "")),
    ]
    existing = [str(path) for path in candidates if str(path) and path.is_dir()]
    if existing:
        os.environ["PATH"] = os.pathsep.join(existing + [os.environ.get("PATH", "")])


def which(name: str) -> str | None:
    return shutil.which(name)


def command_for(executable: str, *args: str) -> list[str]:
    """`.cmd` / `.bat` 在 Windows 上必须经 cmd.exe 启动。"""
    if os.name == "nt" and executable.lower().endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", executable, *args]
    return [executable, *args]


def run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    printable = " ".join(part if " " not in part else f'"{part}"' for part in cmd)
    log(printable)
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env, check=True)


def capture(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        fail(f"命令失败：{' '.join(cmd)}\n{result.stderr.strip()}")
    return result.stdout


def human_size(path: Path) -> str:
    size = path.stat().st_size if path.is_file() else dir_size(path)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"  # pragma: no cover - 不会走到


def dir_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:  # pragma: no cover - 符号链接/权限
            continue
    return total


# ----------------------------------------------------------------------
# 前置检查
# ----------------------------------------------------------------------
INSTALL_HINTS = {
    "pnpm": "npm i -g pnpm（或 corepack enable pnpm）",
    "uv": (
        "https://docs.astral.sh/uv/ "
        "（macOS: curl -LsSf https://astral.sh/uv/install.sh | sh；"
        "Windows: winget install --id=astral-sh.uv）"
    ),
    "rustc": "https://rustup.rs/（curl https://sh.rustup.rs -sSf | sh）",
    "cargo": "https://rustup.rs/",
}


def require(tools: dict[str, str | None]) -> dict[str, str]:
    missing = [name for name, path in tools.items() if not path]
    if missing:
        detail = "\n".join(f"  - {name}: {INSTALL_HINTS.get(name, '')}" for name in missing)
        fail(f"缺少构建工具：\n{detail}")
    return {name: path for name, path in tools.items() if path}


def host_triple(rustc: str) -> str:
    for line in capture([rustc, "-vV"]).splitlines():
        if line.startswith("host:"):
            return line.split()[1].strip()
    fail("无法从 `rustc -vV` 读取 host target triple")


def ensure_rust_target(triple: str) -> None:
    """--target 指向别的架构时，需要那个 target 已经装上。"""
    rustup = which("rustup")
    if rustup is None:
        return
    installed = capture([rustup, "target", "list", "--installed"])
    if triple not in installed.split():
        fail(
            f"Rust target {triple} 未安装。先运行：\n"
            f"  rustup target add {triple}"
        )


def macos_checks() -> None:
    if shutil.which("xcode-select") and subprocess.run(
        ["xcode-select", "-p"], capture_output=True
    ).returncode != 0:
        fail("缺少 Xcode Command Line Tools：xcode-select --install")


def windows_checks() -> None:
    # MSVC 链接器由 rustup 的 msvc toolchain 依赖，缺失时 cargo 的报错足够清楚，
    # 这里只提醒 WebView2（Windows 10/11 通常已内置）。
    log("Windows 打包提示：安装器依赖 WebView2（Win10 1803+ 默认自带）")


# ----------------------------------------------------------------------
# 三个构建阶段
# ----------------------------------------------------------------------
def build_frontend(pnpm: str) -> None:
    log("构建前端 (pnpm build)")
    run(command_for(pnpm, "build"), cwd=DESKTOP_DIR)
    if not (FRONTEND_DIST / "index.html").is_file():
        fail(f"前端产物缺失：{FRONTEND_DIST}/index.html")


def build_sidecar(
    *,
    uv: str,
    triple: str,
    with_agentscope: bool,
    install: bool,
) -> Path:
    extras = ["--extra", "packaging"] + (["--extra", "agentscope"] if with_agentscope else [])
    if install:
        log("同步后端依赖" + ("（含 AgentScope 运行时）" if with_agentscope else "（不含 AgentScope）"))
        if not with_agentscope:
            print(
                "    注意：uv sync 会把 agentscope 从当前 venv 移除；"
                "之后要跑 agentscope/mock 相关测试需要 uv sync --extra agentscope",
                flush=True,
            )
        run(command_for(uv, "sync", *extras), cwd=BACKEND_DIR)
    else:
        log("跳过依赖同步（--no-install）")

    if not (FRONTEND_DIST / "index.html").is_file():
        fail("打包后端前需要前端产物，请先运行 pnpm build（或去掉 --skip-frontend）")

    log(f"PyInstaller 打包后端 (target: {triple})")
    run(
        command_for(
            uv,
            "run",
            "pyinstaller",
            "--noconfirm",
            "--clean",
            "--onefile",
            "--name",
            SIDECAR_NAME,
            "--paths",
            "src",
            # PyInstaller 的 --add-data 分隔符跟平台走：macOS/Linux 是 `:`，Windows 是 `;`
            "--add-data",
            f"{FRONTEND_DIST}{os.pathsep}web",
            "--collect-submodules",
            "zooagent",
            "--distpath",
            "dist",
            "--workpath",
            "build/pyinstaller",
            "--specpath",
            "build",
            "packaging/entry.py",
        ),
        cwd=BACKEND_DIR,
    )

    produced = BACKEND_DIR / "dist" / f"{SIDECAR_NAME}{EXE_SUFFIX}"
    if not produced.is_file():
        fail(f"PyInstaller 没有产出 {produced}")

    SIDECAR_DIR.mkdir(parents=True, exist_ok=True)
    target = SIDECAR_DIR / f"{SIDECAR_NAME}-{triple}{EXE_SUFFIX}"
    shutil.copy2(produced, target)
    if os.name != "nt":
        target.chmod(0o755)
    # 同一个目录里可能留着上一个架构的 sidecar（比如 arm64 机器打 x64 包），
    # Tauri 只会取与 target triple 匹配的那份，所以不用清理。
    log(f"sidecar: {target} ({human_size(target)})")
    return target


def build_bundle(
    *,
    pnpm: str,
    triple: str,
    host: str,
    bundles: list[str],
    debug: bool,
    frontend_built: bool,
) -> None:
    cmd = command_for(pnpm, "exec", "tauri", "build")
    if triple != host:
        cmd += ["--target", triple]
    if bundles:
        cmd += ["--bundles", ",".join(bundles)]
    if debug:
        cmd.append("--debug")
    override: Path | None = None
    if frontend_built:
        # beforeBuildCommand 还会再跑一次 pnpm build；dist 已经是同一份产物了。
        # 用临时文件而不是内联 JSON：Windows 上参数要经 cmd.exe，带引号的 JSON 会被吃掉。
        handle, name = tempfile.mkstemp(prefix="zooagent-build-", suffix=".json")
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump({"build": {"beforeBuildCommand": ""}}, file)
        override = Path(name)
        cmd += ["--config", str(override)]
    log("编译桌面壳并生成安装包")
    try:
        run(cmd, cwd=DESKTOP_DIR)
    finally:
        if override is not None:
            override.unlink(missing_ok=True)


def report(*, triple: str, host: str, debug: bool, started: float) -> None:
    profile = "debug" if debug else "release"
    target_root = TAURI_DIR / "target"
    bundle_root = target_root / (profile if triple == host else f"{triple}/{profile}") / "bundle"
    if not bundle_root.is_dir():
        log(f"没有找到安装包目录：{bundle_root}")
        return

    found: list[Path] = []
    for path in sorted(bundle_root.rglob("*")):
        if path.is_dir() and path.suffix in BUNDLE_SUFFIXES:
            found.append(path)
        elif path.is_file() and path.suffix in BUNDLE_SUFFIXES:
            found.append(path)
    # .app 目录里会包含可执行文件，过滤掉 app 内部的条目
    results = [
        path
        for path in found
        if not any(parent.suffix == ".app" and parent != path for parent in path.parents)
    ]
    fresh = [path for path in results if path.stat().st_mtime >= started - 1]

    log("构建完成，产物：")
    for path in fresh or results:
        relative = path.relative_to(REPO_ROOT)
        print(f"    {relative}  ({human_size(path)})", flush=True)
    if not fresh and results:
        print("    （以上是目录里已有的产物，本次构建可能被跳过）", flush=True)


# ----------------------------------------------------------------------
def default_bundles(platform: str, wants_dmg: bool, wants_msi: bool) -> list[str]:
    if platform == "macos":
        return ["app", "dmg"] if wants_dmg else ["app"]
    if platform == "windows":
        return ["nsis", "msi"] if wants_msi else ["nsis"]
    return ["appimage", "deb"]


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="build-app",
        description="打包 zooagent 桌面端（macOS / Windows）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  scripts/build/build-app.sh --dmg                 # macOS：.app + .dmg\n"
            "  scripts/build/build-app.sh --target x86_64-apple-darwin\n"
            "  scripts/build/build-app.sh --no-agentscope       # 更小的包（约省 17 MB）\n"
            "  powershell -File scripts/build/build-app.ps1 -Bundles nsis,msi\n"
        ),
    )
    parser.add_argument("--target", help="Rust target triple（默认当前机器，例如 aarch64-apple-darwin）")
    parser.add_argument("--bundles", help="覆盖安装包类型，逗号分隔，例如 app,dmg 或 nsis,msi")
    parser.add_argument("--dmg", action="store_true", help="macOS：同时生成 DMG")
    parser.add_argument("--msi", action="store_true", help="Windows：同时生成 MSI")
    parser.add_argument("--debug", action="store_true", help="用 debug profile（产物大、启动快，便于排查）")
    parser.add_argument(
        "--no-agentscope",
        action="store_true",
        help="sidecar 不带 AgentScope 运行时（体积小，但 agentscope/mock 配置不可用）",
    )
    parser.add_argument("--skip-frontend", action="store_true", help="复用已有的 apps/desktop/dist")
    parser.add_argument("--skip-sidecar", action="store_true", help="复用已有的 sidecar 二进制")
    parser.add_argument(
        "--sidecar-only",
        action="store_true",
        help="只做前端 + sidecar（之后可手动 cd apps/desktop && pnpm tauri build）",
    )
    parser.add_argument("--no-install", action="store_true", help="不跑 uv sync（依赖已就绪时更快）")
    parser.add_argument(
        "--clean",
        action="store_true",
        help="构建前清掉后端 build/dist、前端 dist 与 sidecar 二进制",
    )
    return parser.parse_args(argv)


def clean_outputs() -> None:
    log("清理旧产物")
    for path in (
        BACKEND_DIR / "build" / "pyinstaller",
        BACKEND_DIR / "dist",
        FRONTEND_DIST,
        SIDECAR_DIR,
    ):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.is_file():
            path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_console()
    args = parse_args(argv)
    if os.environ.get("ZOOAGENT_WITHOUT_AGENTSCOPE", "0") == "1":
        # 兼容旧的两步式脚本（build-sidecar.sh 时代的开关）
        args.no_agentscope = True
    prepare_path()
    started = time.time()

    tools = require(
        {
            "pnpm": which("pnpm"),
            "uv": which("uv"),
            "rustc": which("rustc"),
            "cargo": which("cargo"),
        }
    )
    host = host_triple(tools["rustc"])
    triple = args.target or host
    ensure_rust_target(triple)

    if PLATFORM == "macos":
        macos_checks()
    elif PLATFORM == "windows":
        windows_checks()

    if args.dmg and PLATFORM != "macos":
        fail("--dmg 只能在 macOS 上使用")
    if args.msi and PLATFORM != "windows":
        fail("--msi 只能在 Windows 上使用")
    if args.bundles:
        bundles = [item.strip() for item in args.bundles.split(",") if item.strip()]
    else:
        bundles = default_bundles(PLATFORM, args.dmg, args.msi)

    if args.clean:
        clean_outputs()

    log(f"平台 {PLATFORM} · target {triple} · 安装包 {', '.join(bundles) or '(仅编译)'}")

    frontend_built = False
    if not args.skip_frontend:
        build_frontend(tools["pnpm"])
        frontend_built = True

    if not args.skip_sidecar:
        build_sidecar(
            uv=tools["uv"],
            triple=triple,
            with_agentscope=not args.no_agentscope,
            install=not args.no_install,
        )
    else:
        expected = SIDECAR_DIR / f"{SIDECAR_NAME}-{triple}{EXE_SUFFIX}"
        if not expected.is_file():
            fail(f"--skip-sidecar 需要已存在的 {expected}")
        log(f"复用 sidecar: {expected}")

    if args.sidecar_only:
        log("只构建 sidecar（--sidecar-only），跳过 tauri build")
        print(
            "\n下一步：cd apps/desktop && pnpm tauri build\n"
            "或者直接重跑本脚本去掉 --sidecar-only 产出安装包。",
            flush=True,
        )
        return 0

    build_bundle(
        pnpm=tools["pnpm"],
        triple=triple,
        host=host,
        bundles=bundles,
        debug=args.debug,
        frontend_built=frontend_built,
    )
    report(triple=triple, host=host, debug=args.debug, started=started)

    if PLATFORM == "macos":
        print(
            "\n提示：未签名的 .app 在别的机器上会被 Gatekeeper 拦下；"
            "分发前设置 APPLE_CERTIFICATE / APPLE_ID 等环境变量让 Tauri 走签名与公证。",
            flush=True,
        )
    elif PLATFORM == "windows":
        print(
            "\n提示：未签名的安装器会触发 SmartScreen 警告；"
            "分发前可用代码签名证书（tauri 支持 Windows 的 signCommand 或 EV 证书）。",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
