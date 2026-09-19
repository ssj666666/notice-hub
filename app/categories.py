"""通知分类体系。

两级分类：
  一级「校区」：北大本部 / 医学部 / 通用（看板先按这个分两大块）
  二级「类目」：教务学业 / 讲座讲坛 / 就业实习 ……（每块下面再细分）

分类逻辑分两层：
  1. 规则分类 classify()：关键词加权计分，零成本、可预测、随时可改
  2. LLM 分类：在 llm_judge 里让模型从同一份类目表里选，处理规则拿不准的

改类目或关键词只需改这个文件，前后端和 LLM 提示词都会跟着变。
"""
from __future__ import annotations

# ============================================================
#  一级分类：校区 / 归属
#  看板先按这个分两大块，再在每块下面按二级类目细分。
#  「通用」用于不属于任何校区的源（比如自己订阅的公众号 RSS）。
# ============================================================
CAMPUSES: list[dict] = [
    {"key": "北大本部", "icon": "🏛", "desc": "燕园 · 校本部各单位"},
    {"key": "医学部", "icon": "⚕️", "desc": "北医 · 医学部各单位"},
    {"key": "通用", "icon": "📡", "desc": "不属于特定校区的源（自定义订阅等）"},
]
CAMPUS_KEYS = [c["key"] for c in CAMPUSES]
CAMPUS_ICONS = {c["key"]: c["icon"] for c in CAMPUSES}
DEFAULT_CAMPUS = "通用"

_MEDICAL_HINTS = ("医学部", "北医", "医学", "药学院", "公共卫生", "护理")
_PKU_HINTS = ("北大", "教务部", "团委", "新闻网", "体育", "讲座网", "就业")


def campus_from_name(name: str) -> str:
    """没显式配置 campus 时，按源名字猜一个。"""
    if not name:
        return DEFAULT_CAMPUS
    for hint in _MEDICAL_HINTS:
        if hint in name:
            return "医学部"
    for hint in _PKU_HINTS:
        if hint in name:
            return "北大本部"
    return DEFAULT_CAMPUS


