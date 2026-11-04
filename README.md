# reverse_searcher

根据用户意图和图片内容选择搜图引擎，复核候选后返回图文结果。支持 AnimeTrace、SauceNAO、E-Hentai、Google Lens 和 Yandex。结果包含平台、关键词、匹配说明、名字与账号（有数据时）及预览图，不展示来源链接。

## 搜索方式

AI 判断用户是否有搜图需求，再结合原图选择引擎：

| 需求 | 优先引擎 |
| --- | --- |
| 这是谁、哪个角色、cos 角色 | AnimeTrace |
| 插画出处、作者、Pixiv PID | SauceNAO |
| 相似图、像这个的图片 | Yandex |
| 本子、同人作品 | E-Hentai |
| 原图、综合搜索 | Google Lens |

画面也参与判断：普通真人照片、商品、风景、梗图或类型不明确时使用 Google Lens 和 Yandex；不会仅凭暴露衣着或封面排版判定为本子。专门引擎没有结果、请求失败，或模型认为候选与原图不匹配时，也会补搜 Google Lens 和 Yandex。用户明确指定引擎时只查该引擎。

普通结果从本次调用的引擎中选择相关候选，首次最多展示五条，不凑满三条。无法确认出处时，展示 Google Lens 和 Yandex 的可用近似候选并说明不确定性。没有候选的引擎列明数量或失败原因。

统计只包含实际请求过的引擎，例如：

```text
搜索结果候选：E-Hentai 0 · Google Lens 2 · Yandex 5
未完成的引擎：E-Hentai：E-Hentai 返回 HTTP 403
```

## 追问候选

结果末尾会由 AI 提醒可以继续追问，例如：

- “把 Yandex 的全部结果给我”
- “看看 SauceNAO 的所有候选”
- “再用 Google 搜一下”

同一会话、同一发送者默认有十分钟的追问时间。已经搜索过的引擎直接展开缓存中的候选（每个引擎最多六条），不再次请求搜索服务；尚未搜索的引擎会用上次的原图补搜。新图片开始新的搜索，其他人的图片不会接续你的请求。

每个引擎最多保留六条候选，用于模型复核、统计、缓存和追问。SauceNAO 请求数量为六条；不能指定返回数量的引擎也只取前六条，不翻页抓取更多。追问‘全部候选’只展开该引擎缓存中的这六条以内的结果。

## 安装与使用

使用 AstrBot 4.23.2 或更新版本。将仓库目录放到 `data/plugins/reverse_searcher`，安装 `requirements.txt` 中的依赖并重启 AstrBot。

可发送“以图搜图”“找鸡”“搜图”加图片，也可引用含图片的消息提问，或先提出搜图要求、下一条再发图片。仅发图片、评价图片、普通聊天不会主动触发搜图。工具名为 `reverse_search_top3`，实际行为按意图路由，不限制返回三条。

结果经过模型复核并生成文字，预览图附在对应条目后，最终通过 AstrBot 正常回复流程作为一条图文消息发送。等待提示是单独的消息。需要关闭 AstrBot 的流式输出及对所有结果启用的分段回复，才能保持完整图文结果在同一条消息里。

## 配置

- `ranking_provider_id`：选择 AstrBot 已有模型，用于图片路由、候选复核和回复生成；推荐支持图片的模型，留空使用当前会话模型。
- `serpapi_key` / `zenserp_key`：单个 Google 搜图 API Key。
- `serpapi_keys` / `zenserp_keys`：多 Key 列表，每项填一个 Key。与对应单 Key 合并去重后组成轮询池；至少配置一个有效 Key。
- `saucenao_api_key`：SauceNAO API Key。
- `proxies`：访问搜索服务的 HTTP 代理地址。
- `yandex_cookies` / `ehentai_cookies`：对应引擎的 Cookie。
- `allow_third_party_image_host`：允许 Google Lens、Yandex 将消息图片上传到临时图床；默认开启。图床地址必须实际返回有效图片，否则尝试其他图床。
- `trigger_keywords`：唤醒 AI 判断搜图需求的关键词。
- `keyword_image_wait_seconds` / `ai_image_wait_seconds`：两种入口等待下一张图片的秒数，默认各 120 秒。
- `candidate_cache_seconds`：候选及原图的追问有效时间，默认 600 秒，每次追问后续期。

