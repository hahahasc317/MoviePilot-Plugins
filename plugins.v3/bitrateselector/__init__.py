import inspect
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from app.chain.tmdb import TmdbChain
from app.application.subscription import priority as subscription_priority
from app.plugins import _PluginBase
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


class BitrateSelector(_PluginBase):
    """
    码率优选：在下载执行前对比候选资源对每集的估算码率，自动保留高码率资源。

    码率估算公式：种子体积 × 8 ÷ (覆盖集时长之和 × 60)，单位 Mbps。
    对同一媒体、同一覆盖类别的候选，淘汰被更高码率候选覆盖的低码率候选；
    未标注集数的整季包按全季时长估算后同类比较。
    """

    plugin_name = "码率优选"
    plugin_desc = (
        "下载前按「种子体积 ÷ 每集时长」估算码率，同覆盖范围自动优选高码率资源；"
        "可选拦截低于门槛的低码率下载；支持洗版准入按估算码率优先（码率优先洗版）。"
    )
    plugin_icon = ""
    plugin_version = "1.1.0"
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
        self._baselines = self.get_data(WASH_BASELINES_KEY) or {}
        self._apply_wash_patch()

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
                                "props": {"cols": 12, "md": 6},
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
                                "props": {"cols": 12, "md": 6},
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
                                                "码率=种子体积×8÷每集时长之和。批量下载前，同一媒体内"
                                                "集数覆盖相同（或被超集覆盖）且码率领先超过设定比例的低码率候选会被淘汰；"
                                                "整季包按全季时长估算后同类比较。无法估算（缺集数/缺时长/非TMDB）的候选不参与淘汰。"
                                                "「低码率拦截」作用于每个资源提交下载前，可能造成缺集，默认关闭。"
                                                "「码率优先洗版」开启后，洗版准入改按估算码率与插件记录的每集基线比较"
                                                "（需领先超过设定比例才升级），旧规则优先级基线不再参与洗版；"
                                                "规则组仍负责资源资格筛选。"
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
        无法估算码率时回退原始规则优先级判定。
        """
        if getattr(subscribe, "type", None) != MediaType.TV.value or original is None:
            return original(subscribe, context, priority)
        margin = max(0, int(self._margin_percent)) / 100.0
        if subscription_priority.is_full_best_version_enabled(subscribe):
            candidate_mbps = self._estimate_bitrate(context, set())
            if candidate_mbps is None:
                return original(subscribe, context, priority)
            baseline = self._wash_baseline_entry(subscribe).get("full")
            if baseline is None:
                return True
            if candidate_mbps > float(baseline) * (1 + margin):
                logger.info(
                    f"码率优选：全集洗版准入 {candidate_mbps:.1f}Mbps 超过基线 "
                    f"{float(baseline):.1f}Mbps（需 +{margin:.0%}）"
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
        """
        target_episodes = set(subscription_priority.get_best_version_target_episodes(subscribe))
        if not target_episodes:
            return []
        meta = context.meta_info
        selected_episodes = getattr(context, "selected_episodes", None)
        if selected_episodes is None:
            selected_episodes = meta.episode_list if meta else []
        candidate_mbps = self._estimate_bitrate(
            context, set(selected_episodes) if selected_episodes else set()
        )
        if candidate_mbps is None:
            return None
        episode_baselines = self._wash_baseline_entry(subscribe).get("episodes") or {}
        threshold = candidate_mbps / (1 + margin)
        title = (getattr(context.torrent_info, "title", "") or "")[:60]
        if not selected_episodes:
            interested = sorted(
                episode
                for episode in target_episodes
                if (baseline := episode_baselines.get(str(episode))) is None
                or float(baseline) < threshold
            )
        else:
            interested = []
            for episode in selected_episodes:
                try:
                    episode_num = int(episode)
                except (TypeError, ValueError):
                    continue
                if episode_num not in target_episodes:
                    continue
                baseline = episode_baselines.get(str(episode_num))
                if baseline is None or float(baseline) < threshold:
                    interested.append(episode_num)
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
            return
        bitrate = self._estimate_bitrate(ctx, episodes)
        if bitrate is None:
            return
        entry = self._baselines.setdefault(key, {})
        if episodes:
            episode_map = entry.setdefault("episodes", {})
            for episode in episodes:
                episode_map[str(episode)] = round(bitrate, 2)
        else:
            entry["full"] = round(bitrate, 2)
        self.save_data(WASH_BASELINES_KEY, self._baselines)
        logger.debug(
            f"码率优选：记录洗版码率基线 {key} "
            f"{sorted(episodes) if episodes else '全集'} => {bitrate:.2f}Mbps"
        )

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
        torrent = ctx.torrent_info
        media = ctx.media_info
        if not torrent or not torrent.size or not media:
            return None
        meta = ctx.meta_info
        season = (meta.begin_season or 1) if meta else 1
        runtimes = self._episode_runtimes(media, season)
        if not runtimes:
            return None
        if episodes:
            total_minutes = sum(runtimes.get(ep, 0) for ep in episodes)
        else:
            total_minutes = sum(runtimes.values())
        if total_minutes <= 0:
            return None
        return torrent.size * 8 / (total_minutes * 60) / 1_000_000

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
