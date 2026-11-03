"""LLM tool shared by natural language requests and refusal fallback."""

from __future__ import annotations

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic import Field
from pydantic.dataclasses import dataclass

from .search import SearchService


@dataclass(config={"arbitrary_types_allowed": True})
class ReverseSearchTool(FunctionTool[AstrAgentContext]):
    name: str = "reverse_search_top3"
    description: str = (
        "搜索当前消息或引用消息里的图片，同时调用 AnimeTrace、SauceNAO、E-Hentai、"
        "Google Lens 和 Yandex，返回合并后的前三条匹配。当用户要找图片出处、"
        "原图、相似图片、角色、名称或公开账号时，判断是否需要调用此工具。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "image_url": {"type": "string", "description": "可选，公网图片 URL"},
                "image_base64": {"type": "string", "description": "可选，图片的纯 base64 编码"},
            },
            "required": [],
        }
    )

    def inject_service(self, service: SearchService, astrbot_context) -> None:
        self.service = service
        self.astrbot_context = astrbot_context

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        from ..main import image_ref_from_event

        event = context.context.event
        image_ref = image_ref_from_event(event) or event.get_extra("reverse_search_image_ref") or kwargs.get("image_url")
        image_base64 = kwargs.get("image_base64")
        if not image_ref and not image_base64:
            return "请先发送图片，或回复一条含图片的消息。"
        try:
            report = await self.service.search(image_ref=image_ref, image_base64=image_base64)
            query = getattr(event.message_obj, "message_str", "") or ""
            selected, notes = await self.service.rank(
                report,
                query,
                self.astrbot_context,
                event.unified_msg_origin,
                image_ref or (f"base64://{image_base64}" if image_base64 else None),
            )
            chain = await self.service.result_chain(report, selected, notes)
            event.set_extra("reverse_search_final_chain", chain)
            return "\n".join(part.text for part in chain if hasattr(part, "text"))
        except Exception as exc:
            return f"搜图失败：{exc}"
