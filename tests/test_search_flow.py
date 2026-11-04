"""Search workflow tests with simulated providers and AstrBot events."""

import importlib
import json
import logging
import sys
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Generic, TypeVar
from unittest.mock import AsyncMock, patch

import httpx


def module(name, **attributes):
    value = types.ModuleType(name)
    value.__dict__.update(attributes)
    sys.modules[name] = value
    return value


class Plain:
    def __init__(self, text):
        self.text = text


class Image:
    def __init__(self, file=""):
        self.file = file

    async def convert_to_base64(self):
        return "bmV3"

    @classmethod
    def fromBytes(cls, data):
        return cls(data)


class Reply:
    def __init__(self, chain):
        self.chain = chain


class TextPart(Plain):
    def mark_as_temp(self):
        return self


class MessageChain:
    def __init__(self, chain):
        self.chain = chain


class Star:
    def __init__(self, context):
        self.context = context


T = TypeVar("T")


class FunctionTool(Generic[T]):
    pass


class ContextWrapper(Generic[T]):
    pass


decorator = lambda *args: lambda fn: fn
module("astrbot")
module("astrbot.api", logger=logging.getLogger("test"))
module("astrbot.api.message_components", Plain=Plain, Image=Image, Reply=Reply)
module("astrbot.api.star", Star=Star, Context=object)
module("astrbot.api.event", AstrMessageEvent=object, filter=SimpleNamespace(
    event_message_type=decorator, EventMessageType=SimpleNamespace(ALL="all"),
    on_agent_done=decorator, on_llm_request=decorator, on_decorating_result=decorator,
))
module("astrbot.core")
module("astrbot.core.agent")
module("astrbot.core.agent.message", TextPart=TextPart)
module("astrbot.core.agent.run_context", ContextWrapper=ContextWrapper)
module("astrbot.core.agent.tool", FunctionTool=FunctionTool)
module("astrbot.core.astr_agent_context", AstrAgentContext=object)
module("astrbot.core.message")
module("astrbot.core.message.message_event_result", MessageChain=MessageChain,
       ResultContentType=SimpleNamespace(GENERAL_RESULT="general"))
module("astrbot.core.utils")
module("astrbot.core.utils.astrbot_path", get_astrbot_data_path=lambda: str(Path(__file__).parent))
module("search_plugin", __path__=[str(Path(__file__).resolve().parents[1])])
search = importlib.import_module("search_plugin.reverse_searcher.search")
main = importlib.import_module("search_plugin.main")
google = importlib.import_module("search_plugin.reverse_searcher.utils.api_request.google_lens_req")
ehentai = importlib.import_module("search_plugin.reverse_searcher.utils.api_request.ehentai_req")


class Event:
    def __init__(self, text, parts=None, sender="user", origin="chat"):
        self.message_obj = SimpleNamespace(message_str=text, message=parts or [])
        self.unified_msg_origin = origin
        self.sender = sender
        self.extra = {}
        self.sent = []

    def get_sender_id(self):
        return self.sender

    def set_extra(self, key, value):
        self.extra[key] = value

    def get_extra(self, key):
        return self.extra.get(key)

    def plain_result(self, text):
        return text

    def stop_event(self):
        pass

    async def send(self, value):
        self.sent.append(value)


def context(event):
    return SimpleNamespace(context=SimpleNamespace(event=event), messages=[])


def provider(*replies):
    return SimpleNamespace(
        get_current_chat_provider_id=AsyncMock(return_value="vision"),
        llm_generate=AsyncMock(side_effect=[SimpleNamespace(completion_text=json.dumps(r, ensure_ascii=False))
                                          for r in replies]),
        add_llm_tools=lambda *args: None,
    )


def response(engine, count=2):
    if engine == "animetrace":
        return SimpleNamespace(raw=[SimpleNamespace(characters=[SimpleNamespace(name="角色", work="作品")])])
    return SimpleNamespace(raw=[SimpleNamespace(title=f"{engine}-{i}", url=f"https://example.com/{engine}/{i}")
                                for i in range(count)])


