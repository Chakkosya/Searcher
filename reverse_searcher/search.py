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
DEFAULT_HEADER = "🎯 **找图结果**"
DEFAULT_ITEM = "\n\n{rank_symbol} 🌐 平台：{platform}\n🔎 匹配：{match}\n🔑 关键词：{keyword}{name_line}{account_line}"
DEFAULT_FOOTER = "\n\n⚠️ 识图存在误差，请自行核对哦～"
DEFAULT_ENGINE_SUMMARY = "\n\n📡 五引擎候选：{engine_counts}"
OLD_DEFAULTS = {
    "result_header_template": "🔎 图像线索｜五站检索\n按匹配证据整理的 {count} 条候选：",
    "result_item_template": "\n\n{rank_symbol} {platform} · {match}\n{keyword}{account_line}{reason_line}",
    "result_footer_template": "\n\n识图线索可能有误，请对照原图核实。",
    "result_empty_text": "🔎 图像线索｜五站检索\n\n暂时没有找到可信的匹配结果。",
}


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
        parts = urlsplit(self.url)
        if (parts.hostname or "").lower() in ("x.com", "www.x.com", "twitter.com", "www.twitter.com"):
            name = parts.path.strip("/").split("/")[0]
            if name and name not in ("i", "search", "home"):
                return f"@{name}"
        return ""

@dataclass
class SearchReport:
    hits: list[SearchHit]
    failures: dict[str, str]
    counts: dict[str, int] = field(default_factory=dict)

def _normal_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, ""))


