"""处理层：规则预筛（便宜）+ LLM 精判抽取（准）。"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta

import httpx

from .categories import CATEGORY_KEYS, DEFAULT_CATEGORY, as_prompt_list, classify
from .collectors import RawItem
from .db import CST

log = logging.getLogger("notice-hub.analyze")

# 匹配 9月20日 / 2026年9月20日 / 09-20 / 2026-09-20
_DATE_PATTERNS = [
    re.compile(r"(20\d{2})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})"),
    re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]"),
]
_DEADLINE_HINTS = ("截止", "报名时间", "办理时间", "申请时间", "有效期至", "前完成", "之前")


def _find_near_deadline(text: str, urgent_days: int) -> str:
    """找到离今天最近的、在 urgent_days 天内的日期，返回 YYYY-MM-DD。"""
    today = datetime.now(CST).date()
    found: list[datetime.date] = []
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            groups = match.groups()
            try:
                if len(groups) == 3:
                    year, month, day = (int(g) for g in groups)
                else:
                    month, day = (int(g) for g in groups)
                    year = today.year
                found.append(datetime(year, month, day).date())
            except ValueError:
                continue
    near = [d for d in found if 0 <= (d - today).days <= urgent_days]
    if not near:
        return ""
    return min(near).isoformat()


def _age_days(published_at: str) -> int | None:
    """发布日期距今多少天。拿不到日期返回 None。"""
    if not published_at:
        return None
    try:
        published = datetime.fromisoformat(str(published_at).strip()).date()
    except ValueError:
        return None
    return (datetime.now(CST).date() - published).days


def _is_future(date_str: str) -> bool:
    """有效期是否还没到（含今天）。空值返回 False。"""
    if not date_str:
        return False
    try:
        target = datetime.fromisoformat(str(date_str).strip()).date()
    except ValueError:
        return False
    return target >= datetime.now(CST).date()


def _has_future_date(text: str, days: int, reference: str = "") -> str:
    """正文里有没有"未来 N 天内"的日期。有就说明这条通知还没过期。

    例：10 天前发的通知，里面写着"9月25日前完成"，那它今天依然有用。

    ★ 关键：无年份的日期（"9月26日"）要**相对这条通知的发布日期**推断年份，
    不能一律套当前年。否则去年 9 月的旧预告里提到"9月26日"，
    会被误判成"今年的 9 月 26 日快到了"，于是一年前的旧通知被留在库里。
    """
    if not text or days <= 0:
        return ""
    today = datetime.now(CST).date()
    ref = today
    if reference:
        try:
            ref = datetime.fromisoformat(str(reference).strip()).date()
        except ValueError:
            ref = today

    found: list[datetime.date] = []
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            groups = match.groups()
            try:
                if len(groups) == 3:
                    found.append(datetime(*(int(g) for g in groups)).date())
                else:
                    month, day = (int(g) for g in groups)
                    # 年份按发布日期推断：先试发布当年，早于发布日期就顺延一年
                    for year in (ref.year, ref.year + 1):
                        try:
                            candidate = datetime(year, month, day).date()
                        except ValueError:
                            continue
                        if candidate >= ref:
                            found.append(candidate)
                            break
            except ValueError:
                continue
    upcoming = [d for d in found if 0 <= (d - today).days <= days]
    return min(upcoming).isoformat() if upcoming else ""


def prescreen(item: RawItem, cfg: dict, source_weight: float = 1.0) -> dict:
    """第一道漏斗：关键词规则 + 日期衰减。输出 importance(1-5)、类目、是否丢弃。"""
    flt = cfg["filter"]
    text = f"{item.title}\n{item.content}"
    head = item.title

    l3 = [k for k in flt.get("l3_keywords", []) if k in text]
    l2 = [k for k in flt.get("l2_keywords", []) if k in text]
    drops = [k for k in flt.get("drop_keywords", []) if k in head]

    matched = l3 + l2

    if l3:
        importance, dropped, analysis = 5, False, "rule:l3"
    elif drops:
        importance, dropped, analysis = 1, True, "rule:drop"
    elif l2:
        importance, dropped, analysis = 4, False, "rule:l2"
    else:
        importance, dropped, analysis = 2, False, "rule:none"

    # 截止日期紧迫度加权
    if not dropped and any(h in text for h in _DEADLINE_HINTS):
        deadline = _find_near_deadline(text, int(flt.get("deadline_urgent_days", 3)))
        if deadline:
            importance = min(5, importance + 1)
            matched.append(f"近截止:{deadline}")
            analysis += "+urgent"

    # 来源权重微调（只在中间档起作用，不改变 5 分）
    if not dropped and importance in (2, 3) and source_weight >= 1.5:
        importance = 3
        analysis += "+src"

    # ★ 日期衰减：过期的通知再"重要"也没有意义，一律压到 2 分以下。
    # 4 月的停电停水通知命中了 l3 关键词，但它对今天毫无价值。
    # 两种例外不算过期：
    #   1) 采集器给了 expires_at 且还没到期（讲座：提前一个月发布、下周才开讲）
    #   2) 正文里提到未来 N 天内的日期（"9月25日前完成"）
    #      —— 但通知本身不能太老（stale_exempt_max_age_days），
    #         否则一条 75 天前的旧通知里随便提个未来日期就能永久赖在库里
    age = _age_days(item.published_at)
    decay_days = int(flt.get("importance_decay_days", 7))
    exempt_max_age = int(flt.get("stale_exempt_max_age_days", 45))
    too_old_for_exempt = (age is not None and exempt_max_age >= 0
                          and age > exempt_max_age)
    still_valid = _is_future(item.expires_at)
    upcoming = "" if too_old_for_exempt else _has_future_date(
        text, int(flt.get("upcoming_days", 30)), item.published_at)
    if not dropped and age is not None and decay_days >= 0 and age > decay_days \
            and not still_valid and not upcoming:
        if importance > 2:
            importance = 2
            analysis += f"+过期{age}天"
            matched.append(f"已过期{age}天")
    elif upcoming and age is not None and age > decay_days:
        analysis += "+未到期"
        matched.append(f"未来:{upcoming}")

    # 分类：标题权重更高，所以标题单独算一次
    category, confidence = classify(item.title)
    if category == DEFAULT_CATEGORY or confidence < 0.6:
        body_category, body_conf = classify(text)
        if body_category != DEFAULT_CATEGORY and body_conf > confidence:
            category, confidence = body_category, body_conf

    return {
        "importance": importance,
        "dropped": dropped,
        "matched": matched,
        "analysis": analysis,
        "category": category,
        "category_confidence": confidence,
        "age_days": age,
        "upcoming": upcoming,
    }


SYSTEM_PROMPT_TEMPLATE = """你是北京大学学生的通知助理。你的任务是判断一条信息对一名北大本科新生是否重要，抽取关键字段，并把它归入一个类目。

