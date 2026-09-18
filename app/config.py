"""配置加载：默认值 + config.yaml 深合并。"""
from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
EXAMPLE_PATH = ROOT / "config.example.yaml"
# 通过网页界面添加的源存这里，不动你手写的 config.yaml
CUSTOM_PATH = ROOT / "data" / "custom_sources.yaml"

DEFAULTS: dict = {
    "server": {"host": "0.0.0.0", "port": 8787, "open_browser": True},
    "poll_interval_minutes": 10,
    "log_level": "INFO",
    "sources": {"rss": [], "imap": [], "web": [], "api": []},
    "filter": {
        "l3_keywords": [],
        "l2_keywords": [],
        "drop_keywords": [],
        "deadline_urgent_days": 3,
        "importance_decay_days": 7,
        "visible_days": 60,
        # 正文里提到未来 N 天内的日期 → 认为这条还没过期，不降级也不隐藏
        "upcoming_days": 30,
    },
    "llm": {
        "enabled": False,
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "api_key": "",
        "model": "deepseek-chat",
        "min_importance_for_llm": 4,
        "max_calls_per_run": 20,
        "timeout_seconds": 60,
        "concurrency": 3,
    },
    "notify": {
        "min_importance": 4,
        "instant_push_max_per_day": 8,
        "wechat": {
            "enabled": False,
            "appid": "",
            "appsecret": "",
            "openid": "",
            "template_id": "",
        },
        "quiet_hours": {
            "enabled": False,
            "start": "23:30",
            "end": "06:30",
            "allow_min_importance": 5,
        },
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _env_override(cfg: dict) -> dict:
    """让密钥可以走环境变量，避免写进配置文件。"""
    wx = cfg["notify"]["wechat"]
    mapping = {
        "appid": "NOTICE_HUB_WX_APPID",
        "appsecret": "NOTICE_HUB_WX_SECRET",
        "openid": "NOTICE_HUB_WX_OPENID",
        "template_id": "NOTICE_HUB_WX_TEMPLATE",
    }
    for field, env_name in mapping.items():
        if not wx.get(field) and os.environ.get(env_name):
            wx[field] = os.environ[env_name]

    if not cfg["llm"].get("api_key") and os.environ.get("NOTICE_HUB_LLM_KEY"):
        cfg["llm"]["api_key"] = os.environ["NOTICE_HUB_LLM_KEY"]

    if os.environ.get("NOTICE_HUB_LLM_MODEL"):
        cfg["llm"]["model"] = os.environ["NOTICE_HUB_LLM_MODEL"]
    if os.environ.get("NOTICE_HUB_LLM_BASE_URL"):
        cfg["llm"]["base_url"] = os.environ["NOTICE_HUB_LLM_BASE_URL"]
    return cfg


def ensure_config_file() -> Path:
    """首次运行时从模板生成 config.yaml。"""
    if not CONFIG_PATH.exists() and EXAMPLE_PATH.exists():
        CONFIG_PATH.write_text(EXAMPLE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    return CONFIG_PATH


def load_custom_sources() -> dict:
    """读取网页界面上添加的源。"""
    if not CUSTOM_PATH.exists():
        return {}
    try:
        data = yaml.safe_load(CUSTOM_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items()
            if k in ("rss", "imap", "web", "api") and isinstance(v, list)}


def save_custom_sources(data: dict) -> None:
    CUSTOM_PATH.parent.mkdir(parents=True, exist_ok=True)
    CUSTOM_PATH.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


def load_config(path: str | os.PathLike | None = None) -> dict:
    env_path = os.environ.get("NOTICE_HUB_CONFIG")
    target = Path(path) if path else (Path(env_path) if env_path else ensure_config_file())

    user_cfg: dict = {}
    if target.exists():
        loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            user_cfg = loaded

    cfg = _env_override(_deep_merge(DEFAULTS, user_cfg))

    # 把手写配置和网页添加的源合起来
    custom = load_custom_sources()
    for stype, items in custom.items():
        bucket = cfg["sources"].setdefault(stype, [])
        existing = {s.get("name") for s in bucket}
        for item in items:
            if not isinstance(item, dict) or item.get("name") in existing:
                continue
            item = dict(item)
            item["_custom"] = True
            bucket.append(item)
    return cfg


def llm_ready(cfg: dict) -> bool:
    return bool(cfg["llm"]["enabled"] and cfg["llm"]["api_key"])


def wechat_ready(cfg: dict) -> bool:
    wx = cfg["notify"]["wechat"]
    return bool(wx["enabled"] and wx["appid"] and wx["appsecret"]
                and wx["openid"] and wx["template_id"])


def enabled_sources(cfg: dict) -> list[dict]:
    out = []
    for stype in ("rss", "imap", "web", "api"):
        for src in cfg["sources"].get(stype) or []:
            if src.get("enabled", True):
                src = dict(src)
                src["_type"] = stype
                out.append(src)
    return out
