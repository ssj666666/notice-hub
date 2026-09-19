"""医学部各源异常排查：看原始日期标记 vs 我的解析结果。

重点怀疑：医学部页面日期形如 `14 Aug`（不带年份），
我的 _resolve_year 会把"未来日期"回退一年。如果页面本身是新的，
这个推断就可能把新内容误判成去年的。
"""
from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.autodetect import extract_date          # noqa: E402
from app.db import CST                            # noqa: E402

UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")}

PAGES = [
    ("医学部·通知公告", "https://www.bjmu.edu.cn/tzgg/index.htm", "li.noImg"),
    ("医学部·校园信息", "https://www.bjmu.edu.cn/xyxx/index.htm", "li.noImg"),
    ("医学部·讲座信息", "https://www.bjmu.edu.cn/jzxx/index.htm", "ul.list07 li"),
    ("北医教育处·重要通知", "https://jiaoyuchu.bjmu.edu.cn/tzgggb/zzytz/index.htm",
     "div.list30 li"),
    ("北医团委·通知公告", "https://bytw.bjmu.edu.cn/tzgg/index.htm", "ul.t2-list01 li"),
]


async def main() -> None:
    today = datetime.now(CST).date()
    print(f"今天 = {today}\n")
    async with httpx.AsyncClient(headers=UA, follow_redirects=True, verify=False,
                                 timeout=30) as c:
        for label, url, sel in PAGES:
            print("=" * 78)
            print(f"  {label}")
            print(f"  {url}")
            print("=" * 78)
            try:
                r = await c.get(url)
            except Exception as exc:
                print(f"  ERR {type(exc).__name__}: {exc}\n")
                continue
            r.encoding = r.encoding or "utf-8"
            soup = BeautifulSoup(r.text, "html.parser")
            nodes = soup.select(sel)
            print(f"  页面 {len(r.content)}B，选择器 {sel!r} 命中 {len(nodes)} 条")
            print(f"  {'原始日期文本':22} {'我解析成':12} {'距今':>7}  标题")
            for node in nodes[:6]:
                text = node.get_text(" ", strip=True)
                # 找出所有像日期的片段
                raw = re.findall(r"\d{1,2}\s*[A-Za-z]{3,9}|\d{4}[-/年.]\s*\d{1,2}[-/月.]\s*\d{1,2}", text)
                rawstr = " ".join(raw[:2]) if raw else "(无)"
                parsed = extract_date(text) or "(空)"
                age = ""
                if parsed:
                    try:
                        d = datetime.fromisoformat(parsed).date()
                        age = f"{(today - d).days}天"
                    except ValueError:
                        pass
                # 标题：取 <a> 的文本或前 40 字
                a = node.find("a")
                title = (a.get_text(" ", strip=True) if a else text)[:38]
                print(f"  {rawstr:22} {parsed:12} {age:>7}  {title}")
            # 打印第一条的原始 HTML，便于人工核对
            if nodes:
                raw_html = re.sub(r"\s+", " ", str(nodes[0]))
                print(f"\n  第 1 条原始 HTML: {raw_html[:300]}")
            print()
            sys.stdout.flush()


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