Google API 凭据按 SerpAPI、Zenserp 的配置顺序组成轮询池，每次搜索轮换起始位置。遇到 HTTP 错误、API 业务错误或连接失败时继续尝试下一组凭据，每组在本次搜索中最多尝试一次。切换凭据复用同一个已验证的图片 URL，不重复上传。正常的空结果不会消耗其他 Key 重搜；所有凭据都失败时显示具体失败原因。

回复配置是给 AI 的提示词，由模型结合当前事实生成文案：

| 配置项 | 控制内容 |
| --- | --- |
| `keyword_image_wait_prompt` | 关键词触发后请用户补图 |
| `ai_image_wait_prompt` | AI 调用工具后请用户补图 |
| `search_progress_prompt` | 搜索时的等待语；留空不发送 |
| `result_header_template` | 结果标题 |
| `result_item_template` | 每条候选的措辞和排列方式 |
| `result_footer_template` | 结果结尾 |
| `result_empty_text` | 没有候选时的说明 |
| `result_no_preview_text` | 预览不可用时的说明 |
| `result_followup_prompt` | 查看全部候选或指定引擎补搜的提醒 |

例如等待语提示词可写“用一句俏皮的话让用户等一下，带一个放大镜表情”；结果提示词可写“分行列出平台、匹配情况和关键词，有名字与账号时补上，语气简洁”。实际等待秒数、候选数据和缓存时长会自动提供给模型。

等待语、结果标题、条目和结尾的配置键保留，内容作为写作要求或风格参考交给模型，不再直接格式化发送。名称仍带 `_template` 的配置项也按提示词处理。候选数量及失败原因由程序准确附加，使用固定标题“搜索结果候选：”和“未完成的引擎：”。

## 拒绝后的搜图

AI 正常判断是否调用工具。明确的搜图需求被拒绝时，插件补调工具；其余请求结合最近对话判断。“这张呢”一类短追问若被明确拒绝查找出处，也会补搜。拒绝查看缓存中的指定引擎候选时同样支持补调。普通图片聊天不会因出现拒绝措辞就触发搜索。

## 已知限制

- 缓存仅保存在内存中，插件重启或有效时间结束后需要重新提供图片。
- 模型不支持图片或路由失败时使用 Google Lens 和 Yandex；无法复核时只展示近似候选，不确认出处。文案生成失败时使用简短的数据摘要。
- 首次复核最多使用三个引擎各六条候选及其可用预览，模型需支持相应图片数量。全部候选展开可能生成较长的消息，受聊天平台消息大小限制。
- 引擎可能只提供缩略图，原图不可访问时使用缩略图；两者都不可用时没有预览图。
- Google Lens 需要有效 Key；Yandex 可能要求验证码；E-Hentai 的 HTTP 403 等错误会保留原因，不能保证外站允许访问。
- 引用消息需要平台适配器提供图片组件。普通群聊提问沿用 AstrBot 的唤醒规则，需要先唤醒机器人。

## 开发验证

安装 `requirements.txt` 及 AstrBot 使用的 `pydantic` 后，可执行 `python -m unittest discover -s tests -v`。测试使用模拟模型与搜索响应，覆盖引擎路由、补搜、候选完整性、缓存隔离、文案生成及拒绝后的补调。

## 来源与许可

引擎请求和响应解析代码基于 [OMSociety/astrbot_plugin_reverse_searcher](https://github.com/OMSociety/astrbot_plugin_reverse_searcher) 修改。本仓库按原项目的 AGPL-3.0 许可发布，详见 `LICENSE`。
