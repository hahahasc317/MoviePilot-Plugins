"""durable 整理人工复核自动判定引擎。

只对「可证实外部操作已按意图生效」的复核任务提交 applied 判定：

- 落地类步骤（materialize_target / stage_overwrite_target）：目标路径存在且
  大小与源文件完全一致（与人工复核已实证的 storage.list 核验等强）；
- 晋级/备份改名类步骤（promote_overwrite_target / backup_overwrite_target）：
  源路径消失且目标路径存在，如步骤证据带有目标大小则须一致
  （与 TransHandler 重命名观察的 APPLIED 判据相同）。

其余情况（目标缺失、大小不符、不支持的步骤类别、查询异常、判定冲突）
一律跳过并交由人工处理；一次错误的 resolve 会永久关闭复核通道并把该源
结算成失败历史，因此本模块宁可漏判绝不误判。存储查询被限流（115 770004
等）时熔断整轮，绝不加速重试。

判定提交使用与公开 API 相同的内部用例：
TransferManualReviewQuery 列表、TransferExecutionCommand.resolve_manual_review，
result_payload 采用内部成功路径的 {"item": FileItem.dump, "message": str} 形态，
保证结算引擎能恢复目标文件并写出成功历史。
"""

from __future__ import annotations

import posixpath
from typing import Any, Dict, List, Optional, Tuple

# 落地类步骤：intent = {source: FileItem 字典, target_storage, target_path, transfer_type}
MATERIALIZE_KINDS = frozenset({"materialize_target", "stage_overwrite_target"})
# 同目录改名类步骤：intent = {storage, source_path, target_path}
RENAME_KINDS = frozenset({"promote_overwrite_target", "backup_overwrite_target"})
# 存储/网盘限流特征（小写匹配）
RATE_LIMIT_MARKERS = ("770004", "限流", "rate limit", "too many request", "qps")
# 单轮最多处理任务数，防止队列积压时长时间占用存储查询
MAX_TASKS_PER_ROUND = 100
LIST_PAGE_SIZE = 50


class ReviewRateLimited(RuntimeError):
    """存储查询被限流，本轮复核中止。"""


def make_repository():
    """构造 durable 整理执行仓储（操作级短 Session，与宿主装配一致）。"""
    from app.db.adapters.transfer.execution import (
        TransactionalTransferExecutionRepository,
    )
    from app.db.session import SessionFactory

    return TransactionalTransferExecutionRepository(SessionFactory)


def list_pending(repository, limit: int = MAX_TASKS_PER_ROUND) -> List[Any]:
    """分页拉取 manual_review 状态的全部待复核任务视图。

    retry_wait 状态的任务已有判定、正等待 durable 恢复，绝不重复触碰。
    """
    from app.application.transfer.execution import (
        TransferExecutionState,
        TransferManualReviewQuery,
    )

    tasks: List[Any] = []
    page = 1
    while len(tasks) < limit:
        result = TransferManualReviewQuery(repository).list(
            state=TransferExecutionState.MANUAL_REVIEW,
            page=page,
            page_size=LIST_PAGE_SIZE,
        )
        tasks.extend(result.items)
        if not result.items or len(tasks) >= result.total:
            break
        page += 1
    return tasks[:limit]


def _list_dir(chain: Any, storage: str, dir_path: str) -> List[Any]:
    """列出指定存储目录的直接子项；限流时抛 ReviewRateLimited。

    :param chain: StorageChain 实例
    :param storage: 存储配置标识
    :param dir_path: 目录路径
    :return: 目录下的文件项列表；查询失败抛出原异常
    """
    from app.schemas.file import FileItem

    name = posixpath.basename(dir_path.rstrip("/")) or "/"
    parent = FileItem(storage=storage, path=dir_path, type="dir", name=name)
    try:
        return chain.list_files(parent) or []
    except Exception as err:
        message = str(err).lower()
        if any(marker in message for marker in RATE_LIMIT_MARKERS):
            raise ReviewRateLimited(str(err)) from err
        raise


def _find_item(items: List[Any], target_path: str) -> Optional[Any]:
    """在列目录结果中按路径精确匹配目标文件项。"""
    wanted = target_path.rstrip("/")
    for item in items:
        if getattr(item, "type", None) == "dir":
            continue
        if (getattr(item, "path", None) or "").rstrip("/") == wanted:
            return item
    return None


def _evidence_item_size(step: Any) -> Optional[int]:
    """从步骤证据的目标文件项中提取大小，缺省返回 None。"""
    evidence = getattr(step, "evidence", None)
    if not isinstance(evidence, dict):
        return None
    item = evidence.get("item")
    if isinstance(item, dict) and item.get("size") is not None:
        try:
            return int(item["size"])
        except (TypeError, ValueError):
            return None
    return None


