"""中英文停用词表 — 用于 task_topic 启发式抽取。

设计原则：保留 keyword（中文 2-4 字名词、英文/拼音 token、路径片段），去掉
礼貌语 / 提问语气 / 连接词 / 口头语。
"""
from __future__ import annotations

# 中文停用词（口头语 / 提问 / 连接 / 礼貌）
CN_STOPWORDS = {
    "看下", "查下", "帮我", "一下", "嗯", "那", "吗", "呢", "请", "麻烦",
    "怎么", "啥", "哪个", "哪些", "什么", "如何", "可以", "能否", "需要",
    "现在", "目前", "之前", "之后", "然后", "并且", "因此", "所以",
    "我", "你", "他", "她", "它", "的", "了", "在", "是", "和", "与", "或",
    "让", "把", "对", "从", "给", "到", "去", "来", "会", "要", "想", "说",
    "做", "用", "有", "没有", "不", "也", "都", "就", "还", "更", "最",
    "项目", "任务", "进展", "进度", "情况", "部分", "下一步",
    "看下一步", "看看", "尝试", "试一下", "继续", "暂停", "专注",
    "确认", "决定", "判断", "查看", "了解",
    "执行", "运行", "跑",
    # 单字 high-frequency
    "上", "下", "中", "前", "后", "里", "外", "时", "事",
}

# 英文停用词（功能词 / 高频动词）
EN_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "i", "you", "he", "she", "it", "we", "they", "this", "that", "these", "those",
    "and", "or", "but", "so", "of", "to", "in", "on", "at", "by", "for", "with",
    "as", "if", "do", "does", "did", "have", "has", "had", "can", "could",
    "should", "would", "will", "shall", "may", "might", "must", "shall",
    "what", "when", "where", "why", "how", "which", "who", "whom",
    "now", "then", "there", "here",
    "let", "make", "get", "go", "come", "take", "see", "look", "know",
    "next", "step", "task", "project", "progress",
}


def is_stopword(token: str) -> bool:
    if not token:
        return True
    t = token.strip().lower()
    if not t:
        return True
    return t in CN_STOPWORDS or t in EN_STOPWORDS


def filter_tokens(tokens: list[str]) -> list[str]:
    return [t for t in tokens if not is_stopword(t)]
