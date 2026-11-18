import inspect
import json
import re
import sys
import threading
import time
import urllib.request
from pathlib import Path
import os
from typing import Any, Dict, List, Optional, Set, Tuple

from app.chain.tmdb import TmdbChain
from app.chain.subscribe import SubscribeChain
from app.application.subscription import priority as subscription_priority
from app.db.oper.subscribe import SubscribeOper
from app.plugins import _PluginBase
from apscheduler.triggers.cron import CronTrigger
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.media import Context
from app.schemas.event import ResourceDownloadEventData, ResourceSelectionEventData
from app.schemas.media import resolve_media_identity
from app.schemas.types import ChainEventType, EventType, MediaType

from .constants import *  # noqa: F401,F403  pylint: disable=wildcard-import,unused-wildcard-import
from .patches import PatchMixin
from .wash import WashMixin
from .library import LibraryMixin
from .selection import SelectionMixin
from .estimation import EstimationMixin




class BitrateSelector(PatchMixin, WashMixin, LibraryMixin, SelectionMixin, EstimationMixin, _PluginBase):
    """
    码率优选：在下载执行前对比候选资源对每集的估算码率，自动保留高码率资源。

    码率估算公式：种子体积 × 8 ÷ (覆盖集时长之和 × 60)，单位 Mbps。
    单集时长按「TMDB → 库内 mediainfo 实测 → 同季中位数」三级回退解析，
    综艺等 TMDB 缺时长的剧集不再被排除在估算之外。
    对同一媒体、同一覆盖类别的候选，淘汰被更高码率候选覆盖的低码率候选；
    未标注集数的整季包按全季时长估算后同类比较。
    """

    plugin_name = "码率优选"
    plugin_desc = (
        "下载前按「种子体积 ÷ 每集时长」估算码率，同覆盖范围自动优选高码率资源；"
        "可选拦截低码率下载；支持洗版准入按估算码率优先，从媒体库播种实测基线，"
        "并支持每天定时巡检媒体库低码集、自动触发订阅搜索洗版。"
    )
    plugin_icon = ""
    plugin_version = "1.6.2"
    plugin_label = "下载管理"
    plugin_author = "hahahasc317"
    plugin_config_prefix = "bitrateselector_"
    plugin_order = 20
    auth_level = 1

    _enabled = False
    _margin_percent = 15
    _veto_enabled = False
    _veto_min_mbps = 8.0
    _notify = True
    _wash_enabled = False
    _library_root = DEFAULT_LIBRARY_ROOT
    _inspect_enabled = False
    _inspect_min_mbps = 8.0
    _inspect_days = 30
    _baselines: Dict[str, Dict[str, Any]] = {}
    # 基线读改写锁：下载事件、播种线程、定时任务会并发修改 _baselines
    _baseline_lock = threading.RLock()
    # 播种互斥：同一时间只允许一个播种任务
    _seed_lock = threading.Lock()
    # 手动巡检互斥
    _inspect_lock = threading.Lock()
    # 本次播种剩余的 strm 核实额度
    _seed_verify_budget = 0
    # 最近一次巡检中「边车低码但实际文件已是高码」被忽略的条目数
    _last_scan_stale = 0
    # 整包码率放行标记：{id(context): (时间, 种子标题, 可洗集合)}
    _wash_marks: Dict[int, Tuple[float, str, Set[int]]] = {}
    # 各补丁当前状态：{函数名: "active" / "incompatible:<原因>" / "missing"}
    _patch_status: Dict[str, str] = {}

    def _check_local_shadow(self) -> List[str]:
        """
        检查本地插件目录中是否存在本插件的同名副本。

        MoviePilot 会优先加载本地副本，插件市场的更新因此不生效（安装提示成功但仍运行旧版）。
        发现副本时按路径去重通知一次，返回发现的路径列表。
        """
        roots: List[str] = []
        try:
            from app.core.config import settings  # pylint: disable=import-outside-toplevel

            raw = getattr(settings, "PLUGIN_LOCAL_REPO_PATHS", "") or ""
            roots = [part.strip() for part in str(raw).replace(";", ",").split(",") if part.strip()]
        except Exception:
            roots = []
        if not roots:
            roots = ["/config/local-plugins"]
        plugin_dir = self.__class__.__name__.lower()
        found: List[str] = []
        for root in roots:
            for sub in ("", "plugins.v3", "plugins.v2", "plugins"):
                candidate = os.path.join(root, sub, plugin_dir) if sub else os.path.join(root, plugin_dir)
                if os.path.isdir(candidate):
                    found.append(candidate)
        if not found:
            return []
        signature = "|".join(sorted(found))
        try:
            if self.get_data("local_shadow_alert") != signature:
                self.save_data("local_shadow_alert", signature)
                self.post_message(
                    mtype=None,
                    title=f"{self.plugin_name}：发现本地副本",
                    text=(
                        "本地插件目录中有本插件的同名副本，MoviePilot 会优先加载它，"
                        "插件市场的更新可能不会生效：\n" + "\n".join(found)
                        + "\n如非有意保留，请移走该目录后重新安装插件。"
                    ),
                )
        except Exception as err:
            logger.warning(f"{self.plugin_name}：本地副本告警发送失败：{err}")
        logger.warning(f"{self.plugin_name}：本地插件目录存在同名副本 {found}，可能覆盖插件市场版本")
        return found

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        """根据插件配置初始化运行开关与参数。"""
        try:
            self._check_local_shadow()
        except Exception as err:
            logger.warning(f"{self.plugin_name}：本地副本检查失败：{err}")
        self.stop_service()
        if not config:
            return
        self._enabled = bool(config.get("enabled"))
        try:
            self._margin_percent = max(0, int(float(config.get("margin_percent") or 15)))
        except (TypeError, ValueError):
            self._margin_percent = 15
        self._veto_enabled = bool(config.get("veto_enabled"))
        try:
            self._veto_min_mbps = max(0.0, float(config.get("veto_min_mbps") or 8.0))
        except (TypeError, ValueError):
            self._veto_min_mbps = 8.0
        self._notify = bool(config.get("notify"))
        self._wash_enabled = bool(config.get("wash_enabled"))
        self._library_root = (
            str(config.get("library_root") or DEFAULT_LIBRARY_ROOT).rstrip("/")
            or DEFAULT_LIBRARY_ROOT
        )
        self._inspect_enabled = bool(config.get("inspect_enabled"))
        try:
            self._inspect_min_mbps = max(0.0, float(config.get("inspect_min_mbps") or 8.0))
        except (TypeError, ValueError):
            self._inspect_min_mbps = 8.0
        try:
            self._inspect_days = max(1, int(float(config.get("inspect_days") or 30)))
        except (TypeError, ValueError):
            self._inspect_days = 30
        with self._baseline_lock:
            self._baselines = self.get_data(WASH_BASELINES_KEY) or {}
            self._migrate_inflated_floors()
        self._wash_marks = {}
        self._apply_wash_patch()
        if self._wash_enabled:
            threading.Thread(
                target=self._safe_seed_baselines, name="bitrateselector-seed", daemon=True
            ).start()

    def get_state(self) -> bool:
        """获取插件启用状态。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回插件远程命令列表。"""
        return [
            {
                "cmd": "/bitrate_inspect",
                "event": EventType.PluginAction,
                "desc": "立即执行低码巡检洗版",
                "category": "",
                "data": {"action": "bitrate_inspect"},
            }
        ]

    @eventmanager.register(EventType.PluginAction)
    def on_plugin_action(self, event: Event = None) -> None:
        """响应 /bitrate_inspect 命令，在后台立即执行一轮低码巡检。"""
        event_data = getattr(event, "event_data", None) or {}
        if not isinstance(event_data, dict) or event_data.get("action") != "bitrate_inspect":
            return
        if not (self._enabled and self._inspect_enabled):
            self.post_message(
                mtype=None, title="码率优选巡检", text="插件或「低码巡检洗版」未启用，未执行。"
            )
            return
        if not self._inspect_lock.acquire(blocking=False):
            self.post_message(mtype=None, title="码率优选巡检", text="已有巡检在运行，请稍候。")
            return

        def run() -> None:
            """后台执行巡检；没有低码条目时也回报一条结果。"""
            try:
                result = self._safe_low_bitrate_inspect()
                if result.get("success") and not result.get("low"):
                    self.post_message(
                        mtype=None,
                        title="码率优选巡检",
                        text=f"最近 {self._inspect_days} 天更新的条目都不低于 {self._inspect_min_mbps:g} Mbps，无需处理。",
                    )
                elif not result.get("success"):
                    self.post_message(
                        mtype=None,
                        title="码率优选巡检",
                        text=f"巡检失败：{result.get('message', '未知错误')}",
                    )
            finally:
                self._inspect_lock.release()

        self.post_message(mtype=None, title="码率优选巡检", text="已开始巡检，完成后通知结果。")
        threading.Thread(target=run, name="bitrateselector-inspect", daemon=True).start()

    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件 API 列表。"""
        return []

    def get_form(self) -> Tuple[Optional[List[Dict[str, Any]]], Dict[str, Any]]:
        """返回插件配置表单与默认配置。"""
        form = [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "wash_enabled",
                                            "label": "码率优先洗版",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "notify", "label": "发送优选通知"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "veto_enabled", "label": "低码率拦截"},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "inspect_enabled",
                                            "label": "低码巡检洗版",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "inspect_min_mbps",
                                            "label": "低码巡检门槛(Mbps)",
                                            "type": "number",
                                            "hint": "媒体库实测码率低于此值视为低码，默认 8",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "inspect_days",
                                            "label": "巡检范围(天)",
                                            "type": "number",
                                            "hint": "只检查最近 N 天内更新的条目，默认 30",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "margin_percent",
                                            "label": "码率领先比例(%)",
                                            "type": "number",
                                            "hint": "候选被淘汰需高码率候选领先此比例，默认 15%",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "veto_min_mbps",
                                            "label": "低码率拦截门槛(Mbps)",
                                            "type": "number",
                                            "hint": "估算码率低于此值时取消下载；可能导致缺集，慎用",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "library_root",
                                            "label": "媒体库根目录",
                                            "type": "text",
                                            "hint": "STRM 媒体库挂载点，用于按实测码率播种洗版基线",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": (
                                                "码率=种子体积×8÷每集时长之和。单集时长按「TMDB→库内实测→同季中位」"
                                                "三级回退解析，综艺缺 TMDB 时长也可估算。批量下载前，同一媒体内"
                                                "集数覆盖相同（或被超集覆盖）且码率领先超过设定比例的低码率候选会被淘汰；"
                                                "整季包按全季时长估算后同类比较。无法估算（缺集数/缺时长/非TMDB）的候选不参与淘汰。"
                                                "「低码率拦截」作用于每个资源提交下载前，可能造成缺集，默认关闭。"
                                                "「码率优先洗版」开启后，洗版准入改按估算码率与插件记录的每集基线比较"
                                                "（需领先超过设定比例才升级），旧规则优先级基线不再参与洗版；"
                                                "候选与基线的时长来源不一致（如一方含中位数填充）时阈值自动加倍；"
                                                "规则组仍负责资源资格筛选。开启后会扫描媒体库根目录下"
                                                " P115StrmHelper 生成的 -mediainfo.json，为没有基线的剧集"
                                                "播种实测码率，倍差超过 2 倍的异常基线自动按实测校正，"
                                                "并每天 07:30 自动补充。规则同为满分的高码整包也按码率放行。"
                                                "「低码巡检洗版」把实测低于门槛的满分集降为待洗再搜索，"
                                                "48 小时内没洗到更高码则恢复并冷却 7 天，每轮最多 20 集。"
                                            ),
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ]
        defaults = {
            "enabled": False,
            "notify": True,
            "veto_enabled": False,
            "wash_enabled": False,
            "margin_percent": 15,
            "veto_min_mbps": 8.0,
            "library_root": DEFAULT_LIBRARY_ROOT,
            "inspect_enabled": False,
            "inspect_min_mbps": 8.0,
            "inspect_days": 30,
        }
        return form, defaults

    def get_page(self) -> Optional[List[Dict[str, Any]]]:
        """返回插件详情页面。"""
        if not self._enabled:
            return None
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "text": "码率优选已启用：批量下载前自动淘汰同覆盖范围的低码率候选。"
                    + ("洗版准入按估算码率优先。" if self._wash_enabled else ""),
                },
            },
            *self._patch_status_page(),
        ]

    def _patch_status_page(self) -> List[Dict[str, Any]]:
        """详情页展示运行时补丁状态与当前降级待洗记录数。"""
        if not self._wash_enabled:
            return []
        labels = {PATCH_PREPARE: "洗版准入", PATCH_PACK_GATE: "整包门槛"}
        rows = []
        for name, state in (self._patch_status or {}).items():
            text = "已接管" if state == "active" else f"未接管（{state}）"
            rows.append(f"{labels.get(name, name)}：{text}")
        demotions = self.get_data(INSPECT_DEMOTIONS_KEY) or {}
        pending = sum(
            1 for records in demotions.values() for rec in records.values() if rec.get("ts")
        )
        rows.append(f"巡检降级待洗：{pending} 集")
        broken = any(state != "active" for state in (self._patch_status or {}).values())
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "warning" if broken else "success",
                    "variant": "tonal",
                    "title": "运行时补丁（每次加载自动重新替换）",
                    "text": "\n".join(rows),
                },
            }
        ]

    def stop_service(self) -> None:
        """停止插件后台服务并清理运行时缓存。"""
        self._remove_wash_patch()
        self._runtime_cache = {}
        self._tmdb_chain = None

    def get_service(self) -> List[Dict[str, Any]]:
        """注册库内基线播种与低码巡检自动洗版定时服务。"""
        services: List[Dict[str, Any]] = []
        if self._enabled and self._wash_enabled:
            services.append(
                {
                    "id": "BitrateSelectorGraduate",
                    "name": "码率达标集停止洗版",
                    "trigger": CronTrigger.from_crontab("45 7 * * *"),
                    "func": self._safe_graduate,
                    "kwargs": {},
                }
            )
        if self._enabled and self._wash_enabled:
            services.append(
                {
                    "id": "BitrateSelectorSeedBaselines",
                    "name": "码率基线库内播种",
                    "trigger": CronTrigger.from_crontab("30 7 * * *"),
                    "func": self._safe_seed_baselines,
                    "kwargs": {},
                }
            )
        if self._enabled and self._wash_enabled:
            services.append(
                {
                    "id": "BitrateSelectorPatchWatchdog",
                    "name": "码率优选补丁自检",
                    "trigger": CronTrigger.from_crontab("*/30 * * * *"),
                    "func": self._patch_watchdog,
                    "kwargs": {},
                }
            )
        if self._enabled and self._inspect_enabled:
            services.append(
                {
                    "id": "BitrateSelectorLowBitrateInspect",
                    "name": "低码巡检自动洗版",
                    "trigger": CronTrigger.from_crontab("30 10 * * *"),
                    "func": self._safe_low_bitrate_inspect,
                    "kwargs": {},
                }
            )
        return services

    @eventmanager.register(ChainEventType.ResourceSelection)
    def select_high_bitrate(self, event: Event) -> None:
        """批量下载前淘汰同覆盖范围内被高码率候选支配的低码率候选。"""
        if not self._enabled:
            return
        data = event.event_data
        if not isinstance(data, ResourceSelectionEventData):
            return
        # 链式事件中前序插件（如综艺正片净化）可能已改写候选，须在其结果上继续筛选
        if data.updated and data.updated_contexts is not None:
            contexts = list(data.updated_contexts)
        else:
            contexts = list(data.contexts or [])
        if not contexts:
            return
        try:
            kept, dropped_logs, dropped_notices = self._drop_dominated_candidates(contexts)
        except Exception as err:
            logger.error(f"码率优选：处理候选列表失败：{err}")
            return
        if not dropped_logs:
            return
        data.updated = True
        data.updated_contexts = kept
        data.source = "码率优选"
        logger.info(f"码率优选：淘汰 {len(dropped_logs)} 个低码率候选。{'；'.join(dropped_logs)}")
        if self._notify:
            fresh = self._dedupe_notices(dropped_notices)
            if fresh:
                self.post_message(
                    mtype=None,
                    title="码率优选",
                    text=self._render_selection_notice(fresh),
                )

    @eventmanager.register(ChainEventType.ResourceDownload)
    def on_resource_download(self, event: Event) -> None:
        """单个资源提交下载前拦截低码率，并在放行后记录洗版码率基线。"""
        data = event.event_data
        if not isinstance(data, ResourceDownloadEventData) or data.cancel:
            return
        ctx = data.context
        if not isinstance(ctx, Context) or not ctx.media_info or not ctx.torrent_info:
            return
        if getattr(ctx.media_info, "type", None) != MediaType.TV:
            return
        if self._enabled and self._veto_enabled:
            self._veto_low_bitrate(data, ctx)
        if self._enabled and self._wash_enabled and not data.cancel:
            self._record_wash_baseline(data)

    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event = None) -> None:
        """整理入库后按实际文件大小记录该集码率基线（覆盖下载前的估算值）。"""
        if not (self._enabled and self._wash_enabled):
            return
        try:
            self._record_transferred_baseline(getattr(event, "event_data", None))
        except Exception as err:
            logger.warning(f"码率优选：入库实测记录基线失败：{err}")

