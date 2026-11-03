"""AstrBot entry point for five-engine reverse image search."""

from __future__ import annotations

import re
import time

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
NEGATED_SEARCH = re.compile(r"(?:不要|别|不用|无需).{0,3}(?:以图搜图|搜图|找鸡)")
PENDING_SECONDS = 120


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
        self.pending_search: dict[tuple[str, str], float] = {}

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def prepare_search_request(self, event: AstrMessageEvent):
        text = getattr(event.message_obj, "message_str", "") or ""
        image_ref = image_ref_from_event(event)
        triggers = self.config.get("trigger_keywords") or ["以图搜图", "找鸡", "搜图"]
        explicit = not NEGATED_SEARCH.search(text) and (
            text.strip() == "搜图" or any(word and word in text for word in triggers)
        )
        key = (event.unified_msg_origin, event.get_sender_id())
        pending_at = self.pending_search.pop(key, None)
        followup = pending_at is not None and time.monotonic() - pending_at <= PENDING_SECONDS and bool(image_ref)
        if not (explicit or followup):
            return
        if not image_ref:
            event.stop_event()
            now = time.monotonic()
            self.pending_search = {
                pending_key: started
                for pending_key, started in self.pending_search.items()
                if now - started <= PENDING_SECONDS
            }
            self.pending_search[key] = now
            yield event.plain_result("请在下一条消息发送图片，或回复一条含图片的消息（两分钟内有效）。")
            return
        event.is_at_or_wake_command = True
        event.set_extra("reverse_search_requested", True)
        if followup:
            event.set_extra("reverse_search_followup", True)

    @filter.on_llm_request()
    async def add_followup_context(self, event, req):
        if event.get_extra("reverse_search_followup"):
            event.set_extra("reverse_search_followup", False)
            req.extra_user_content_parts.append(
                TextPart(
                    text="上一条消息是搜图请求，本条消息补充了图片。请判断是否需要调用 reverse_search_top3 工具。"
                ).mark_as_temp()
            )

    @filter.on_agent_done()
    async def search_after_refusal(self, event, run_context, resp):
        """Run the registered search tool when a reply refuses a request with an image."""
        if not resp or getattr(resp, "role", "") != "assistant":
            return
        if not image_ref_from_event(event):
            return
        refused = bool(REFUSAL.search(getattr(resp, "completion_text", "") or ""))
        if event.get_extra("reverse_search_result_sent"):
            if refused:
                resp.completion_text = ""
                resp.result_chain = None
                if run_context.messages and run_context.messages[-1].role == "assistant":
                    run_context.messages[-1].content = [TextPart(text="已发送五引擎搜图结果。")]
            return
        if not (event.get_extra("reverse_search_requested") or refused):
            return
        answer = await self.search_tool.call(run_context)
        resp.completion_text = answer if answer.startswith("搜图失败：") else ""
        resp.result_chain = None
        if run_context.messages and run_context.messages[-1].role == "assistant":
            run_context.messages[-1].content = [TextPart(text=answer)]