只输出一个 JSON 对象，不要任何解释文字、不要 markdown 代码块。JSON 字段：
{{
  "importance": 1-5 的整数,
  "category": "从下面的类目表里选一个，必须完全照抄类目名",
  "topic": "更具体的事项，如 选课/四六级/推免/招聘宣讲；不超过 12 字",
  "summary": "一句话摘要，不超过 40 字，说清是什么事",
  "deadline": "截止日期 YYYY-MM-DD；没有就空字符串",
  "audience": "面向谁，如 2026级本科/全体本科生/某院系；看不出来就空字符串",
  "action": "学生需要做什么，不超过 30 字；不需要行动就空字符串",
  "is_notice": true 或 false
}}

重要性评分标准：
5 = 错过会有实际后果：选课、补退选、报名截止、缴费、报到注册、成绩、学籍、体测、军训、停电停水
4 = 应该知道：考试安排、奖学金助学金、评优、竞赛报名、交换项目、培养方案变动
3 = 有点用：讲座、实习信息、一般活动
2 = 日常宣传
1 = 纯宣传/活动回顾/广告/无关内容

类目表（category 只能从这里选）：
{categories}

is_notice=false 表示这不是一则需要学生知晓的通知（例如纯宣传、广告、活动回顾、与学业无关）。"""


def system_prompt() -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(categories=as_prompt_list())


async def llm_judge(item: RawItem, cfg: dict, client: httpx.AsyncClient) -> dict | None:
    """第二道漏斗。失败返回 None，调用方保留预筛结果。"""
    llm = cfg["llm"]
    payload = {
        "model": llm["model"],
        "messages": [
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content": (
                f"来源：{item.source}\n"
                f"标题：{item.title}\n"
                f"正文（截断）：\n{item.content[:3000]}"
            )},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    try:
        resp = await client.post(
            f"{llm['base_url'].rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {llm['api_key']}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=float(llm.get("timeout_seconds", 60)),
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except Exception as exc:
        log.warning("LLM 判定失败 [%s]: %s", item.title[:30], exc)
        return None

    try:
        importance = int(parsed.get("importance", 3))
    except (TypeError, ValueError):
        importance = 3
    importance = max(1, min(5, importance))
    if parsed.get("is_notice") is False:
        importance = min(importance, 2)

    # LLM 给的类目必须在类目表里，否则退回规则分类结果
    category = str(parsed.get("category", "")).strip()
    if category not in CATEGORY_KEYS:
        category = classify(item.title)[0]

    return {
        "importance": importance,
        "category": category,
        "topic": str(parsed.get("topic", ""))[:40],
        "summary": str(parsed.get("summary", ""))[:200],
        "deadline": str(parsed.get("deadline", ""))[:20],
        "audience": str(parsed.get("audience", ""))[:60],
        "action": str(parsed.get("action", ""))[:120],
        "analysis": "llm",
    }


async def judge_batch(items: list[tuple[int, RawItem]], cfg: dict) -> dict[int, dict]:
    """并发调用 LLM，返回 {item_id: result}。受 max_calls_per_run 限制。"""
    from .config import llm_ready

    if not llm_ready(cfg) or not items:
        return {}

    llm = cfg["llm"]
    limit = int(llm.get("max_calls_per_run", 20))
    picked = items[:limit]
    skipped = len(items) - len(picked)
    if skipped > 0:
        log.info("LLM 调用上限 %d，本轮跳过 %d 条", limit, skipped)

    semaphore = asyncio.Semaphore(max(1, int(llm.get("concurrency", 3))))
    out: dict[int, dict] = {}

    async with httpx.AsyncClient(timeout=float(llm.get("timeout_seconds", 60)) + 10) as client:
        async def worker(item_id: int, raw: RawItem) -> None:
            async with semaphore:
                result = await llm_judge(raw, cfg, client)
                if result:
                    out[item_id] = result

        await asyncio.gather(*(worker(i, r) for i, r in picked))
    log.info("LLM 完成 %d 条", len(out))
    return out
