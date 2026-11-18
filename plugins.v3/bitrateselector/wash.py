"""码率优选：洗版准入判定与码率基线的记录、迁移。"""

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


class WashMixin:
    """洗版准入判定与码率基线的记录、迁移。"""

    def _migrate_inflated_floors(self) -> None:
        """
        一次性清除 1.4.7 之前记录的防降级下限。

        旧版整季包只为部分集下载时把整包体积摊到所选集上，下限可能虚高数倍，
        会在 48 小时内拦下所有正常候选；清除后由库内播种按实际文件重建基线。
        """
        if self.get_data(FLOOR_MIGRATION_KEY):
            return
        cleared = 0
        for entry in self._baselines.values():
            floors = entry.get("ep_floors") or {}
            cleared += len(floors)
            if floors:
                entry["ep_floors"] = {}
            if entry.pop("full_floor", None) is not None:
                cleared += 1
        if cleared:
            self.save_data(WASH_BASELINES_KEY, self._baselines)
            logger.info(f"码率优选：已清除 {cleared} 条旧版可能虚高的防降级下限，由库内播种重建基线")
        self.save_data(FLOOR_MIGRATION_KEY, True)

    def _wash_admit(
        self, subscribe: Any, context: Any, priority: int, original: Any
    ) -> bool:
        """
        按估算码率执行洗版准入判定。

        全集模式对比整季基线；分集模式按每集基线计算仍可提升的剧集范围。
        无法估算码率时回退原始规则优先级判定；基线缺失时保守处理：
        全集模式回退原始判定，分集模式订阅整体无基线（库内尚无已入库集）
        时同样回退原始判定，让新订阅正常完成初始下载。
        部分库存时缺基线的集区分处理：库内未下载的集按初始补缺放行，
        已下载但播种遗漏的集跳过防降级。
        候选与基线的时长来源不一致时（如基线为逐集实测、候选含中位数填充），
        准入阈值加倍，防止时长误差穿越边界。
        """
        if getattr(subscribe, "type", None) != MediaType.TV.value or original is None:
            return original(subscribe, context, priority)
        margin = max(0, int(self._margin_percent)) / 100.0
        if subscription_priority.is_full_best_version_enabled(subscribe):
            candidate_mbps, candidate_source = self._estimate_bitrate_ex(context, set())
            if candidate_source == "implausible":
                return False
            if candidate_mbps is None:
                return original(subscribe, context, priority)
            baseline_entry = self._wash_baseline_entry(subscribe)
            baseline = baseline_entry.get("full")
            if baseline is None:
                logger.info(
                    "码率优选：全集洗版基线缺失，回退规则优先级判定，防止低码候选盲目洗版"
                )
                return original(subscribe, context, priority)
            baseline_source = str(baseline_entry.get("full_source") or "measured")
            effective_margin = margin * 2 if candidate_source != baseline_source else margin
            floor = self._floor_value(baseline_entry.get("full_floor"))
            effective_baseline = max(float(baseline), floor) if floor else float(baseline)
            if candidate_mbps > effective_baseline * (1 + effective_margin):
                logger.info(
                    f"码率优选：全集洗版准入 {candidate_mbps:.1f}Mbps 超过基线 "
                    f"{effective_baseline:.1f}Mbps（需 +{effective_margin:.0%}，"
                    f"时长来源 候选={candidate_source}/基线={baseline_source}）"
                )
                return True
            logger.debug(
                f"码率优选：全集洗版候选 {candidate_mbps:.1f}Mbps 未超基线 "
                f"{effective_baseline:.1f}Mbps（需 +{margin:.0%}），拒绝"
            )
            return False
        interested = self._wash_interested_episodes(subscribe, context, margin)
        if interested is None:
            return original(subscribe, context, priority)
        if not interested:
            return False
        context.allowed_episodes = set(interested)
        return True

    def _wash_interested_episodes(
        self, subscribe: Any, context: Any, margin: float
    ) -> Optional[List[int]]:
        """
        按码率基线计算仍值得洗版的剧集。

        返回 None 表示候选无法估算码率，需回退原始判定。
        订阅无任何分集基线（库内尚无已入库集）时同样返回 None，
        交回核心初始下载判定，避免 0 库存新订阅被准入死锁。
        缺基线的集区分处理：库内未下载的集是待补缺的新集，按初始下载放行；
        已下载但缺基线的集（播种遗漏）仍跳过，待库内播种补齐基线后再比较。
        标题解析出明确集数边界时，待补/待洗范围先限定在候选包自身集数内。
        基线被拒绝覆盖的集带有按提交估算垫高的准入下限（48 小时内有效），
        比较取基线与下限的较大者，防止播种校正窗口内低码变体再洗入覆盖。
        """
        target_episodes = set(subscription_priority.get_best_version_target_episodes(subscribe))
        if not target_episodes:
            return []
        meta = context.meta_info
        # 整季无明确集号的候选按标题解析的集数范围限定目标集：
        # 季内尚未播出的远端集不在包内，不能当作本包待补缺，
        # 否则准入放行后与包内真实集求交为空，下载阶段永远跳过并每轮重复发通知。
        span = self._candidate_episode_span(meta, target_episodes)
        if span is not None:
            target_episodes &= span
        selected_episodes = getattr(context, "selected_episodes", None)
        if selected_episodes is None:
            selected_episodes = meta.episode_list if meta else []
        candidate_mbps, candidate_source = self._estimate_bitrate_ex(
            context, set(selected_episodes) if selected_episodes else set()
        )
        if candidate_source == "implausible":
            # 码率估算不可信（集数识别错误）时不能回退规则判定：规则会照常放行
            return []
        if candidate_mbps is None:
            return None
        episode_baselines = self._wash_baseline_entry(subscribe).get("episodes") or {}
        baseline_sources = self._wash_baseline_entry(subscribe).get("ep_sources") or {}
        episode_floors = self._wash_baseline_entry(subscribe).get("ep_floors") or {}
        if not episode_baselines:
            logger.info(
                "码率优选：订阅尚无任何已入库集码率基线，"
                "回退核心判定放行初始下载，防止 0 库存订阅死锁"
            )
            return None
        downloaded = set(
            subscription_priority.get_downloaded_best_version_episodes(subscribe)
        )

        def _threshold_for(baseline_source: str) -> float:
            """按候选与基线的时长来源是否一致计算该集的比较阈值。"""
            effective = margin * 2 if candidate_source != baseline_source else margin
            return candidate_mbps / (1 + effective)

        title = (getattr(context.torrent_info, "title", "") or "")[:60]
        floor_blocked: List[int] = []

        def _admit_by_baseline(episode: int, threshold: float) -> bool:
            """按基线与垫高下限的较大值判断该集是否仍值得洗版。"""
            if not float(episode_baselines[str(episode)]) < threshold:
                return False
            floor = self._floor_value(episode_floors.get(str(episode)))
            if floor is not None and floor >= threshold:
                floor_blocked.append(episode)
                return False
            return True

        if not selected_episodes:
            missing = [
                episode
                for episode in target_episodes
                if episode_baselines.get(str(episode)) is None
            ]
            pending_new = sorted(ep for ep in missing if ep not in downloaded)
            skipped_known = len(missing) - len(pending_new)
            if skipped_known:
                logger.debug(
                    f"码率优选：{skipped_known} 集已下载但缺基线，本轮不参与码率优先洗版"
                )
            if pending_new:
                logger.debug(
                    f"码率优选：{len(pending_new)} 集库内未下载，按初始补缺放行"
                )
            washable = [
                episode
                for episode in target_episodes
                if (baseline := episode_baselines.get(str(episode))) is not None
                and _admit_by_baseline(
                    episode,
                    _threshold_for(str(baseline_sources.get(str(episode), "measured"))),
                )
            ]
            interested = sorted(set(washable) | set(pending_new))
            washable_eps = list(washable)
        else:
            interested = []
            skipped_known = 0
            pending_new = []
            for episode in selected_episodes:
                try:
                    episode_num = int(episode)
                except (TypeError, ValueError):
                    continue
                if episode_num not in target_episodes:
                    continue
                baseline = episode_baselines.get(str(episode_num))
                if baseline is None:
                    if episode_num in downloaded:
                        skipped_known += 1
                    else:
                        pending_new.append(episode_num)
                    continue
                if _admit_by_baseline(
                    episode_num,
                    _threshold_for(str(baseline_sources.get(str(episode_num), "measured"))),
                ):
                    interested.append(episode_num)
            if skipped_known:
                logger.debug(
                    f"码率优选：{skipped_known} 集已下载但缺基线，本轮不参与码率优先洗版"
                )
            if pending_new:
                logger.debug(
                    f"码率优选：{len(pending_new)} 集库内未下载，按初始补缺放行"
                )
            washable_eps = list(interested)
            interested = sorted(set(interested) | set(pending_new))
        self._mark_wash_candidate(context, washable_eps)
        if interested:
            episode_range = (
                f"E{interested[0]}" if len(interested) == 1
                else f"E{interested[0]}-E{interested[-1]}({len(interested)}集)"
            )
            logger.info(
                f"码率优选：分集洗版准入 {candidate_mbps:.1f}Mbps 命中 {episode_range}（{title}）"
            )
        else:
            logger.debug(
                f"码率优选：分集洗版候选 {candidate_mbps:.1f}Mbps 无可提升剧集，拒绝（{title}）"
            )
        if floor_blocked:
            logger.info(
                f"码率优选：{self._compress_episode_ranges(floor_blocked)} 命中垫高准入下限，"
                f"候选 {candidate_mbps:.1f}Mbps 未超已提交估算，拒绝防同集降级（{title}）"
            )
        return interested

    @staticmethod
    def _candidate_episode_span(
        meta: Any, target_episodes: Set[int]
    ) -> Optional[Set[int]]:
        """
        从标题解析的集数边界推断候选包的真实覆盖范围。

        仅当标题解析出明确集数边界（起始集、结束集或总集数）时，
        返回该范围与洗版目标集的交集；无明确集号的候选返回 None，
        保持原有目标范围不变。用于整季包场景：季内未播的远端集
        不在包内，不应被当作待补缺重复放行。
        """
        if meta is None:
            return None
        span_start = getattr(meta, "begin_episode", None) or 1
        span_end = getattr(meta, "end_episode", None) or getattr(meta, "total_episode", None)
        if not span_end or span_end < span_start:
            return None
        span = set(range(span_start, span_end + 1)) & target_episodes
        return span or None

    def _wash_baseline_entry(self, subscribe: Any) -> Dict[str, Any]:
        """读取指定订阅媒体与季的码率基线记录。"""
        key = self._wash_media_key(
            source=getattr(subscribe, "media_source", None),
            media_id=getattr(subscribe, "media_id", None),
            season=getattr(subscribe, "season", None),
        )
        if not key:
            return {}
        entry = getattr(self, "_baselines", {}).get(key)
        return entry or {}

    @staticmethod
    def _wash_media_key(source: Any, media_id: Any, season: Any) -> str:
        """构造「媒体来源:媒体ID:季」维度的基线存储键。"""
        if not source or not media_id:
            return ""
        try:
            season_number = int(season) if season else 1
        except (TypeError, ValueError):
            season_number = 1
        return f"{source}:{media_id}:{season_number}"

    def _record_transferred_baseline(self, data: Any) -> Optional[Tuple[str, List[int], float]]:
        """
        用整理完成事件中的实际文件大小 ÷ 该集时长计算码率并写入基线。

        下载前的估算会受整季包部分下载、集数识别错误、边车未刷新等影响；
        入库后的文件大小是真实值，以此为准并清除对应集的防降级下限。
        单文件整理按该文件大小计算；多集合集一次整理多个文件时，视频文件数与
        识别出的集数一致才按合集总大小 ÷ 总时长计算平均码率记给每一集，对不上则跳过。

        :return: (基线键, 集号列表, 码率)；未记录时返回 None
        """
        info = getattr(data, "transferinfo", None)
        meta = getattr(data, "meta", None)
        media = getattr(data, "mediainfo", None)
        if info is None or not getattr(info, "success", False) or meta is None or media is None:
            return None
        media_type = getattr(media, "type", None)
        if media_type not in (MediaType.TV, MediaType.TV.value, "电视剧"):
            return None
        episodes = sorted({int(e) for e in (getattr(meta, "episode_list", None) or []) if e})
        if not episodes:
            begin = getattr(meta, "begin_episode", None)
            episodes = [int(begin)] if begin else []
        if not episodes:
            return None
        file_count = int(getattr(info, "file_count", 0) or 0)
        if file_count > 1:
            listed = list(getattr(info, "file_list_new", None) or getattr(info, "file_list", None) or [])
            videos = [str(f) for f in listed if str(f).lower().endswith(VIDEO_EXTS)]
            video_count = len(videos) if listed else file_count
            if video_count <= 1 or video_count != len(episodes):
                return None
            size = int(getattr(info, "total_size", 0) or 0)
            name = videos[0].rsplit("/", 1)[-1] if videos else ""
            if size < TRANSFER_MIN_VIDEO_BYTES * video_count:
                return None
        else:
            target = getattr(info, "target_item", None)
            size = int(getattr(target, "size", 0) or 0) or int(getattr(info, "total_size", 0) or 0)
            name = str(getattr(target, "name", "") or "")
            if size < TRANSFER_MIN_VIDEO_BYTES or (name and not name.lower().endswith(VIDEO_EXTS)):
                return None
        season = int(getattr(meta, "begin_season", None) or 1)
        key = self._transfer_media_key(media, season)
        if not key:
            return None
        minutes_map, source = self._resolve_episode_minutes(media, season, set(episodes))
        total_minutes = sum((minutes_map or {}).values())
        if total_minutes <= 0:
            return None
        mbps = round(size * 8 / (total_minutes * 60) / 1_000_000, 2)
        if mbps > IMPLAUSIBLE_WEB_MBPS and not DISC_SOURCE_RE.search(name):
            logger.warning(f"码率优选：入库文件码率 {mbps}Mbps 不合常理，未记录（{name}）")
            return None
        with self._baseline_lock:
            entry = self._baselines.setdefault(key, {})
            episode_map = entry.setdefault("episodes", {})
            source_map = entry.setdefault("ep_sources", {})
            floor_map = entry.setdefault("ep_floors", {})
            actual_map = entry.setdefault("ep_actual", {})
            for episode in episodes:
                episode_map[str(episode)] = mbps
                source_map[str(episode)] = source or "measured"
                floor_map.pop(str(episode), None)
                actual_map[str(episode)] = int(time.time())
            self.save_data(WASH_BASELINES_KEY, self._baselines)
        logger.info(
            f"码率优选：入库实测 {key} {self._compress_episode_ranges(episodes)} => {mbps}Mbps"
            f"（{size / 1024 ** 3:.2f}GB，{name[:60]}）"
        )
        return key, episodes, mbps

    def _transfer_media_key(self, media: Any, season: int) -> str:
        """从整理事件的媒体快照构造基线键，与下载阶段的键保持一致。"""
        try:
            source, media_id = resolve_media_identity(media=media)
        except Exception:
            source, media_id = None, None
        if not (source and media_id) and getattr(media, "tmdb_id", None):
            source, media_id = "themoviedb", getattr(media, "tmdb_id")
        return self._wash_media_key(source=source, media_id=media_id, season=season)

    def _record_wash_baseline(self, data: ResourceDownloadEventData) -> None:
        """
        下载提交后按覆盖剧集记录估算码率基线。

        估算值与同集既有基线倍差超过 WASH_BASELINE_MAX_DIVERGENCE 时拒绝
        覆盖并告警：整季包集数解析不全会让估算虚高数倍，直接覆盖会把
        洗版准入门槛抬到不可达；被拒集保留既有值，待库内播种按实测校正。
        向上被拒（提交估算明显高于既有基线）的集同时按提交估算垫高准入下限
        （取历史最高，48 小时内有效）：播种校正前旧低基线仍会放行同集候选，
        下限用于阻止更低码变体反向覆盖刚下载的高码版。
        """
        ctx = data.context
        meta = ctx.meta_info
        if data.episodes:
            episodes = set(data.episodes)
        elif meta:
            episodes = set(meta.episode_list or [])
        else:
            episodes = set()
        season = (meta.begin_season or 1) if meta else 1
        source, media_id = resolve_media_identity(media=ctx.media_info)
        key = self._wash_media_key(source=source, media_id=media_id, season=season)
        if not key:
            logger.warning(
                f"码率优选：下载事件缺少媒体身份（{ctx.media_info}），未记录码率基线"
            )
            return
        # 码率按种子自身覆盖范围估算（与准入同口径）：整季包只为其中几集下载时，
        # 若按所选集时长计算，会把整包体积摊到一两集上，估算虚高数倍
        coverage = set(meta.episode_list or []) if meta else set()
        bitrate, duration_source = self._estimate_bitrate_ex(ctx, coverage)
        if bitrate is None:
            logger.warning(
                f"码率优选：无法估算下载码率（{ctx.torrent_info.title}），未记录码率基线"
            )
            return
        try:
            self._baseline_lock.acquire()
            entry = self._baselines.setdefault(key, {})
            new_value = round(bitrate, 2)
            refused: List[str] = []
            refused_eps: Set[int] = set()
            floored: List[int] = []
            floored_full = False
            if episodes:
                episode_map = entry.setdefault("episodes", {})
                source_map = entry.setdefault("ep_sources", {})
                floor_map = entry.setdefault("ep_floors", {})
                for episode in sorted(episodes):
                    ep_key = str(episode)
                    existing = episode_map.get(ep_key)
                    if existing is not None and self._baseline_diverged(
                        existing, new_value
                    ):
                        refused.append(
                            f"E{episode}（新估算 {new_value:.2f} / 既有 {float(existing):.2f}）"
                        )
                        refused_eps.add(episode)
                        if new_value > float(existing):
                            floor = self._floor_value(floor_map.get(ep_key))
                            floor_map[ep_key] = [
                                max(new_value, floor or 0.0),
                                time.time(),
                            ]
                            floored.append(episode)
                        continue
                    episode_map[ep_key] = new_value
                    source_map[ep_key] = duration_source
                    floor_map.pop(ep_key, None)
            else:
                existing = entry.get("full")
                if existing is not None and self._baseline_diverged(existing, new_value):
                    refused.append(
                        f"全集（新估算 {new_value:.2f} / 既有 {float(existing):.2f}）"
                    )
                    if new_value > float(existing):
                        floor = self._floor_value(entry.get("full_floor"))
                        entry["full_floor"] = [max(new_value, floor or 0.0), time.time()]
                        floored_full = True
                else:
                    entry["full"] = new_value
                    entry["full_source"] = duration_source
                    entry.pop("full_floor", None)
            self.save_data(WASH_BASELINES_KEY, self._baselines)
        except Exception as err:
            logger.error(f"码率优选：保存码率基线失败：{err}")
            return
        finally:
            self._baseline_lock.release()
        if refused:
            shown = "；".join(refused[:8]) + ("等" if len(refused) > 8 else "")
            logger.warning(
                f"码率优选：估算基线疑似异常，拒绝覆盖 {key} {shown}"
                f"（倍差超过 {WASH_BASELINE_MAX_DIVERGENCE:g} 倍，"
                "保留既有基线，待库内播种按实测校正）"
            )
        if floored:
            logger.info(
                f"码率优选：{key} {self._compress_episode_ranges(floored)} 基线覆盖被拒，"
                f"按提交估算 {new_value:.2f}Mbps 垫高准入下限防同集降级"
                f"（{WASH_FLOOR_TTL_SECONDS // 3600} 小时内或库内播种实测后失效）"
            )
        if floored_full:
            logger.info(
                f"码率优选：{key} 全集基线覆盖被拒，按提交估算 {new_value:.2f}Mbps "
                f"垫高全集准入下限防降级（{WASH_FLOOR_TTL_SECONDS // 3600} 小时后失效）"
            )
        if episodes:
            accepted = sorted(episodes - refused_eps)
            if accepted:
                logger.info(
                    f"码率优选：记录洗版码率基线 {key} {accepted} => {new_value:.2f}Mbps"
                    f"（时长来源={duration_source}）"
                )
        elif not refused:
            logger.info(
                f"码率优选：记录洗版码率基线 {key} 全集 => {new_value:.2f}Mbps"
                f"（时长来源={duration_source}）"
            )

    @staticmethod
    def _baseline_diverged(existing: Any, candidate: float) -> bool:
        """判断候选基线与既有基线是否倍差超过合理阈值（默认 2 倍）。"""
        try:
            old = float(existing)
        except (TypeError, ValueError):
            return False
        if old <= 0 or candidate <= 0:
            return False
        return (
            candidate > old * WASH_BASELINE_MAX_DIVERGENCE
            or candidate < old / WASH_BASELINE_MAX_DIVERGENCE
        )

    @staticmethod
    def _floor_value(raw: Any) -> Optional[float]:
        """解析垫高准入下限记录，返回未过期的码率值；损坏或已过期返回 None。"""
        if isinstance(raw, (list, tuple)) and len(raw) == 2:
            try:
                value = float(raw[0])
                ts = float(raw[1])
            except (TypeError, ValueError):
                return None
            if value > 0 and 0 <= time.time() - ts <= WASH_FLOOR_TTL_SECONDS:
                return value
        return None