def _from_response(engine: str, response) -> list[SearchHit]:
    hits = []
    if engine == "animetrace":
        rank = 0
        for item in response.raw[:5]:
            for character in item.characters:
                rank += 1
                hits.append(SearchHit(engine, rank, character.name, source=character.work))
                if rank == 5:
                    return hits
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

    def _template(self, key: str, default: str) -> str:
        value = self.config.get(key, default)
        return default if value == OLD_DEFAULTS.get(key) else value

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
        counts: dict[str, int] = {}
        for engine, response in zip(ENGINES, responses):
            if isinstance(response, BaseException):
                logger.warning("[reverse_searcher_top3] %s search failed: %s", engine, type(response).__name__)
                failures[engine] = str(response)[:120] if isinstance(response, ValueError) else type(response).__name__
                counts[engine] = 0
                continue
            engine_hits = _from_response(engine, response)
            counts[engine] = len(engine_hits)
            hits.extend(engine_hits)
        return SearchReport(hits, failures, counts)

    async def rank(self, report: SearchReport, query: str, context, origin: str, image_ref: str | None = None):
        """Let the configured AstrBot model choose three candidates from all engines."""
        hits = report.hits
        if not hits:
            return [], {}

        page_engines: dict[str, set[str]] = {}
        page_hits: dict[str, list[SearchHit]] = {}
        for hit in hits:
            if hit.url:
                page = _normal_url(hit.url)
                page_engines.setdefault(page, set()).add(hit.engine)
                page_hits.setdefault(page, []).append(hit)
        for page, group in page_hits.items():
            best_preview = next(
                (hit.preview_url for hit in group if hit.preview_url and hit.preview_url != hit.thumbnail_url),
                "",
            )
            for hit in group:
                hit.engines.update(page_engines[page])
                if best_preview:
                    hit.preview_url = best_preview

        candidates = [
            {
                "id": number,
                "platform": hit.platform,
                "title": hit.title[:180],
                "page": hit.url[:300],
                "name": hit.author[:80],
                "account": hit.account[:80],
                "source": hit.source[:120],
                "similarity": hit.similarity,
                "engine": hit.engine,
                "same_page_engines": sorted(page_engines.get(_normal_url(hit.url), {hit.engine})),
                "has_preview": bool(hit.preview_url or hit.thumbnail_url),
                "engine_rank": hit.rank,
                "exact_page": hit.engine == "google" and hit.group in ("exact", "pages"),
            }
            for number, hit in enumerate(hits, 1)
        ]
        prompt = (
            "用户问题：" + (query or "请找出这张图片最可能的出处或角色") + "\n"
            "五个以图搜图引擎的候选结果：\n"
            + json.dumps(candidates, ensure_ascii=False)
            + "\n各引擎返回数：" + json.dumps(report.counts, ensure_ascii=False)
            + "\n未完成的引擎：" + json.dumps(report.failures, ensure_ascii=False)
            + "\n每个引擎最多提供五条原始候选。选最符合用户问题、最可能对应原图的前三条，"
            "同一页面只选一次。优先考虑明确相似度、多个引擎相互印证、原图页面和具体名称；"
            "相似度为零表示该引擎没有提供分数，不表示零匹配；不要把不同引擎的分数当成同一概率。"
            "相关性接近时优先选择有预览图的候选。"
            "只返回 JSON：{\"ids\":[候选id],\"notes\":{\"id\":\"一句简短的匹配依据\"}}。"
            "不要输出链接，不要补造候选中没有的名字或账号。"
        )
        image_urls = [image_ref] if image_ref else []
        preview_ids = []
        for number, hit in enumerate(hits, 1):
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
        required = min(3, len({
            _normal_url(hit.url) if hit.url else f"{hit.engine}:{hit.title}:{hit.source}"
            for hit in hits
        }))

        def parse_selection(response):
            raw = response.completion_text or ""
            data = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
            ids = []
            seen = set()
            for value in data.get("ids", []):
                number = int(value)
                if 1 <= number <= len(hits):
                    hit = hits[number - 1]
                    key = _normal_url(hit.url) if hit.url else f"{hit.engine}:{hit.title}:{hit.source}"
                    if key not in seen:
                        ids.append(number)
                        seen.add(key)
                if len(ids) == required:
                    break
            if len(ids) != required:
                raise ValueError("模型未选出足够的不同候选")
            raw_notes = data.get("notes", {})
            notes = {
                number: re.sub(r"https?://\S+", "", str(raw_notes.get(str(number), "")))[:100].strip()
                for number in ids
            }
            return [hits[number - 1] for number in ids], notes

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
                    return parse_selection(response)
                except Exception as exc:
                    logger.warning("[reverse_searcher_top3] Visual ranking unavailable: %s", type(exc).__name__)
            response = await context.llm_generate(**request)
            return parse_selection(response)
        except Exception as exc:
            logger.warning("[reverse_searcher_top3] AI ranking unavailable: %s", type(exc).__name__)
            raise ValueError("复核模型未能从搜图候选中选出前三条；请检查所选模型") from exc

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
        engine_counts = " · ".join(f"{LABELS[name]} {report.counts.get(name, 0)}" for name in ENGINES)
        common = {"query": query, "count": len(selected), "engine_counts": engine_counts}
        summary = render_template(
            self.config.get("result_engine_summary_template", DEFAULT_ENGINE_SUMMARY),
            DEFAULT_ENGINE_SUMMARY,
            **common,
        )
        if not selected:
            default = "🎯 **找图结果**\n\n暂时没有找到可信的匹配结果。"
            text = render_template(self._template("result_empty_text", default), default, **common) + summary
            if report.failures:
                failed = "；".join(f"{LABELS[name]}：{report.failures[name]}" for name in ENGINES if name in report.failures)
                default = "\n未完成的引擎：{failures}"
                text += render_template(self.config.get("result_failures_template", default), default, failures=failed, **common)
            return [Plain(text)]
        async with Network(proxies=self.config.get("proxies") or None, timeout=25) as network:
            previews = await asyncio.gather(
                *(self._preview_bytes(network, hit) for hit in selected)
            )
        chain = [Plain(render_template(self._template("result_header_template", DEFAULT_HEADER), DEFAULT_HEADER, **common))]
        for number, (hit, preview) in enumerate(zip(selected, previews), 1):
            original_id = report.hits.index(hit) + 1
            name_line = f"\n👤 名字：{hit.author}" if hit.author and hit.author.lstrip("@") != hit.account.lstrip("@") else ""
            account_line = f"\n🆔 账号：{hit.account}" if hit.account else ""
            reason_line = f"\n📌 依据：{notes[original_id]}" if notes.get(original_id) else ""
            keyword = hit.title or hit.source or "相似图片"
            if keyword.startswith(("http://", "https://")):
                keyword = "相似图片"
            detail = render_template(
                self._template("result_item_template", DEFAULT_ITEM),
                DEFAULT_ITEM,
                rank=number,
                rank_symbol=("①", "②", "③")[number - 1],
                platform=hit.platform,
                match=hit.evidence,
                keyword=keyword,
                name=hit.author,
                account=hit.account,
                name_line=name_line,
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
        if summary:
            chain.append(Plain(summary))
        if report.failures:
            failed = "；".join(f"{LABELS[name]}：{report.failures[name]}" for name in ENGINES if name in report.failures)
            default = "\n未完成的引擎：{failures}"
            chain.append(Plain(render_template(self.config.get("result_failures_template", default), default, failures=failed, **common)))
        chain.append(Plain(render_template(self._template("result_footer_template", DEFAULT_FOOTER), DEFAULT_FOOTER, **common)))
        return chain
