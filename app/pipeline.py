"""流程编排：采集 → 去重入库 → 规则预筛 → LLM 精判 → 分级推送。"""
from __future__ import annotations

import logging
from datetime import datetime, time as dtime

import httpx

from .analyze import _is_future, judge_batch, prescreen
from .categories import DEFAULT_CAMPUS, campus_from_name
from .collectors import collect_all
from .db import CST, Database, now_iso
from .notify import WeChatPusher, build_message

log = logging.getLogger("notice-hub.pipeline")


def _parse_date(value: str):
    """把 published_at 解析成 date。

    采集层给的可能是 '2026-09-10'（纯日期），也可能是 ISO 带时区的字符串，
    还可能是 RSS 那种 RFC822。统一处理，失败返回 None。
    """
    if not value:
        return None
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(text).date()
    except Exception:
        return None


def _in_quiet_hours(cfg: dict, moment: datetime | None = None) -> bool:
    qh = cfg["notify"].get("quiet_hours", {})
    if not qh.get("enabled"):
        return False
    moment = moment or datetime.now(CST)

    def parse(value: str) -> dtime:
        hour, _, minute = str(value).partition(":")
        return dtime(int(hour), int(minute or 0))

    try:
        start, end = parse(qh["start"]), parse(qh["end"])
    except Exception:
        return False
    current = moment.time()
    if start <= end:
        return start <= current < end
    return current >= start or current < end  # 跨午夜


