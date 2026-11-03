"""Instructions for model-generated search replies."""

DEFAULT_PROMPTS = {
    "keyword_image_wait_prompt": "用一句亲切的话请用户在指定时间内发送图片，也可以引用含图片的消息。",
    "ai_image_wait_prompt": "自然地回应用户的搜图需求，请他在指定时间内补充图片。",
    "search_progress_prompt": "用一句简短的话说明正在查找图片线索，请稍候。可以带一个放大镜表情。",
    "result_header_template": "用 🎯 和简短标题引出找图结果。",
    "result_item_template": "每条候选分行写：🌐 平台、🔎 匹配、🔑 关键词；有数据才写 👤 名字、🆔 账号。语言简洁，预览图会自动附在文字后。",
    "result_footer_template": "用一句轻松的话提醒识图可能有误，需要对照原图核实。",
    "result_empty_text": "如实说明本次没有找到可用的图片候选。",
    "result_no_preview_text": "该条预览不可用时，简短说明没有预览图。",
    "result_followup_prompt": "用一句话提醒用户可以追问某个引擎的全部候选，例如“把 Yandex 的全部结果给我”；也可以指定其他引擎补搜。",
}
