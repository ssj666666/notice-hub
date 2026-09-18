"""启动入口。

用法:
    py -3.12 run.py
或直接双击  scripts\\start.bat
"""
from __future__ import annotations

import logging
import sys
import threading
import webbrowser

from app import __version__
from app.api import create_app
from app.config import ROOT, load_config


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def local_ip() -> str:
    import socket
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main() -> None:
    cfg = load_config()
    setup_logging(cfg.get("log_level", "INFO"))
    log = logging.getLogger("notice-hub")

    host = cfg["server"]["host"]
    port = int(cfg["server"]["port"])
    app = create_app(cfg)

    banner = f"""
============================================================
  通知中枢 notice-hub v{__version__}
------------------------------------------------------------
  本机访问 : http://127.0.0.1:{port}
  手机访问 : http://{local_ip()}:{port}     (需同一网络)
  数据目录 : {ROOT / 'data'}
------------------------------------------------------------
  首次使用请先编辑 config.yaml 添加信息源
  按 Ctrl+C 停止
============================================================
"""
    print(banner)
    log.info("日志级别 %s，抓取间隔 %s 分钟", cfg.get("log_level"), cfg.get("poll_interval_minutes"))

    if cfg["server"].get("open_browser", True):
        threading.Timer(2.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()

    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