class Pipeline:
    def __init__(self, cfg: dict, db: Database):
        self.cfg = cfg
        self.db = db
        self.pusher = WeChatPusher(cfg)
        self._running = False

    def recompute_all(self) -> int:
        """按**当前** config.yaml 重新判定所有历史条目。

        重要度和过期状态是入库时算好存下来的，所以改了关键词表或日期阈值之后，
        必须重算一次才能生效——否则你得清库重抓。启动时自动跑一次。
        """
        from .collectors import RawItem

        weights = {}
        for group in ("rss", "imap", "web", "api"):
            for src in self.cfg["sources"].get(group) or []:
                weights[src.get("name", "")] = float(src.get("weight", 1.0))

        visible_days = int(self.cfg["filter"].get("visible_days", 60))
        default_cats: dict[str, str] = {}
        campuses: dict[str, str] = {}
        for group in ("rss", "imap", "web", "api"):
            for src in self.cfg["sources"].get(group) or []:
                name = src.get("name", "")
                if src.get("default_category"):
                    default_cats[name] = str(src["default_category"])
                campuses[name] = str(src.get("campus") or campus_from_name(name))
        rows = self.db.all_items()
        changed = 0
        for row in rows:
            raw = RawItem(
                source=row["source"], source_type=row["source_type"],
                title=row["title"], url=row.get("url") or "",
                content=row.get("content") or "",
                published_at=row.get("published_at") or "",
            )
            verdict = prescreen(raw, self.cfg, weights.get(raw.source, 1.0))
            fallback = default_cats.get(raw.source)
            if fallback and verdict["category"] == "其他":
                verdict["category"] = fallback
            campus = campuses.get(raw.source, DEFAULT_CAMPUS)
            age = verdict.get("age_days")
            still_valid = bool(verdict.get("upcoming"))
            is_stale = (age is not None and visible_days >= 0
                        and age > visible_days and not still_valid)
            if (row["importance"] != verdict["importance"]
                    or (row.get("category") or "") != verdict["category"]
                    or (row.get("campus") or "") != campus
                    or bool(row["dropped"]) != bool(verdict["dropped"])
                    or bool(row["is_stale"]) != is_stale):
                changed += 1
            self.db.update_verdict(int(row["id"]), verdict["importance"],
                                   verdict["category"], verdict["analysis"],
                                   verdict["dropped"], is_stale, campus)
        if changed:
            log.info("重算完成：%d/%d 条判定发生变化", changed, len(rows))
        return len(rows)

    async def fetch_source(self, name: str) -> dict:
        """只抓某一个源。网页上新增源之后用它立刻验证，不用等整轮跑完。"""
        from .config import enabled_sources
        from .collectors import collect_all

        sources = [s for s in enabled_sources(self.cfg) if s.get("name") == name]
        if not sources:
            return {"error": f"找不到源：{name}"}
        async with httpx.AsyncClient(follow_redirects=True, verify=True) as safe, \
                httpx.AsyncClient(follow_redirects=True, verify=False) as lax:
            items = await collect_all(self.cfg, self.db,
                                      {True: safe, False: lax}, sources)
        fresh = self._store(items)
        self.db.mark_fetched(name)
        return {"collected": len(items), "new": len(fresh), "source": name}

    async def run_once(self, force: bool = False) -> dict:
        if self._running:
            log.info("上一轮还在跑，跳过本次触发")
            return {"skipped": True}
        self._running = True
        run_id = self.db.start_run()
        notes: list[str] = []
        llm_calls = 0
        try:
            collected = await self._collect(force=force)
            new_items = self._store(collected)
            llm_calls = await self._analyze(new_items)
            pushed = await self._push()
            self.db.finish_run(run_id, len(collected), len(new_items), pushed,
                               llm_calls, "; ".join(notes))
            summary = {
                "collected": len(collected), "new": len(new_items),
                "pushed": pushed, "llm_calls": llm_calls,
            }
            log.info("一轮完成: %s", summary)
            return summary
        except Exception as exc:
            log.exception("一轮执行失败")
            self.db.finish_run(run_id, 0, 0, 0, llm_calls, f"ERROR: {exc}")
            return {"error": str(exc)}
        finally:
            self._running = False

    def _due_sources(self, force: bool = False) -> list[dict]:
        """挑出这一轮该抓的源。

        每个源可以有自己的 interval_minutes（比如讲座类设 5 分钟，
        教务类设 30 分钟），没到点的这轮就跳过，避免白白请求。
        force=True 时忽略间隔全部抓取（手动点「立即抓取」用这个）。
        """
        from .config import enabled_sources

        default_interval = max(1, int(self.cfg.get("poll_interval_minutes", 10)))
        now = datetime.now(CST)
        due, waiting = [], 0
        for src in enabled_sources(self.cfg):
            if not force:
                interval = int(src.get("interval_minutes") or default_interval)
                last = self.db.last_fetch(src.get("name", ""))
                if last:
                    try:
                        elapsed = (now - datetime.fromisoformat(last)).total_seconds() / 60
                        if elapsed < interval:
                            waiting += 1
                            continue
                    except ValueError:
                        pass
            due.append(src)
        if waiting:
            log.info("有 %d 个源还没到抓取间隔，本轮跳过", waiting)
        return due

    async def _collect(self, force: bool = False):
        due = self._due_sources(force)
        if not due:
            log.info("没有任何源到点，本轮不抓取")
            return []
        # 两个客户端：默认校验证书；证书链不完整的站（需 verify_ssl: false）走另一个
        async with httpx.AsyncClient(follow_redirects=True, verify=True) as safe, \
                httpx.AsyncClient(follow_redirects=True, verify=False) as lax:
            items = await collect_all(self.cfg, self.db,
                                      {True: safe, False: lax}, due)
        for src in due:
            self.db.mark_fetched(src.get("name", ""))
        return items

    def _store(self, collected) -> list[tuple[int, object]]:
        """入库并返回 (item_id, raw) 列表，只含真正的新条目。"""
        weights = {}
        change_detect = set()   # 自己处理了基线的源，流水线不要再抑制一次
        default_cats: dict[str, str] = {}   # 整站就是某一类的源（讲座网=讲座讲坛）
        campuses: dict[str, str] = {}       # 源 → 一级分类（北大本部 / 医学部）
        for group in ("rss", "imap", "web", "api"):
            for src in self.cfg["sources"].get(group) or []:
                name = src.get("name", "")
                weights[name] = float(src.get("weight", 1.0))
                if src.get("default_category"):
                    default_cats[name] = str(src["default_category"])
                campuses[name] = str(src.get("campus") or campus_from_name(name))
                if group == "web" and not src.get("item_selector") \
                        and not src.get("auto_detect"):
                    change_detect.add(name)

        # 首次接入某个源时，把这个源已有的历史条目当作基线：
        # 存进看板可查，但绝不推送（否则一接教务页就被 13 条旧通知炸掉）。
        baseline: dict[str, bool] = {}
        for raw in collected:
            if raw.source in baseline:
                continue
            if raw.source in change_detect:
                baseline[raw.source] = False
                continue
            key = f"src::{raw.source_type}::{raw.source}"
            if self.db.get_snapshot(key) is None:
                self.db.set_snapshot(key, now_iso())
                baseline[raw.source] = True
                log.info("源 [%s] 首次接入：本批 %d 条记作基线，不推送",
                         raw.source, sum(1 for r in collected if r.source == raw.source))
            else:
                baseline[raw.source] = False

        fresh: list[tuple[int, object]] = []
        visible_days = int(self.cfg["filter"].get("visible_days", 30))
        decay_days = int(self.cfg["filter"].get("importance_decay_days", 7))
        stale_count = decay_count = 0
        for raw in collected:
            verdict = prescreen(raw, self.cfg, weights.get(raw.source, raw.weight))

            # 整站性质明确的（讲座网整站都是讲座），规则分类拿不准时用源默认类目
            fallback = default_cats.get(raw.source)
            if fallback and verdict["category"] == "其他":
                verdict["category"] = fallback

            # 过期闸门：发布日期超过 visible_days 的，看板默认隐藏且永不推送。
            # 两种例外：① 有效期还没到（讲座提前发布，下周才开讲）
            #           ② 正文里提到未来 N 天内的日期（"9月25日前完成"）
            age = verdict.get("age_days")
            still_valid = _is_future(raw.expires_at) or bool(verdict.get("upcoming"))
            is_stale = (age is not None and visible_days >= 0
                        and age > visible_days and not still_valid)
            if is_stale:
                stale_count += 1
            if (age is not None and decay_days >= 0 and age > decay_days
                    and not still_valid):
                decay_count += 1

            item_id = self.db.insert_item({
                "uid": raw.uid,
                "source": raw.source,
                "source_type": raw.source_type,
                "title": raw.title,
                "url": raw.url,
                "content": raw.content,
                "published_at": raw.published_at,
                "importance": verdict["importance"],
                "campus": campuses.get(raw.source, DEFAULT_CAMPUS),
                "category": verdict["category"],
                "analysis": verdict["analysis"] + ("+stale" if is_stale else ""),
                "matched": verdict["matched"],
                "dropped": verdict["dropped"],
                "is_baseline": baseline.get(raw.source, False),
                "is_stale": is_stale,
            })
            if item_id is not None:
                fresh.append((item_id, raw))
        if stale_count:
            log.info("其中 %d 条超过 %d 天，标记为过期（隐藏且不推送）",
                     stale_count, visible_days)
        if decay_count:
            log.info("其中 %d 条超过 %d 天，重要度已降级", decay_count, decay_days)
        log.info("新条目 %d 条（其余为重复）", len(fresh))
        return fresh

    async def _analyze(self, fresh: list[tuple[int, object]]) -> int:
        """对达到门槛的新条目调用 LLM。"""
        from .config import llm_ready

        if not llm_ready(self.cfg) or not fresh:
            return 0
        threshold = int(self.cfg["llm"].get("min_importance_for_llm", 4))

        candidates: list[tuple[int, object]] = []
        for item_id, raw in fresh:
            row = self.db.get_item(item_id)
            if row and not row["dropped"] and row["importance"] >= threshold:
                candidates.append((item_id, raw))
        if not candidates:
            return 0

        results = await judge_batch(candidates, self.cfg)
        for item_id, result in results.items():
            self.db.update_analysis(item_id, result)
        return len(results)

    async def _push(self) -> int:
        from .config import wechat_ready

        if not wechat_ready(self.cfg):
            # 通道没配好就不要标记 pushed，否则以后配好了这些条目再也推不出去
            log.info("微信通道未启用，跳过推送（条目保留在看板里）")
            return 0

        notify = self.cfg["notify"]
        threshold = int(notify.get("min_importance", 4))
        cap = int(notify.get("instant_push_max_per_day", 8))
        already = self.db.pushed_count_today()
        quiet = _in_quiet_hours(self.cfg)
        quiet_min = int(notify.get("quiet_hours", {}).get("allow_min_importance", 5))

        pushed = 0
        failures = 0
        for item in self.db.candidates_for_push(threshold):
            if already + pushed >= cap:
                log.info("已达每日即时推送上限 %d，其余留给看板/日报", cap)
                break
            if quiet and int(item["importance"]) < quiet_min:
                log.info("静默时段，跳过: %s", item["title"][:30])
                continue
            title, content, url = build_message(item)
            ok = await self.pusher.send(title, content, url, int(item["importance"]))
            if ok:
                self.db.mark_pushed(int(item["id"]))
                pushed += 1
            else:
                failures += 1
                if failures >= 3:
                    log.warning("连续推送失败 3 次，本轮中止（下轮会重试）")
                    break
        return pushed
