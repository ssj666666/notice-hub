"""自动识别网页的列表项结构，以及从任意文本里抽日期。"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

# ---------- 日期抽取 ----------
_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}

_PATTERNS: list[tuple[re.Pattern, str]] = [
    # 2026-09-10 / 2026/09/10 / 2026.09.10
    (re.compile(r"(20\d{2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})"), "ymd"),
    # 2026年9月10日
    (re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?"), "ymd"),
    # 17 Nov 2025 / 17 Nov, 2025
    (re.compile(r"(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?,?\s*(20\d{2})",
                re.I), "dmy_en"),
    # Nov 17, 2025
    (re.compile(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),?\s*(20\d{2})",
                re.I), "mdy_en"),
    # 14 Aug（医学部列表页用的是这个，没有年份）
    (re.compile(r"(?<!\d)(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?(?!\s*\d)",
                re.I), "dm_en_noyear"),
    # 09-10（没年份）
    (re.compile(r"(?<!\d)(\d{1,2})\s*[-/]\s*(\d{1,2})(?!\d)"), "md"),
    # 9月10日（没年份）
    (re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日"), "md"),
]


def _resolve_year(month: int, day: int, today) -> int:
    """没有年份时推断年份。

    关键：这是**发布日期**，不可能在未来。所以只要算出来比今天晚（哪怕一天），
    就说明那是去年同月同日 —— 很多列表页会把年份省略，而列表会跨越一年。
    例：今天 2026-09-18，页面写「22 Sep」，那实际是 2025-09-22。
    """
    try:
        candidate = today.replace(year=today.year, month=month, day=day)
    except ValueError:
        return today.year
    if (candidate - today).days > 0:
        return today.year - 1
    return today.year


def extract_date(text: str, default_year: int | None = None) -> str:
    """从一段文本里抽出最像发布日期的那个，返回 YYYY-MM-DD；抽不到返回空串。"""
    if not text:
        return ""
    from datetime import date, datetime

    from .db import CST

    today: date = datetime.now(CST).date()

    for pattern, kind in _PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        try:
            if kind == "ymd":
                y, m, d = (int(g) for g in match.groups())
            elif kind == "dmy_en":
                d = int(match.group(1))
                m = _MONTHS[match.group(2).lower()[:3]]
                y = int(match.group(3))
            elif kind == "mdy_en":
                m = _MONTHS[match.group(1).lower()[:3]]
                d = int(match.group(2))
                y = int(match.group(3))
            elif kind == "dm_en_noyear":
                d = int(match.group(1))
                m = _MONTHS[match.group(2).lower()[:3]]
                y = default_year or _resolve_year(m, d, today)
            else:  # md
                m, d = (int(g) for g in match.groups())
                y = default_year or _resolve_year(m, d, today)
            return datetime(y, m, d).strftime("%Y-%m-%d")
        except (ValueError, KeyError):
            continue
    return ""


# ---------- 列表结构识别 ----------
DATE_HINT = re.compile(r"20\d{2}\s*[-/年.]\s*\d{1,2}\s*[-/月.]\s*\d{1,2}")


def detect_selectors(html: str, top: int = 5) -> list[dict]:
    """找出页面上重复出现、每条含"像通知标题的链接"的元素。

    分三种分组方式，按优先级尝试（很多高校站的 <li> 没有 class，只有 id）：
      1. 按 class 分组        → div.notice_item
      2. 按 id 前缀分组       → li[id^=line_u]   （id="line_u11_0" 这种）
      3. 按标签名兜底         → li / tr / dd（要求更严，避免选中导航）
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    groups: dict[tuple[str, str], dict] = {}

    def add(key: tuple[str, str], selector: str, el) -> None:
        slot = groups.setdefault(key, {"sel": selector, "els": []})
        if isinstance(el, (list, tuple)):
            slot["els"].extend(el)
        else:
            slot["els"].append(el)

    for el in soup.find_all(True):
        classes = el.get("class")
        if classes:
            cls = ".".join(classes)
            add((el.name, "cls:" + cls), f"{el.name}.{cls}", el)
            continue
        el_id = el.get("id")
        if el_id:
            m = re.match(r"([A-Za-z_][A-Za-z_\-]{2,})", str(el_id))
            if m:
                prefix = m.group(1)
                add((el.name, "id:" + prefix), f"{el.name}[id^={prefix}]", el)
            continue
        # 既没 class 也没 id：往上找最近的"带 class 的祖先"，
        # 按 (祖先, 标签) 分组。这样 div.list30 > ul > li 这种孙节点也能覆盖。
        if el.name in ("li", "tr", "dd", "article"):
            ancestor = el.parent
            while ancestor is not None and ancestor.name:
                if ancestor.get("class"):
                    acls = ".".join(ancestor.get("class"))
                    sel = f"{ancestor.name}.{acls} {el.name}"
                    add((el.name, "under:" + sel), sel, el)
                    break
                ancestor = ancestor.parent

    results: list[dict] = []
    for (tag, kind), slot in groups.items():
        els = slot["els"]
        if len(els) < 4 or len(els) > 400:
            continue
        sample_n = min(len(els), 12)
        link_hits = date_hits = 0
        sample_title = sample_date = ""
        for el in els[:sample_n]:
            anchor = el.find("a")
            if anchor:
                text = anchor.get_text(" ", strip=True)
                if 6 <= len(text) <= 150:
                    link_hits += 1
                    if not sample_title:
                        sample_title = text[:56]
            found = DATE_HINT.search(el.get_text(" ", strip=True))
            if found:
                date_hits += 1
                if not sample_date:
                    sample_date = found.group(0)

        link_ratio = link_hits / sample_n
        date_ratio = date_hits / sample_n
        if link_ratio < 0.5:
            continue
        # 按标签兜底的分组风险高（容易选中导航菜单），要求带日期才认
        if kind.endswith("tag") and date_ratio < 0.5:
            continue
        score = link_ratio * 2 + date_ratio * 3 + min(len(els), 40) / 40.0
        # class/id 精确匹配的优先于标签兜底
        if kind.endswith("tag"):
            score -= 1.0
        results.append({
            "selector": slot["sel"],
            "count": len(els),
            "link_ratio": round(link_ratio, 2),
            "date_ratio": round(date_ratio, 2),
            "score": round(score, 2),
            "sample_title": sample_title,
            "sample_date": sample_date,
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:top]