# ============================================================
#  二级分类
# ============================================================
# 顺序即优先级（同分时靠前者胜出）。key 会存进数据库，改名要谨慎。
CATEGORIES: list[dict] = [
    {
        "key": "教务学业",
        "icon": "📚",
        "desc": "选课、考试、成绩、学籍、培养方案、毕业、推免、转专业",
        "keywords": [
            "选课", "补退选", "退课", "选课通知", "课程", "开课", "停开", "调课",
            "考试", "考场", "成绩", "绩点", "学籍", "培养方案", "教学计划",
            "学分", "毕业", "学位", "转专业", "转院系", "推免", "免试推荐",
            "缓考", "重修", "四六级", "大学英语", "分级", "教室", "教学",
            "本科教学", "研究生教学", "毕业论文", "答辩",
            # 招生类容易被「宣讲会」抢到就业去，这里补上更具体的关键词
            "招生", "二次招生", "结业", "辅修", "双学位", "双专业",
        ],
    },
    {
        "key": "讲座讲坛",
        "icon": "🎓",
        "desc": "讲座、讲坛、沙龙、学术报告、论坛、研讨会",
        "keywords": [
            "讲座", "讲坛", "沙龙", "学术报告", "报告会", "论坛", "研讨会",
            "讲堂", "分享会", "名家", "预告", "学术活动", "工作坊", "seminar",
        ],
    },
    {
        "key": "就业实习",
        "icon": "💼",
        "desc": "招聘、宣讲会、实习、求职、校招、选调",
        "keywords": [
            "招聘", "宣讲会", "宣讲", "实习", "求职", "校招", "简历", "面试",
            "就业", "岗位", "双选会", "选调", "用人单位", "招聘会", "职业生涯",
            "职业发展", "offer", "春招", "秋招",
        ],
    },
    {
        "key": "奖助学金",
        "icon": "🏆",
        "desc": "奖学金、助学金、评优、资助、助学贷款",
        "keywords": [
            "奖学金", "助学金", "评优", "资助", "助学贷款", "困难补助",
            "荣誉", "评奖", "三好学生", "优秀学生", "补助", "减免", "勤工助学",
        ],
    },
    {
        "key": "科研竞赛",
        "icon": "🔬",
        "desc": "科研训练、学科竞赛、创新创业、项目申报",
        "keywords": [
            "科研训练", "竞赛", "创新创业", "挑战杯", "项目申报", "立项",
            "结题", "学科竞赛", "大赛", "科研项目", "本科生科研", "实验室",
            "创新计划", "创业者",
        ],
    },
    {
        "key": "国际交流",
        "icon": "🌏",
        "desc": "交换、留学、海外访学、国际项目、暑期学校",
        "keywords": [
            "交换", "留学", "海外", "出国", "访学", "国际项目", "暑校",
            "暑期学校", "国际交流", "境外", "双学位项目", "联合培养", "游学",
        ],
    },
    {
        "key": "体育活动",
        "icon": "🏃",
        "desc": "体育课、体测、运动会、场馆、健身",
        "keywords": [
            "体育", "体测", "体质测试", "运动会", "健身", "场馆", "游泳",
            "球场", "早操", "锻炼", "马拉松", "跑步", "球赛", "运动队",
        ],
    },
    {
        "key": "校园生活",
        "icon": "🏠",
        "desc": "宿舍、食堂、后勤、停水停电、校园卡、校医院、网络",
        "keywords": [
            "宿舍", "食堂", "餐饮", "后勤", "停电", "停水", "校园卡", "网络",
            "校医院", "就医", "医疗", "报销", "班车", "快递", "绿化", "维修",
            "供暖", "浴室", "占道", "喷药", "施工", "一卡通", "wifi", "校园网",
        ],
    },
    {
        "key": "社团活动",
        "icon": "🎭",
        "desc": "学生会、社团、文艺演出、志愿招募",
        "keywords": [
            "学生会", "社团", "文艺", "演出", "晚会", "志愿", "志愿者",
            "招募", "招新", "文化节", "比赛活动", "观影", "歌会", "展览",
        ],
    },
    {
        "key": "行政通知",
        "icon": "📋",
        "desc": "放假、值班、办公时间、管理办法、公示、招标采购",
        "keywords": [
            "放假", "值班", "办公时间", "管理办法", "制度", "征求意见",
            "公示", "聘任", "招标", "采购", "会议", "通知公告", "工作安排",
            "实施方案", "细则", "规定", "调整", "关于开展", "关于印发",
        ],
    },
]

CATEGORY_KEYS = [c["key"] for c in CATEGORIES]
ICONS = {c["key"]: c["icon"] for c in CATEGORIES}
DESCS = {c["key"]: c["desc"] for c in CATEGORIES}
DEFAULT_CATEGORY = "其他"
ICONS[DEFAULT_CATEGORY] = "📌"


def classify(text: str) -> tuple[str, float]:
    """规则分类。返回 (类目, 置信度 0~1)。拿不准就返回「其他」。"""
    if not text:
        return DEFAULT_CATEGORY, 0.0

    scores: dict[str, float] = {}
    for cat in CATEGORIES:
        score = 0.0
        for kw in cat["keywords"]:
            if kw in text:
                # 越长的关键词越具体，权重越高
                score += 1.0 + len(kw) / 10.0
        if score:
            scores[cat["key"]] = score

    if not scores:
        return DEFAULT_CATEGORY, 0.0

    winner = max(scores, key=lambda k: (scores[k], -CATEGORY_KEYS.index(k)))
    total = sum(scores.values())
    confidence = scores[winner] / total if total else 0.0
    return winner, round(confidence, 3)


def as_prompt_list() -> str:
    """给 LLM 用的类目清单。"""
    lines = []
    for cat in CATEGORIES:
        lines.append(f"- {cat['key']}：{cat['desc']}")
    lines.append(f"- {DEFAULT_CATEGORY}：以上都不属于")
    return "\n".join(lines)
