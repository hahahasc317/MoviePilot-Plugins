"""码率优选：运行时补丁：洗版准入与整包门槛的替换、自检与整包码率放行标记。"""

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


class PatchMixin:
    """运行时补丁：洗版准入与整包门槛的替换、自检与整包码率放行标记。"""

    def _apply_wash_patch(self) -> None:
        """
        按配置安装或移除码率优先洗版的运行时补丁。

        插件每次加载（含 MoviePilot 升级重启后）都会自动重新替换；目标函数签名
        与预期不一致时该项补丁不安装，相关功能自动停用并通知一次，保持核心原判定。
        """
        if not (self._enabled and self._wash_enabled):
            self._remove_wash_patch()
            return
        module = subscription_priority
        factories = {
            PATCH_PREPARE: self._make_patched_prepare,
            PATCH_PACK_GATE: self._make_patched_pack_gate,
        }
        status: Dict[str, str] = {}
        for name, expected in PATCH_TARGETS.items():
            current = getattr(module, name, None)
            if current is None:
                status[name] = "missing"
                continue
            if getattr(current, "_bitrateselector_wrapper", False):
                original = getattr(current, "_bitrateselector_original", None)
            else:
                original = current
            check = self._signature_check(original, expected)
            if check:
                status[name] = f"incompatible:{check}"
                continue
            wrapper = factories[name](original)
            wrapper._bitrateselector_wrapper = True
            wrapper._bitrateselector_original = original
            setattr(module, name, wrapper)
            status[name] = "active"
        self._patch_status = status
        broken = {name: state for name, state in status.items() if state != "active"}
        if broken:
            self._alert_patch_incompatible(broken)
        logger.info(
            "码率优选：运行时补丁状态 "
            + "，".join(f"{name}={state}" for name, state in status.items())
        )

    @staticmethod
    def _signature_check(func: Any, expected: List[str]) -> str:
        """校验目标函数参数列表，一致返回空串，否则返回不一致说明。"""
        if func is None or not callable(func):
            return "不可调用"
        try:
            params = list(inspect.signature(func).parameters)
        except (TypeError, ValueError):
            return "无法读取签名"
        if params != expected:
            return f"签名变为 {params}"
        return ""

    def _alert_patch_incompatible(self, broken: Dict[str, str]) -> None:
        """补丁不兼容时按「MoviePilot 版本 + 不兼容详情」去重通知一次。"""
        try:
            from app.runtime.config import settings  # pylint: disable=import-outside-toplevel

            version = str(getattr(settings, "VERSION_FLAG", "") or getattr(settings, "APP_VERSION", "") or "")
        except Exception:
            version = ""
        signature = f"{version}|" + "|".join(f"{k}={v}" for k, v in sorted(broken.items()))
        if self.get_data(PATCH_ALERT_KEY) == signature:
            return
        self.save_data(PATCH_ALERT_KEY, signature)
        detail = "；".join(f"{k}：{v}" for k, v in broken.items())
        logger.error(f"码率优选：MoviePilot 内部函数与插件预期不一致，相关功能已自动停用：{detail}")
        if self._notify:
            self.post_message(
                mtype=None,
                title="码率优选补丁失效",
                text=(
                    "MoviePilot 升级后内部函数发生变化，以下补丁未安装，"
                    f"对应功能已停用并回退核心原判定：\n{detail}\n请更新码率优选插件。"
                ),
            )

    def _patch_watchdog(self) -> None:
        """定时自检：补丁被热重载或其他插件覆盖时自动重新替换。"""
        if not (self._enabled and self._wash_enabled):
            return
        module = subscription_priority
        lost = [
            name
            for name, state in self._patch_status.items()
            if state == "active"
            and not getattr(getattr(module, name, None), "_bitrateselector_wrapper", False)
        ]
        if lost:
            logger.warning(f"码率优选：补丁被覆盖或模块重载（{lost}），重新替换")
            self._apply_wash_patch()

    def _remove_wash_patch(self) -> None:
        """移除全部运行时补丁并还原原始函数。"""
        module = subscription_priority
        restored = []
        for name in PATCH_TARGETS:
            current = getattr(module, name, None)
            if getattr(current, "_bitrateselector_wrapper", False):
                setattr(module, name, current._bitrateselector_original)
                restored.append(name)
        # 兼容 1.3.x 以前的补丁存放方式
        legacy = getattr(module, "_bitrateselector_original_prepare", None)
        if legacy is not None:
            module.prepare_best_version_tv_candidate = legacy
            del module._bitrateselector_original_prepare
            restored.append(PATCH_PREPARE)
        if hasattr(module, "_bitrateselector_patched_by"):
            del module._bitrateselector_patched_by
        self._patch_status = {}
        if restored:
            logger.info(f"码率优选：运行时补丁已移除，恢复核心判定（{sorted(set(restored))}）")

    def _make_patched_prepare(self, original: Any):
        """构造替换洗版准入函数的闭包，判定异常时回退原始规则优先级。"""

        def patched(subscribe: Any, context: Any, priority: int) -> bool:
            """按估算码率执行洗版准入，异常时回退原始判定。"""
            try:
                return self._wash_admit(subscribe, context, priority, original)
            except Exception as err:
                logger.warning(f"码率优选：码率优先洗版判定异常，本次回退规则优先级：{err}")
                return original(subscribe, context, priority)

        return patched

    def _make_patched_pack_gate(self, original: Any):
        """
        构造替换整包准入门槛的闭包。

        核心要求整包规则优先级严格高于每个目标集；规则同为满分时高码整包永远被拒。
        原判定拒绝时，若该整包在本轮准入中已被插件按码率核验为可洗（存在高于基线
        领先比例的集），则放行；实际下载集数仍由核心与候选允许集数取交集限定。
        """

        def patched(subscribe: Any, priority: int) -> bool:
            """先走原判定；被拒时按本轮码率准入标记放行整包。"""
            accepted = original(subscribe, priority)
            if accepted:
                return True
            try:
                if not (self._enabled and self._wash_enabled):
                    return False
                if not getattr(subscribe, "best_version", False):
                    return False
                if subscription_priority.is_full_best_version_enabled(subscribe):
                    return False
                context = self._find_caller_context(priority)
                if context is None:
                    return False
                washable = self._wash_mark_for(context)
                if not washable:
                    return False
                title = (getattr(context.torrent_info, "title", "") or "")[:60]
                logger.info(
                    f"码率优选：整包规则优先级未高于已有集，但码率可提升 "
                    f"{self._compress_episode_ranges(sorted(washable))}，放行整包（{title}）"
                )
                return True
            except Exception as err:
                logger.warning(f"码率优选：整包码率放行判定异常，保持核心原判定：{err}")
                return False

        return patched

    @staticmethod
    def _find_caller_context(priority: Any, max_depth: int = 8) -> Optional[Any]:
        """
        沿调用栈查找正在被整包门槛判定的候选上下文。

        核心门槛函数只接收 (subscribe, priority)，候选位于调用方循环的局部变量
        context 中；按 torrent_info.pri_order 与 priority 一致校验，找不到返回 None。
        """
        frame = sys._getframe(1)  # pylint: disable=protected-access
        try:
            depth = 0
            while frame is not None and depth < max_depth:
                candidate = frame.f_locals.get("context")
                torrent = getattr(candidate, "torrent_info", None)
                if torrent is not None and getattr(torrent, "pri_order", None) == priority:
                    return candidate
                frame = frame.f_back
                depth += 1
            return None
        finally:
            del frame

    def _mark_wash_candidate(self, context: Any, washable: List[int]) -> None:
        """记录本轮按码率核验可洗的候选，供整包门槛放行使用。"""
        now = time.time()
        marks = self._wash_marks
        for key in [k for k, v in marks.items() if now - v[0] > WASH_MARK_TTL_SECONDS]:
            marks.pop(key, None)
        title = str(getattr(getattr(context, "torrent_info", None), "title", "") or "")
        if washable:
            marks[id(context)] = (now, title, set(washable))
        else:
            marks.pop(id(context), None)

    def _wash_mark_for(self, context: Any) -> Set[int]:
        """读取候选的可洗集合；对象 id 复用或过期时返回空集。"""
        mark = self._wash_marks.get(id(context))
        if not mark:
            return set()
        ts, title, washable = mark
        current_title = str(getattr(getattr(context, "torrent_info", None), "title", "") or "")
        if time.time() - ts > WASH_MARK_TTL_SECONDS or title != current_title:
            return set()
        return washable
