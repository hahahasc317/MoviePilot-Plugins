"""码率优选：选种淘汰、低码拦截与通知文案。"""

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


class SelectionMixin:
    """选种淘汰、低码拦截与通知文案。"""

    def _dedupe_notices(self, notices: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        淘汰通知去重：同一剧集的同一组「淘汰版本 → 保留版本」24 小时内只通知一次。

        订阅每轮搜索都会遇到同一批候选，码率数值随时长估算小幅波动，
        去重键只看剧集与版本标签，不含码率。
        """
        now = time.time()
        sent: Dict[str, float] = {
            key: ts
            for key, ts in (self.get_data(SELECTION_NOTICE_KEY) or {}).items()
            if now - float(ts) < SELECTION_NOTICE_TTL_SECONDS
        }
        fresh: List[Dict[str, Any]] = []
        for notice in notices:
            key = f"{notice['media']}|{notice['drop_ver']}|{notice['keep_ver']}"
            if key in sent:
                continue
            sent[key] = now
            fresh.append(notice)
        self.save_data(SELECTION_NOTICE_KEY, sent)
        return fresh

    @staticmethod
    def _render_selection_notice(notices: List[Dict[str, Any]], limit: int = 4) -> str:
        """
        把淘汰明细渲染为按剧分组的通知正文。

        每部剧一段：剧名一行，保留版本一行，其下列出被它淘汰的版本及码率差。
        """
        grouped: Dict[str, Dict[Tuple[str, float], List[Dict[str, Any]]]] = {}
        for notice in notices:
            keeps = grouped.setdefault(notice["media"], {})
            keeps.setdefault((notice["keep_ver"], notice["keep_rate"]), []).append(notice)
        blocks: List[str] = []
        for media, keeps in list(grouped.items())[:limit]:
            lines = [media]
            for (keep_ver, keep_rate), drops in keeps.items():
                lines.append(f"✅ 保留 {keep_ver}　{keep_rate:.1f} Mbps")
                for drop in drops:
                    lower = (1 - drop["drop_rate"] / keep_rate) * 100 if keep_rate else 0
                    lines.append(
                        f"⛔ 淘汰 {drop['drop_ver']}　{drop['drop_rate']:.1f} Mbps（低 {lower:.0f}%）"
                    )
            blocks.append("\n".join(lines))
        more = len(grouped) - limit
        if more > 0:
            blocks.append(f"…另有 {more} 部剧，详见日志")
        return "\n\n".join(blocks)

    def _veto_low_bitrate(self, data: ResourceDownloadEventData, ctx: Context) -> None:
        """按最低估算码率门槛拦截单个下载提交。"""
        meta = ctx.meta_info
        episodes = set(meta.episode_list or []) if meta else set(data.episodes or [])
        bitrate = self._estimate_bitrate(ctx, episodes)
        if bitrate is None:
            return
        if bitrate >= self._veto_min_mbps:
            return
        data.cancel = True
        data.reason = f"估算码率 {bitrate:.1f} Mbps 低于门槛 {self._veto_min_mbps:g} Mbps"
        data.source = "码率优选"
        logger.warning(f"码率优选：拦截下载 {ctx.torrent_info.title}：{data.reason}")
        if self._notify:
            self.post_message(
                mtype=None,
                title="码率优选拦截",
                text=(
                    f"{self._notify_media_label(ctx)}：估算 "
                    f"{bitrate:.1f}Mbps，低于门槛 {self._veto_min_mbps:g}Mbps，已拦截"
                ),
            )

    @staticmethod
    def _notify_media_label(ctx: Any) -> str:
        """构造通知用的「剧名 + 季集」简短媒体标签。"""
        media = getattr(ctx, "media_info", None)
        meta = getattr(ctx, "meta_info", None)
        title = (
            getattr(media, "title", None)
            or getattr(meta, "cn_name", None)
            or getattr(meta, "en_name", None)
            or ""
        )
        season_episode = str(getattr(meta, "season_episode", "") or "").replace(" ", "")
        return f"{title} {season_episode}".strip() or "未知媒体"

    @staticmethod
    def _notify_version_label(ctx: Any) -> str:
        """构造通知用的「发布组 + 特效」版本短标签，缺失时截取种子名。"""
        meta = getattr(ctx, "meta_info", None)
        team = str(getattr(meta, "resource_team", "") or "").strip()
        effect = SelectionMixin._dedupe_effect_tokens(
            str(getattr(meta, "resource_effect", "") or "")
        )
        label = " · ".join(part for part in (team, effect) if part)
        if not label:
            label = (getattr(getattr(ctx, "torrent_info", None), "title", "") or "")[:24]
        return label

    @staticmethod
    def _dedupe_effect_tokens(effect: str) -> str:
        """去掉被更具体标记包含的重复特效词，如「HDR10 HDR」→「HDR10」。"""
        tokens: List[str] = []
        for token in effect.split():
            if token.lower() not in (t.lower() for t in tokens):
                tokens.append(token)
        kept = [
            token
            for token in tokens
            if not any(
                other is not token
                and len(other) > len(token)
                and other.lower().startswith(token.lower())
                for other in tokens
            )
        ]
        return " ".join(kept)

    def _drop_dominated_candidates(
        self, contexts: List[Any]
    ) -> Tuple[List[Any], List[str], List[Dict[str, Any]]]:
        """
        按媒体身份与覆盖类别分组，淘汰被高码率候选覆盖的低码率候选。

        :return: (保留的候选列表, 日志用完整描述列表, 通知用结构化淘汰明细列表)
        """
        groups: Dict[str, List[Tuple[int, Set[int], float, str, str, str]]] = {}
        for idx, ctx in enumerate(contexts):
            if not isinstance(ctx, Context) or not ctx.media_info or not ctx.torrent_info:
                continue
            media = ctx.media_info
            if getattr(media, "type", None) != MediaType.TV:
                continue
            meta = ctx.meta_info
            episodes = set(meta.episode_list or []) if meta else set()
            season = (meta.begin_season or 1) if meta else 1
            bitrate = self._estimate_bitrate(ctx, episodes)
            if bitrate is None:
                continue
            group_key = self._media_group_key(media)
            if not group_key:
                continue
            coverage = f"{season}:episodes" if episodes else f"{season}:pack"
            title = getattr(ctx.torrent_info, "title", "") or f"候选#{idx}"
            media_label = self._notify_media_label(ctx)
            version_label = self._notify_version_label(ctx)
            groups.setdefault(f"{group_key}|{coverage}", []).append(
                (idx, episodes, bitrate, title, media_label, version_label)
            )
        drop: Dict[int, Tuple[str, Dict[str, Any]]] = {}
        # 做种不足的候选不能淘汰别人：码率再高，下不动也没用
        weak = {
            idx for idx, ctx in enumerate(contexts)
            if isinstance(ctx, Context) and self._low_seeders(ctx) is not None
        }
        margin = max(0, int(self._margin_percent)) / 100.0
        for items in groups.values():
            for idx, eps, bitrate, title, media_label, version_label in items:
                if idx in drop:
                    continue
                for jdx, eps2, bitrate2, title2, label2, version2 in items:
                    if jdx == idx or jdx in drop or jdx in weak:
                        continue
                    if not self._covers(eps2, eps):
                        continue
                    if bitrate2 >= bitrate * (1 + margin):
                        drop[idx] = (
                            (
                                f"{title}（{bitrate:.1f}Mbps）← 保留 {title2}（{bitrate2:.1f}Mbps）"
                            ),
                            {
                                "media": media_label,
                                "drop_ver": version_label,
                                "drop_rate": round(bitrate, 1),
                                "keep_ver": version2,
                                "keep_rate": round(bitrate2, 1),
                            },
                        )
                        break
        if not drop:
            return contexts, [], []
        kept = [c for i, c in enumerate(contexts) if i not in drop]
        dropped = list(drop.values())
        return kept, [item[0] for item in dropped], [item[1] for item in dropped]
