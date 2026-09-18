"""推送层：微信测试号模板消息。"""
from __future__ import annotations

import logging
import time

import httpx

from .config import wechat_ready

log = logging.getLogger("notice-hub.notify")

IMPORTANCE_ICON = {5: "🔴", 4: "🟠", 3: "🟡", 2: "⚪", 1: "⚪"}


class WeChatPusher:
    """注意：微信模板消息的字段有长度限制，这里做了保守截断。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._token = ""
        self._token_expire = 0.0

    async def _get_token(self, client: httpx.AsyncClient) -> str:
        if self._token and time.time() < self._token_expire - 60:
            return self._token
        wx = self.cfg["notify"]["wechat"]
        resp = await client.post(
            "https://api.weixin.qq.com/cgi-bin/stable_token",
            json={
                "grant_type": "client_credential",
                "appid": wx["appid"],
                "secret": wx["appsecret"],
                "force_refresh": False,
            },
            timeout=20,
        )
        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise RuntimeError(f"获取 access_token 失败: {data}")
        self._token = token
        self._token_expire = time.time() + float(data.get("expires_in", 7200))
        return token

    async def send(self, title: str, content: str, url: str = "",
                   importance: int = 4) -> bool:
        if not wechat_ready(self.cfg):
            log.info("[未配置微信] 本应推送: %s", title)
            return False

        wx = self.cfg["notify"]["wechat"]
        icon = IMPORTANCE_ICON.get(int(importance), "")
        safe_title = f"{icon}{title}"[:60]
        safe_content = content[:350]

        try:
            async with httpx.AsyncClient(timeout=25) as client:
                token = await self._get_token(client)
                resp = await client.post(
                    "https://api.weixin.qq.com/cgi-bin/message/template/send",
                    params={"access_token": token},
                    json={
                        "touser": wx["openid"],
                        "template_id": wx["template_id"],
                        "url": url or "",
                        "data": {
                            "title": {"value": safe_title, "color": "#173177"},
                            "content": {"value": safe_content},
                        },
                    },
                )
                result = resp.json()
            if result.get("errcode") == 0:
                log.info("已推送: %s", safe_title)
                return True
            log.warning("推送失败: %s", result)
            return False
        except Exception as exc:
            log.warning("推送异常: %s", exc)
            return False


def build_message(item: dict) -> tuple[str, str, str]:
    """把一条 item 变成 (标题, 正文, 跳转链接)。"""
    title = item.get("title", "")
    parts = []
    if item.get("summary"):
        parts.append(item["summary"])
    if item.get("deadline"):
        parts.append(f"截止：{item['deadline']}")
    if item.get("audience"):
        parts.append(f"面向：{item['audience']}")
    if item.get("action"):
        parts.append(f"要做：{item['action']}")
    if not parts:
        content = (item.get("content") or "")[:160].replace("\n", " ")
        parts.append(content or "（无正文）")
    parts.append(f"来源：{item.get('source', '')}")
    return title, "\n".join(parts), item.get("url") or ""
