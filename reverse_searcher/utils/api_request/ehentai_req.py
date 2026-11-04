from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from astrbot.api import logger
from typing_extensions import override

from ..ext_tools import read_file
from ..response_parser import EHentaiResponse
from .base_req import BaseSearchReq


def access_issue(response) -> str:
    """Identify explicit access errors in the search response."""
    body = response.text.lower()
    headers = {key.lower(): value.lower() for key, value in response.headers.items()}
    if "your ip address has been temporarily banned" in body or "you have been temporarily banned" in body:
        return "页面提示临时访问限制，请等待限制解除"
    if "please wait a bit longer between each file search" in body or response.status_code == 429:
        return "请求过于频繁，请稍后再试"
    if headers.get("cf-mitigated") == "challenge" or (
        "just a moment" in body and "/cdn-cgi/challenge-platform/" in body
    ):
        return "页面要求浏览器访问验证"
    if ("must be logged in" in body or "you need to log in" in body
            or "act=login" in response.url.lower()):
        return "页面要求登录，请检查 E-Hentai Cookie"
    return ""


class EHentai(BaseSearchReq[EHentaiResponse]):
    """
    E-Hentai搜索请求类

    用于与E-Hentai/ExHentai图像搜索功能交互，支持多种搜索选项
    """

    def __init__(
        self,
        is_ex: bool = False,
        covers: bool = False,
        similar: bool = True,
        exp: bool = False,
        **request_kwargs: Any,
    ):
        """
        初始化E-Hentai搜索请求

        参数:
            is_ex: 是否使用ExHentai而不是E-Hentai
            covers: 是否搜索封面图像
            similar: 是否搜索相似图像
            exp: 是否使用扩展搜索
            **request_kwargs: 其他请求参数
        """
        base_url = "https://upld.exhentai.org" if is_ex else "https://upld.e-hentai.org"
        super().__init__(base_url, **request_kwargs)
        self.is_ex: bool = is_ex
        self.covers: bool = covers
        self.similar: bool = similar
        self.exp: bool = exp

    @override
    async def search(
        self,
        url: str | None = None,
        file: str | bytes | Path | None = None,
        **kwargs: Any,
    ) -> EHentaiResponse:
        """
        执行E-Hentai图像搜索

        参数:
            url: 图像URL
            file: 本地文件内容
            **kwargs: 其他搜索参数

        返回:
            EHentaiResponse: 搜索响应对象

        异常:
            ValueError: 当未提供url或file参数时抛出
        """
        endpoint = "upld/image_lookup.php" if self.is_ex else "image_lookup.php"
        data: dict[str, Any] = {"f_sfile": "File Search"}
        if url:
            # Add filename to tuple to enable Similarity Search
            files = {
                "sfile": (
                    "image.jpg",
                    await self.download(url),
                    "application/octet-stream",
                )
            }
        elif file:
            file_bytes = read_file(file)
            files = {"sfile": ("image.jpg", file_bytes, "application/octet-stream")}
        else:
            raise ValueError("Either 'url' or 'file' must be provided")
        if self.covers:
            data["fs_covers"] = "on"
        if self.similar:
            data["fs_similar"] = "on"
        if self.exp:
            data["fs_exp"] = "on"
        has_cookie = bool(self._client.cookies)
        resp = await self._send_request(
            method="post",
            endpoint=endpoint,
            data=data,
            files=files,
        )
        issue = access_issue(resp)
        if resp.status_code >= 400 or issue:
            label = f"E-Hentai 返回 HTTP {resp.status_code}" if resp.status_code >= 400 else "E-Hentai 搜索未完成"
            detail = issue or "响应未提供具体拒绝原因"
            if resp.status_code == 403 and not has_cookie:
                detail += "；未提供登录 Cookie"
            logger.warning("[E-Hentai] HTTP %s at %s; cookie_present=%s; %s",
                           resp.status_code, urlsplit(resp.url).hostname, has_cookie, detail)
            raise ValueError(f"{label}（{detail}）")
        return EHentaiResponse(resp.text, resp.url)
