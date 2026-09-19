"""单测新增的源：逐个抓一次，报告条数/日期/样例。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.collectors import collect_all           # noqa: E402
from app.config import enabled_sources, load_config  # noqa: E402
from app.db import Database                       # noqa: E402

NEW_NAMES = [
    "就业·重点推荐", "就业·选调生通知公告", "就业·国际组织实习", "就业·实习信息",
    "就业·校内双选会", "就业·选调生宣讲会",
    "计算中心·通知公告", "网络服务·通知公告",
    "总务部·相关通知", "总务部·后勤动态",
    "保卫部·通知公告", "国际合作部·公告通知", "国际合作部·国际组织实习",
    "校医院·通知公告",
]


async def main() -> None:
    cfg = load_config()
    db = Database("data/noticehub.db")
    srcs = [s for s in enabled_sources(cfg) if s.get("name") in NEW_NAMES]
    print(f"待测 {len(srcs)} 个新源\n")

    async with httpx.AsyncClient(follow_redirects=True, verify=True) as safe, \
            httpx.AsyncClient(follow_redirects=True, verify=False) as lax:
        for src in srcs:
            name = src.get("name")
            items = await collect_all(cfg, db, {True: safe, False: lax}, [src])
            dated = sum(1 for i in items if i.published_at)
            newest = max((i.published_at for i in items if i.published_at),
                         default="-")
            flag = "OK " if items else "空 "
            print(f"  [{flag}] {name:22} {len(items):3} 条  有日期 {dated:3}  最新 {newest}")
            for it in items[:2]:
                print(f"           · {it.title[:56]}")
                if it.published_at:
                    print(f"             ({it.published_at})")
    db.close()


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
