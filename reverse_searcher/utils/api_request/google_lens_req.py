import json
from typing import Any

from astrbot.api import logger
from typing_extensions import override

from ..response_parser.google_lens_parser import GoogleLensResponse
from .base_req import BaseSearchReq


class SearchAPIError(ValueError):
    """API 请求失败，可继续尝试池中的其他凭据。"""


class GoogleLensSerpApi(BaseSearchReq[GoogleLensResponse]):
    """SerpApi 实现：通过 Google Lens 引擎搜索图片。

    SerpApi 只接受公开 URL，本地文件需要先上传到图床再传入。
    """

    def __init__(self, api_key: str, network: Any = None, **kwargs: Any):
        # 转发共享 network（代理/超时配置），避免子引擎各自新建且不关闭的 client
        super().__init__("https://serpapi.com/search", network=network)
        self.api_key = api_key
        self.engine = "google_lens"

    @override
    async def search(
        self, file: bytes | None = None, url: str | None = None, **kwargs: Any
    ) -> GoogleLensResponse:
        """执行 SerpApi Google Lens 搜索。

        参数:
            file: 本地图片 bytes（会上传到图床后转 URL）
            url: 公开图片 URL（优先级高于 file）
            kwargs: 可包含 country, hl, q, no_cache

        返回:
            GoogleLensResponse 包含序列化后的搜索结果 JSON
        """
        logger.info("[SerpApi] Searching via Google Lens engine...")

        params = {
            "engine": self.engine,
            "api_key": self.api_key,
            "country": kwargs.get("country", "HK"),
            "hl": kwargs.get("hl", "en"),
            "no_cache": kwargs.get("no_cache", False),
        }
        # q 是可选的补充关键词，无值时不发空参数
        if kwargs.get("q"):
            params["q"] = kwargs["q"]

        if url:
            params["url"] = url
        elif file:
            # SerpApi 只接受公开 URL，本地文件先上传到临时图床
            url = await self._upload_image(file)
            params["url"] = url

        data = await self._fetch_serpapi(params)

        return GoogleLensResponse(
            resp_data=json.dumps(data),
            resp_url=f"https://serpapi.com/search?engine={self.engine}",
            **kwargs,
        )

    async def _fetch_serpapi(self, params: dict) -> dict:
        """调用 SerpApi 搜索接口并返回 JSON 结果"""
        resp = await self._client.get("https://serpapi.com/search", params=params)
        if resp.status_code >= 400:
            raise SearchAPIError(f"SerpAPI 返回 HTTP {resp.status_code}")
        data = resp.json()
        if data.get("error"):
            detail = str(data["error"]).replace(self.api_key, "[redacted]")[:160]
            raise SearchAPIError(f"SerpAPI：{detail}")
        return data


class GoogleLensZenserp(BaseSearchReq[GoogleLensResponse]):
    """Zenserp 实现：通过 Google Reverse Image 引擎搜索图片。

    同样只接受公开 URL，本地文件需先上传到图床。
    """

    def __init__(self, api_key: str, network: Any = None, **kwargs: Any):
        super().__init__("https://app.zenserp.com/api/v2/search", network=network)
        self.api_key = api_key

    @override
    async def search(
        self, file: bytes | None = None, url: str | None = None, **kwargs: Any
    ) -> GoogleLensResponse:
        """执行 Zenserp Google Reverse Image 搜索。

        参数:
            file: 本地图片 bytes（会上传到图床后转 URL）
            url: 公开图片 URL（优先级高于 file）
            kwargs: 可包含 country, hl

        返回:
            GoogleLensResponse
        """
        logger.info("[Zenserp] Searching via Google Reverse Image...")

        headers = {"apikey": self.api_key}
        params = {
            "gl": kwargs.get("country", "CN"),
            "hl": kwargs.get("hl", "zh-CN"),
        }

        if url:
            params["image_url"] = url
        elif file:
            url = await self._upload_image(file)
            params["image_url"] = url

        data = await self._fetch_zenserp(headers, params)

        return GoogleLensResponse(
            resp_data=json.dumps(data),
            resp_url="https://app.zenserp.com/api/v2/search",
            **kwargs,
        )

    async def _fetch_zenserp(self, headers: dict, params: dict) -> dict:
        """调用 Zenserp 搜索接口并返回 JSON 结果"""
        resp = await self._client.get(
            "https://app.zenserp.com/api/v2/search", headers=headers, params=params
        )
        if resp.status_code >= 400:
            raise SearchAPIError(f"Zenserp 返回 HTTP {resp.status_code}")
        data = resp.json()
        if data.get("error"):
            detail = str(data["error"]).replace(self.api_key, "[redacted]")[:160]
            raise SearchAPIError(f"Zenserp：{detail}")
        return data


class GoogleLens(BaseSearchReq[GoogleLensResponse]):
    """轮流使用 Google 搜索 API 凭据，失败后继续尝试下一个。"""

    def __init__(self, network: Any = None, **kwargs: Any):
        super().__init__("https://google.com", network=network)
        self.engines = []
        api_keys = kwargs.get("api_keys", {})
        for provider, engine_class in (("serpapi", GoogleLensSerpApi), ("zenserp", GoogleLensZenserp)):
            keys = []
            for key in [api_keys.get(provider) or kwargs.get(f"{provider}_key"),
                        *(kwargs.get(f"{provider}_keys") or [])]:
                if isinstance(key, str) and key.strip() and key.strip() not in keys:
                    keys.append(key.strip())
            for number, key in enumerate(keys, 1):
                self.engines.append((f"{provider} #{number}", engine_class(key, network=network or self)))
        if self.engines:
            start = kwargs.get("start_index", 0) % len(self.engines)
            self.engines = self.engines[start:] + self.engines[:start]

    @override
    async def search(
        self, file: bytes | None = None, url: str | None = None, **kwargs: Any
    ) -> GoogleLensResponse:
        """同一次搜图只上传一次图片，每组 API 凭据最多尝试一次。"""
        if not self.engines:
            raise ValueError("未配置 SerpAPI/Zenserp Key")
        if not url:
            if not file:
                raise ValueError("请提供图片文件或 URL")
            url = await self._upload_image(file)
        failures = []
        for label, engine in self.engines:
            try:
                return await engine.search(url=url, **kwargs)
            except Exception as exc:
                detail = str(exc) if isinstance(exc, SearchAPIError) else type(exc).__name__
                failures.append(f"{label}：{detail}")
                logger.warning("[GoogleLens] %s failed: %s", label, detail)
        raise RuntimeError("Google Lens API 均失败：" + "；".join(failures))
