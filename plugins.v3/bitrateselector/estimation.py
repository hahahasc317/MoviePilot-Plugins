"""码率优选：码率估算：种子覆盖范围、单集时长解析与候选分组。"""

import inspect
import json
import os
import re
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from app.application.subscription import priority as subscription_priority
from app.chain.subscribe import SubscribeChain
from app.chain.tmdb import TmdbChain
from app.db.oper.subscribe import SubscribeOper
from app.sdk.logging import logger
from app.sdk.media import Context
from app.schemas.event import ResourceDownloadEventData
from app.schemas.media import resolve_media_identity
from app.schemas.types import MediaType

from .constants import *  # noqa: F401,F403  pylint: disable=wildcard-import,unused-wildcard-import


class EstimationMixin:
    """码率估算：种子覆盖范围、单集时长解析与候选分组。"""

    def _estimate_bitrate(
        self, ctx: Context, episodes: Set[int]
    ) -> Optional[float]:
        """
        估算候选资源对覆盖剧集的平均码率（Mbps）。

        无集数标注的整季包按全季已标注时长之和估算；无法估算时返回 None。
        """
        mbps, _ = self._estimate_bitrate_ex(ctx, episodes)
        return mbps

    def _estimate_bitrate_ex(
        self, ctx: Context, episodes: Set[int]
    ) -> Tuple[Optional[float], str]:
        """
        估算候选资源对覆盖剧集的平均码率，并返回时长来源标签。

        :return: (Mbps, 时长来源)；来源为 measured（逐集实测）或 estimated
                 （含同季中位数填充），无法估算时返回 (None, "")。
        """
        torrent = ctx.torrent_info
        media = ctx.media_info
        if not torrent or not torrent.size or not media:
            return None, ""
        meta = ctx.meta_info
        season = (meta.begin_season or 1) if meta else 1
        # 种子体积对应的是种子自身全部内容：识别出的集数少于标题声明的范围时
        # （如整季包只解析出 E01），按标题范围计算时长，避免整包体积摊到一集上
        title_range = self._title_episode_range(torrent)
        if title_range and episodes and len(title_range) > len(episodes):
            episodes = title_range
        minutes_map, source = self._resolve_episode_minutes(media, season, episodes)
        if not minutes_map:
            return None, ""
        total_minutes = sum(minutes_map.values())
        if total_minutes <= 0:
            return None, ""
        mbps = torrent.size * 8 / (total_minutes * 60) / 1_000_000
        if mbps > IMPLAUSIBLE_WEB_MBPS and not self._is_disc_source(torrent):
            logger.warning(
                f"码率优选：估算 {mbps:.1f}Mbps 超出网络源合理范围，疑似集数识别错误，"
                f"不采信（{(getattr(torrent, 'title', '') or '')[:60]}）"
            )
            return None, "implausible"
        return mbps, source

    @staticmethod
    def _title_episode_range(torrent: Any) -> Set[int]:
        """从种子标题/副标题解析声明的集数范围（E01-E30、全30集、共30集），无则空集。"""
        text = f"{getattr(torrent, 'title', '') or ''} {getattr(torrent, 'description', '') or ''}"
        best: Set[int] = set()
        for match in TITLE_EP_RANGE_RE.finditer(text):
            start, end = int(match.group(1)), int(match.group(2))
            if 0 < start <= end <= 2000 and end - start + 1 > len(best):
                best = set(range(start, end + 1))
        for match in TITLE_EP_TOTAL_RE.finditer(text):
            total = int(match.group(1))
            if 0 < total <= 2000 and total > len(best):
                best = set(range(1, total + 1))
        return best

    @staticmethod
    def _is_disc_source(torrent: Any) -> bool:
        """是否原盘/重编码蓝光等高码率来源（不适用网络源码率上限）。"""
        title = f"{getattr(torrent, 'title', '') or ''} {getattr(torrent, 'description', '') or ''}"
        return bool(DISC_SOURCE_RE.search(title))

    def _resolve_episode_minutes(
        self, media: Any, season: int, episodes: Set[int]
    ) -> Tuple[Optional[Dict[int, int]], str]:
        """
        三级回退解析覆盖剧集的单集时长（分钟）。

        时长来源依次为：TMDB 单集 runtime、库内 mediainfo 实测时长、
        同季已知时长的中位数填充。

        :param episodes: 候选覆盖的集数；空集表示整季包，取全部已知集。
        :return: (每集分钟数映射, 来源标签)；标签为 measured 或 estimated，
                 完全无任何时长信息时返回 (None, "")。
        """
        tmdb = self._episode_runtimes(media, season)
        library = self._library_episode_minutes(media, season)
        if episodes:
            targets = set(episodes)
        else:
            targets = set(tmdb) | set(library)
        if not targets:
            return None, ""
        merged: Dict[int, int] = {}
        for episode in sorted(targets):
            minutes = tmdb.get(episode) or library.get(episode) or 0
            if minutes > 0:
                merged[episode] = int(minutes)
        if len(merged) >= len(targets):
            return (merged, "measured") if merged else (None, "")
        # 覆盖集有缺失时，用同季已知时长（TMDB ∪ 库内实测）的中位数填充
        known = sorted(set(tmdb.values()) | set(library.values()))
        median = self._median_int(known)
        if median <= 0:
            # 无任何中位可填：保持旧行为，按已解析的部分时长估算
            return (merged, "measured") if merged else (None, "")
        for episode in targets - set(merged):
            merged[episode] = median
        return merged, "estimated"

    def _library_episode_minutes(self, media: Any, season: int) -> Dict[int, int]:
        """读取库内同季 mediainfo sidecar 的单集实测时长（分钟），缓存 6 小时。"""
        cache = getattr(self, "_libdur_cache", None)
        if cache is None:
            cache = {}
            self._libdur_cache = cache
        title = str(getattr(media, "title", "") or "")
        year = getattr(media, "year", None)
        cache_key = (title, str(year or ""), int(season))
        cached = cache.get(cache_key)
        if cached and time.time() - cached[0] < RUNTIME_CACHE_TTL_SECONDS:
            return cached[1]
        minutes: Dict[int, int] = {}
        try:
            root = Path(self._library_root)
            show_dir = self._find_show_dir(root, title, year) if title else None
            season_dir = self._find_season_dir(show_dir, season) if show_dir else None
            if season_dir is not None:
                for info_path in season_dir.glob("*-mediainfo.json"):
                    episode = self._episode_from_filename(info_path.name)
                    if not episode:
                        continue
                    episode_minutes = self._mediainfo_runtime_minutes(info_path)
                    if episode_minutes and episode_minutes > 0:
                        minutes[episode] = int(episode_minutes)
        except Exception as err:
            logger.warning(f"码率优选：读取库内单集时长失败：{err}")
            return {}
        if minutes:
            cache[cache_key] = (time.time(), minutes)
        return minutes

    @staticmethod
    def _mediainfo_runtime_minutes(path: Path) -> Optional[int]:
        """读取 mediainfo sidecar 的 RunTimeTicks 并换算为分钟。"""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            source = data[0]["MediaSourceInfo"]
            ticks = float(source.get("RunTimeTicks") or 0)
            if ticks <= 0:
                return None
            return int(ticks / 10_000_000 / 60)
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            return None

    @staticmethod
    def _median_int(values: List[int]) -> int:
        """取正整数中位数（偶数个时取中间两数均值）。"""
        ordered = sorted(v for v in values if v and v > 0)
        if not ordered:
            return 0
        middle = len(ordered) // 2
        if len(ordered) % 2:
            return int(ordered[middle])
        return int((ordered[middle - 1] + ordered[middle]) / 2)

    def _episode_runtimes(
        self, media: Any, season: int
    ) -> Dict[int, int]:
        """
        读取并缓存 TMDB 指定季的单集时长映射，缓存 6 小时。
        """
        tmdbid = getattr(media, "tmdb_id", None)
        if not tmdbid:
            return {}
        cache = getattr(self, "_runtime_cache", None)
        if cache is None:
            cache = {}
            self._runtime_cache = cache
        cache_key = (int(tmdbid), int(season))
        cached = cache.get(cache_key)
        if cached and time.time() - cached[0] < RUNTIME_CACHE_TTL_SECONDS:
            return cached[1]
        try:
            chain = getattr(self, "_tmdb_chain", None)
            if chain is None:
                chain = TmdbChain()
                self._tmdb_chain = chain
            episodes = chain.tmdb_episodes(tmdbid=int(tmdbid), season=int(season)) or []
        except Exception as err:
            logger.warning(f"码率优选：读取 TMDB 第 {season} 季时长失败：{err}")
            return {}
        runtimes = {
            ep.episode_number: int(ep.runtime or 0)
            for ep in episodes
            if ep.episode_number and ep.runtime
        }
        if runtimes:
            cache[cache_key] = (time.time(), runtimes)
        return runtimes

    @staticmethod
    def _media_group_key(media: Any) -> str:
        """
        返回候选的媒体分组键，优先使用来源与媒体 ID，无法取得时回落到标题年份。
        """
        source = getattr(media, "media_source", None)
        media_id = getattr(media, "media_id", None)
        if source and media_id:
            return f"{source}:{media_id}"
        return str(getattr(media, "title_year", "") or "")

    @staticmethod
    def _covers(outer: Set[int], inner: Set[int]) -> bool:
        """
        判断 outer 集数集合是否完全覆盖 inner（相同或超集）。

        分组已按覆盖类别隔离：双方均为空集表示同类整季覆盖，视为互相覆盖；
        仅一方为空集时视为未知覆盖，不参与淘汰。
        """
        if not outer and not inner:
            return True
        if not outer or not inner:
            return False
        return inner <= outer
