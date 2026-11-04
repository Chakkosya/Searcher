"""LLM tool shared by natural language requests and refusal fallback."""

from __future__ import annotations

import re

from astrbot.api import logger
from astrbot.api.message_components import Plain
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic import Field
from pydantic.dataclasses import dataclass

from .search import ENGINES, SearchService


@dataclass(config={"arbitrary_types_allowed": True})
class ReverseSearchTool(FunctionTool[AstrAgentContext]):
    name: str = "reverse_search_top3"
    description: str = (
        "搜索当前消息或引用消息里的图片，结合画面与意图选择合适的引擎，按配置限制各引擎候选数，"
        "不确定时由 Google Lens 和 Yandex 补搜。"
        "当用户要找图片出处、"
        "原图、相似图片、作品或角色、图片中的人物身份或公开账号时才调用此工具。"
        "用户追问某个引擎的全部候选时，传 engine 和 show_all=true，自动复用上次图片及缓存，"
        "该引擎未搜过则补搜，无须用户重发图片。仅发送图片，或要求描述、评价、编辑图片时不要调用。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "image_url": {"type": "string", "description": "可选，公网图片 URL"},
                "image_base64": {"type": "string", "description": "可选，图片的纯 base64 编码"},
                "engine": {"type": "string", "enum": list(ENGINES), "description": "仅当用户明确指定某个引擎时传入；否则由工具根据图片和意图路由"},
                "show_all": {"type": "boolean", "description": "用户要求展开某引擎全部候选时设 true，并传 engine"},
            },
            "required": [],
        }
    )

    def inject_service(self, service: SearchService, astrbot_context) -> None:
        self.service = service
        self.astrbot_context = astrbot_context

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        from ..main import candidate_engine, image_component_from_event, image_ref_from_event, recent_dialogue_text

        event = context.context.event
        query = event.get_extra("reverse_search_query") or getattr(event.message_obj, "message_str", "") or ""
        event.set_extra("reverse_search_attempted", True)
        key = (event.unified_msg_origin, event.get_sender_id())
        engine = kwargs.get("engine") or event.get_extra("reverse_search_candidate_engine") or candidate_engine(query)
        if engine and engine not in ENGINES:
            return "请选择 AnimeTrace、SauceNAO、E-Hentai、Google Lens 或 Yandex。"
        show_all = kwargs.get("show_all", bool(engine and re.search(r"全部|所有|候选|结果", query))) is True
        if show_all and not engine:
            return "请指定要展开候选的引擎。"
        event_session = event.get_extra("reverse_search_session")
        cached = event_session or self.service.session(key)
        reuse = bool(engine and cached) and not (
            kwargs.get("image_url") or kwargs.get("image_base64")
            or (not event_session and (image_component_from_event(event) or image_ref_from_event(event)))
        )
        image_ref = image_ref_from_event(event) or event.get_extra("reverse_search_image_ref") or kwargs.get("image_url")
        image_base64 = kwargs.get("image_base64")
        component = image_component_from_event(event) or event.get_extra("reverse_search_image_component")
        if component and not reuse:
            try:
                image_base64 = await component.convert_to_base64()
            except Exception as exc:
                if not image_ref or image_ref.startswith(("file://", "base64://")):
                    return f"搜图失败：无法读取消息中的图片（{type(exc).__name__}）"
                image_base64 = None
        if not image_ref and not image_base64 and not reuse:
            event.set_extra("reverse_search_waiting_ai", True)
            return "等待用户补充图片。"
        try:
            needs_search = not reuse or engine not in cached.report.counts
            if needs_search and not event.get_extra("reverse_search_progress_sent"):
                try:
                    progress = await self.service.notice("search_progress_prompt", self.astrbot_context,
                                                         event.unified_msg_origin, query=query)
                    if progress:
                        await event.send(event.plain_result(progress))
                    event.set_extra("reverse_search_progress_sent", True)
                except Exception as exc:
                    logger.warning("[reverse_searcher_top3] Progress message failed: %s", type(exc).__name__)
            if reuse:
                image = cached.image
                if engine not in cached.report.counts:
                    cached.report.extend(await self.service.search(image, [engine]))
                report = cached.report
                selected = [hit for hit in report.hits if hit.engine == engine]
                certain = False
                self.service.remember(key, image, cached.report, cached.query)
            else:
                image = await self.service._image_bytes(image_ref, image_base64)
                report, selected, certain = await self.service.run_search(
                    image, query, self.astrbot_context, event.unified_msg_origin,
                    engine=engine, show_all=show_all, dialogue=recent_dialogue_text(context),
                )
                self.service.remember(key, image, report, query)
            event.set_extra("reverse_search_session", self.service.session(key))
            chain = await self.service.result_chain(
                report, selected, query, self.astrbot_context, event.unified_msg_origin,
                certain=certain, show_all=show_all or reuse,
            )
            event.set_extra("reverse_search_final_chain", chain)
            return "\n".join(part.text for part in chain if hasattr(part, "text"))
        except Exception as exc:
            error = f"搜图失败：{exc}"
            event.set_extra("reverse_search_final_chain", [Plain(error)])
            return error
