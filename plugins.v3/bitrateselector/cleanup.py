"""码率优选：清理被其他种子替代、长期停滞的订阅下载任务。"""

import json
import re
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from app.sdk.logging import logger
from app.schemas.types import MediaType

from .constants import *  # noqa: F401,F403  pylint: disable=wildcard-import,unused-wildcard-import


class CleanupMixin:
    """
    同一集已经由别的种子整理入库后，清理仍停在 0% 的旧下载任务。

    典型场景：洗版先选中了一个几乎没人做种的单集种子，迟迟下不动；
    之后另一个种子把同一集洗进了库，旧任务却一直挂在下载器里。
    只删同时满足以下条件的任务：订阅来源、进度为 0、添加超过
    CLEANUP_MIN_AGE_SECONDS、所覆盖的每一集都在该任务添加之后由其他种子入库。
    手动下载、已有进度的任务、无法识别集数的任务一律不动。
    """

    _cleanup_lock = threading.Lock()

    # ---------- 入库记录 ----------

    def _note_transfer_marks(self, data: Any) -> Optional[Tuple[str, List[int]]]:
        """整理成功后记下「媒体:季」的哪些集在何时入库，供清理判断用。"""
        info = getattr(data, "transferinfo", None)
        meta = getattr(data, "meta", None)
        media = getattr(data, "mediainfo", None)
        if info is None or not getattr(info, "success", False) or meta is None or media is None:
            return None
        if getattr(media, "type", None) not in (MediaType.TV, MediaType.TV.value, "电视剧"):
            return None
        episodes = sorted({int(e) for e in (getattr(meta, "episode_list", None) or []) if e})
        if not episodes:
            begin = getattr(meta, "begin_episode", None)
            episodes = [int(begin)] if begin else []
        if not episodes:
            return None
        season = int(getattr(meta, "begin_season", None) or 1)
        key = self._transfer_media_key(media, season)
        if not key:
            return None
        now = time.time()
        with self._baseline_lock:
            marks: Dict[str, Dict[str, float]] = self.get_data(TRANSFER_MARKS_KEY) or {}
            entry = marks.setdefault(key, {})
            for episode in episodes:
                entry[str(episode)] = now
            # 只保留最近的入库记录，防止数据无限增长
            for mkey in list(marks):
                marks[mkey] = {
                    ep: ts for ep, ts in marks[mkey].items()
                    if now - float(ts) < TRANSFER_MARKS_TTL_SECONDS
                }
                if not marks[mkey]:
                    marks.pop(mkey)
            self.save_data(TRANSFER_MARKS_KEY, marks)
        return key, episodes

    # ---------- 解析工具 ----------

    @staticmethod
    def _parse_season(raw: Any) -> Optional[int]:
        """解析下载任务的季号：1 / "S01" / [1]；多季或无法解析返回 None。"""
        if isinstance(raw, (list, tuple, set)):
            values = list(raw)
            return CleanupMixin._parse_season(values[0]) if len(values) == 1 else None
        if raw is None or raw == "":
            return None
        if isinstance(raw, int):
            return raw
        found = re.findall(r"\d+", str(raw))
        return int(found[0]) if len(found) == 1 else None

    @staticmethod
    def _parse_episodes(raw: Any) -> Set[int]:
        """解析下载任务覆盖的集号：10 / "E10" / "E10-E12" / [10, 11]；无法解析返回空集。"""
        if raw is None or raw == "":
            return set()
        if isinstance(raw, int):
            return {raw}
        if isinstance(raw, (list, tuple, set)):
            result: Set[int] = set()
            for item in raw:
                result |= CleanupMixin._parse_episodes(item)
            return result
        text = str(raw)
        result = set()
        for start, end in re.findall(r"(\d+)\s*(?:-\s*[Ee]?[Pp]?\s*(\d+))?", text):
            begin = int(start)
            stop = int(end) if end else begin
            if stop < begin or stop - begin > 500:
                return set()
            result |= set(range(begin, stop + 1))
        return result

    @staticmethod
    def _parse_history_time(raw: Any) -> Optional[float]:
        """把下载历史的时间文本（%Y-%m-%d %H:%M:%S）转成时间戳。"""
        if not raw:
            return None
        try:
            return datetime.strptime(str(raw)[:19], "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            return None

    @staticmethod
    def _is_subscribe_note(note: Any) -> bool:
        """下载历史 note.source 以 Subscribe| 开头才算订阅下载。"""
        payload = note
        if not isinstance(payload, dict):
            try:
                payload = json.loads(str(note or "{}"))
            except (TypeError, ValueError):
                return False
        if not isinstance(payload, dict):
            return False
        return str(payload.get("source") or "").startswith("Subscribe|")

    # ---------- 判定与执行 ----------

    def _plan_superseded(
        self,
        torrents: List[Any],
        histories: Dict[str, Any],
        marks: Dict[str, Dict[str, float]],
        now: float,
    ) -> List[Tuple[Any, str]]:
        """
        找出可清理的下载任务。

        :return: [(下载任务, 说明文字)]
        """
        plan: List[Tuple[Any, str]] = []
        for torrent in torrents or []:
            torrent_hash = str(getattr(torrent, "hash", "") or "")
            if not torrent_hash:
                continue
            try:
                progress = float(getattr(torrent, "progress", 0) or 0)
            except (TypeError, ValueError):
                continue
            if progress > 0:
                continue
            media = getattr(torrent, "media", None)
            if media is None:
                continue
            source = getattr(media, "media_source", None)
            source = getattr(source, "value", source)
            media_id = getattr(media, "media_id", None)
            season = self._parse_season(getattr(media, "season", None))
            episodes = self._parse_episodes(getattr(media, "episode", None))
            if not (source and media_id and season and episodes):
                continue
            entry = marks.get(f"{source}:{media_id}:{season}") or {}
            if not entry:
                continue
            history = histories.get(torrent_hash) or histories.get(torrent_hash.lower())
            if history is None or not self._is_subscribe_note(getattr(history, "note", None)):
                continue
            added = self._parse_history_time(getattr(history, "date", None))
            if added is None or now - added < CLEANUP_MIN_AGE_SECONDS:
                continue
            if not all(float(entry.get(str(ep), 0)) > added for ep in episodes):
                continue
            label = (
                f"{getattr(media, 'title', '') or getattr(torrent, 'name', '') or ''} "
                f"S{season:02d}{self._compress_episode_ranges(sorted(episodes))}"
            ).strip()
            plan.append((torrent, label))
        return plan

    def _safe_cleanup_superseded(self) -> Dict[str, Any]:
        """清理入口：异常只记日志，不影响其他功能。"""
        if not (self._enabled and self._wash_enabled and self._cleanup_enabled):
            return {"success": False, "removed": 0}
        if not self._cleanup_lock.acquire(blocking=False):
            return {"success": False, "removed": 0}
        try:
            return self._cleanup_superseded()
        except Exception as err:
            logger.warning(f"码率优选：清理被替代的停滞下载失败：{err}")
            return {"success": False, "removed": 0, "message": str(err)}
        finally:
            self._cleanup_lock.release()

    def _cleanup_superseded(self) -> Dict[str, Any]:
        """查询下载中的任务，删除已被其他种子替代且停在 0% 的订阅任务。"""
        marks = self.get_data(TRANSFER_MARKS_KEY) or {}
        if not marks:
            return {"success": True, "removed": 0}
        from app.chain.download import DownloadChain  # pylint: disable=import-outside-toplevel
        from app.db.oper.downloadhistory import (  # pylint: disable=import-outside-toplevel
            DownloadHistoryOper,
        )

        chain = DownloadChain()
        torrents = chain.downloading(None) or []
        stalled = [t for t in torrents if not float(getattr(t, "progress", 0) or 0) and getattr(t, "hash", None)]
        if not stalled:
            return {"success": True, "removed": 0}
        histories = DownloadHistoryOper().get_by_hashes([str(t.hash) for t in stalled]) or {}
        plan = self._plan_superseded(stalled, histories, marks, time.time())
        removed: List[str] = []
        for torrent, label in plan:
            if chain.remove_downloading(str(torrent.hash), getattr(torrent, "downloader", None)):
                removed.append(label)
                logger.info(
                    f"码率优选：{label} 已由其他种子入库，删除停在 0% 的旧下载任务"
                    f"（{(getattr(torrent, 'title', '') or '')[:60]}）"
                )
            else:
                logger.warning(f"码率优选：删除停滞下载任务失败（{label}）")
        if removed and self._notify:
            self.post_message(
                mtype=None,
                title="码率优选",
                text="以下集已由其他种子入库，已删除停在 0% 的旧下载任务：\n"
                + "\n".join(f"· {line}" for line in removed),
            )
        return {"success": True, "removed": len(removed)}
