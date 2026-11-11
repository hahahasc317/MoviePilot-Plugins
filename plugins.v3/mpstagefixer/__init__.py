import hashlib
import threading
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger
from app.plugins import _PluginBase
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.scheduler import add_plugin_once_job
from app.schemas.types import EventType

DEFAULT_CRON = "15 */6 * * *"
REPORT_KEY = "last_report"
REVIEW_KEY = "last_review_report"
DEFAULT_REVIEW_CRON = "*/30 * * * *"
QUICK_DELAY_SECONDS = 120
RUN_LOCK = threading.Lock()
REVIEW_LOCK = threading.Lock()


class MPStageFixer(_PluginBase):
    """MP 暂存残留修复：自动检测并重建 115 网盘上的 mp-stage 暂存对象。"""

    plugin_name = "MP暂存残留修复"
    plugin_desc = (
        "自动检测并修复 115 网盘上的 mp-stage 暂存残留（CD2 带**尾巴 / Plus 丢扩展名变体），"
        "SHA1 秒传重建恢复 strm 缺集；整理完成后即时定向检测 + 定时全量扫描兜底；"
        "定时核验 durable 整理待复核任务，仅对目标可证实的步骤自动判定 applied。"
    )
    plugin_icon = ""
    plugin_version = "1.1.0"
    plugin_author = "hahahasc317"
    plugin_config_prefix = "mpstagefixer_"
    plugin_order = 15
    auth_level = 1

    _enabled = False
    _quick_check = True
    _notify = True
    _orphan_clean = True
    _sweep_cron = DEFAULT_CRON
    _max_dirs = 3000
    _review_enabled = True
    _review_cron = DEFAULT_REVIEW_CRON

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。"""
        if not config:
            return
        self._enabled = bool(config.get("enabled") or False)
        self._quick_check = bool(config.get("quick_check", True))
        self._notify = bool(config.get("notify", True))
        self._orphan_clean = bool(config.get("orphan_clean", True))
        cron_text = str(config.get("sweep_cron") or "").strip()
        self._sweep_cron = cron_text or DEFAULT_CRON
        try:
            self._max_dirs = max(50, int(config.get("max_dirs") or 3000))
        except (TypeError, ValueError):
            self._max_dirs = 3000
        self._review_enabled = bool(config.get("review_enabled", True))
        review_cron = str(config.get("review_cron") or "").strip()
        self._review_cron = review_cron or DEFAULT_REVIEW_CRON
        logger.info(
            f"MPStageFixer：已{'启用' if self._enabled else '停用'}，"
            f"即时检测={self._quick_check}，扫描周期={self._sweep_cron}，"
            f"待复核自动判定={'开' if self._review_enabled else '关'}"
            f"（{self._review_cron}）"
        )

    def get_state(self) -> bool:
        """获取插件启用状态。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回插件远程命令列表。"""
        return [
            {
                "cmd": "/mpstage_fix",
                "event": EventType.PluginAction,
                "desc": "立即扫描修复mp-stage残留",
                "category": "",
                "data": {"action": "mpstage_fix"},
            },
            {
                "cmd": "/mpstage_review",
                "event": EventType.PluginAction,
                "desc": "立即核验判定整理待复核任务",
                "category": "",
                "data": {"action": "mpstage_review"},
            },
        ]

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
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
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
                                            "model": "quick_check",
                                            "label": "整理完成后即时定向检测（约2分钟后复查目标目录）",
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
                                            "model": "notify",
                                            "label": "修复结果发送通知",
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
                                            "model": "orphan_clean",
                                            "label": "顺带清理孤儿边车（超24小时）",
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
                                            "model": "sweep_cron",
                                            "label": "全量扫描周期（cron 表达式）",
                                            "placeholder": DEFAULT_CRON,
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
                                            "model": "max_dirs",
                                            "label": "单次全量扫描目录上限",
                                            "type": "number",
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
                                            "model": "review_enabled",
                                            "label": "待复核任务自动判定（仅目标核验一致才判 applied）",
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
                                            "model": "review_cron",
                                            "label": "待复核核验周期（cron 表达式）",
                                            "placeholder": DEFAULT_REVIEW_CRON,
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
            "quick_check": True,
            "notify": True,
            "orphan_clean": True,
            "sweep_cron": DEFAULT_CRON,
            "max_dirs": 3000,
            "review_enabled": True,
            "review_cron": DEFAULT_REVIEW_CRON,
        }

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页面（最近扫描修复与待复核判定报告）。"""
        content = self._scan_page_content()
        content.extend(self._review_page_content())
        return content

    def _scan_page_content(self) -> List[dict]:
        """构造最近一次扫描修复报告内容。"""
        report = self.get_data(REPORT_KEY) or {}
        if not report:
            return [
                {
                    "component": "VAlert",
                    "props": {"type": "info", "text": "尚未运行过扫描。"},
                }
            ]
        lines = [
            f"时间：{report.get('time', '-')}",
            f"来源：{report.get('source', '-')}",
            f"扫描目录：{report.get('visited', '-')}　残留：{report.get('findings', '-')}",
            f"重建：{report.get('fixed', 0)}　清理重复：{report.get('dedup', 0)}"
            f"　失败：{report.get('failed', 0)}　需人工：{report.get('manual', 0)}",
            f"孤儿边车：删除 {report.get('orphan_deleted', 0)} / 防护拦截 {report.get('orphan_guard', 0)}",
        ]
        details = report.get("details") or []
        content = [
            {
                "component": "VAlert",
                "props": {"type": "info", "title": "最近一次扫描修复报告", "text": "\n".join(lines)},
            }
        ]
        if details:
            content.append(
                {
                    "component": "VTextarea",
                    "props": {
                        "model": None,
                        "label": "明细",
                        "readonly": True,
                        "rows": 8,
                        "modelValue": "\n".join(details[:30]),
                    },
                }
            )
        return content

    def _review_page_content(self) -> List[dict]:
        """构造最近一次待复核判定报告内容。"""
        report = self.get_data(REVIEW_KEY) or {}
        if not report:
            return []
        lines = [
            f"时间：{report.get('time', '-')}　来源：{report.get('source', '-')}",
            f"待复核：{report.get('scanned', 0)}　自动判定：{report.get('resolved', 0)}"
            f"　跳过：{report.get('skipped', 0)}　冲突：{report.get('conflict', 0)}",
        ]
        if report.get("rate_limited"):
            lines.append("上轮存储查询被限流，已中止等待下轮")
        details = report.get("details") or []
        if details:
            lines.append("　".join(details[:6]))
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "title": "最近一次待复核判定报告",
                    "text": "\n".join(lines),
                },
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """注册 mp-stage 残留定时扫描与待复核自动判定服务。"""
        if not self._enabled:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._sweep_cron)
        except ValueError:
            logger.warning(f"MPStageFixer：cron 表达式无效（{self._sweep_cron}），回退默认 {DEFAULT_CRON}")
            trigger = CronTrigger.from_crontab(DEFAULT_CRON)
        return [
            {
                "id": "MPStageFixerSweep",
                "name": "mp-stage 残留定时扫描修复",
                "trigger": trigger,
                "func": self._safe_sweep,
                "kwargs": {},
            },
        ]
        if self._review_enabled:
            try:
                review_trigger = CronTrigger.from_crontab(self._review_cron)
            except ValueError:
                logger.warning(
                    f"MPStageFixer：待复核 cron 表达式无效（{self._review_cron}），"
                    f"回退默认 {DEFAULT_REVIEW_CRON}"
                )
                review_trigger = CronTrigger.from_crontab(DEFAULT_REVIEW_CRON)
            services.append(
                {
                    "id": "MPStageFixerReview",
                    "name": "整理待复核任务自动判定",
                    "trigger": review_trigger,
                    "func": self._safe_review,
                    "kwargs": {},
                }
            )
        return services

    def stop_service(self) -> None:
        """停止插件后台服务并释放资源。"""
        return None

    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event = None) -> None:
        """整理入库完成后延迟定向复查目标目录，尽快修复暂存残留。"""
        if not self._enabled or not self._quick_check:
            return
        data = getattr(event, "event_data", None)
        info = getattr(data, "transferinfo", None) if data is not None else None
        if info is None or not getattr(info, "success", False):
            return
        diritem = getattr(info, "target_diritem", None)
        path = getattr(diritem, "path", None) if diritem is not None else None
        if not path:
            return
        dir_path = self._normalize_115_dir(str(path))
        if not dir_path:
            return
        try:
            job_id = "quick-" + hashlib.md5(dir_path.encode("utf-8")).hexdigest()[:12]
            add_plugin_once_job(
                self.__class__.__name__,
                job_id,
                self._run_quick_check,
                name="mp-stage 定向复查",
                delay_seconds=QUICK_DELAY_SECONDS,
                func_kwargs={"dir_path": dir_path},
            )
            logger.info(f"MPStageFixer：已排队 {QUICK_DELAY_SECONDS}s 后定向复查 {dir_path}")
        except Exception as err:
            logger.error(f"MPStageFixer：排队定向复查失败：{err}")

    @eventmanager.register(EventType.PluginAction)
    def on_plugin_action(self, event: Event = None) -> None:
        """响应 /mpstage_fix 与 /mpstage_review 命令，立即执行对应任务。"""
        if not self._enabled:
            return
        event_data = getattr(event, "event_data", None)
        action = event_data.get("action") if event_data else None
        if action == "mpstage_review":
            if not self._review_enabled:
                return
            threading.Thread(
                target=self._run_review,
                kwargs={"source": "manual"},
                name="mpstagefixer-review",
                daemon=True,
            ).start()
            return
        if action != "mpstage_fix":
            return
        threading.Thread(
            target=self._run_sweep,
            kwargs={"source": "manual"},
            name="mpstagefixer-manual",
            daemon=True,
        ).start()

    def _safe_sweep(self) -> None:
        """定时全量扫描入口，吞掉异常避免影响调度器。"""
        try:
            self._run_sweep(source="sweep")
        except Exception as err:
            logger.error(f"MPStageFixer：定时扫描失败：{err}")

    def _safe_review(self) -> None:
        """定时待复核判定入口，吞掉异常避免影响调度器。"""
        try:
            self._run_review(source="sweep")
        except Exception as err:
            logger.error(f"MPStageFixer：待复核判定失败：{err}")

    def _run_review(self, source: str = "sweep") -> Dict[str, Any]:
        """执行一轮待复核核验判定，保存报告并按需通知。

        仅 review.verify_task 判定可证实的任务会提交 applied；
        大小不符、目标缺失、不支持的步骤一律保留给人工。
        """
        if not REVIEW_LOCK.acquire(blocking=False):
            logger.info("MPStageFixer：已有待复核判定在运行，本次跳过")
            return {}
        try:
            from . import review

            report = review.run_review()
            report["time"] = self._now()
            report["source"] = source
            self._save_review_report(report)
            return report
        finally:
            REVIEW_LOCK.release()

    def _save_review_report(self, report: Dict[str, Any]) -> None:
        """保存最近判定报告；结果有变化或有限流时推送通知。"""
        old = self.get_data(REVIEW_KEY) or {}
        self.save_data(REVIEW_KEY, report)
        if not self._notify:
            return
        resolved = report.get("resolved", 0)
        unresolved = set(report.get("unresolved") or [])
        old_unresolved = set(old.get("unresolved") or [])
        if resolved <= 0 and unresolved == old_unresolved and not report.get("rate_limited"):
            return
        lines = [
            f"时间：{report.get('time', '-')}",
            f"待复核：{report.get('scanned', 0)}　自动判定：{resolved}"
            f"　留人工：{len(unresolved)}",
        ]
        if report.get("rate_limited"):
            lines.append("存储查询被限流，本轮中止，下轮自动重试")
        details = report.get("details") or []
        if details:
            lines.extend(details[:8])
        self.post_message(mtype=None, title="MP整理待复核判定", text="\n".join(lines))

    def _run_sweep(self, source: str = "sweep") -> Dict[str, Any]:
        """全量扫描媒体库并修复残留，返回并保存报告。"""
        if not RUN_LOCK.acquire(blocking=False):
            logger.info("MPStageFixer：已有扫描任务在运行，本次跳过")
            return {}
        try:
            from . import stagefix

            report: Dict[str, Any] = {
                "time": self._now(),
                "source": source,
                "fixed": 0,
                "dedup": 0,
                "failed": 0,
                "manual": 0,
                "findings": 0,
                "visited": 0,
                "details": [],
                "orphan_deleted": 0,
                "orphan_guard": 0,
            }
            client = stagefix.make_client()
            root_cid = stagefix.library_root_cid(client)
            findings, visited = stagefix.walk(client, root_cid, self._max_dirs)
            report["visited"] = visited
            report["findings"] = len(findings)
            for finding in findings:
                logger.info(f"MPStageFixer：发现残留 {finding['path']} ({finding['size']} 字节)")
                result, detail = stagefix.fix_one(client, finding)
                report[result] = report.get(result, 0) + 1
                report["details"].append(f"[{result}] {finding['path']} → {detail}")
                if result == "fixed":
                    logger.info(f"MPStageFixer：已重建 {detail}")
                else:
                    logger.warning(f"MPStageFixer：[{result}] {detail}")
            if self._orphan_clean:
                o_deleted, o_guard = stagefix.clean_orphan_sidecars(apply=True)
                report["orphan_deleted"] = o_deleted
                report["orphan_guard"] = o_guard
            self._save_and_notify(report)
            return report
        finally:
            RUN_LOCK.release()

    def _run_quick_check(self, dir_path: str) -> None:
        """定向复查单个网盘目录并修复其中的暂存残留。"""
        if not self._enabled:
            return
        if not RUN_LOCK.acquire(blocking=False):
            logger.info(f"MPStageFixer：扫描任务占用中，定向复查稍后由定时扫描覆盖：{dir_path}")
            return
        try:
            from . import stagefix

            client = stagefix.make_client()
            root_cid = stagefix.library_root_cid(client)
            cid = stagefix.resolve_dir_cid(client, root_cid, dir_path)
            if cid is None:
                logger.info(f"MPStageFixer：目录不存在或已改名，跳过定向复查：{dir_path}")
                return
            findings = stagefix.scan_one_dir(client, cid)
            report: Dict[str, Any] = {
                "time": self._now(),
                "source": "quick",
                "visited": 1,
                "findings": len(findings),
                "fixed": 0,
                "dedup": 0,
                "failed": 0,
                "manual": 0,
                "details": [],
                "orphan_deleted": 0,
                "orphan_guard": 0,
            }
            for finding in findings:
                logger.info(f"MPStageFixer：发现残留 {finding['path']} ({finding['size']} 字节)")
                result, detail = stagefix.fix_one(client, finding)
                report[result] = report.get(result, 0) + 1
                report["details"].append(f"[{result}] {finding['path']} → {detail}")
            self._save_and_notify(report, always=False)
        except Exception as err:
            logger.error(f"MPStageFixer：定向复查失败 {dir_path}：{err}")
        finally:
            RUN_LOCK.release()

    def _save_and_notify(self, report: Dict[str, Any], always: bool = True) -> None:
        """保存最近报告，并按需推送修复结果通知。"""
        old = self.get_data(REPORT_KEY) or {}
        if report.get("source") == "quick" and report.get("findings", 0) == 0:
            # 定向复查无残留时不覆盖全量扫描报告
            return
        self.save_data(REPORT_KEY, report)
        if not self._notify:
            return
        interesting = (
            report.get("fixed", 0)
            + report.get("dedup", 0)
            + report.get("failed", 0)
            + report.get("manual", 0)
        )
        if not always and interesting <= 0:
            return
        lines = [
            f"时间：{report.get('time', '-')}",
            f"残留：{report.get('findings', 0)}　重建：{report.get('fixed', 0)}"
            f"　清理重复：{report.get('dedup', 0)}　失败：{report.get('failed', 0)}"
            f"　需人工：{report.get('manual', 0)}",
        ]
        details = report.get("details") or []
        if details:
            lines.extend(details[:5])
        self.post_message(mtype=None, title="MP暂存残留修复", text="\n".join(lines))

    @staticmethod
    def _normalize_115_dir(path: str) -> Optional[str]:
        """把整理目标目录路径归一化为 115 网盘 /最近接收/... 路径。"""
        cleaned = path.strip()
        for prefix in ("/115open/最近接收", "/最近接收"):
            if cleaned.startswith(prefix):
                normalized = "/最近接收" + cleaned[len(prefix):]
                return normalized or "/最近接收"
        return None

    @staticmethod
    def _now() -> str:
        """返回当前本地时间字符串。"""
        import datetime

        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
