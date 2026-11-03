"""AstrBot entry point for five-engine reverse image search."""

from __future__ import annotations

import json
import re
import time

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image as AstrImage
from astrbot.api.message_components import Plain, Reply
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart
from astrbot.core.message.message_event_result import MessageChain, ResultContentType

from .reverse_searcher.search import SearchService
from .reverse_searcher.tool import ReverseSearchTool

REFUSAL = re.compile(
    r"抱歉|对不起|很遗憾|不好意思|无法|不能|不可以|没法|没办法|做不到|不方便|不便"
    r"|sorry|can't|cannot|unable|won't",
    re.IGNORECASE,
)
IMAGE_URL = re.compile(r"https?://[^\s<>]+\.(?:jpg|jpeg|png|gif|webp)(?:\?[^\s<>]*)?", re.I)
NEGATED_SEARCH = re.compile(r"(?:不要|别|不用|无需).{0,3}(?:以图搜图|搜图|找鸡)")
IMAGE_FOLLOWUP = re.compile(r"图片|这张图|那张图|这张呢|那张呢|同一张|上一张|漫画|角色|人物|出处|原图|名字|这是谁")
SHORT_IMAGE_FOLLOWUP = re.compile(r"^(?:这|那)(?:张|个)(?:图|图片)?(?:呢|怎么样|如何|也查一下)?[？?]?$")
SEARCH_REFUSAL = re.compile(r"(?:不能|无法|没法|不方便).{0,30}(?:找|查|提供|辨认).{0,20}(?:出处|来源|原图|图源|链接|角色|人物|作品|影片)")
RECENT_IMAGE_SECONDS = 120


def image_component_from_event(event: AstrMessageEvent) -> AstrImage | None:
    message = getattr(event.message_obj, "message", []) or []
    for component in message:
        if isinstance(component, AstrImage):
            return component
    for component in message:
        if isinstance(component, Reply):
            for quoted in component.chain or []:
                if isinstance(quoted, AstrImage):
                    return quoted
    return None


def image_ref_from_event(event: AstrMessageEvent) -> str | None:
    """Use an attached or quoted image, then a public image URL in the text."""
    component = image_component_from_event(event)
    if component:
        ref = getattr(component, "url", "") or getattr(component, "file", "") or getattr(component, "path", "")
        if ref:
            return ref
    match = IMAGE_URL.search(getattr(event.message_obj, "message_str", "") or "")
    return match.group(0) if match else None


def recent_dialogue_text(run_context) -> str:
    lines = []
    for message in (getattr(run_context, "messages", None) or [])[-6:]:
        role = getattr(message, "role", "")
        if role not in ("user", "assistant"):
            continue
        content = getattr(message, "content", None)
        if isinstance(content, str):
            content_text = content
        else:
            content_text = "".join(part.text for part in (content or []) if isinstance(part, TextPart))
        if content_text.strip():
            lines.append(f"{role}: {content_text[:400]}")
    return "\n".join(lines)


def replace_final_reply(resp, components: list) -> None:
    current = getattr(resp, "result_chain", None)
    if current is not None:
        current.chain[:] = components
    else:
        resp.result_chain = MessageChain(chain=components)


