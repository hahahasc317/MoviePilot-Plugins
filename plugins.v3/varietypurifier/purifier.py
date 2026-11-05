"""综艺正片净化核心判定逻辑。

设计为无宿主依赖的纯函数，便于直接单测；插件主类负责配置、状态与事件接线。

背景（2026-11-05/06 现在就出发 S04 实证）：
订阅/洗版下载的综艺期包（QHstudIo 等）除正片外还包含加更、还有加更、花絮特辑、
先导片等文件。下载准入过滤（include/exclude）只作用于搜索选种阶段，管不住已下载
种子在整理阶段被逐文件转移；这些文件名中的期号会被识别成 TMDB 正片集号，占号入库
污染媒体库。此外 QHstudIo/PTerWEB 等发布组的 Part1/Part2 半集文件在 MP 命名下保留
同一集号（-Part2- 后缀），与对齐发布的完整单集冲突。
"""

from __future__ import annotations

import re
from typing import Optional

# 默认拦截关键词（大小写不敏感子串匹配，命中文件名或解析标题）
DEFAULT_KEYWORDS = (
    "加更",
    "还有加更",
    "花絮",
    "特辑",
    "先导",
    "预告",
    "纯享",
    "陪看",
    "幕后",
    "专访",
    "抢先看",
    "Pure",
    "Extra.Version",
    "Trailer",
    "Behind.The.Scenes",
    "Watch",
)

# Part 半集文件名模式：.Part1. / _part2 / -PART01 等（词边界含数字，避免误伤 partner 等词）
PART_FILE_RE = re.compile(r"[.\-_\s]part\s?\d{1,2}[.\-_\s]", re.IGNORECASE)

# 订阅下载来源前缀（与宿主 SubscribePolicy.get_subscribe_source_keyword 一致）
SUBSCRIBE_SOURCE_PREFIX = "Subscribe|"


def is_subscribe_source(source: Optional[str]) -> bool:
    """判断下载来源是否为订阅自动化来源。

    :param source: DownloadAdded/下载历史记录中的 source 字段
    :return: 以 Subscribe| 开头视为订阅来源；空值或其他值均视为非订阅（手动/其他插件）
    """
    return bool(source) and str(source).startswith(SUBSCRIBE_SOURCE_PREFIX)


def match_keyword(text: str, keywords: tuple[str, ...]) -> Optional[str]:
    """在文本中查找命中的拦截关键词。

    :param text: 参与匹配的文本（文件名 + 解析标题）
    :param keywords: 拦截关键词元组
    :return: 命中的第一个关键词；未命中返回 None
    """
    lowered = text.lower()
    for keyword in keywords:
        if keyword and keyword.lower() in lowered:
            return keyword
    return None


def match_part(text: str) -> bool:
    """判断文件名是否为 Part 分半文件。

    :param text: 参与匹配的文本（文件名）
    :return: 命中 Part 半集模式返回 True
    """
    return bool(PART_FILE_RE.search(text))


def decide(
    *,
    filename: str,
    org_string: str = "",
    media_type: str = "",
    library_category: str = "",
    begin_episode: Optional[int] = None,
    part: str = "",
    keywords: tuple[str, ...] = DEFAULT_KEYWORDS,
    scope_variety_only: bool = True,
    block_parts: bool = True,
    block_special_ep: bool = True,
    is_manual_source: bool = False,
    exempt_manual: bool = True,
) -> str:
    """核心拦截裁决，返回裁决结果字符串。

    :param filename: 源文件名
    :param org_string: 识别后的解析标题（可选补充文本）
    :param media_type: 媒体类型（电视剧/电影等，电影直接放行）
    :param library_category: 媒体库分类（用于综艺范围判定）
    :param begin_episode: 解析集号（0 为特殊集）
    :param part: 元数据分卷标记
    :param keywords: 拦截关键词元组
    :param scope_variety_only: 是否仅作用于综艺分类
    :param block_parts: 是否拦截 Part 半集文件
    :param block_special_ep: 是否拦截 E00 特殊集号
    :param is_manual_source: 该种子是否为手动/非订阅来源
    :param exempt_manual: 是否放行手动来源
    :return: "allow" 放行；"blocked:关键词" 命中关键词；"blocked:part" 半集；
             "blocked:e00" 特殊集号
    """
    if media_type and media_type != "电视剧":
        return "allow"
    if scope_variety_only and "综艺" not in (library_category or ""):
        return "allow"
    if exempt_manual and is_manual_source:
        return "allow"
    text = f"{filename} {org_string}".strip()
    keyword = match_keyword(text, keywords)
    if keyword:
        return f"blocked:{keyword}"
    if block_special_ep and begin_episode == 0:
        return "blocked:e00"
    if block_parts and (part or match_part(filename)):
        return "blocked:part"
    return "allow"