def verify_task(chain: Any, task: Any) -> Tuple[Optional[Dict[str, Any]], str]:
    """核验单个待复核任务是否可证实已按意图生效。

    :param chain: StorageChain 实例
    :param task: TransferManualReviewTaskView 视图
    :return: (目标文件项字典, 核验说明)；可判定时字典非空，否则为 None
    """
    step = getattr(task, "step", None)
    intent = dict(getattr(step, "intent", None) or {})
    kind = str(getattr(step, "kind", "") or "")
    if kind in MATERIALIZE_KINDS:
        source = intent.get("source")
        source = source if isinstance(source, dict) else {}
        target_storage = intent.get("target_storage")
        target_path = intent.get("target_path")
        expected_size = source.get("size")
        if not target_storage or not target_path or expected_size is None:
            return None, "意图缺少目标路径或源大小，无法自动核验"
        items = _list_dir(chain, str(target_storage),
                          posixpath.dirname(str(target_path)))
        target = _find_item(items, str(target_path))
        if target is None:
            return None, "目标文件不存在"
        if target.size is None or int(target.size) != int(expected_size):
            return None, f"目标大小不符（实际 {target.size} / 期望 {expected_size}）"
        return target.model_dump(mode="json"), f"目标存在且大小一致（{target.size} 字节）"
    if kind in RENAME_KINDS:
        storage = intent.get("storage")
        source_path = intent.get("source_path")
        target_path = intent.get("target_path")
        if not storage or not source_path or not target_path:
            return None, "意图缺少改名路径，无法自动核验"
        # 晋级/备份改名的源与目标同目录，列目标父目录即可覆盖两端
        items = _list_dir(chain, str(storage), posixpath.dirname(str(target_path)))
        target = _find_item(items, str(target_path))
        source_item = _find_item(items, str(source_path))
        if target is None:
            return None, "改名目标不存在"
        if source_item is not None:
            return None, "改名源仍存在，目标可能是旧文件"
        evidence_size = _evidence_item_size(step)
        if evidence_size is not None and (
                target.size is None or int(target.size) != evidence_size):
            return None, f"目标大小与证据不符（实际 {target.size} / 证据 {evidence_size}）"
        return target.model_dump(mode="json"), (
            f"源已消失且目标存在（{target.size if target.size is not None else '?'} 字节）")
    return None, f"步骤类别 {kind} 不支持自动判定"


def resolve_applied(repository: Any, task: Any,
                    item_payload: Dict[str, Any], note: str) -> Any:
    """对可证实任务提交 applied 判定并附目标文件证据。

    :param repository: durable 整理执行仓储
    :param task: 待复核任务视图
    :param item_payload: 实测目标文件项字典（FileItem dump）
    :param note: 供审计记录的核验说明
    :return: TransferManualReviewResult
    """
    from app.application.transfer.execution import (
        TransferExecutionCommand,
        TransferManualReviewDecision,
        TransferStepResult,
    )

    return TransferExecutionCommand(repository).resolve_manual_review(
        task_id=task.task_id,
        operation_id=task.step.operation_id,
        decision=TransferManualReviewDecision.APPLIED,
        actor="MPStageFixer",
        reason=note,
        result=TransferStepResult(payload={
            "item": item_payload,
            "message": f"MPStageFixer 自动核验：{note}",
        }),
    )


def _task_label(task: Any) -> str:
    """生成复核任务的可读标签（源文件名 + 步骤类别）。"""
    source_path = getattr(task.source, "path", None) or ""
    name = posixpath.basename(source_path) or source_path or task.task_id
    kind = getattr(task.step, "kind", "?")
    return f"{name} [{kind}]"


def run_review() -> Dict[str, Any]:
    """执行一轮待复核扫描与可证实判定，返回判定报告。

    报告键：scanned/resolved/skipped/conflict/rate_limited/details/unresolved。
    unresolved 保存仍需人工处理的任务 id，供调用方做通知去重。
    """
    from app.chain.storage import StorageChain
    from app.sdk.logging import logger

    repository = make_repository()
    tasks = list_pending(repository)
    report: Dict[str, Any] = {
        "scanned": len(tasks),
        "resolved": 0,
        "skipped": 0,
        "conflict": 0,
        "rate_limited": False,
        "details": [],
        "unresolved": [],
    }
    if not tasks:
        return report
    chain = StorageChain()
    for task in tasks:
        label = _task_label(task)
        try:
            item_payload, note = verify_task(chain, task)
        except ReviewRateLimited as err:
            report["rate_limited"] = True
            report["unresolved"].append(task.task_id)
            report["details"].append(f"[限流] {label}：本轮中止，下轮自动重试")
            logger.warning(f"MPStageFixer：复核存储查询被限流，本轮中止：{err}")
            break
        except Exception as err:
            report["skipped"] += 1
            report["unresolved"].append(task.task_id)
            report["details"].append(f"[异常] {label}：{err}")
            logger.error(f"MPStageFixer：复核任务 {task.task_id} 核验异常：{err}")
            continue
        if item_payload is None:
            report["skipped"] += 1
            report["unresolved"].append(task.task_id)
            report["details"].append(f"[跳过] {label}：{note}")
            continue
        try:
            resolve_applied(repository, task, item_payload, note)
            report["resolved"] += 1
            report["details"].append(f"[判定] {label}：{note}")
            logger.info(f"MPStageFixer：已自动判定 applied：{label}（{note}）")
        except Exception as err:
            report["conflict"] += 1
            report["unresolved"].append(task.task_id)
            report["details"].append(f"[冲突] {label}：{err}")
            logger.warning(f"MPStageFixer：复核判定提交失败 {task.task_id}：{err}")
    return report