class FlowTests(unittest.IsolatedAsyncioTestCase):
    def service(self):
        service = search.SearchService({})
        service._search_one = AsyncMock(side_effect=lambda engine, image: response(engine))
        return service

    async def test_clear_intent_uses_one_engine_and_does_not_pad_results(self):
        service = self.service()
        llm = provider({"engine": "saucenao", "uncertain": False}, {"ids": [1], "certain": True})
        report, selected, certain = await service.run_search(b"image", "找作者", llm, "chat")
        self.assertEqual(list(report.counts), ["saucenao"])
        self.assertEqual(len(selected), 1)
        self.assertTrue(certain)
        self.assertEqual(llm.llm_generate.call_args_list[0].kwargs["image_urls"], ["base64://aW1hZ2U="])

    async def test_uncertain_specialist_adds_both_general_engines(self):
        service = self.service()
        llm = provider({"engine": "animetrace", "uncertain": False},
                       {"ids": [1], "certain": False}, {"ids": [], "certain": False})
        report, selected, certain = await service.run_search(b"image", "这是谁", llm, "chat")
        self.assertEqual(list(report.counts), ["animetrace", "google", "yandex"])
        self.assertEqual({hit.engine for hit in selected}, {"google", "yandex"})
        self.assertFalse(certain)
        self.assertEqual(service._search_one.await_count, 3)

    async def test_routing_unavailable_uses_general_engines(self):
        service = self.service()
        llm = provider({"ids": [], "certain": False})
        llm.llm_generate.side_effect = [RuntimeError("vision unavailable"), SimpleNamespace(completion_text='{"ids":[],"certain":false}')]
        report, selected, _ = await service.run_search(b"image", "搜图", llm, "chat")
        self.assertEqual(list(report.counts), ["google", "yandex"])
        self.assertEqual(len(selected), 2)

    async def test_failure_falls_back_without_losing_error(self):
        service = self.service()
        async def request(engine, image):
            if engine == "ehentai":
                raise ValueError("E-Hentai 返回 HTTP 403")
            return response(engine)
        service._search_one = AsyncMock(side_effect=request)
        llm = provider({"engine": "ehentai", "uncertain": False}, {"ids": [1, 3], "certain": False})
        report, selected, _ = await service.run_search(b"image", "找同人", llm, "chat")
        self.assertEqual(report.failures["ehentai"], "E-Hentai 返回 HTTP 403")
        self.assertEqual({hit.engine for hit in selected}, {"google", "yandex"})

    async def test_explicit_engine_candidates_capped_at_six(self):
        service = self.service()
        service._search_one = AsyncMock(return_value=response("yandex", 8))
        llm = provider()
        report, selected, _ = await service.run_search(b"image", "Yandex全部结果", llm, "chat", engine="yandex", show_all=True)
        self.assertEqual(len(selected), 6)
        self.assertEqual(report.counts, {"yandex": 6})
        llm.llm_generate.assert_not_called()

    async def test_cached_followup_is_scoped_and_searches_missing_engine_only(self):
        service = self.service()
        report = await service.search(b"image", ["google"])
        service.remember(("chat", "user"), b"image", report, "搜图")
        self.assertIsNone(service.session(("chat", "other")))
        self.assertIsNone(service.session(("other", "user")))
        service.notice = AsyncMock(return_value="稍等")
        service.result_chain = AsyncMock(return_value=[Plain("候选")])
        tool = main.ReverseSearchTool()
        tool.inject_service(service, provider())
        event = Event("把 Google 的全部结果给我")
        await tool.call(context(event), engine="google", show_all=True)
        self.assertEqual(service._search_one.await_count, 1)
        self.assertEqual(event.sent, [])
        event = Event("把 Yandex 的全部结果给我")
        await tool.call(context(event), engine="yandex", show_all=True)
        self.assertEqual(service._search_one.await_count, 2)
        self.assertEqual(service._search_one.call_args.args, ("yandex", b"image"))
        self.assertEqual(event.sent, ["稍等"])
        self.assertEqual(len(event.get_extra("reverse_search_final_chain")), 1)
        service.session(("chat", "user")).expires_at = time.monotonic() - 1
        self.assertIsNone(service.session(("chat", "user")))

    async def test_new_image_does_not_reuse_previous_search(self):
        service = self.service()
        service.remember(("chat", "user"), b"old", search.SearchReport([], {}, {"yandex": 0}), "old")
        service.notice = AsyncMock(return_value="")
        service.result_chain = AsyncMock(return_value=[Plain("新结果")])
        tool = main.ReverseSearchTool()
        tool.inject_service(service, provider())
        await tool.call(context(Event("Yandex全部结果", [Image()])), engine="yandex", show_all=True)
        self.assertEqual(service._search_one.call_args.args, ("yandex", b"new"))

    async def test_model_writes_notice_and_all_items_with_exact_statistics(self):
        service = self.service()
        service.config["search_progress_prompt"] = "用猫娘语气提示等待"
        llm = provider()
        llm.llm_generate.return_value = SimpleNamespace(completion_text="正在找图喵")
        llm.llm_generate.side_effect = None
        self.assertEqual(await service.notice("search_progress_prompt", llm, "chat", query="搜图"), "正在找图喵")
        self.assertIn("用猫娘语气提示等待", llm.llm_generate.call_args.kwargs["prompt"])
        hits = search._from_response("yandex", response("yandex", 8))
        report = search.SearchReport(hits, {"ehentai": "E-Hentai 返回 HTTP 403"}, {"yandex": 6, "ehentai": 0})
        llm.llm_generate.return_value = SimpleNamespace(completion_text=json.dumps({
            "header": "🎯 近似候选", "items": [{"id": n, "text": f"🌐 平台：example.com\\n🔑 关键词：{n}"} for n in range(1, 7)],
            "footer": "可以继续追问其他引擎的全部候选。",
        }))
        service._preview_bytes = AsyncMock(return_value=b"preview")
        chain = await service.result_chain(report, hits, "全部结果", llm, "chat", show_all=True)
        self.assertEqual(sum(isinstance(part, Image) for part in chain), 6)
        text = "".join(part.text for part in chain if isinstance(part, Plain))
        self.assertIn("搜索结果候选：E-Hentai 0 · Yandex 6", text)
        self.assertIn("未完成的引擎：E-Hentai：E-Hentai 返回 HTTP 403", text)
        self.assertNotIn("五引擎", text)
        self.assertNotIn("AnimeTrace 0", text)

    async def test_refused_named_followup_uses_cache_without_image(self):
        plugin = main.ReverseSearcherTop3(provider({"search": True}), {})
        plugin.search_service.remember(("chat", "user"), b"image", search.SearchReport([], {}, {"google": 0}), "搜图")
        event = Event("把 Google 的全部结果给我")
        _ = [value async for value in plugin.prepare_search_request(event)]
        plugin.search_tool.call = AsyncMock(return_value="候选")
        resp = SimpleNamespace(role="assistant", completion_text="抱歉，无法提供", raw_completion=None, result_chain=MessageChain([]))
        await plugin.search_after_refusal(event, context(event), resp)
        plugin.search_tool.call.assert_awaited_once()
        self.assertEqual(resp.result_chain.chain[0].text, "候选")

    async def test_keyword_wait_prompt_generated_and_ordinary_chat_does_not_search(self):
        llm = provider()
        llm.llm_generate.side_effect = None
        llm.llm_generate.return_value = SimpleNamespace(completion_text="发图来吧，等你 45 秒")
        plugin = main.ReverseSearcherTop3(llm, {"keyword_image_wait_seconds": 45})
        event = Event("搜图")
        results = [value async for value in plugin.prepare_search_request(event)]
        self.assertEqual(results, ["发图来吧，等你 45 秒"])
        self.assertIn('"seconds": 45', llm.llm_generate.call_args.kwargs["prompt"])
        llm.llm_generate.return_value = SimpleNamespace(completion_text='{"search":false}')
        plugin.search_tool.call = AsyncMock()
        ordinary = Event("描述这张图片", [Image()])
        resp = SimpleNamespace(role="assistant", completion_text="抱歉，无法描述", raw_completion=None)
        await plugin.search_after_refusal(ordinary, context(ordinary), resp)
        plugin.search_tool.call.assert_not_awaited()

    async def test_short_quoted_image_followup_with_chain_only_refusal(self):
        plugin = main.ReverseSearcherTop3(provider(), {})
        plugin.search_tool.call = AsyncMock(return_value="图片线索")
        event = Event("这张呢", [Reply([Image("base64://aW1hZ2U=")])])
        resp = SimpleNamespace(role="assistant", completion_text="", raw_completion=None,
                               result_chain=MessageChain([Plain("我不能帮你找影片出处或链接")]))
        await plugin.search_after_refusal(event, context(event), resp)
        plugin.search_tool.call.assert_awaited_once()
        self.assertEqual(resp.result_chain.chain[0].text, "图片线索")

    async def test_incomplete_model_reply_keeps_all_cached_candidates(self):
        service = self.service()
        hits = search._from_response("yandex", response("yandex", 8))
        llm = provider({"header": "结果", "items": [{"id": 1, "text": "只写一条"}], "footer": ""})
        service._preview_bytes = AsyncMock(return_value=None)
        chain = await service.result_chain(search.SearchReport(hits, {}, {"yandex": 6}), hits,
                                           "全部结果", llm, "chat", show_all=True)
        text = "".join(part.text for part in chain if isinstance(part, Plain))
        for number in range(6):
            self.assertIn(f"yandex-{number}", text)

    async def test_all_engines_cap_six_and_sixth_candidate_reaches_model(self):
        service = self.service()
        async def many(engine, image):
            if engine == "animetrace":
                return SimpleNamespace(raw=[SimpleNamespace(characters=[
                    SimpleNamespace(name=f"角色{number}", work="作品") for number in range(10)
                ])])
            return response(engine, 10)
        service._search_one = AsyncMock(side_effect=many)
        report = await service.search(b"image", list(search.ENGINES))
        self.assertEqual(report.counts, {name: 6 for name in search.ENGINES})
        llm = provider({"ids": [6], "certain": True})
        selected, _ = await service.select(report, "搜图", "base64://image", llm, "chat")
        self.assertEqual(selected[0].rank, 6)
        service.remember(("chat", "user"), b"image", report, "搜图")
        self.assertEqual(len(service.session(("chat", "user")).report.hits), 30)

    async def test_service_advances_google_rotation_and_requests_six(self):
        service = search.SearchService({"serpapi_keys": ["one", "two"]})
        client = SimpleNamespace(search=AsyncMock(return_value=response("google")))
        with patch.object(search, "GoogleLens", return_value=client) as factory:
            await service._search_one("google", b"image")
            await service._search_one("google", b"image")
        self.assertEqual([call.kwargs["start_index"] for call in factory.call_args_list], [0, 1])
        self.assertEqual(client.search.call_args.kwargs["max_results"], 6)
        with patch.object(search, "SauceNAO", return_value=client) as factory:
            await service._search_one("saucenao", b"image")
        self.assertEqual(factory.call_args.kwargs["numres"], 6)


    async def test_keyword_engine_followup_does_not_request_another_image(self):
        plugin = main.ReverseSearcherTop3(provider({"search": True}), {})
        plugin.search_service.remember(("chat", "user"), b"image", search.SearchReport([], {}, {"yandex": 0}), "搜图")
        event = Event("搜图，给我 Yandex 的全部结果")
        self.assertEqual([value async for value in plugin.prepare_search_request(event)], [])
        self.assertTrue(event.get_extra("reverse_search_requested"))
        self.assertFalse(plugin.pending_search)

    async def test_engine_limits_and_cache_duration_follow_config(self):
        config = {f"{engine}_max_results": i + 1 for i, engine in enumerate(search.ENGINES)}
        config["candidate_cache_seconds"] = 95
        service = search.SearchService(config)
        async def many(engine, image):
            if engine == "animetrace":
                return SimpleNamespace(raw=[SimpleNamespace(characters=[
                    SimpleNamespace(name=str(i), work="作品") for i in range(10)
                ])])
            return response(engine, 10)
        service._search_one = AsyncMock(side_effect=many)
        report = await service.search(b"image", list(search.ENGINES))
        self.assertEqual(report.counts, {engine: i + 1 for i, engine in enumerate(search.ENGINES)})
        before = time.monotonic()
        service.remember(("chat", "user"), b"image", report, "搜图")
        self.assertGreaterEqual(service.session(("chat", "user")).expires_at, before + 95)
        self.assertLess(service.session(("chat", "user")).expires_at, time.monotonic() + 96)

    async def test_configured_limits_reach_quantity_parameters(self):
        service = search.SearchService({"saucenao_max_results": 9, "google_max_results": 7, "yandex_max_results": 4})
        client = SimpleNamespace(search=AsyncMock(return_value=response("google")))
        with patch.object(search, "SauceNAO", return_value=client) as factory:
            await service._search_one("saucenao", b"image")
            self.assertEqual(factory.call_args.kwargs["numres"], 9)
        with patch.object(search, "GoogleLens", return_value=client):
            await service._search_one("google", b"image")
            self.assertEqual(client.search.call_args.kwargs["max_results"], 7)
        with patch.object(search, "Yandex", return_value=client):
            await service._search_one("yandex", b"image")
            self.assertEqual(client.search.call_args.kwargs["max_results"], 4)

    async def test_hidden_low_match_candidates_keep_counts_and_no_match_is_not_no_results(self):
        service = self.service()
        llm = provider({"engine": "animetrace", "uncertain": False}, {"ids": [], "certain": False},
                       {"ids": [], "certain": False}, {"header": "找到近似候选", "items": [
                           {"id": 1, "text": "Google 候选"}, {"id": 2, "text": "Yandex 候选"}], "footer": "可以追问"})
        report, selected, certain = await service.run_search(b"image", "找角色", llm, "chat")
        service._preview_bytes = AsyncMock(return_value=None)
        chain = await service.result_chain(report, selected, "找角色", llm, "chat", certain=certain)
        text = "".join(part.text for part in chain if isinstance(part, Plain))
        self.assertIn("搜索结果候选：AnimeTrace 1 · Google Lens 2 · Yandex 2", text)
        self.assertIn("暂未展示的候选：AnimeTrace 1 · Google Lens 1 · Yandex 1", text)
        llm.llm_generate.side_effect = RuntimeError("offline")
        chain = await service.result_chain(report, [], "找角色", llm, "chat")
        self.assertIn("已搜到候选", chain[0].text)
        self.assertNotIn("没有取得候选", chain[0].text)

    async def test_same_message_additional_engine_retains_full_statistics(self):
        service = self.service()
        service.notice = AsyncMock(return_value="")
        service.result_chain = AsyncMock(return_value=[Plain("候选")])
        image = Image()
        image.convert_to_base64 = AsyncMock(return_value="aW1hZ2U=")
        event = Event("查 Google 和 Yandex 的候选", [image])
        tool = main.ReverseSearchTool()
        tool.inject_service(service, provider())
        await tool.call(context(event), engine="google", show_all=True)
        await tool.call(context(event), engine="yandex", show_all=True)
        report, selected = service.result_chain.call_args.args[:2]
        self.assertEqual(report.counts, {"google": 2, "yandex": 2})
        self.assertTrue(all(hit.engine == "yandex" for hit in selected))
        image.convert_to_base64.assert_awaited_once()
        later = Event("Google的全部结果")
        await tool.call(context(later), engine="google", show_all=True)
        self.assertEqual(service.result_chain.call_args.args[0].counts, {"google": 2, "yandex": 2})
        self.assertEqual(service._search_one.await_count, 2)

    async def test_chat_context_receives_counts_and_failures_for_current_image(self):
        plugin = main.ReverseSearcherTop3(provider(), {})
        plugin.search_service.remember(("chat", "user"), b"image",
            search.SearchReport([], {"ehentai": "HTTP 403"}, {"animetrace": 5, "ehentai": 0}), "找作者")
        req = SimpleNamespace(extra_user_content_parts=[])
        await plugin.add_followup_context(Event("这三个呢"), req)
        text = req.extra_user_content_parts[0].text
        self.assertIn('"AnimeTrace": 5', text)
        self.assertIn('"E-Hentai": "HTTP 403"', text)


class EHentaiAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_cookie_reaches_upload_request_and_generic_403_stays_unknown(self):
        seen = []
        def handler(request):
            seen.append(request.headers.get("cookie", ""))
            self.assertEqual(request.url.path, "/image_lookup.php")
            return httpx.Response(403, text="<title>Forbidden</title>")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), cookies={"ipb_member_id": "member-test"}) as client:
            request = ehentai.EHentai(network=SimpleNamespace(client=client))
            with self.assertRaises(ValueError) as caught:
                await request.search(file=b"image")
            self.assertIn("ipb_member_id=member-test", seen[0])
            self.assertIn("未提供具体拒绝原因", str(caught.exception))
            self.assertNotIn("未提供登录 Cookie", str(caught.exception))
            self.assertNotIn("member-test", str(caught.exception))

    async def test_explicit_site_errors_and_missing_cookie_are_reported(self):
        fixtures = [
            (403, "Forbidden", {}, "未提供登录 Cookie"),
            (403, "Challenge", {"cf-mitigated": "challenge"}, "浏览器访问验证"),
            (200, "You must be logged in to use this feature", {}, "页面要求登录"),
            (200, "Please wait a bit longer between each file search.", {}, "请求过于频繁"),
            (403, "Your IP address has been temporarily banned", {}, "临时访问限制"),
        ]
        for status, body, headers, expected in fixtures:
            with self.subTest(status=status, expected=expected):
                async with httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda request: httpx.Response(status, text=body, headers=headers)
                )) as client:
                    request = ehentai.EHentai(network=SimpleNamespace(client=client))
                    with self.assertRaises(ValueError) as caught:
                        await request.search(file=b"image")
                    self.assertIn(expected, str(caught.exception))


class GooglePoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_rotates_keys_and_fails_over_without_uploading_again(self):
        seen = []
        def handler(request):
            key = request.url.params.get("api_key")
            seen.append(key)
            if key == "limited":
                return httpx.Response(429, json={"error": "quota"})
            return httpx.Response(200, json={"visual_matches": [
                {"title": "match", "link": "https://example.com/post", "thumbnail": "https://example.com/image.jpg"}
            ]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            network = SimpleNamespace(client=client)
            request = google.GoogleLens(network=network, serpapi_key="limited",
                                        serpapi_keys=["limited", "working"], start_index=0)
            request._upload_image = AsyncMock(return_value="https://example.com/image.jpg")
            result = await request.search(file=b"image", max_results=6)
            self.assertEqual(len(result.raw), 1)
            self.assertEqual(seen, ["limited", "working"])
            request._upload_image.assert_awaited_once()
            seen.clear()
            request = google.GoogleLens(network=network, serpapi_keys=["limited", "working"], start_index=1)
            await request.search(url="https://example.com/image.jpg")
            self.assertEqual(seen, ["working"])

    async def test_json_error_switches_to_zenserp_and_empty_success_stops(self):
        seen = []
        def handler(request):
            seen.append(request.url.host)
            if request.url.host == "serpapi.com":
                return httpx.Response(200, json={"error": "invalid key"})
            self.assertEqual(request.headers["apikey"], "zen-key")
            return httpx.Response(200, json={"image_results": []})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            request = google.GoogleLens(network=SimpleNamespace(client=client),
                                        serpapi_keys=["serp-key"], zenserp_keys=["zen-key", "unused"])
            result = await request.search(url="https://example.com/image.jpg")
            self.assertEqual(result.raw, [])
            self.assertEqual(seen, ["serpapi.com", "app.zenserp.com"])

    async def test_all_failures_report_status_without_keys(self):
        def handler(request):
            if request.url.params.get("api_key") == "secret-one":
                return httpx.Response(401, text="secret-one invalid")
            return httpx.Response(200, json={"error": "invalid secret-two"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            request = google.GoogleLens(network=SimpleNamespace(client=client), serpapi_keys=["secret-one", "secret-two"])
            with self.assertRaises(RuntimeError) as caught:
                await request.search(url="https://example.com/image.jpg")
            self.assertIn("HTTP 401", str(caught.exception))
            self.assertIn("serpapi #2", str(caught.exception))
            self.assertNotIn("secret-one", str(caught.exception))
            self.assertNotIn("secret-two", str(caught.exception))

if __name__ == "__main__":
    unittest.main()
