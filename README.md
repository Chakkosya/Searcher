# Search Image

AstrBot 插件：同一张图片同时交给 AnimeTrace、SauceNAO、E-Hentai、Google Lens 和 Yandex 搜索，每个引擎取前五条候选，交给指定的 AstrBot 模型选择全局前三条。消息包含平台、匹配情况、关键词、名字与账号（有数据时）及预览图；不展示页面链接，也不生成卡片。

## 安装

使用 AstrBot 4.23.2 或更新版本。将本仓库目录放到 AstrBot 的 `data/plugins/reverse_searcher`，安装 `requirements.txt` 中的依赖并重启 AstrBot。可直接发送“以图搜图”“找鸡”或“搜图”加图片，也可回复一条含图片的消息提问。也可以先提出搜图要求、再发送图片；等待时长在插件配置中设置，同一会话和发送者的图片会接续该请求。插件提供 `reverse_search_top3` 工具，由 AI 根据用户问题判断是否调用。

工具会把五个引擎各自的前五条原始候选交给指定的 AstrBot 模型复核，由模型选出不重复的前三条；结果经 AstrBot 的正常回复流程作为一条图文消息发送，不由工具主动发送最终结果。每条线索后附预览图，优先使用引擎给出的原图地址，失败时使用缩略图。开始搜索时会先发送一条可配置的等待提示。

## 配置

在 AstrBot 插件设置中填写：

- `ranking_provider_id`：选择已经在 AstrBot 配好的聊天模型，用于按用户问题复核五个引擎的候选结果；留空使用当前会话模型。支持图像的模型还会收到原图和所有可用的候选预览图。
- `serpapi_key` 或 `zenserp_key`：Google Lens 所需，二者至少填一个。
- `saucenao_api_key`：SauceNAO 的 API Key，建议填写。
- `proxies`：服务器访问这些引擎需要代理时填写。
- `yandex_cookies`：Yandex 出现验证码时填写。
- `ehentai_cookies`：需要登录搜索时填写。
- `allow_third_party_image_host`：Google Lens 和 Yandex 搜索消息图片时需先把图片上传到临时图床；默认开启。
- `trigger_keywords`：默认 `以图搜图`、`找鸡`、`搜图`。
- `keyword_image_wait_seconds` / `ai_image_wait_seconds`：关键词或 AI 工具触发后，等待下一张图片的秒数，默认各 120 秒。
- `keyword_image_wait_prompt` / `ai_image_wait_prompt`：两种入口的等待提示。可写 `{seconds}`，发送时替换为对应秒数。
- `search_progress_prompt`：开始搜索时发送的等待提示；留空则不发送。
- `result_header_template` / `result_item_template` / `result_footer_template`：结果标题、每条线索、结尾的文字格式。每条预览图固定跟在该条线索文字后。
- `result_engine_summary_template`：显示五个引擎各自返回的候选数量；留空则不显示。
- `result_empty_text` / `result_no_preview_text` / `result_failures_template`：无匹配、无预览图、部分引擎失败时的文字。

结果模板可使用 `{query}`（用户问题）和 `{count}`（返回条数）。每条线索还可使用 `{rank}`、`{rank_symbol}`、`{platform}`、`{match}`、`{keyword}`、`{name}`、`{account}`、`{name_line}`、`{account_line}`、`{reason}`、`{reason_line}`、`{engines}`。其中 `name_line`、`account_line` 和 `reason_line` 在没有数据时为空。`result_engine_summary_template` 可使用 `{engine_counts}`，`result_failures_template` 可使用 `{failures}`。要显示字面量大括号，写成 `{{` 和 `}}`。

五个引擎会并发尝试。Google Lens 和 Yandex 搜索本地图片前会使用临时图床；插件会读取上传后的地址，确认拿到的确实是图片，再交给搜索引擎。未配置 Google Key、图床只返回 HTML、遭遇验证码或某个引擎出错时，其余引擎仍返回结果。消息末尾分别列出各引擎候选数与未完成原因；E-Hentai 等引擎的 HTTP 错误不会再显示成普通的零匹配。模型未返回有效的前三条选择时，插件会说明复核失败，不会自行挑选前三条。

## 拒绝后的搜图

AI 会先判断是否调用工具。只有带图片的搜图需求被 LLM 以拒绝语句结束，插件才会补调 `reverse_search_top3`：明确的搜图关键词直接作为需求依据，其余请求由所选模型结合最近对话判断。像“这张呢”这样的短追问，如果 AI 明确拒绝查找出处，也会补调搜图。普通图片聊天不会因 AI 拒绝而触发搜图。AI 调用工具时若当前没有图片，会按 `ai_image_wait_seconds` 等待下一张图片；关键词触发则按 `keyword_image_wait_seconds` 等待。两分钟内追问同一会话中刚发送的图片也可复用该图片。关闭 AstrBot 的流式输出和平台分段回复，才能确保完整图文结果在同一条消息中发送。

## 已知限制

- 五个引擎的相似度含义不同，模型会结合图片和可用证据选择候选，不能把各引擎分数当作统一概率。名称与账号只取搜索结果明确给出的字段或页面地址中的公开账号路径。
- 纯文本模型依据标题、平台、相似度和引擎交叉命中判断；支持图像的模型还可比较所有可用的候选预览。一次请求最多附带 26 张图（用户原图及 25 条候选预览），模型供应商需允许对应的图像数量；图像请求失败时会以文字候选再试一次。
- 若 AstrBot 平台配置为对所有结果启用“分段回复”，它仍会把图文消息拆开；需在 AstrBot 中关闭该设置。
- 部分引擎只提供缩略图，或预览图地址无法下载，此时该条结果会显示“预览图：暂无”。
- Google Lens 需要有效 Key。Yandex 的网页结果可能受到验证码或页面改版影响。
- 引用消息需由平台适配器提供被引用的图片组件；引用内容无法读取时，请重新发送图片。
- 普通群聊带图提问沿用 AstrBot 的唤醒规则；没有“搜图”指令时，需先唤醒机器人，AI 才会判断是否调用工具。

## 来源与许可

五个引擎的请求和响应解析代码基于 [OMSociety/astrbot_plugin_reverse_searcher](https://github.com/OMSociety/astrbot_plugin_reverse_searcher) 修改。本仓库按原项目的 AGPL-3.0 许可发布，详见 `LICENSE`。
