"""AstrBot entry point for five-engine reverse image search."""

from __future__ import annotations

import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image as AstrImage
from astrbot.api.message_components import Reply
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart

from .reverse_searcher.search import SearchService
from .reverse_searcher.tool import ReverseSearchTool

REFUSAL = re.compile(
    r"(?:抱歉|对不起|很遗憾).{0,100}(?:无法|不能|不可以|没法|做不到)"
    r"|(?:我|这边)(?:无法|不能|没法|不可以).{0,80}(?:帮助|识别|搜索|查找|提供|完成|回答|处理|执行|满足|这样做|这么做)"
    r"|sorry.{0,100}(?:can't|cannot|unable|won't)"
    r"|i (?:can't|cannot|am unable|won't)\b",
    re.IGNORECASE | re.DOTALL,
)
IMAGE_URL = re.compile(r"https?://[^\s<>]+\.(?:jpg|jpeg|png|gif|webp)(?:\?[^\s<>]*)?", re.I)


def image_ref_from_event(event: AstrMessageEvent) -> str | None:
    """Use an attached or quoted image, then a public image URL in the text."""
    message = getattr(event.message_obj, "message", []) or []
    for component in message:
        if isinstance(component, AstrImage):
            ref = getattr(component, "url", "") or getattr(component, "file", "")
            if ref:
                return ref
    for component in message:
        if isinstance(component, Reply):
            for quoted in component.chain or []:
                if isinstance(quoted, AstrImage):
                    ref = getattr(quoted, "url", "") or getattr(quoted, "file", "")
                    if ref:
                        return ref
    match = IMAGE_URL.search(getattr(event.message_obj, "message_str", "") or "")
    return match.group(0) if match else None


class ReverseSearcherTop3(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.search_service = SearchService(config)
        self.search_tool = ReverseSearchTool()
        self.search_tool.inject_service(self.search_service)
        self.context.add_llm_tools(self.search_tool)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def keyword_search(self, event: AstrMessageEvent):
        text = getattr(event.message_obj, "message_str", "") or ""
        triggers = self.config.get("trigger_keywords") or ["以图搜图", "找鸡"]
        if not any(word in text for word in triggers):
            return
        event.stop_event()
        image_ref = image_ref_from_event(event)
        if not image_ref:
            yield event.plain_result("请附上图片，或回复一条含图片的消息。")
            return
        await event.send(event.plain_result("🔍 正在全网找图，请稍候…"))
        try:
            report = await self.search_service.search(image_ref=image_ref)
            yield event.chain_result(await self.search_service.result_chain(report))
        except Exception as exc:
            logger.warning("[reverse_searcher_top3] search failed: %s", exc)
            yield event.plain_result(f"搜图失败：{exc}")

    @filter.on_agent_done()
    async def search_after_refusal(self, event, run_context, resp):
        """Run the registered search tool when a reply refuses a request with an image."""
        if not resp or getattr(resp, "role", "") != "assistant":
            return
        if not REFUSAL.search(getattr(resp, "completion_text", "") or ""):
            return
        if not image_ref_from_event(event):
            return
        answer = await self.search_tool.call(run_context)
        resp.completion_text = answer if answer.startswith("搜图失败：") else ""
        resp.result_chain = None
        if run_context.messages and run_context.messages[-1].role == "assistant":
            run_context.messages[-1].content = [TextPart(text=answer)]
