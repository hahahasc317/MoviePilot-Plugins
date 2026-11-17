import inspect
import json
import re
import sys
import threading
import time
import urllib.request
from pathlib import Path
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
from app.schemas.types import ChainEventType, MediaType

# 单集时长缓存的存活时间（秒）
RUNTIME_CACHE_TTL_SECONDS = 6 * 3600
# 洗版码率基线在插件数据中的存储键
WASH_BASELINES_KEY = "wash_baselines"
# 洗版基线与库内实测/既有值之间的最大合理倍差，超出视为异常需校正或拒绝记录
WASH_BASELINE_MAX_DIVERGENCE = 2.0
# 基线被拒绝覆盖期间的准入垫高下限存活时间（秒），超时失效防错误估算长期锁死洗版
WASH_FLOOR_TTL_SECONDS = 48 * 3600
# 洗版基线默认的媒体库根目录（STRM 库挂载点）
DEFAULT_LIBRARY_ROOT = "/media"
# 低码巡检触发订阅搜索的节流时间（秒）
INSPECT_THROTTLE_SECONDS = 24 * 3600
# 低码巡检触发记录在插件数据中的存储键
INSPECT_TRIGGERS_KEY = "inspect_triggers"
# 低码巡检降级记录在插件数据中的存储键：{sid: {集号: {"ts": 降级时间, "cooldown": 冷却截止}}}
INSPECT_DEMOTIONS_KEY = "inspect_demotions"
# 核心「已完成洗版」的分集优先级（规则满分）与巡检降级后的待洗优先级
FULL_EPISODE_PRIORITY = 100
DEMOTED_EPISODE_PRIORITY = 99
# 单轮巡检最多降级的集数，防止一次性放出大量洗版搜索
INSPECT_MAX_DEMOTE_PER_ROUND = 20
# 降级后未洗到更高码版本的恢复期限，以及恢复后同集的冷却期
INSPECT_DEMOTE_TTL_SECONDS = 48 * 3600
INSPECT_DEMOTE_COOLDOWN_SECONDS = 7 * 86400
# 整包码率放行标记的存活时间（准入判定与下载提交在同一轮搜索内完成）
WASH_MARK_TTL_SECONDS = 1800
# 运行时补丁目标：(函数名, 期望参数列表)。MoviePilot 升级后签名一致即自动重新替换，
# 不一致则该项功能自动停用并通知，绝不带病运行
PATCH_PREPARE = "prepare_best_version_tv_candidate"
PATCH_PACK_GATE = "should_prefer_full_pack_for_episode_best_version"
PATCH_TARGETS = {
    PATCH_PREPARE: ["subscribe", "context", "priority"],
    PATCH_PACK_GATE: ["subscribe", "priority"],
}
# 选种淘汰通知去重记录的存储键与去重时长
SELECTION_NOTICE_KEY = "selection_notices"
SELECTION_NOTICE_TTL_SECONDS = 24 * 3600
# 补丁不兼容告警去重记录的存储键
PATCH_ALERT_KEY = "patch_alert"


class BitrateSelector(_PluginBase):
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
    plugin_version = "1.4.3"
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
    # 整包码率放行标记：{id(context): (时间, 种子标题, 可洗集合)}
    _wash_marks: Dict[int, Tuple[float, str, Set[int]]] = {}
    # 各补丁当前状态：{函数名: "active" / "incompatible:<原因>" / "missing"}
    _patch_status: Dict[str, str] = {}

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        """根据插件配置初始化运行开关与参数。"""
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
        return []

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
        effect = BitrateSelector._dedupe_effect_tokens(
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
        bitrate, duration_source = self._estimate_bitrate_ex(ctx, episodes)
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
            episodes_text = BitrateSelector._compress_episode_ranges(episode_numbers)
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
                reason = "已在冷却期或缺基线" if skipped else "无可洗集"
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
        seeded = 0
        corrected = 0
        for info_path in sorted(season_dir.glob("*-mediainfo.json")):
            episode_number = self._episode_from_filename(info_path.name)
            if not episode_number:
                continue
            bitrate = self._mediainfo_bitrate_mbps(info_path)
            if bitrate is None:
                continue
            existing = episode_map.get(str(episode_number))
            if existing is None:
                episode_map[str(episode_number)] = bitrate
                floor_map.pop(str(episode_number), None)
                seeded += 1
            elif self._baseline_diverged(existing, bitrate):
                episode_map[str(episode_number)] = bitrate
                floor_map.pop(str(episode_number), None)
                corrected += 1
            else:
                floor_map.pop(str(episode_number), None)
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
        """读取 P115StrmHelper mediainfo 中视频流的实测码率（Mbps）。"""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            source = data[0]["MediaSourceInfo"]
            video = next(
                stream
                for stream in source.get("MediaStreams", [])
                if stream.get("Type") == "Video"
            )
            bitrate = int(video.get("BitRate") or 0)
            if not bitrate:
                size = int(source.get("Size") or 0)
                ticks = float(source.get("RunTimeTicks") or 0)
                if size and ticks:
                    bitrate = int(size * 8 / (ticks / 10_000_000))
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

    @staticmethod
    def _mediainfo_runtime_seconds(path: Path) -> Optional[float]:
        """读取 mediainfo 中媒体运行时长（秒），用于按实际文件大小估算码率。"""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            ticks = float(data[0]["MediaSourceInfo"].get("RunTimeTicks") or 0)
            return ticks / 10_000_000 if ticks > 0 else None
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            return None

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
        margin = max(0, int(self._margin_percent)) / 100.0
        for items in groups.values():
            for idx, eps, bitrate, title, media_label, version_label in items:
                if idx in drop:
                    continue
                for jdx, eps2, bitrate2, title2, label2, version2 in items:
                    if jdx == idx or jdx in drop:
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
        minutes_map, source = self._resolve_episode_minutes(media, season, episodes)
        if not minutes_map:
            return None, ""
        total_minutes = sum(minutes_map.values())
        if total_minutes <= 0:
            return None, ""
        return torrent.size * 8 / (total_minutes * 60) / 1_000_000, source

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
