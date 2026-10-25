import inspect
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from app.chain.tmdb import TmdbChain
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
# 洗版基线默认的媒体库根目录（STRM 库挂载点）
DEFAULT_LIBRARY_ROOT = "/media"


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
        "可选拦截低码率下载；支持洗版准入按估算码率优先，并可从媒体库播种实测基线。"
    )
    plugin_icon = ""
    plugin_version = "1.2.2"
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
    _baselines: Dict[str, Dict[str, Any]] = {}

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
        self._baselines = self.get_data(WASH_BASELINES_KEY) or {}
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
                                                "播种实测码率，并每天 07:30 自动补充。"
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
            }
        ]

    def stop_service(self) -> None:
        """停止插件后台服务并清理运行时缓存。"""
        self._remove_wash_patch()
        self._runtime_cache = {}
        self._tmdb_chain = None

    def get_service(self) -> List[Dict[str, Any]]:
        """注册每天 07:30 的库内基线播种服务。"""
        if not (self._enabled and self._wash_enabled):
            return []
        return [
            {
                "id": "BitrateSelectorSeedBaselines",
                "name": "码率基线库内播种",
                "trigger": CronTrigger.from_crontab("30 7 * * *"),
                "func": self._safe_seed_baselines,
                "kwargs": {},
            }
        ]

    @eventmanager.register(ChainEventType.ResourceSelection)
    def select_high_bitrate(self, event: Event) -> None:
        """批量下载前淘汰同覆盖范围内被高码率候选支配的低码率候选。"""
        if not self._enabled:
            return
        data = event.event_data
        if not isinstance(data, ResourceSelectionEventData) or not data.contexts:
            return
        try:
            kept, dropped = self._drop_dominated_candidates(list(data.contexts))
        except Exception as err:
            logger.error(f"码率优选：处理候选列表失败：{err}")
            return
        if not dropped:
            return
        data.updated = True
        data.updated_contexts = kept
        data.source = "码率优选"
        logger.info(f"码率优选：淘汰 {len(dropped)} 个低码率候选。{'；'.join(dropped)}")
        if self._notify:
            self.post_message(
                mtype=None,
                title="码率优选",
                text=f"已淘汰 {len(dropped)} 个低码率候选：\n" + "\n".join(dropped[:5]),
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
                text=f"{ctx.torrent_info.title}\n{data.reason}",
            )

    def _apply_wash_patch(self) -> None:
        """按配置安装或移除洗版准入的码率优先补丁。"""
        if not (self._enabled and self._wash_enabled):
            self._remove_wash_patch()
            return
        module = subscription_priority
        original = getattr(module, "prepare_best_version_tv_candidate", None)
        if original is None:
            logger.error("码率优选：未找到洗版准入函数，码率优先洗版未生效")
            return
        try:
            params = list(inspect.signature(original).parameters)
        except (TypeError, ValueError):
            params = []
        if params != ["subscribe", "context", "priority"]:
            logger.error(
                f"码率优选：洗版准入函数签名变化（{params}），"
                f"码率优先洗版未生效，请检查 MoviePilot 版本兼容性"
            )
            return
        if not getattr(module, "_bitrateselector_original_prepare", None):
            module._bitrateselector_original_prepare = original
        module.prepare_best_version_tv_candidate = self._make_patched_prepare(module)
        module._bitrateselector_patched_by = self.__class__.__name__
        logger.info("码率优选：洗版准入已切换为估算码率优先，规则优先级基线不再参与洗版判定")

    def _remove_wash_patch(self) -> None:
        """移除洗版准入补丁并还原原始判定函数。"""
        module = subscription_priority
        original = getattr(module, "_bitrateselector_original_prepare", None)
        if original is None:
            return
        module.prepare_best_version_tv_candidate = original
        del module._bitrateselector_original_prepare
        if hasattr(module, "_bitrateselector_patched_by"):
            del module._bitrateselector_patched_by
        logger.info("码率优选：洗版准入补丁已移除，恢复规则优先级判定")

    def _make_patched_prepare(self, module: Any):
        """构造替换洗版准入函数的闭包，判定异常时回退原始规则优先级。"""

        def patched(subscribe: Any, context: Any, priority: int) -> bool:
            """按估算码率执行洗版准入，异常时回退原始判定。"""
            original = getattr(module, "_bitrateselector_original_prepare", None)
            try:
                return self._wash_admit(subscribe, context, priority, original)
            except Exception as err:
                logger.warning(f"码率优选：码率优先洗版判定异常，本次回退规则优先级：{err}")
                if original is not None:
                    return original(subscribe, context, priority)
                return False

        return patched

    def _wash_admit(
        self, subscribe: Any, context: Any, priority: int, original: Any
    ) -> bool:
        """
        按估算码率执行洗版准入判定。

        全集模式对比整季基线；分集模式按每集基线计算仍可提升的剧集范围。
        无法估算码率时回退原始规则优先级判定；基线缺失时保守处理：
        全集模式回退原始判定，分集模式跳过缺基线的集，防止低码候选盲目洗版。
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
            if candidate_mbps > float(baseline) * (1 + effective_margin):
                logger.info(
                    f"码率优选：全集洗版准入 {candidate_mbps:.1f}Mbps 超过基线 "
                    f"{float(baseline):.1f}Mbps（需 +{effective_margin:.0%}，"
                    f"时长来源 候选={candidate_source}/基线={baseline_source}）"
                )
                return True
            logger.debug(
                f"码率优选：全集洗版候选 {candidate_mbps:.1f}Mbps 未超基线 "
                f"{float(baseline):.1f}Mbps（需 +{margin:.0%}），拒绝"
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
        无基线记录的集不参与码率优先洗版，待库内播种补齐基线后再比较。
        """
        target_episodes = set(subscription_priority.get_best_version_target_episodes(subscribe))
        if not target_episodes:
            return []
        meta = context.meta_info
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

        def _threshold_for(baseline_source: str) -> float:
            """按候选与基线的时长来源是否一致计算该集的比较阈值。"""
            effective = margin * 2 if candidate_source != baseline_source else margin
            return candidate_mbps / (1 + effective)

        title = (getattr(context.torrent_info, "title", "") or "")[:60]
        if not selected_episodes:
            missing = [
                episode
                for episode in target_episodes
                if episode_baselines.get(str(episode)) is None
            ]
            if missing:
                logger.debug(
                    f"码率优选：{len(missing)} 集无码率基线，本轮不参与码率优先洗版"
                )
            interested = sorted(
                episode
                for episode in target_episodes
                if (baseline := episode_baselines.get(str(episode))) is not None
                and float(baseline)
                < _threshold_for(str(baseline_sources.get(str(episode), "measured")))
            )
        else:
            interested = []
            missing = 0
            for episode in selected_episodes:
                try:
                    episode_num = int(episode)
                except (TypeError, ValueError):
                    continue
                if episode_num not in target_episodes:
                    continue
                baseline = episode_baselines.get(str(episode_num))
                if baseline is None:
                    missing += 1
                    continue
                if float(baseline) < _threshold_for(
                    str(baseline_sources.get(str(episode_num), "measured"))
                ):
                    interested.append(episode_num)
            if missing:
                logger.debug(
                    f"码率优选：{missing} 集无码率基线，本轮不参与码率优先洗版"
                )
            interested = sorted(set(interested))
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
        return interested

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
        """下载提交后按覆盖剧集记录估算码率基线。"""
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
            entry = self._baselines.setdefault(key, {})
            if episodes:
                episode_map = entry.setdefault("episodes", {})
                source_map = entry.setdefault("ep_sources", {})
                for episode in episodes:
                    episode_map[str(episode)] = round(bitrate, 2)
                    source_map[str(episode)] = duration_source
            else:
                entry["full"] = round(bitrate, 2)
                entry["full_source"] = duration_source
            self.save_data(WASH_BASELINES_KEY, self._baselines)
        except Exception as err:
            logger.error(f"码率优选：保存码率基线失败：{err}")
            return
        logger.info(
            f"码率优选：记录洗版码率基线 {key} "
            f"{sorted(episodes) if episodes else '全集'} => {bitrate:.2f}Mbps"
            f"（时长来源={duration_source}）"
        )

    def _safe_seed_baselines(self) -> Dict[str, Any]:
        """执行库内播种并吞掉异常，返回结果摘要。"""
        try:
            return self.seed_baselines_from_library()
        except Exception as err:
            logger.error(f"码率优选：库内基线播种失败：{err}")
            return {"success": False, "seeded": 0, "message": str(err)}

    def seed_baselines_from_library(self) -> Dict[str, Any]:
        """
        扫描媒体库 mediainfo，为尚无基线的剧集填充实测码率。

        只补空缺、不覆盖下载时记录的基线；已下载集以事件记录为准。
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
        matched = 0
        for subscribe in subscriptions:
            seeded = self._seed_subscription_baseline(subscribe, root)
            if seeded < 0:
                continue
            matched += 1
            changed += seeded
        if changed:
            self.save_data(WASH_BASELINES_KEY, self._baselines)
        logger.info(
            f"码率优选：库内基线播种完成，补种 {changed} 集（匹配 {matched}/{len(subscriptions)} 个订阅）"
        )
        return {
            "success": True,
            "seeded": changed,
            "matched_subscriptions": matched,
            "total_subscriptions": len(subscriptions),
        }

    def _seed_subscription_baseline(self, subscribe: Any, root: Path) -> int:
        """
        为单个洗版订阅按库内 mediainfo 播种缺失的每集基线。

        :return: 补种集数；库内未找到对应目录时返回 -1
        """
        name = str(getattr(subscribe, "name", "") or "")
        year = getattr(subscribe, "year", None)
        season = getattr(subscribe, "season", None) or 1
        show_dir = self._find_show_dir(root, name, year)
        if show_dir is None:
            return -1
        season_dir = self._find_season_dir(show_dir, season)
        if season_dir is None:
            return -1
        key = self._wash_media_key(
            source=getattr(subscribe, "media_source", None),
            media_id=getattr(subscribe, "media_id", None),
            season=season,
        )
        if not key:
            return -1
        entry = self._baselines.setdefault(key, {})
        episode_map = entry.setdefault("episodes", {})
        seeded = 0
        for info_path in sorted(season_dir.glob("*-mediainfo.json")):
            episode_number = self._episode_from_filename(info_path.name)
            if not episode_number:
                continue
            if episode_map.get(str(episode_number)) is not None:
                continue
            bitrate = self._mediainfo_bitrate_mbps(info_path)
            if bitrate is None:
                continue
            episode_map[str(episode_number)] = bitrate
            seeded += 1
        if seeded:
            logger.info(
                f"码率优选：库内播种 {key} 补种 {seeded} 集实测基线（{show_dir.name}）"
            )
        return seeded

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

    def _drop_dominated_candidates(
        self, contexts: List[Any]
    ) -> Tuple[List[Any], List[str]]:
        """
        按媒体身份与覆盖类别分组，淘汰被高码率候选覆盖的低码率候选。

        :return: (保留的候选列表, 被淘汰候选的描述列表)
        """
        groups: Dict[str, List[Tuple[int, Set[int], float, str]]] = {}
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
            groups.setdefault(f"{group_key}|{coverage}", []).append(
                (idx, episodes, bitrate, title)
            )
        drop: Dict[int, str] = {}
        margin = max(0, int(self._margin_percent)) / 100.0
        for items in groups.values():
            for idx, eps, bitrate, title in items:
                if idx in drop:
                    continue
                for jdx, eps2, bitrate2, title2 in items:
                    if jdx == idx or jdx in drop:
                        continue
                    if not self._covers(eps2, eps):
                        continue
                    if bitrate2 >= bitrate * (1 + margin):
                        drop[idx] = (
                            f"{title}（{bitrate:.1f}Mbps）← 保留 {title2}（{bitrate2:.1f}Mbps）"
                        )
                        break
        if not drop:
            return contexts, []
        kept = [c for i, c in enumerate(contexts) if i not in drop]
        return kept, list(drop.values())

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
