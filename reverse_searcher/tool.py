"""LLM tool shared by natural language requests and refusal fallback."""

from __future__ import annotations

from astrbot.api import logger
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
        "Google Lens 和 Yandex，每个引擎取前五条，由模型选择前三条匹配。当用户要找图片出处、"
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
        from ..main import image_component_from_event, image_ref_from_event

        event = context.context.event
        image_ref = image_ref_from_event(event) or event.get_extra("reverse_search_image_ref") or kwargs.get("image_url")
        image_base64 = kwargs.get("image_base64")
        component = image_component_from_event(event) or event.get_extra("reverse_search_image_component")
        if component:
            try:
                image_base64 = await component.convert_to_base64()
            except Exception as exc:
                if not image_ref or image_ref.startswith(("file://", "base64://")):
                    return f"搜图失败：无法读取消息中的图片（{type(exc).__name__}）"
                image_base64 = None
        if not image_ref and not image_base64:
            event.set_extra("reverse_search_waiting_ai", True)
            return "等待用户补充图片。"
        try:
            progress = self.service.config.get("search_progress_prompt", "🔍 正在全网找图，请稍候...")
            if progress and not event.get_extra("reverse_search_progress_sent"):
                try:
                    await event.send(event.plain_result(progress))
                    event.set_extra("reverse_search_progress_sent", True)
                except Exception as exc:
                    logger.warning("[reverse_searcher_top3] Progress message failed: %s", type(exc).__name__)
            report = await self.service.search(image_ref=image_ref, image_base64=image_base64)
            query = event.get_extra("reverse_search_query") or getattr(event.message_obj, "message_str", "") or ""
            selected, notes = await self.service.rank(
                report,
                query,
                self.astrbot_context,
                event.unified_msg_origin,
                f"base64://{image_base64}" if image_base64 else image_ref,
            )
            chain = await self.service.result_chain(report, selected, notes, query)
            event.set_extra("reverse_search_final_chain", chain)
            return "\n".join(part.text for part in chain if hasattr(part, "text"))
        except Exception as exc:
            return f"搜图失败：{exc}"
