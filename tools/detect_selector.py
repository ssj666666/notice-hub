"""自动识别网页里的"列表项"结构，输出可用的 CSS 选择器。

用法:
    py -3.12 tools/detect_selector.py <url> [<url> ...]

原理：找出页面上重复出现、每个都含有"像通知标题的链接"、且附近带日期的元素类名，
按 链接比例 / 日期比例 / 出现次数 打分排序。
配 config.yaml 时把结果填进 item_selector / title_selector / link_selector / date_selector。
"""
from __future__ import annotations

import re
import sys

import httpx
from bs4 import BeautifulSoup

UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")}
DATE_RE = re.compile(r"20\d{2}\s*[-/年.]\s*\d{1,2}\s*[-/月.]\s*\d{1,2}")


def detect(html: str, top: int = 6) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    groups: dict[tuple[str, str], list] = {}
    for el in soup.find_all(True):
        classes = el.get("class")
        if not classes:
            continue
        key = (el.name, " ".join(classes))
        groups.setdefault(key, []).append(el)

    results = []
    for (name, cls), els in groups.items():
        if len(els) < 4:
            continue
        sample_n = min(len(els), 12)
        link_hits = date_hits = 0
        sample_title = ""
        sample_date = ""
        for el in els[:sample_n]:
            a = el.find("a")
            if a:
                text = a.get_text(" ", strip=True)
                if 6 <= len(text) <= 150:
                    link_hits += 1
                    if not sample_title:
                        sample_title = text[:56]
            found = DATE_RE.search(el.get_text(" ", strip=True))
            if found:
                date_hits += 1
                if not sample_date:
                    sample_date = found.group(0)
        link_ratio = link_hits / sample_n
        date_ratio = date_hits / sample_n
        if link_ratio < 0.5:
            continue
        score = link_ratio * 2 + date_ratio * 3 + min(len(els), 40) / 40.0
        results.append({
            "selector": f"{name}.{'.'.join(cls.split())}",
            "count": len(els),
            "link_ratio": round(link_ratio, 2),
            "date_ratio": round(date_ratio, 2),
            "score": round(score, 2),
            "sample_title": sample_title,
            "sample_date": sample_date,
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top]


def probe(url: str) -> None:
    print("\n" + "=" * 74)
    print(f"  {url}")
    print("=" * 74)
    try:
        r = httpx.get(url, headers=UA, timeout=25, follow_redirects=True)
    except Exception as exc:
        print(f"  ERR {type(exc).__name__}: {exc}")
        return
    r.encoding = r.encoding or "utf-8"
    print(f"  HTTP {r.status_code}  {len(r.content)} bytes")

    for i, res in enumerate(detect(r.text), 1):
        print(f"\n  [{i}] item_selector: {res['selector']}")
        print(f"      出现 {res['count']} 次 | 含链接 {res['link_ratio']:.0%} | "
              f"含日期 {res['date_ratio']:.0%} | 得分 {res['score']}")
        print(f"      样例标题: {res['sample_title']}")
        print(f"      样例日期: {res['sample_date'] or '(无)'}")
        if res["date_ratio"] > 0.5:
            print(f"      → 建议 date_selector: \"span\"  或看样例日期所在标签")


def main() -> None:
    urls = sys.argv[1:]
    if not urls:
        urls = [
            "http://news.pku.edu.cn/wyyd/xxyg/index.htm",
            "https://www.bjmu.edu.cn/jzxx/index.htm",
            "https://www.bjmu.edu.cn/tzgg/index.htm",
            "https://www.bjmu.edu.cn/xyxx/index.htm",
            "https://pe.pku.edu.cn/xwzx/ttxw.htm",
        ]
    for url in urls:
        probe(url)


if __name__ == "__main__":
    main()
