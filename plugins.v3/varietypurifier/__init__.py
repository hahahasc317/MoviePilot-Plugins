import json
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from app.plugins import _PluginBase
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.schemas.types import ChainEventType, EventType, MediaType

from . import purifier
from .purifier import DEFAULT_KEYWORDS, decide, is_subscribe_source

DATA_SOURCES_KEY = "torrent_sources"
DATA_BLOCKS_KEY = "recent_blocks"
SOURCE_TTL_DAYS = 30
NOTIFY_DEBOUNCE_SECONDS = 30
BLOCKS_CAP = 50


class VarietyPurifier(_PluginBase):
    """综艺正片净化：整理阶段拦截自动下载综艺中的加更/花絮等非正片文件，手动下载放行。"""

    plugin_name = "综艺正片净化"
    plugin_desc = (
        "自动下载的综艺在整理入库时拦截加更/花絮/纯享/先导等非正片文件及 Part 半集，"
        "防止占号污染媒体库；手动下载放行。文件保留在下载目录不删除，不影响做种。"
    )
    plugin_icon = ""
    plugin_version = "1.0.0"
    plugin_author = "hahahasc317"
    plugin_config_prefix = "varietypurifier_"
    plugin_order = 16
    auth_level = 1

    _enabled = False
    _scope_variety_only = True
    _block_parts = True
    _block_special_ep = True
    _notify = True
    _exempt_manual = True
    _keywords: Tuple[str, ...] = DEFAULT_KEYWORDS

    _torrent_sources: Dict[str, Dict[str, Any]] = {}
    _notify_buffer: List[str] = []
    _notify_timer: Optional[threading.Timer] = None
    _notify_lock = threading.Lock()

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态，并回填近期下载来源。"""
        self._notify_buffer = []
        if self._notify_timer is not None:
            self._notify_timer.cancel()
            self._notify_timer = None
        if not config:
            return
        self._enabled = bool(config.get("enabled") or False)
        self._scope_variety_only = bool(config.get("scope_variety_only", True))
        self._block_parts = bool(config.get("block_parts", True))
        self._block_special_ep = bool(config.get("block_special_ep", True))
        self._notify = bool(config.get("notify", True))
        self._exempt_manual = bool(config.get("exempt_manual", True))
        raw_keywords = str(config.get("keywords") or "").strip()
        if raw_keywords:
            self._keywords = tuple(
                kw.strip() for kw in re_split_keywords(raw_keywords) if kw.strip()
            )
        else:
            self._keywords = DEFAULT_KEYWORDS
        self._torrent_sources = self.get_data(DATA_SOURCES_KEY) or {}
        self._prune_sources()
        if self._enabled:
            self._seed_sources_from_history()
        logger.info(
            f"综艺正片净化：已{'启用' if self._enabled else '停用'}，"
            f"仅综艺={self._scope_variety_only}，拦Part={self._block_parts}，"
            f"拦E00={self._block_special_ep}，手动放行={self._exempt_manual}，"
            f"关键词 {len(self._keywords)} 个，来源映射 {len(self._torrent_sources)} 条"
        )

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

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """返回插件配置表单与默认配置。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "scope_variety_only",
                                            "label": "仅作用于综艺分类（关闭则全部电视剧生效）",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "exempt_manual",
                                            "label": "手动下载放行（仅拦截订阅来源）",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "block_parts",
                                            "label": "拦截 Part 分半文件（QHstudIo/PTerWEB 等）",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "block_special_ep",
                                            "label": "拦截 E00 特殊集号",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "notify", "label": "拦截时发送通知"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "keywords",
                                            "label": "拦截关键词（逗号或换行分隔，命中文件名即拦）",
                                            "rows": 3,
                                            "placeholder": "，".join(DEFAULT_KEYWORDS[:8]) + "…",
                                        },
                                    }
                                ],
                            },
                        ],
                    }
                ],
            }
        ], {
            "enabled": False,
            "scope_variety_only": True,
            "exempt_manual": True,
            "block_parts": True,
            "block_special_ep": True,
            "notify": True,
            "keywords": "，".join(DEFAULT_KEYWORDS),
        }

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页面（最近拦截记录）。"""
        blocks = self.get_data(DATA_BLOCKS_KEY) or []
        if not blocks:
            return [
                {
                    "component": "VAlert",
                    "props": {"type": "info", "text": "尚无拦截记录。"},
                }
            ]
        lines = [
            f"{item.get('time', '-')}　{item.get('reason', '-')}\n　{item.get('file', '-')}"
            for item in blocks[:20]
        ]
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "title": f"最近拦截 {len(blocks)} 条（显示前 20 条）",
                    "text": "\n".join(lines),
                },
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """无定时服务，拦截在整理事件内实时完成。"""
        return []

    def stop_service(self) -> None:
        """停止插件后台服务并释放定时器。"""
        if self._notify_timer is not None:
            self._notify_timer.cancel()
            self._notify_timer = None
        self._flush_notify()

    @eventmanager.register(EventType.DownloadAdded)
    def on_download_added(self, event: Event = None) -> None:
        """记录每个下载任务的来源类型（订阅/手动），供整理拦截时判别。"""
        try:
            data = getattr(event, "event_data", None)
            if data is None:
                return
            if isinstance(data, dict):
                source = data.get("source")
                context = data.get("context")
            else:
                source = getattr(data, "source", None)
                context = getattr(data, "context", None)
            title = self._extract_torrent_title(context)
            if not title:
                return
            self._torrent_sources[title] = {
                "manual": not is_subscribe_source(source),
                "ts": time.time(),
            }
            self._prune_sources()
            self.save_data(DATA_SOURCES_KEY, self._torrent_sources)
        except Exception as err:
            logger.error(f"综艺正片净化：记录下载来源失败：{err}")

    @eventmanager.register(ChainEventType.TransferIntercept)
    def on_transfer_intercept(self, event: Event = None) -> None:
        """整理写入前逐文件裁决：命中非正片特征的自动下载综艺文件予以取消。"""
        if not self._enabled:
            return
        try:
            data = getattr(event, "event_data", None)
            if data is None or getattr(data, "cancel", False):
                return
            fileitem = getattr(data, "fileitem", None)
            mediainfo = getattr(data, "mediainfo", None)
            meta = getattr(data, "meta", None)
            if fileitem is None or mediainfo is None or meta is None:
                # 目录事件或上下文不全，不裁决
                return
            path = getattr(fileitem, "path", "") or ""
            filename = getattr(fileitem, "name", "") or path.rsplit("/", 1)[-1]
            media_type = self._media_type_str(mediainfo)
            category = str(
                getattr(mediainfo, "library_category", "")
                or getattr(mediainfo, "category", "")
                or ""
            )
            result = decide(
                filename=filename,
                org_string=str(getattr(meta, "org_string", "") or ""),
                media_type=media_type,
                library_category=category,
                begin_episode=getattr(meta, "begin_episode", None),
                part=str(getattr(meta, "part", "") or ""),
                keywords=self._keywords,
                scope_variety_only=self._scope_variety_only,
                block_parts=self._block_parts,
                block_special_ep=self._block_special_ep,
                is_manual_source=self._is_manual_torrent(path),
                exempt_manual=self._exempt_manual,
            )
            if result == "allow":
                return
            reason = {
                "blocked:e00": "E00 特殊集号",
                "blocked:part": "Part 分半文件",
            }.get(result, f"命中关键词 {result.split(':', 1)[-1]}")
            data.cancel = True
            data.source = "综艺正片净化"
            data.reason = reason
            logger.info(f"综艺正片净化：已拦截 {filename}（{reason}）")
            self._record_block(filename, reason)
        except Exception as err:
            logger.error(f"综艺正片净化：整理拦截处理失败：{err}")

    def _media_type_str(self, mediainfo: Any) -> str:
        """把媒体信息对象的类型字段规范化为字符串。"""
        mt = getattr(mediainfo, "type", None)
        if isinstance(mt, MediaType):
            return str(mt.value)
        if mt is None:
            return ""
        return str(mt)

    def _is_manual_torrent(self, path: str) -> bool:
        """判别文件所属种子是否手动来源：优先事件映射，退化查下载历史。"""
        directory = self._torrent_dir(path)
        if directory:
            entry = self._torrent_sources.get(directory)
            if entry is not None:
                return bool(entry.get("manual"))
        history_manual = self._lookup_source_from_history(path)
        if history_manual is not None:
            return history_manual
        # 未知来源按订阅处理（过滤生效是安全方向）
        return False

    def _lookup_source_from_history(self, path: str) -> Optional[bool]:
        """按路径查下载历史 note.source 判别来源；异常返回 None。"""
        try:
            import sys as _sys

            from sqlalchemy import create_engine, text

            if "/app" not in _sys.path:
                _sys.path.insert(0, "/app")
            from app.runtime.config import settings  # pylint: disable=import-outside-toplevel

            if str(getattr(settings, "DB_TYPE", "")).lower() == "postgresql":
                engine = create_engine(settings.DB_POSTGRESQL_URL())
            else:
                engine = create_engine(f"sqlite:///{getattr(settings, 'CONFIG_PATH')}/user.db")
            try:
                with engine.connect() as conn:
                    rows = conn.execute(
                        text(
                            "select note from downloadhistory "
                            "where path like :pat order by id desc limit 3"
                        ),
                        {"pat": f"%{self._torrent_dir(path) or path}%"},
                    ).fetchall()
            finally:
                engine.dispose()
            for (note,) in rows:
                source = None
                try:
                    payload = json.loads(str(note or "{}"))
                    if isinstance(payload, dict):
                        source = payload.get("source")
                except Exception:
                    source = None
                if source:
                    return not is_subscribe_source(str(source))
            return None
        except Exception:
            return None

    def _seed_sources_from_history(self) -> None:
        """启动时回填近 14 天下载历史中的来源映射，覆盖插件安装前的存量任务。"""
        try:
            import sys as _sys
            from datetime import datetime, timedelta

            from sqlalchemy import create_engine, text

            if "/app" not in _sys.path:
                _sys.path.insert(0, "/app")
            from app.runtime.config import settings  # pylint: disable=import-outside-toplevel

            since = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
            if str(getattr(settings, "DB_TYPE", "")).lower() == "postgresql":
                engine = create_engine(settings.DB_POSTGRESQL_URL())
            else:
                engine = create_engine(f"sqlite:///{getattr(settings, 'CONFIG_PATH')}/user.db")
            try:
                with engine.connect() as conn:
                    rows = conn.execute(
                        text(
                            "select path, torrent_name, note from downloadhistory "
                            "where date >= :since order by id desc limit 200"
                        ),
                        {"since": since},
                    ).fetchall()
            finally:
                engine.dispose()
            added = 0
            for path, torrent_name, note in rows:
                title = str(torrent_name or "").strip() or self._torrent_dir(str(path or ""))
                if not title or title in self._torrent_sources:
                    continue
                source = None
                try:
                    payload = json.loads(str(note or "{}"))
                    if isinstance(payload, dict):
                        source = payload.get("source")
                except Exception:
                    source = None
                self._torrent_sources[title] = {
                    "manual": not is_subscribe_source(source if source else ""),
                    "ts": time.time(),
                }
                added += 1
            if added:
                self.save_data(DATA_SOURCES_KEY, self._torrent_sources)
                logger.info(f"综艺正片净化：从下载历史回填 {added} 条来源映射")
        except Exception as err:
            logger.warning(f"综艺正片净化：回填下载来源失败（忽略）：{err}")

    @staticmethod
    def _extract_torrent_title(context: Any) -> str:
        """从下载事件上下文中尽力提取种子标题。"""
        if context is None:
            return ""
        torrent_info = getattr(context, "torrent_info", None)
        if torrent_info is None and isinstance(context, dict):
            torrent_info = context.get("torrent_info")
        title = getattr(torrent_info, "title", None)
        if title is None and isinstance(torrent_info, dict):
            title = torrent_info.get("title")
        return str(title or "").strip()

    @staticmethod
    def _torrent_dir(path: str) -> str:
        """从 /downloads/<种子目录>/<文件> 路径中提取种子目录名。"""
        parts = str(path or "").split("/")
        if len(parts) >= 4 and parts[1] == "downloads":
            return parts[2]
        return ""

    def _prune_sources(self) -> None:
        """清理超过 TTL 的来源映射。"""
        deadline = time.time() - SOURCE_TTL_DAYS * 86400
        stale = [k for k, v in self._torrent_sources.items() if v.get("ts", 0) < deadline]
        for key in stale:
            self._torrent_sources.pop(key, None)

    def _record_block(self, filename: str, reason: str) -> None:
        """记录拦截明细，聚合推送通知。"""
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        blocks = self.get_data(DATA_BLOCKS_KEY) or []
        blocks.insert(0, {"time": now, "file": filename, "reason": reason})
        self.save_data(DATA_BLOCKS_KEY, blocks[:BLOCKS_CAP])
        if not self._notify:
            return
        with self._notify_lock:
            self._notify_buffer.append(f"{filename}（{reason}）")
            if self._notify_timer is None:
                self._notify_timer = threading.Timer(
                    NOTIFY_DEBOUNCE_SECONDS, self._flush_notify
                )
                self._notify_timer.daemon = True
                self._notify_timer.start()

    def _flush_notify(self) -> None:
        """把缓冲的拦截记录合并为一条通知发送。"""
        with self._notify_lock:
            if not self._notify_buffer:
                return
            lines = self._notify_buffer[:10]
            more = len(self._notify_buffer) - len(lines)
            self._notify_buffer = []
            if self._notify_timer is not None:
                self._notify_timer.cancel()
                self._notify_timer = None
        text = "已拦截非正片文件（保留在下载目录）：\n" + "\n".join(lines)
        if more > 0:
            text += f"\n…等共 {more + len(lines)} 项"
        self.post_message(mtype=None, title="综艺正片净化", text=text)


def re_split_keywords(raw: str) -> List[str]:
    """按逗号、换行、中文逗号拆分关键词。"""
    import re as _re

    return [part for part in _re.split(r"[,，\n]+", raw)]