class ReverseSearcherTop3(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.search_service = SearchService(config)
        self.search_tool = ReverseSearchTool()
        self.search_tool.inject_service(self.search_service, context)
        self.context.add_llm_tools(self.search_tool)
        self.pending_search: dict[tuple[str, str], tuple[float, str]] = {}
        self.recent_images: dict[tuple[str, str], tuple[str | None, AstrImage | None, float]] = {}

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def prepare_search_request(self, event: AstrMessageEvent):
        text = getattr(event.message_obj, "message_str", "") or ""
        image_ref = image_ref_from_event(event)
        image_component = image_component_from_event(event)
        has_image = bool(image_ref or image_component)
        key = (event.unified_msg_origin, event.get_sender_id())
        now = time.monotonic()
        if has_image:
            self.recent_images = {
                recent_key: recent
                for recent_key, recent in self.recent_images.items()
                if now - recent[2] <= RECENT_IMAGE_SECONDS
            }
            self.recent_images[key] = (image_ref, image_component, now)
        elif IMAGE_FOLLOWUP.search(text):
            recent = self.recent_images.get(key)
            if recent and now - recent[2] <= RECENT_IMAGE_SECONDS:
                event.set_extra("reverse_search_image_ref", recent[0])
                event.set_extra("reverse_search_image_component", recent[1])
        triggers = self.config.get("trigger_keywords") or ["以图搜图", "找鸡", "搜图"]
        direct_keyword = text.strip() in triggers
        explicit = not NEGATED_SEARCH.search(text) and (
            direct_keyword or any(word and word in text for word in triggers)
        )
        if explicit and not direct_keyword:
            explicit = await self.request_wants_search(text, event.unified_msg_origin)
        pending = self.pending_search.get(key)
        if pending and now > pending[0]:
            self.pending_search.pop(key, None)
            pending = None
        followup = pending is not None and has_image
        if followup:
            self.pending_search.pop(key, None)
            event.set_extra("reverse_search_query", pending[1])
        if not (explicit or followup):
            return
        if not (has_image or event.get_extra("reverse_search_image_ref") or event.get_extra("reverse_search_image_component")):
            event.stop_event()
            seconds = max(1, int(self.config.get("keyword_image_wait_seconds", 120)))
            self.pending_search = {
                pending_key: state for pending_key, state in self.pending_search.items()
                if state[0] >= now
            }
            self.pending_search[key] = (now + seconds, text)
            prompt = self.config.get("keyword_image_wait_prompt", "请在 {seconds} 秒内发送图片，或回复一条含图片的消息。")
            yield event.plain_result(prompt.replace("{seconds}", str(seconds)))
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
                    text="上一条消息请求查找图片线索，本条消息补充了图片。请判断是否需要调用 reverse_search_top3 工具。"
                ).mark_as_temp()
            )
        elif event.get_extra("reverse_search_image_ref"):
            req.extra_user_content_parts.append(
                TextPart(
                    text="用户正在追问同一会话里刚发送的图片。只有明确要求查找出处、角色、原图或相似图片时，才调用 reverse_search_top3 工具。"
                ).mark_as_temp()
            )

    async def request_wants_search(self, text: str, origin: str, dialogue: str = "") -> bool:
        """Classify a refused request before forcing a reverse image search."""
        try:
            provider_id = self.config.get("ranking_provider_id") or await self.context.get_current_chat_provider_id(umo=origin)
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=(
                    "判断这条用户请求是否要求根据图片查找出处、原图、相似图片、作品、角色、人物身份或公开账号。"
                    "结合最近对话理解‘这张呢’等追问。描述、评价、编辑、翻译图片和普通聊天回答 false。"
                    "助手的拒绝可帮助理解上下文，但不能单独决定。"
                    "只返回 JSON：{\"search\":true} 或 {\"search\":false}。\n最近对话："
                    + dialogue + "\n最新用户请求：" + text
                ),
                system_prompt="你只判断用户是否提出了图片检索需求。",
            )
            raw = response.completion_text or ""
            return json.loads(raw[raw.index("{"):raw.rindex("}") + 1]).get("search") is True
        except Exception:
            return False

    @filter.on_decorating_result()
    async def keep_search_reply_together(self, event):
        if event.get_extra("reverse_search_final_chain") and event.get_result():
            event.get_result().set_result_content_type(ResultContentType.GENERAL_RESULT)

    @filter.on_agent_done()
    async def search_after_refusal(self, event, run_context, resp):
        """Run the registered search tool when a reply refuses a request with an image."""
        if not resp or getattr(resp, "role", "") != "assistant":
            return
        if event.get_extra("reverse_search_waiting_ai"):
            seconds = max(1, int(self.config.get("ai_image_wait_seconds", 120)))
            key = (event.unified_msg_origin, event.get_sender_id())
            query = getattr(event.message_obj, "message_str", "") or ""
            now = time.monotonic()
            self.pending_search = {
                pending_key: state for pending_key, state in self.pending_search.items()
                if state[0] >= now
            }
            self.pending_search[key] = (now + seconds, query)
            prompt = self.config.get("ai_image_wait_prompt", "我可以帮你找图。请在 {seconds} 秒内发送图片。")
            wait_text = prompt.replace("{seconds}", str(seconds))
            replace_final_reply(resp, [Plain(wait_text)])
            if run_context.messages and run_context.messages[-1].role == "assistant":
                run_context.messages[-1].content = [TextPart(text=wait_text)]
            return
        image_ref = image_ref_from_event(event) or event.get_extra("reverse_search_image_ref")
        has_image = bool(image_ref or image_component_from_event(event) or event.get_extra("reverse_search_image_component"))
        prepared = event.get_extra("reverse_search_final_chain")
        text = getattr(event.message_obj, "message_str", "") or ""
        if prepared is None and NEGATED_SEARCH.search(text):
            return
        choices = getattr(getattr(resp, "raw_completion", None), "choices", None) or []
        reply_text = getattr(resp, "completion_text", "") or ""
        if not reply_text:
            reply_text = "\n".join(
                part.text for part in (getattr(getattr(resp, "result_chain", None), "chain", None) or [])
                if isinstance(part, Plain)
            )
        refused = bool(REFUSAL.search(reply_text)) or any(
            getattr(getattr(choice, "message", None), "refusal", None) for choice in choices
        )
        if prepared is None:
            if not has_image or not refused or event.get_extra("reverse_search_attempted"):
                return
            if not event.get_extra("reverse_search_requested"):
                search_specific_refusal = bool(
                    SHORT_IMAGE_FOLLOWUP.fullmatch(text.strip())
                    and SEARCH_REFUSAL.search(reply_text)
                )
                if not search_specific_refusal:
                    dialogue = recent_dialogue_text(run_context)
                    if not await self.request_wants_search(text, event.unified_msg_origin, dialogue):
                        return
            answer = await self.search_tool.call(run_context, image_url=image_ref)
            prepared = event.get_extra("reverse_search_final_chain")
            if prepared is None:
                prepared = [Plain(answer)]
        replace_final_reply(resp, prepared)
        if run_context.messages and run_context.messages[-1].role == "assistant":
            run_context.messages[-1].content = [
                TextPart(text="\n".join(part.text for part in prepared if isinstance(part, Plain)))
            ]
