"""通知中枢 · 桌面 App 入口。

把服务跑在后台线程里，然后开一个原生窗口指向它——没有浏览器地址栏、
没有标签页，就是一个独立应用。关掉窗口即退出。

优先用 pywebview（真正的原生窗口，可设图标）；
没有 pywebview 就退回到 Edge/Chrome 的 --app 无边框窗口模式。

用法:
    py -3.12 app_window.py
或双击桌面的「通知中枢」快捷方式（走 scripts\\start-app.bat）
"""
from __future__ import annotations

import logging
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.api import create_app                      # noqa: E402
from app.config import load_config                  # noqa: E402

ICON = ROOT / "assets" / "notice-hub.ico"
WINDOW_TITLE = "通知中枢"
WIDTH, HEIGHT = 1180, 860

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def wait_for_port(host: str, port: int, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def start_server(cfg: dict) -> tuple[str, int]:
    """在后台线程里起 uvicorn，返回 (url, port)。"""
    import uvicorn

    app = create_app(cfg)
    host = cfg["server"].get("host", "127.0.0.1")
    # App 窗口只需要本机访问；手机要连的话改成 0.0.0.0
    bind_host = "0.0.0.0" if cfg["server"].get("lan_access") else "127.0.0.1"
    port = int(cfg["server"].get("port", 8787))

    config = uvicorn.Config(app, host=bind_host, port=port,
                            log_level="warning", access_log=False)
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True, name="uvicorn")
    thread.start()

    if not wait_for_port("127.0.0.1", port):
        raise RuntimeError(f"服务没能在 {port} 端口起来")

    follow = "0.0.0.0" if bind_host == "0.0.0.0" else "127.0.0.1"
    return f"http://{follow}:{port}", port


def open_native_window(url: str) -> bool:
    """用 pywebview 开原生窗口。成功返回 True。"""
    try:
        import webview
    except Exception as exc:
        print(f"  pywebview 不可用（{type(exc).__name__}），改用浏览器 App 窗口")
        return False

    try:
        window = webview.create_window(
            WINDOW_TITLE, url,
            width=WIDTH, height=HEIGHT,
            min_size=(420, 560),
            text_select=True,
        )
        kwargs = {}
        if ICON.exists():
            kwargs["icon"] = str(ICON)
        # 显式指定 edgechromium，Windows 上比默认的 WinForms 后端更稳
        try:
            webview.start(gui="edgechromium", **kwargs)
        except Exception:
            webview.start(**kwargs)
        return True
    except Exception as exc:
        print(f"  原生窗口启动失败（{exc}），改用浏览器 App 窗口")
        return False


def open_edge_app(url: str) -> bool:
    """退路：用 Edge/Chrome 的 --app 模式开一个无边框窗口。"""
    import subprocess

    exe = next((p for p in EDGE_CANDIDATES if Path(p).exists()), None)
    if not exe:
        return False
    profile = ROOT / "data" / "app-profile"
    subprocess.Popen([
        exe,
        f"--app={url}",
        f"--window-size={WIDTH},{HEIGHT}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
    ])
    return True


def main() -> int:
    cfg = load_config()
    setup_logging(cfg.get("log_level", "INFO"))

    print("=" * 60)
    print(f"  {WINDOW_TITLE} · 桌面版")
    print("=" * 60)

    url, port = start_server(cfg)
    print(f"  服务已就绪: {url}")
    if cfg["server"].get("lan_access"):
        import socket as _s
        try:
            probe = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
            probe.connect(("8.8.8.8", 80))
            print(f"  局域网访问: http://{probe.getsockname()[0]}:{port}")
            probe.close()
        except Exception:
            pass

    if open_native_window(url):
        print("  窗口已关闭，退出。")
        return 0

    if open_edge_app(url):
        print("  已用浏览器 App 窗口打开。")
        print("  关闭那个窗口后，回到这里按 Ctrl+C 结束服务。")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return 0

    print("  没找到可用的窗口方案，请手动打开：", url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
