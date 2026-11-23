"""码率优选：媒体库相关：库内播种、低码巡检、降级恢复与洗版毕业。"""

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


class LibraryMixin:
    """媒体库相关：库内播种、低码巡检、降级恢复与洗版毕业。"""

    def _safe_seed_baselines(self) -> Dict[str, Any]:
        """执行库内播种并吞掉异常，返回结果摘要；已有播种在跑时直接跳过。"""
        if not self._seed_lock.acquire(blocking=False):
            logger.info("码率优选：已有库内播种在运行，本次跳过")
            return {"success": False, "seeded": 0, "message": "播种进行中"}
        try:
            with self._baseline_lock:
                return self.seed_baselines_from_library()
        except Exception as err:
            logger.error(f"码率优选：库内基线播种失败：{err}")
            return {"success": False, "seeded": 0, "message": str(err)}
        finally:
            self._seed_lock.release()

    def _safe_low_bitrate_inspect(self) -> Dict[str, Any]:
        """执行低码巡检并吞掉异常，返回结果摘要。"""
        try:
            return self._low_bitrate_inspect()
        except Exception as err:
            logger.error(f"码率优选：低码巡检失败：{err}")
            return {"success": False, "low": 0, "triggered": [], "message": str(err)}

    @staticmethod
    def _parse_show_dir_name(dir_name: str) -> str:
        """解析剧目录名为可比较的规范剧名（去年份与空白）。"""
        base = re.sub(r"\(\d{4}\)\s*$", "", str(dir_name or "").strip())
        return re.sub(r"\s+", "", base)

    def _match_subscribe_id(
        self, dir_name: str, season_num: int, subscriptions: List[Any]
    ) -> Optional[Any]:
        """
        按目录名、年份与季号匹配在订订阅，返回订阅对象或 None。

        优先剧名完全相等；目录名带年份时要求订阅年份一致。只有唯一一个
        包含关系候选时才接受模糊匹配，避免短剧名误触发其他订阅。
        """
        normalized = self._parse_show_dir_name(dir_name)
        if not normalized:
            return None
        year_match = re.search(r"\((\d{4})\)\s*$", str(dir_name or ""))
        dir_year = year_match.group(1) if year_match else ""
        exact: List[Any] = []
        fuzzy: List[Any] = []
        for subscribe in subscriptions:
            name = self._parse_show_dir_name(str(getattr(subscribe, "name", "") or ""))
            if not name:
                continue
            try:
                if int(getattr(subscribe, "season", None) or 1) != int(season_num):
                    continue
            except (TypeError, ValueError):
                continue
            sub_year = str(getattr(subscribe, "year", "") or "")
            if dir_year and sub_year and dir_year != sub_year:
                continue
            if name == normalized:
                exact.append(subscribe)
            elif name in normalized or normalized in name:
                fuzzy.append(subscribe)
        if exact:
            return exact[0]
        if len(fuzzy) == 1:
            return fuzzy[0]
        return None

    def _scan_low_bitrate_items(self, cutoff: float) -> Dict[str, Dict[str, Any]]:
        """
        扫描媒体库最近更新的低码集。

        :return: {剧目录路径: {"label": 目录名, "season": 季号,
                 "eps": {集号: [各版本实测码率]}, "unknown": [(标签, 码率)]}}
        """
        result: Dict[str, Dict[str, Any]] = {}
        root = Path(self._library_root)
        if not root.is_dir():
            return result
        stale_skipped = 0
        for info_path in root.rglob("*-mediainfo.json"):
            try:
                if info_path.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            bitrate = self._mediainfo_bitrate_mbps(info_path)
            if bitrate is None or bitrate >= self._inspect_min_mbps:
                continue
            # 边车码率低于门槛时，用同名 strm 的 115 实际文件大小复核，
            # 排除洗版入库后 mediainfo 边车未刷新导致的假低码通报与重复触发搜索
            has_strm, remote_size = self._strm_remote_size(info_path)
            if not has_strm:
                # 无同名 strm 说明该边车是换版残留，不代表当前生效版本
                stale_skipped += 1
                continue
            if remote_size:
                runtime_seconds = self._mediainfo_runtime_seconds(info_path)
                if runtime_seconds:
                    actual_bitrate = remote_size * 8 / runtime_seconds / 1_000_000
                    if actual_bitrate >= self._inspect_min_mbps:
                        stale_skipped += 1
                        self._retire_stale_sidecar(info_path, bitrate, actual_bitrate)
                        continue
                    bitrate = round(actual_bitrate, 2)
            season_dir = info_path.parent
            show_dir = season_dir.parent
            season_match = re.search(r"[Ss]eason\s*(\d+)", season_dir.name)
            season_num = int(season_match.group(1)) if season_match else 1
            episode_number = self._episode_from_filename(info_path.name)
            entry = result.setdefault(
                str(show_dir),
                {"label": show_dir.name, "season": season_num, "eps": {}, "unknown": []},
            )
            rate = round(bitrate, 1)
            if episode_number:
                entry["eps"].setdefault(episode_number, []).append(rate)
            else:
                entry["unknown"].append((info_path.stem[:20], rate))
        self._last_scan_stale = stale_skipped
        if stale_skipped:
            logger.info(
                f"码率优选：低码巡检复核 strm 实际大小，跳过 {stale_skipped} 个已换版/无 strm 的过期边车条目"
            )
        return result

    @staticmethod
    def _compress_episode_ranges(numbers: List[int]) -> str:
        """把集号列表压缩为「E10-E12、E19」形式的简短段落。"""
        if not numbers:
            return ""
        ordered = sorted(set(numbers))
        parts: List[str] = []
        start = prev = ordered[0]
        for num in ordered[1:]:
            if num == prev + 1:
                prev = num
                continue
            parts.append(f"E{start}-E{prev}" if prev > start else f"E{start}")
            start = prev = num
        parts.append(f"E{start}-E{prev}" if prev > start else f"E{start}")
        return "、".join(parts)

    @staticmethod
    def _low_bitrate_group_summary(group: Dict[str, Any]) -> str:
        """生成单个剧目录的低码摘要行：集段压缩 + 全体码率区间。"""
        rates = [rate for rates in group.get("eps", {}).values() for rate in rates]
        rates.extend(rate for _, rate in group.get("unknown", []))
        if rates:
            low, high = min(rates), max(rates)
            range_text = (
                f"（{low:.1f}-{high:.1f}Mbps）" if high > low else f"（{low:.1f}Mbps）"
            )
        else:
            range_text = ""
        episode_numbers = sorted(group.get("eps", {}).keys())
        if episode_numbers:
            episodes_text = LibraryMixin._compress_episode_ranges(episode_numbers)
        elif group.get("unknown"):
            episodes_text = "、".join(label for label, _ in group["unknown"][:2])
        else:
            episodes_text = "未知集"
        return f"{group.get('label', '未知剧集')}：{episodes_text}{range_text}"

    @staticmethod
    def _plan_demotions(
        episode_priority: Dict[str, int],
        low_episodes: List[int],
        baselines: Dict[str, Any],
        records: Dict[str, Any],
        now: float,
        budget: int,
    ) -> Tuple[List[int], List[int], List[int]]:
        """
        规划单个订阅的低码集降级。

        :return: (需降级为待洗的集, 已处于待洗的集, 跳过的集)
                 只降级规则满分（核心视为洗版完成）、已有码率基线、不在冷却期的集；
                 没有基线的集降级后插件无从比较码率，会退化为规则判定，故跳过。
        """
        demote: List[int] = []
        pending: List[int] = []
        skipped: List[int] = []
        for episode in sorted(set(low_episodes)):
            key = str(episode)
            priority = episode_priority.get(key)
            record = records.get(key) or {}
            if priority is None:
                skipped.append(episode)
                continue
            if priority < FULL_EPISODE_PRIORITY:
                pending.append(episode)
                continue
            if baselines.get(key) is None:
                skipped.append(episode)
                continue
            if float(record.get("cooldown", 0) or 0) > now:
                skipped.append(episode)
                continue
            if len(demote) >= budget:
                skipped.append(episode)
                continue
            demote.append(episode)
        return demote, pending, skipped

    @staticmethod
    def _plan_restores(
        episode_priority: Dict[str, int], records: Dict[str, Any], now: float
    ) -> Tuple[List[int], List[int]]:
        """
        规划降级记录的恢复与清理。

        :return: (超时仍停在降级优先级、需恢复满分的集, 已被洗版改写、可清理记录的集)
        """
        restore: List[int] = []
        finished: List[int] = []
        for key, record in records.items():
            try:
                episode = int(key)
            except (TypeError, ValueError):
                continue
            if record.get("cooldown"):
                if float(record["cooldown"]) <= now:
                    finished.append(episode)
                continue
            priority = episode_priority.get(key)
            if priority != DEMOTED_EPISODE_PRIORITY:
                finished.append(episode)
            elif now - float(record.get("ts", 0) or 0) >= INSPECT_DEMOTE_TTL_SECONDS:
                restore.append(episode)
        return sorted(restore), sorted(finished)

    @staticmethod
    def _write_episode_priority(sid: Any, episode_priority: Dict[str, int]) -> None:
        """在独立短事务中写回订阅分集优先级。"""
        from app.db.session import SessionFactory  # pylint: disable=import-outside-toplevel

        with SessionFactory() as session:
            SubscribeOper(session).update(int(sid), {"episode_priority": dict(episode_priority)})
            session.commit()

    def _restore_expired_demotions(
        self, subscriptions: Dict[str, Any], demotions: Dict[str, Any], now: float
    ) -> List[str]:
        """恢复超时未洗成的降级集为满分并进入冷却，返回通知行。"""
        rows: List[str] = []
        for sid, records in list(demotions.items()):
            subscribe = subscriptions.get(sid)
            if subscribe is None:
                demotions.pop(sid, None)
                continue
            priority_map = dict(subscription_priority.get_episode_priority(subscribe))
            restore, finished = self._plan_restores(priority_map, records, now)
            for episode in finished:
                records.pop(str(episode), None)
            if restore:
                for episode in restore:
                    priority_map[str(episode)] = FULL_EPISODE_PRIORITY
                try:
                    self._write_episode_priority(sid, priority_map)
                except Exception as err:
                    logger.error(f"码率优选：恢复订阅 {sid} 分集优先级失败：{err}")
                    continue
                for episode in restore:
                    records[str(episode)] = {"cooldown": now + INSPECT_DEMOTE_COOLDOWN_SECONDS}
                label = self._compress_episode_ranges(restore)
                rows.append(f"· {getattr(subscribe, 'name', sid)} {label}")
                logger.info(
                    f"码率优选：订阅 {sid} {label} 降级 48 小时内未洗到更高码，恢复满分，7 天内不再降级"
                )
            if not records:
                demotions.pop(sid, None)
        return rows

    def _idle_reason(
        self,
        priority_map: Dict[str, int],
        skipped: List[int],
        baselines: Dict[str, Any],
        records: Dict[str, Any],
        now: float,
    ) -> str:
        """说明低码集本轮未降级待洗的原因。"""
        reasons: List[str] = []
        no_record = [e for e in skipped if priority_map.get(str(e)) is None]
        no_base = [e for e in skipped if str(e) not in baselines and e not in no_record]
        cooling = [
            e for e in skipped
            if float((records.get(str(e)) or {}).get("cooldown", 0) or 0) > now
        ]
        budget = [e for e in skipped if e not in no_record + no_base + cooling]
        if cooling:
            until = max(float(records[str(e)]["cooldown"]) for e in cooling)
            reasons.append(
                f"{self._compress_episode_ranges(cooling)} 近期洗过未找到更好版本，"
                f"冷却至 {time.strftime('%m-%d', time.localtime(until))}"
            )
        if no_base:
            reasons.append(f"{self._compress_episode_ranges(no_base)} 缺码率基线")
        if no_record:
            reasons.append(f"{self._compress_episode_ranges(no_record)} 订阅无下载记录")
        if budget:
            reasons.append(f"{self._compress_episode_ranges(budget)} 本轮额度已满，下轮处理")
        return "；".join(reasons) or "无可洗集"

    @staticmethod
    def _plan_graduation(
        priority_map: Dict[str, int],
        baselines: Dict[str, Any],
        watch: Dict[str, Any],
        now: float,
        margin: float,
        excluded: Set[str],
    ) -> Tuple[List[int], Dict[str, Any]]:
        """
        规划可以停止洗版的集。

        条件：已下载但规则未满分（核心仍会每轮搜索），码率不低于 GRADUATE_MIN_MBPS，
        且观察期 GRADUATE_WATCH_SECONDS 内码率未再提升超过领先比例。
        观察期内洗到明显更好的版本会重新计时。

        :return: (可停止洗版的集, 更新后的观察记录)
        """
        graduate: List[int] = []
        new_watch: Dict[str, Any] = {}
        for key, priority in priority_map.items():
            try:
                episode = int(key)
            except (TypeError, ValueError):
                continue
            if priority <= 0 or priority >= FULL_EPISODE_PRIORITY or key in excluded:
                continue
            base = baselines.get(key)
            if base is None or float(base) < GRADUATE_MIN_MBPS:
                continue
            record = watch.get(key)
            if not record or float(base) > float(record.get("mbps", 0)) * (1 + margin):
                new_watch[key] = {"ts": now, "mbps": float(base)}
                continue
            if now - float(record.get("ts", now)) >= GRADUATE_WATCH_SECONDS:
                graduate.append(episode)
                continue
            new_watch[key] = record
        return sorted(graduate), new_watch

    def _safe_graduate(self) -> Dict[str, Any]:
        """定时执行洗版毕业，吞掉异常。"""
        try:
            return self._graduate_episodes()
        except Exception as err:
            logger.error(f"码率优选：码率达标集停止洗版失败：{err}")
            return {"success": False, "message": str(err)}

    def _graduate_episodes(self) -> Dict[str, Any]:
        """
        对码率已达标、观察期内没有洗到更好版本的集停止洗版。

        核心按规则优先级判定洗版完成，规则拿不到满分的集会被永久反复搜索；
        这里把满足条件的集写为规则满分，核心即不再为其搜索。
        """
        if not (self._enabled and self._wash_enabled):
            return {"success": False, "graduated": 0}
        now = time.time()
        margin = max(0, int(self._margin_percent)) / 100.0
        watch_all: Dict[str, Any] = self.get_data(GRADUATION_KEY) or {}
        demotions: Dict[str, Any] = self.get_data(INSPECT_DEMOTIONS_KEY) or {}
        rows: List[str] = []
        total = 0
        new_watch_all: Dict[str, Any] = {}
        for subscribe in SubscribeOper().list() or []:
            if getattr(subscribe, "type", None) != MediaType.TV.value:
                continue
            if not getattr(subscribe, "best_version", False):
                continue
            if subscription_priority.is_full_best_version_enabled(subscribe):
                continue
            sid = str(getattr(subscribe, "id", ""))
            priority_map = dict(subscription_priority.get_episode_priority(subscribe))
            baselines = self._wash_baseline_entry(subscribe).get("episodes") or {}
            excluded = {k for k, rec in (demotions.get(sid) or {}).items() if rec.get("ts")}
            graduate, new_watch = self._plan_graduation(
                priority_map, baselines, watch_all.get(sid) or {}, now, margin, excluded
            )
            if new_watch:
                new_watch_all[sid] = new_watch
            if not graduate:
                continue
            for episode in graduate:
                priority_map[str(episode)] = FULL_EPISODE_PRIORITY
            try:
                self._write_episode_priority(sid, priority_map)
            except Exception as err:
                logger.error(f"码率优选：订阅 {sid} 停止洗版写入失败：{err}")
                continue
            total += len(graduate)
            label = f"{getattr(subscribe, 'name', sid)} {self._compress_episode_ranges(graduate)}"
            rows.append(f"· {label}")
            logger.info(
                f"码率优选：{label} 码率已达 {GRADUATE_MIN_MBPS:g}Mbps 以上且 3 天内未找到更好版本，停止洗版"
            )
        self.save_data(GRADUATION_KEY, new_watch_all)
        if rows and self._notify:
            self.post_message(
                mtype=None,
                title="码率优选：停止洗版",
                text=(
                    f"以下集码率已达 {GRADUATE_MIN_MBPS:g} Mbps 以上，3 天内没找到更好的版本，"
                    "不再反复搜索：\n" + "\n".join(rows[:8])
                    + ("\n（若某部剧全部集都已停止洗版，MoviePilot 会把该洗版订阅视为完成）")
                ),
            )
        return {"success": True, "graduated": total}

    def _low_bitrate_inspect(self) -> Dict[str, Any]:
        """
        巡检媒体库低码集，让在订洗版订阅重新搜索这些集。

        核心按规则优先级判定洗版完成：满分（100）的集永不再搜。对开启洗版的
        分集订阅，把实测低于门槛、已有码率基线的满分集降为 99 使其重新待洗，
        再触发订阅搜索；能否替换仍由码率优先准入把关（须领先基线设定比例）。
        48 小时内未洗到更高码的集恢复满分并冷却 7 天。未开洗版或全集洗版的
        订阅只通知不处理。
        """
        if not (self._enabled and self._inspect_enabled):
            return {"success": False, "low": 0, "triggered": [], "message": "插件或低码巡检未启用"}
        now = time.time()
        all_tv = [
            subscribe
            for subscribe in (SubscribeOper().list() or [])
            if getattr(subscribe, "type", None) == MediaType.TV.value
        ]
        by_sid = {str(getattr(sub, "id", "")): sub for sub in all_tv}
        demotions: Dict[str, Any] = self.get_data(INSPECT_DEMOTIONS_KEY) or {}
        restored_rows = self._restore_expired_demotions(by_sid, demotions, now)
        if self._wash_enabled:
            self._safe_seed_baselines()
        cutoff = now - max(1, self._inspect_days) * 86400
        self._retire_budget = STALE_SIDECAR_MAX_PER_RUN
        self._retired_sidecars = []
        low_groups = self._scan_low_bitrate_items(cutoff)
        if not low_groups and not restored_rows:
            self.save_data(INSPECT_DEMOTIONS_KEY, demotions)
            logger.info("码率优选：低码巡检完成，最近更新条目均高于门槛")
            return {"success": True, "low": 0, "triggered": []}
        triggers = self.get_data(INSPECT_TRIGGERS_KEY) or {}
        budget = INSPECT_MAX_DEMOTE_PER_ROUND
        triggered_rows: List[str] = []
        triggered_labels: List[str] = []
        throttled_rows: List[str] = []
        idle_rows: List[str] = []
        not_wash_rows: List[str] = []
        unmatched_rows: List[str] = []
        failed_rows: List[str] = []
        for group in low_groups.values():
            season_num = group.get("season", 1)
            summary = self._low_bitrate_group_summary(group)
            subscribe = self._match_subscribe_id(group["label"], season_num, all_tv)
            if subscribe is None:
                unmatched_rows.append(f"· {summary}")
                continue
            sid = str(getattr(subscribe, "id", ""))
            if (
                not getattr(subscribe, "best_version", False)
                or subscription_priority.is_full_best_version_enabled(subscribe)
                or not self._wash_enabled
            ):
                not_wash_rows.append(f"· {summary}")
                continue
            priority_map = dict(subscription_priority.get_episode_priority(subscribe))
            baselines = self._wash_baseline_entry(subscribe).get("episodes") or {}
            records = demotions.setdefault(sid, {})
            demote, pending, skipped = self._plan_demotions(
                priority_map, list(group.get("eps", {}).keys()), baselines, records, now, budget
            )
            if demote:
                for episode in demote:
                    priority_map[str(episode)] = DEMOTED_EPISODE_PRIORITY
                try:
                    self._write_episode_priority(sid, priority_map)
                except Exception as err:
                    logger.error(f"码率优选：低码巡检降级订阅 {sid} 失败：{err}")
                    failed_rows.append(f"· {summary}（降级失败：{err}）")
                    continue
                for episode in demote:
                    records[str(episode)] = {"ts": now}
                budget -= len(demote)
                logger.info(
                    f"码率优选：低码巡检将订阅 {sid}（{group['label']}）"
                    f"{self._compress_episode_ranges(demote)} 降为待洗"
                )
            if not records:
                demotions.pop(sid, None)
            if not demote and not pending:
                reason = self._idle_reason(priority_map, skipped, baselines, records, now)
                idle_rows.append(f"· {summary}（{reason}）")
                continue
            if not demote and now - float(triggers.get(sid, 0)) < INSPECT_THROTTLE_SECONDS:
                throttled_rows.append(f"· {summary}")
                continue
            try:
                SubscribeChain().search(sid=int(sid), manual=True)
            except Exception as err:
                logger.error(f"码率优选：低码巡检触发订阅搜索失败（sid={sid}）：{err}")
                failed_rows.append(f"· {summary}（触发失败：{err}）")
                continue
            triggers[sid] = now
            triggered_labels.append(group["label"])
            washing = sorted(set(demote) | set(pending))
            triggered_rows.append(
                f"· {group['label']}：{self._compress_episode_ranges(washing)} 待洗"
            )
            logger.info(f"码率优选：低码巡检触发订阅搜索洗版 sid={sid}（{group['label']}）")
        self.save_data(INSPECT_DEMOTIONS_KEY, demotions)
        if triggered_labels:
            self.save_data(INSPECT_TRIGGERS_KEY, triggers)
        logger.info(
            f"码率优选：低码巡检完成，发现 {len(low_groups)} 个低码目录，"
            f"触发 {len(triggered_labels)} 个订阅洗版搜索"
        )
        if self._notify:
            lines: List[str] = []
            if triggered_rows:
                lines.append(f"已让 {len(triggered_rows)} 部剧的低码集重新待洗并触发搜索：")
                lines.extend(triggered_rows[:8])
            if restored_rows:
                lines.append("以下降级 48 小时内未找到更高码版本，已恢复并冷却 7 天：")
                lines.extend(restored_rows[:6])
            if failed_rows:
                lines.append("以下处理失败，请留意：")
                lines.extend(failed_rows[:4])
            if idle_rows:
                lines.append("以下低码集本轮未处理：")
                lines.extend(idle_rows[:6])
            if self._last_scan_stale:
                retired = len(self._retired_sidecars)
                lines.append(
                    f"另有 {self._last_scan_stale} 个条目的边车显示低码，但实际文件已是高码（边车未刷新），已忽略"
                    + (f"；其中 {retired} 个过期边车已改名为 .stale，Emby 下次刷新时由神医助手重新提取"
                       if retired else "")
                )
            if not_wash_rows:
                lines.append("以下订阅未开启分集洗版，不自动处理：")
                lines.extend(not_wash_rows[:6])
            if unmatched_rows:
                lines.append("以下剧集未订阅，不自动处理：")
                lines.extend(unmatched_rows[:6])
            if lines:
                self.post_message(mtype=None, title="码率优选巡检", text="\n".join(lines))
        return {
            "success": True,
            "low": len(low_groups),
            "triggered": triggered_labels,
            "throttled": len(throttled_rows),
            "idle": len(idle_rows),
            "not_wash": len(not_wash_rows),
            "unmatched": len(unmatched_rows),
        }

    def seed_baselines_from_library(self) -> Dict[str, Any]:
        """
        扫描媒体库 mediainfo，为尚无基线的剧集填充实测码率。

        只补空缺；已有基线与库内实测倍差超过 WASH_BASELINE_MAX_DIVERGENCE
        时（下载提交时的估算虚高，或版本升级后基线陈旧），以实测覆盖。
        """
        if not (self._enabled and self._wash_enabled):
            return {"success": False, "seeded": 0, "message": "插件或码率优先洗版未启用"}
        root = Path(self._library_root)
        if not root.is_dir():
            return {"success": False, "seeded": 0, "message": f"媒体库根目录不存在：{root}"}
        subscriptions = [
            subscribe
            for subscribe in (SubscribeOper().list() or [])
            if getattr(subscribe, "best_version", False)
            and getattr(subscribe, "type", None) == MediaType.TV.value
            and getattr(subscribe, "media_source", None)
            and getattr(subscribe, "media_id", None)
        ]
        if not subscriptions:
            return {"success": True, "seeded": 0, "message": "没有需要播种的洗版订阅"}
        changed = 0
        corrected_total = 0
        matched = 0
        self._seed_verify_budget = SEED_VERIFY_MAX_PER_RUN
        self._retire_budget = STALE_SIDECAR_MAX_PER_RUN
        self._retired_sidecars = []
        for subscribe in subscriptions:
            seeded, corrected = self._seed_subscription_baseline(subscribe, root)
            if seeded < 0:
                continue
            matched += 1
            changed += seeded
            corrected_total += corrected
        if changed or corrected_total:
            self.save_data(WASH_BASELINES_KEY, self._baselines)
        logger.info(
            f"码率优选：库内基线播种完成，补种 {changed} 集、校正 {corrected_total} 集"
            f"（匹配 {matched}/{len(subscriptions)} 个订阅）"
        )
        return {
            "success": True,
            "seeded": changed,
            "corrected": corrected_total,
            "matched_subscriptions": matched,
            "total_subscriptions": len(subscriptions),
        }

    def _seed_subscription_baseline(self, subscribe: Any, root: Path) -> Tuple[int, int]:
        """
        为单个洗版订阅按库内 mediainfo 播种或校正每集基线。

        缺失基线的集直接补种实测值；已有基线与库内实测倍差超过
        WASH_BASELINE_MAX_DIVERGENCE 时以实测覆盖，修复下载提交时
        因集数解析不全导致的估算虚高，以及版本升级后的陈旧低基线。
        取得实测值的集同时清除其垫高准入下限：实测基线接管防降级职责后，
        陈旧下限（尤其是错误虚高的估算）不应继续压制后续正常洗版。

        :return: (补种集数, 校正集数)；库内未找到对应目录时返回 (-1, -1)
        """
        name = str(getattr(subscribe, "name", "") or "")
        year = getattr(subscribe, "year", None)
        season = getattr(subscribe, "season", None) or 1
        show_dir = self._find_show_dir(root, name, year)
        if show_dir is None:
            return -1, -1
        season_dir = self._find_season_dir(show_dir, season)
        if season_dir is None:
            return -1, -1
        key = self._wash_media_key(
            source=getattr(subscribe, "media_source", None),
            media_id=getattr(subscribe, "media_id", None),
            season=season,
        )
        if not key:
            return -1, -1
        entry = self._baselines.setdefault(key, {})
        episode_map = entry.setdefault("episodes", {})
        floor_map = entry.setdefault("ep_floors", {})
        source_map = entry.setdefault("ep_sources", {})
        actual_map = entry.get("ep_actual") or {}
        seeded = 0
        corrected = 0
        verified_fixed = 0
        for info_path in sorted(season_dir.glob("*-mediainfo.json")):
            episode_number = self._episode_from_filename(info_path.name)
            if not episode_number:
                continue
            ep_key = str(episode_number)
            sidecar = self._mediainfo_bitrate_mbps(info_path)
            if sidecar is None:
                continue
            existing = episode_map.get(ep_key)
            has_floor = self._floor_value(floor_map.get(ep_key)) is not None
            bitrate = sidecar
            verified = False
            # 边车 mediainfo 换版后可能不刷新：偏低或带防降级下限的集，按 strm 实际文件核实
            if has_floor or sidecar < SEED_VERIFY_BELOW_MBPS or (
                existing is not None and float(existing) < SEED_VERIFY_BELOW_MBPS
            ):
                actual, status = self._verified_library_bitrate(info_path)
                if status == "stale":
                    # 无同名 strm：边车是换版残留，不代表当前版本
                    continue
                if actual is not None:
                    bitrate = actual
                    verified = True
                    self._retire_stale_sidecar(info_path, sidecar, actual)
                elif has_floor:
                    # 核实失败且有下限：不拿可能过期的边车覆盖，保留下限
                    continue
            if existing is None:
                episode_map[ep_key] = bitrate
                source_map[ep_key] = "measured"
                floor_map.pop(ep_key, None)
                seeded += 1
            elif self._baseline_diverged(existing, bitrate) or (
                verified and abs(float(existing) - bitrate) > float(existing) * SEED_VERIFY_TOLERANCE
            ) or (
                # 统一口径迁移：库内实测与基线差超 5% 时以库内为准；
                # 已按入库文件实测记录的集不被可能过期的边车覆盖
                ep_key not in actual_map
                and abs(float(existing) - bitrate) > float(existing) * SEED_RECALIBRATE_TOLERANCE
            ):
                if verified and not self._baseline_diverged(existing, bitrate):
                    verified_fixed += 1
                else:
                    corrected += 1
                episode_map[ep_key] = bitrate
                source_map[ep_key] = "measured"
                floor_map.pop(ep_key, None)
            elif ep_key in floor_map:
                floor_map.pop(ep_key, None)
                verified_fixed += 1
        if verified_fixed:
            logger.info(
                f"码率优选：库内播种 {key} 按 strm 实际文件核实修正 {verified_fixed} 集基线"
                f"（{show_dir.name}）"
            )
            corrected += verified_fixed
        if corrected:
            logger.warning(
                f"码率优选：库内播种 {key} 校正 {corrected} 集基线为实测值"
                f"（{show_dir.name}），原值与库内实测倍差超过 "
                f"{WASH_BASELINE_MAX_DIVERGENCE:g} 倍"
            )
        if seeded:
            logger.info(
                f"码率优选：库内播种 {key} 补种 {seeded} 集实测基线（{show_dir.name}）"
            )
        return seeded, corrected

    def _find_show_dir(self, root: Path, name: str, year: Any) -> Optional[Path]:
        """在媒体库根目录有限深度内按「剧名 (年份)」匹配剧集目录。"""
        if not name:
            return None
        year_token = f"({year})" if year not in (None, "") else None
        for show_dir in self._iter_show_dirs(root, depth=3):
            if year_token and year_token not in show_dir.name:
                continue
            if name in show_dir.name:
                return show_dir
        return None

    @staticmethod
    def _iter_show_dirs(root: Path, depth: int = 3):
        """逐层遍历根目录下有限深度的子目录，避免整库递归扫描。"""
        current = [root]
        for _ in range(depth):
            next_level: List[Path] = []
            for directory in current:
                try:
                    children = [child for child in directory.iterdir() if child.is_dir()]
                except OSError:
                    continue
                for child in children:
                    yield child
                    next_level.append(child)
            current = next_level

    def _library_present_episodes(self, subscribe: Any) -> Optional[Set[int]]:
        """
        列出订阅对应季目录中实际存在 strm/视频文件的集号。

        订阅里没有下载记录的集，既可能是真缺，也可能是库里已有但不是由该订阅下载的；
        以媒体库文件为准区分两者。找不到剧集或季目录时返回 None（无法判断）。
        结果缓存 LIBRARY_PRESENT_TTL_SECONDS 秒。
        """
        name = str(getattr(subscribe, "name", "") or "")
        year = getattr(subscribe, "year", None)
        season = int(getattr(subscribe, "season", None) or 1)
        cache_key = f"{name}|{year}|{season}"
        cached = self._present_cache.get(cache_key)
        if cached is not None and time.time() - cached[0] < LIBRARY_PRESENT_TTL_SECONDS:
            return cached[1]
        result: Optional[Set[int]] = None
        try:
            root = Path(self._library_root)
            show_dir = self._find_show_dir(root, name, year) if root.is_dir() else None
            season_dir = self._find_season_dir(show_dir, season) if show_dir else None
            if season_dir is not None:
                result = set()
                for child in season_dir.iterdir():
                    if not child.name.lower().endswith(LIBRARY_EPISODE_EXTS):
                        continue
                    matched = re.search(r"S\d+E(\d+)(?:-?E(\d+))?", child.name, re.IGNORECASE)
                    if matched:
                        start = int(matched.group(1))
                        end = int(matched.group(2) or start)
                        result.update(range(start, end + 1))
        except OSError:
            result = None
        self._present_cache[cache_key] = (time.time(), result)
        return result

    @staticmethod
    def _find_season_dir(show_dir: Path, season: int) -> Optional[Path]:
        """在剧集目录内定位指定季的目录，兼容 Season 1 与 Season 01。"""
        for child in show_dir.iterdir():
            if not child.is_dir() or not child.name.lower().startswith("season"):
                continue
            parts = child.name.split()
            if parts and parts[-1].isdigit() and int(parts[-1]) == int(season):
                return child
        return None

    @staticmethod
    def _episode_from_filename(filename: str) -> Optional[int]:
        """从 mediainfo 文件名中解析集号。"""
        matched = re.search(r"S\d+E(\d+)", filename, re.IGNORECASE)
        if not matched:
            return None
        try:
            return int(matched.group(1))
        except (TypeError, ValueError):
            return None

    def _mediainfo_bitrate_mbps(self, path: Path) -> Optional[float]:
        """
        读取 P115StrmHelper mediainfo 的整体码率（Mbps，含音轨）。

        与候选估算、入库实测同口径（文件体积 ÷ 时长）：优先 Size/RunTimeTicks，
        其次容器总码率 Bitrate，最后才退回视频流码率。
        """
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            source = data[0]["MediaSourceInfo"]
            size = int(source.get("Size") or 0)
            ticks = float(source.get("RunTimeTicks") or 0)
            bitrate = int(size * 8 / (ticks / 10_000_000)) if size and ticks else 0
            if not bitrate:
                bitrate = int(source.get("Bitrate") or 0)
            if not bitrate:
                video = next(
                    (s for s in source.get("MediaStreams", []) if s.get("Type") == "Video"),
                    {},
                )
                bitrate = int(video.get("BitRate") or 0)
            return round(bitrate / 1_000_000, 2) if bitrate else None
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            return None

    @staticmethod
    def _strm_remote_size(info_path: Path) -> Tuple[bool, Optional[int]]:
        """
        读取同名 .strm 指向的 115 直链实际文件大小（字节）。

        :return: (是否存在同名 strm, 实际大小；无 strm 或获取失败时大小为 None)
        """
        strm_path = info_path.with_name(
            info_path.name[: -len("-mediainfo.json")] + ".strm"
        )
        try:
            url = strm_path.read_text(encoding="utf-8").strip()
        except OSError:
            return False, None
        if not url:
            return True, None
        try:
            request = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(request, timeout=8) as response:
                length = response.headers.get("Content-Length")
            return True, int(length) if length else None
        except Exception as err:
            logger.debug(
                f"码率优选：strm 实际大小获取失败（{strm_path.name}）：{err}"
            )
            return True, None

    def _retire_stale_sidecar(self, info_path: Path, sidecar_mbps: float, actual_mbps: float) -> bool:
        """
        边车码率与实际文件相差超过阈值时，把边车改名为 .stale 备份。

        神医助手持久化的 -mediainfo.json 在洗版换文件后不会自动更新，Emby 刷新时
        仍会读回旧版本的媒体信息；移走后由神医助手在下次刷新时重新提取。
        只处理生成超过一天的边车，单轮有额度上限；改名而非删除，便于手动恢复。

        :return: 是否已改名
        """
        if self._retire_budget <= 0 or not sidecar_mbps or not actual_mbps:
            return False
        if abs(actual_mbps - sidecar_mbps) <= sidecar_mbps * STALE_SIDECAR_DIFF:
            return False
        try:
            if time.time() - info_path.stat().st_mtime < STALE_SIDECAR_MIN_AGE_SECONDS:
                return False
            target = info_path.with_name(info_path.name + ".stale")
            if target.exists():
                target = info_path.with_name(f"{info_path.name}.{int(time.time())}.stale")
            info_path.rename(target)
        except OSError as err:
            logger.warning(f"码率优选：过期边车改名失败 {info_path}：{err}")
            return False
        self._retire_budget -= 1
        self._retired_sidecars.append(info_path.name)
        logger.info(
            f"码率优选：边车码率 {sidecar_mbps:.1f}Mbps 与实际文件 {actual_mbps:.1f}Mbps 不符，"
            f"已改名为 .stale 待神医助手重新提取：{info_path}"
        )
        return True

    def _verified_library_bitrate(self, info_path: Path) -> Tuple[Optional[float], str]:
        """
        按同名 strm 指向的实际文件大小与边车时长计算当前版本码率（Mbps）。

        :return: (码率, 状态)；状态 ok=核实成功，stale=无同名 strm（边车为换版残留），
                 failed=无法核实（请求失败、无时长或核实额度用尽）
        """
        if self._seed_verify_budget <= 0:
            return None, "failed"
        self._seed_verify_budget -= 1
        has_strm, size = self._strm_remote_size(info_path)
        if not has_strm:
            return None, "stale"
        runtime = self._mediainfo_runtime_seconds(info_path)
        time.sleep(SEED_VERIFY_INTERVAL_SECONDS)
        if not size or not runtime:
            return None, "failed"
        return round(size * 8 / runtime / 1_000_000, 2), "ok"

    @staticmethod
    def _mediainfo_runtime_seconds(path: Path) -> Optional[float]:
        """读取 mediainfo 中媒体运行时长（秒），用于按实际文件大小估算码率。"""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            ticks = float(data[0]["MediaSourceInfo"].get("RunTimeTicks") or 0)
            return ticks / 10_000_000 if ticks > 0 else None
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            return None
