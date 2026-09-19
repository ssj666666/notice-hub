"""FastAPI 应用：JSON API + 静态看板 + 定时调度。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .categories import (CAMPUSES, CATEGORY_KEYS, DEFAULT_CATEGORY, DESCS,
                         ICONS)
from .config import (ROOT, enabled_sources, load_config, load_custom_sources,
                     llm_ready, save_custom_sources, wechat_ready)
from .db import Database
from .notify import WeChatPusher
from .pipeline import Pipeline

log = logging.getLogger("notice-hub.api")
WEB_DIR = Path(__file__).resolve().parent / "web"


def create_app(cfg: dict) -> FastAPI:
    db = Database(ROOT / "data" / "noticehub.db")
    pipeline = Pipeline(cfg, db)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 启动时先清一次旧条目、再按当前配置重算一遍
        try:
            removed = pipeline.purge()
            if removed:
                log.info("启动清理：删除 %d 条超过保留期的旧条目", removed)
        except Exception as exc:
            log.warning("启动清理失败: %s", exc)
        try:
            pipeline.recompute_all()
        except Exception as exc:  # 重算失败不该拦住服务
            log.warning("启动重算失败: %s", exc)

        scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        minutes = max(1, int(cfg.get("poll_interval_minutes", 10)))
        scheduler.add_job(pipeline.run_once, "interval", minutes=minutes,
                          id="poll", max_instances=1, coalesce=True)
        # 每天凌晨 4 点清一次库（run_once 里其实每轮也会清，这里是兜底）
        scheduler.add_job(pipeline.purge, "cron", hour=4, minute=0, id="purge")
        scheduler.start()
        log.info("调度器已启动，每 %d 分钟抓取一次", minutes)
        # 启动 3 秒后先跑一轮，让看板马上有东西
        scheduler.add_job(pipeline.run_once, "date", id="boot", run_date=None)
        try:
            yield
        finally:
            scheduler.shutdown(wait=False)
            db.close()

    app = FastAPI(title="通知中枢", version="1.0.0", lifespan=lifespan)

    # ---------------- API ----------------
    @app.get("/api/items")
    def list_items(
        limit: int = Query(100, ge=1, le=500),
        min_importance: int = Query(1, ge=1, le=5),
        unread_only: bool = False,
        starred_only: bool = False,
        include_dropped: bool = False,
        include_stale: bool = False,
        campus: str = "",
        category: str = "",
        sort: str = Query("importance", pattern="^(importance|time)$"),
        q: str = "",
    ):
        return {
            "items": db.list_items(limit, min_importance, unread_only,
                                   include_dropped, starred_only, q, category,
                                   include_stale, sort, campus)
        }

    @app.get("/api/campuses")
    def campuses(min_importance: int = Query(1, ge=1, le=5),
                 unread_only: bool = False,
                 include_stale: bool = False):
        """一级分类（校区）。看板顶部用这个分两大块。"""
        counts = {row["campus"]: row for row in
                  db.campus_counts(min_importance, unread_only, include_stale)}
        out = []
        for meta in CAMPUSES:
            row = counts.get(meta["key"])
            out.append({
                "key": meta["key"],
                "icon": meta["icon"],
                "desc": meta["desc"],
                "count": int(row["n"]) if row else 0,
                "unread": int(row["unread"]) if row else 0,
                "important": int(row["important"]) if row else 0,
            })
        return {"campuses": out}

    @app.get("/api/categories")
    def categories(min_importance: int = Query(1, ge=1, le=5),
                   unread_only: bool = False,
                   include_stale: bool = False,
                   campus: str = ""):
        counts = {row["category"]: row for row in
                  db.category_counts(min_importance, unread_only, include_stale,
                                     campus)}
        out = []
        for key in CATEGORY_KEYS + [DEFAULT_CATEGORY]:
            row = counts.get(key)
            out.append({
                "key": key,
                "icon": ICONS.get(key, "📌"),
                "desc": DESCS.get(key, ""),
                "count": int(row["n"]) if row else 0,
                "unread": int(row["unread"]) if row else 0,
                "important": int(row["important"]) if row else 0,
            })
        return {"categories": out}

    @app.get("/api/items/{item_id}")
    def get_item(item_id: int):
        item = db.get_item(item_id)
        if not item:
            raise HTTPException(404, "not found")
        return item

    @app.post("/api/items/{item_id}/flag")
    def set_flag(item_id: int, flag: str = Body(..., embed=True),
                 value: bool = Body(True, embed=True)):
        if not db.get_item(item_id):
            raise HTTPException(404, "not found")
        try:
            db.set_flag(item_id, flag, value)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/items/read-all")
    def read_all():
        db.mark_all_read()
        return {"ok": True}

    @app.get("/api/stats")
    def stats():
        data = db.stats()
        data["sources"] = [
            {"name": s.get("name", "?"), "type": s["_type"]}
            for s in enabled_sources(cfg)
        ]
        data["llm_enabled"] = llm_ready(cfg)
        data["wechat_enabled"] = wechat_ready(cfg)
        # 保留策略：超过这个天数的会被清掉（星标除外）
        data["retention_days"] = int(cfg["filter"].get("retention_days", 14))
        data["starred"] = db.starred_count()
        oldest = db.oldest_item()
        data["oldest"] = (oldest or {}).get("published_at") or \
                         ((oldest or {}).get("fetched_at") or "")[:10]
        return data

    @app.get("/api/runs")
    def runs(limit: int = 10):
        return {"runs": db.recent_runs(limit)}

    @app.get("/api/sources")
    def sources():
        """每个源的抓取情况：抓了多少、多少可见、最新一条是什么时候。"""
        stats = {row["source"]: row for row in db.source_stats()}
        # 变化检测模式的源，首轮只建基线、本来就不产出条目
        listening = {
            s.get("name") for s in enabled_sources(cfg)
            if s["_type"] == "web" and not s.get("item_selector")
            and not s.get("auto_detect")
        }
        out = []
        for src in enabled_sources(cfg):
            name = src.get("name", "?")
            row = stats.get(name, {})
            newest_age = row.get("newest_age_days")
            if not row:
                if name in listening:
                    status, note = "监听中", "变化检测模式：首轮只建基线，页面有新内容才产出"
                else:
                    status, note = "无数据", "还没抓到任何条目，检查地址或看控制台日志"
            elif row.get("visible", 0) > 0:
                status, note = "活跃", ""
            elif newest_age is None:
                status, note = "无日期", "抓到了但没有日期，无法判断新旧"
            elif newest_age > 60:
                status, note = "停更", f"最新一条是 {newest_age} 天前，这个站可能很久没更新了"
            else:
                status, note = "全部过期", f"最新一条是 {newest_age} 天前，已超出显示范围"
            out.append({
                "name": name,
                "type": src["_type"],
                "url": src.get("url", ""),
                "status": status,
                "note": note,
                "total": row.get("total", 0),
                "visible": row.get("visible", 0),
                "dropped": row.get("dropped", 0),
                "newest": row.get("newest") or "",
                "newest_age_days": newest_age,
                "last_fetch": row.get("last_fetch", ""),
            })
        out.sort(key=lambda r: (-(r["visible"] or 0), r["name"]))
        enabled = {s.get("name") for s in enabled_sources(cfg)}
        disabled = [
            {"name": s.get("name", "?"), "url": s.get("url", "")}
            for group in ("rss", "imap", "web", "api")
            for s in (cfg["sources"].get(group) or [])
            if not s.get("enabled", True) and s.get("name") not in enabled
        ]
        return {"sources": out, "disabled": disabled}

    @app.post("/api/run")
    async def run_now():
        # 手动触发要强制抓取，忽略各源的 interval_minutes
        result = await pipeline.run_once(force=True)
        return {"ok": True, "result": result, "stats": db.stats()}

    @app.post("/api/discover")
    async def discover_source(payload: dict = Body(...)):
        """给一个网址，判断该怎么抓，返回建议的源配置。"""
        from .discover import discover

        url = str(payload.get("url", "")).strip()
        async with httpx.AsyncClient(follow_redirects=True) as client:
            return await discover(client, url)

    @app.get("/api/custom-sources")
    def list_custom():
        custom = load_custom_sources()
        return {
            "sources": [
                {**s, "type": stype}
                for stype, items in custom.items() for s in items
            ]
        }

    @app.post("/api/custom-sources")
    async def add_custom(payload: dict = Body(...)):
        """把探测结果（或手工填的配置）存成新源，并立刻抓一次。"""
        src = dict(payload)
        stype = str(src.pop("type", "web"))
        if stype not in ("web", "api", "rss", "imap"):
            raise HTTPException(400, f"不支持的源类型：{stype}")
        src.pop("_custom", None)
        src.pop("_type", None)
        name = str(src.get("name", "")).strip()
        url = str(src.get("url", "")).strip()
        if not name or not url:
            raise HTTPException(400, "name 和 url 都不能为空")
        src["name"] = name
        src["url"] = url
        src.setdefault("enabled", True)
        src.setdefault("weight", 1.2)

        custom = load_custom_sources()
        bucket = custom.setdefault(stype, [])
        bucket[:] = [s for s in bucket if s.get("name") != name]   # 同名覆盖
        bucket.append(src)
        save_custom_sources(custom)

        # 让内存里的配置立刻生效
        fresh_cfg = load_config()
        cfg.clear()
        cfg.update(fresh_cfg)

        result = await pipeline.fetch_source(name)
        return {"ok": True, "added": {**src, "type": stype},
                "result": result, "stats": db.stats()}

    @app.delete("/api/custom-sources")
    def delete_custom(name: str = Query(...)):
        custom = load_custom_sources()
        removed = False
        for stype, items in custom.items():
            keep = [s for s in items if s.get("name") != name]
            if len(keep) != len(items):
                removed = True
                custom[stype] = keep
        if not removed:
            raise HTTPException(404, f"没有这个自定义源：{name}")
        save_custom_sources(custom)
        fresh_cfg = load_config()
        cfg.clear()
        cfg.update(fresh_cfg)
        return {"ok": True}

    @app.post("/api/fetch-source")
    async def fetch_source(payload: dict = Body(...)):
        return await pipeline.fetch_source(str(payload.get("name", "")))

    @app.post("/api/purge")
    def purge_now():
        """手动清理超过保留期的条目（星标保留）。"""
        removed = pipeline.purge()
        return {"ok": True, "removed": removed,
                "retention_days": int(cfg["filter"].get("retention_days", 14)),
                "stats": db.stats()}

    @app.post("/api/recompute")
    def recompute():
        """改过 config.yaml 的关键词/日期阈值后，调这个让历史条目重新判定。"""
        n = pipeline.recompute_all()
        return {"ok": True, "checked": n, "stats": db.stats()}

    @app.post("/api/test-push")
    async def test_push():
        """不依赖采集，直接发一条测试消息，用来验证微信通道。"""
        if not wechat_ready(cfg):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "微信通道未配置完成（config.yaml 的 notify.wechat）"},
            )
        ok = await WeChatPusher(cfg).send(
            "通道测试",
            "这条消息来自通知中枢的测试按钮。\n如果你在微信里看到它，说明推送已通。",
            "", 5,
        )
        return {"ok": ok}

    @app.get("/api/health")
    def health():
        return {"ok": True, "stats": db.stats()}

    # ---------------- 静态看板 ----------------
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
    return app
