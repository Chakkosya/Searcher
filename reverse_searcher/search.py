"""Run the five reverse image engines and combine their results."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from PIL import Image
from astrbot.api import logger
from astrbot.api.message_components import Image as AstrImage
from astrbot.api.message_components import Plain

from .utils import AnimeTrace, EHentai, GoogleLens, Network, SauceNAO, Yandex
from .utils.security import is_safe_image_ref, is_safe_local_image_path

ENGINES = ("animetrace", "saucenao", "ehentai", "google", "yandex")
LABELS = {
    "animetrace": "AnimeTrace",
    "saucenao": "SauceNAO",
    "ehentai": "E-Hentai",
    "google": "Google Lens",
    "yandex": "Yandex",
}
DEFAULT_HEADER = "🔎 图像线索｜五站检索\n按匹配证据整理的 {count} 条候选："
DEFAULT_ITEM = "\n\n{rank_symbol} {platform} · {match}\n{keyword}{account_line}{reason_line}"
DEFAULT_FOOTER = "\n\n识图线索可能有误，请对照原图核实。"


def render_template(template: str, default: str, **values) -> str:
    try:
        return template.format(**values)
    except (KeyError, ValueError, IndexError) as exc:
        logger.warning("[reverse_searcher_top3] Invalid result template: %s", exc)
        return default.format(**values)


@dataclass
class SearchHit:
    engine: str
    rank: int
    title: str
    url: str = ""
    source: str = ""
    author: str = ""
    similarity: float = 0
    group: str = ""
    preview_url: str = ""
    thumbnail_url: str = ""
    engines: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.engines.add(self.engine)

    @property
    def evidence(self) -> str:
        if self.similarity:
            return f"{self.similarity:.1f}%（SauceNAO）"
        if self.engine == "google" and self.group in ("exact", "pages"):
            return "包含原图的页面"
        return "视觉匹配"

    @property
    def platform(self) -> str:
        host = urlsplit(self.url).hostname or ""
        host = host.lower().removeprefix("www.")
        if host in ("x.com", "twitter.com"):
            return "X (Twitter)"
        if host.endswith("pixiv.net"):
            return "Pixiv"
        if host.endswith("e-hentai.org") or host.endswith("exhentai.org"):
            return "E-Hentai"
        if host:
            return host
        return LABELS[self.engine]

    @property
    def account(self) -> str:
        if self.author:
            return self.author
        parts = urlsplit(self.url)
        if (parts.hostname or "").lower() in ("x.com", "www.x.com", "twitter.com", "www.twitter.com"):
            name = parts.path.strip("/").split("/")[0]
            if name and name not in ("i", "search", "home"):
                return f"@{name}"
        return ""

    def format_text(self, number: int) -> str:
        keyword = self.title or self.source or "相似图片"
        if keyword.startswith(("http://", "https://")):
            keyword = "相似图片"
        lines = [f"{number}. 🌐 平台：{self.platform}", f"🔎 关键词：{keyword}"]
        if self.account:
            lines.append(f"👤 名称/账号：{self.account}")
        lines.append(f"🎯 匹配：{self.evidence}")
        if len(self.engines) > 1:
            labels = "、".join(LABELS[name] for name in ENGINES if name in self.engines)
            lines.append(f"📌 搜索引擎：{labels}")
        return "\n".join(lines)


@dataclass
class SearchReport:
    hits: list[SearchHit]
    failures: dict[str, str]

    def format_text(self) -> str:
        lines = ["🎯 **找图结果**"]
        if not self.hits:
            lines.append("没有得到可用匹配。")
        for number, hit in enumerate(self.hits[:3], 1):
            lines.extend(["", hit.format_text(number)])
        if self.failures:
            failed = "；".join(
                f"{LABELS[name]}：{self.failures[name]}"
                for name in ENGINES
                if name in self.failures
            )
            lines.extend(["", f"未完成的引擎：{failed}"])
        lines.extend(["", "⚠️ 识图存在误差，请打开来源核对。"])
        return "\n".join(lines)


def _normal_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, ""))


def _priority(hit: SearchHit) -> tuple[int, int, float, int, int, int]:
    direct = 2 if hit.similarity >= 80 else 1 if hit.similarity >= 60 else 0
    exact = 1 if hit.engine == "google" and hit.group in ("exact", "pages") else 0
    return (len(hit.engines), max(direct, exact), hit.similarity, bool(hit.preview_url), -hit.rank, -ENGINES.index(hit.engine))


def merge_hits(hits: list[SearchHit]) -> list[SearchHit]:
    merged: dict[str, SearchHit] = {}
    for hit in hits:
        if not hit.title and not hit.url:
            continue
        key = _normal_url(hit.url) if hit.url else f"{hit.engine}:{hit.title}:{hit.source}"
        previous = merged.get(key)
        if previous is None:
            merged[key] = hit
        else:
            engines = previous.engines | hit.engines
            winner = max((previous, hit), key=_priority)
            winner.engines = engines
            full_preview = next(
                (candidate.preview_url for candidate in (previous, hit)
                 if candidate.preview_url and candidate.preview_url != candidate.thumbnail_url),
                "",
            )
            winner.preview_url = full_preview or winner.preview_url or previous.preview_url or hit.preview_url
            winner.thumbnail_url = winner.thumbnail_url or previous.thumbnail_url or hit.thumbnail_url
            merged[key] = winner
    return sorted(merged.values(), key=_priority, reverse=True)


def _from_response(engine: str, response) -> list[SearchHit]:
    hits = []
    if engine == "animetrace":
        rank = 0
        for item in response.raw[:5]:
            for character in item.characters:
                rank += 1
                hits.append(SearchHit(engine, rank, character.name, source=character.work))
        return hits
    for rank, item in enumerate(response.raw[:5], 1):
        source = getattr(item, "source", "") or getattr(item, "index_name", "") or ""
        if engine == "ehentai":
            source = "E-Hentai"
        hits.append(
            SearchHit(
                engine=engine,
                rank=rank,
                title=getattr(item, "title", "") or "",
                url=getattr(item, "url", "") or "",
                source=source,
                author=getattr(item, "author", "") or "",
                similarity=float(getattr(item, "similarity", 0) or 0),
                group=getattr(item, "group", "") or "",
                preview_url=getattr(item, "preview_url", "") or getattr(item, "thumbnail", "") or "",
                thumbnail_url=getattr(item, "thumbnail", "") or "",
            )
        )
    return hits


class SearchService:
    def __init__(self, config: dict):
        self.config = config

    async def _image_bytes(self, image_ref: str | None, image_base64: str | None) -> bytes:
        if image_base64:
            data = base64.b64decode(image_base64, validate=True)
        elif image_ref and image_ref.startswith("base64://"):
            data = base64.b64decode(image_ref.removeprefix("base64://"), validate=True)
        elif image_ref:
            if not await asyncio.to_thread(is_safe_image_ref, image_ref):
                raise ValueError("图片地址不可用；请发送消息图片或公网图片 URL")
            if is_safe_local_image_path(image_ref):
                data = await asyncio.to_thread(Path(image_ref).read_bytes)
            else:
                async with Network(proxies=self.config.get("proxies") or None, timeout=30) as network:
                    data = await network.download(image_ref)
        else:
            raise ValueError("请先发送图片，或回复一条含图片的消息")
        if not data:
            raise ValueError("图片下载失败")
        if data.startswith((b"GIF87a", b"GIF89a")):
            def convert() -> bytes:
                with Image.open(io.BytesIO(data)) as image:
                    output = io.BytesIO()
                    image.convert("RGB").save(output, format="JPEG")
                    return output.getvalue()
            data = await asyncio.to_thread(convert)
        return data

    async def _search_one(self, engine: str, image: bytes):
        config = self.config
        cookies = config.get(f"{engine}_cookies") or None
        async with Network(proxies=config.get("proxies") or None, cookies=cookies, timeout=45) as network:
            if engine == "animetrace":
                request = AnimeTrace(network=network, is_multi=False, ai_detect=False)
                return await request.search(file=image, model="full_game_model_kira")
            if engine == "saucenao":
                request = SauceNAO(network=network, api_key=config.get("saucenao_api_key") or None, numres=5, minsim=30)
                return await request.search(file=image)
            if engine == "ehentai":
                request = EHentai(network=network, is_ex=False, similar=True)
                return await request.search(file=image)
            if not config.get("allow_third_party_image_host", True):
                raise ValueError("本地图片上传临时图床已关闭")
            if engine == "google":
                serp = config.get("serpapi_key") or None
                zen = config.get("zenserp_key") or None
                if not serp and not zen:
                    raise ValueError("未配置 SerpAPI/Zenserp Key")
                request = GoogleLens(network=network, serpapi_key=serp, zenserp_key=zen)
                return await request.search(file=image, country="HK", hl="zh-CN", max_results=5)
            request = Yandex(network=network, use_ru_fallback=True)
            return await request.search(file=image, max_results=5)

    async def search(self, image_ref: str | None = None, image_base64: str | None = None) -> SearchReport:
        image = await self._image_bytes(image_ref, image_base64)
        responses = await asyncio.gather(
            *(self._search_one(engine, image) for engine in ENGINES),
            return_exceptions=True,
        )
        hits: list[SearchHit] = []
        failures: dict[str, str] = {}
        for engine, response in zip(ENGINES, responses):
            if isinstance(response, BaseException):
                logger.warning("[reverse_searcher_top3] %s search failed: %s", engine, type(response).__name__)
                failures[engine] = str(response)[:120] if isinstance(response, ValueError) else type(response).__name__
                continue
            hits.extend(_from_response(engine, response))
        return SearchReport(merge_hits(hits), failures)

    async def rank(self, report: SearchReport, query: str, context, origin: str, image_ref: str | None = None):
        """Let the configured AstrBot model choose three candidates from all engines."""
        hits = report.hits
        selected = hits[:3]
        notes: dict[int, str] = {}
        if not hits:
            return selected, notes

        candidates = [
            {
                "id": number,
                "platform": hit.platform,
                "title": hit.title[:180],
                "account": hit.account[:80],
                "source": hit.source[:120],
                "similarity": hit.similarity,
                "engines": sorted(hit.engines),
                "engine_rank": hit.rank,
                "exact_page": hit.engine == "google" and hit.group in ("exact", "pages"),
            }
            for number, hit in enumerate(hits, 1)
        ]
        prompt = (
            "用户问题：" + (query or "请找出这张图片最可能的出处或角色") + "\n"
            "五个以图搜图引擎的候选结果：\n"
            + json.dumps(candidates, ensure_ascii=False)
            + "\n选最符合用户问题、最可能对应原图的前三条。优先考虑明确相似度、"
            "多引擎交叉命中、原图页面和具体名称；不要把不同引擎的分数当成同一概率。"
            "只返回 JSON：{\"ids\":[候选id],\"notes\":{\"id\":\"一句简短的匹配依据\"}}。"
            "不要输出链接，不要补造候选中没有的名字或账号。"
        )
        image_urls = [image_ref] if image_ref else []
        preview_ids = []
        for number, hit in enumerate(hits[:6], 1):
            preview = hit.preview_url or hit.thumbnail_url
            if preview.startswith(("https://", "http://")):
                image_urls.append(preview)
                preview_ids.append(number)
        if preview_ids:
            prompt += (
                "\n附图第一张是用户原图，后面的预览图依次对应候选编号 "
                + ", ".join(map(str, preview_ids))
                + "。有图像能力时，请同时比较画面。"
            )
        try:
            provider_id = self.config.get("ranking_provider_id") or await context.get_current_chat_provider_id(umo=origin)
            request = {
                "chat_provider_id": provider_id,
                "prompt": prompt,
                "system_prompt": "你根据反向搜图结果整理出处线索，只判断候选与用户问题的相关性。",
            }
            if image_urls:
                try:
                    response = await context.llm_generate(**request, image_urls=image_urls)
                    if not (response.completion_text or "").lstrip().startswith(("{", "```")):
                        response = await context.llm_generate(**request)
                except Exception:
                    response = await context.llm_generate(**request)
            else:
                response = await context.llm_generate(**request)
            raw = response.completion_text or ""
            data = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
            ids = []
            for value in data.get("ids", []):
                number = int(value)
                if 1 <= number <= len(hits) and number not in ids:
                    ids.append(number)
                if len(ids) == 3:
                    break
            if ids:
                for number in range(1, len(hits) + 1):
                    if len(ids) == 3:
                        break
                    if number not in ids:
                        ids.append(number)
                selected = [hits[number - 1] for number in ids[:3]]
                raw_notes = data.get("notes", {})
                notes = {
                    number: re.sub(r"https?://\S+", "", str(raw_notes.get(str(number), "")))[:100].strip()
                    for number in ids[:3]
                }
        except Exception as exc:
            logger.warning("[reverse_searcher_top3] AI ranking unavailable: %s", type(exc).__name__)
        return selected, notes

    async def _preview_bytes(self, network: Network, hit: SearchHit) -> bytes | None:
        for url in dict.fromkeys((hit.preview_url, hit.thumbnail_url)):
            if not url:
                continue
            try:
                data = await network.download(url)
                with Image.open(io.BytesIO(data)) as image:
                    image.verify()
                with Image.open(io.BytesIO(data)) as image:
                    if image.format not in ("JPEG", "PNG", "GIF"):
                        output = io.BytesIO()
                        image.convert("RGB").save(output, format="JPEG", quality=92)
                        data = output.getvalue()
                return data
            except Exception:
                continue
        return None

    async def result_chain(self, report: SearchReport, selected: list[SearchHit], notes: dict[int, str], query: str = "") -> list:
        """Build the model-ranked answer with inline previews."""
        if not selected:
            default = "🔎 图像线索｜五站检索\n\n暂时没有找到可信的匹配结果。"
            return [Plain(render_template(self.config.get("result_empty_text", default), default, query=query, count=0))]
        async with Network(proxies=self.config.get("proxies") or None, timeout=25) as network:
            previews = await asyncio.gather(
                *(self._preview_bytes(network, hit) for hit in selected)
            )
        common = {"query": query, "count": len(selected)}
        chain = [Plain(render_template(self.config.get("result_header_template", DEFAULT_HEADER), DEFAULT_HEADER, **common))]
        for number, (hit, preview) in enumerate(zip(selected, previews), 1):
            original_id = report.hits.index(hit) + 1
            account_line = f"\n👤 {hit.account}" if hit.account else ""
            reason_line = f"\n✦ {notes[original_id]}" if notes.get(original_id) else ""
            detail = render_template(
                self.config.get("result_item_template", DEFAULT_ITEM),
                DEFAULT_ITEM,
                rank=number,
                rank_symbol=("①", "②", "③")[number - 1],
                platform=hit.platform,
                match=hit.evidence,
                keyword=hit.title or hit.source or "相似图片",
                name=hit.title,
                account=hit.account,
                account_line=account_line,
                reason=notes.get(original_id, ""),
                reason_line=reason_line,
                engines="、".join(LABELS[name] for name in ENGINES if name in hit.engines),
                **common,
            )
            chain.append(Plain(detail))
            if preview:
                chain.append(AstrImage.fromBytes(preview))
            else:
                default = "\n预览图：暂无"
                chain.append(Plain(render_template(self.config.get("result_no_preview_text", default), default, **common)))
        if report.failures:
            failed = "；".join(f"{LABELS[name]}：{report.failures[name]}" for name in ENGINES if name in report.failures)
            default = "\n未完成的引擎：{failures}"
            chain.append(Plain(render_template(self.config.get("result_failures_template", default), default, failures=failed, **common)))
        chain.append(Plain(render_template(self.config.get("result_footer_template", DEFAULT_FOOTER), DEFAULT_FOOTER, **common)))
        return chain
