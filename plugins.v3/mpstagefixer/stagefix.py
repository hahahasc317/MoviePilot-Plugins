"""mp-stage 暂存残留扫描与修复引擎。

移植自实证脚本（2026-10-25 首例，2026-11-06 光阴之外 E32/E33/E35 复证）：
MoviePilot 持久化整理经 CloudDrive2 写 115 时，上传期对象为暂存名
`某某.mkv**.mp-stage-<hex>`；上传完成后 CD2 promote 改名到正式名。promote 失败时
对象永久残留（115 对象级隐藏尾巴属性会拼回 `**`，三条改名通道均不可修复），
且 MP 整理记录记为成功。暂存名被 strm 插件按非媒体跳过 → Emby 缺集。

115网盘Plus（P115Disk）通道偶发变体：`<stem>.mp-stage-<hex>`，无 `**`、扩展名丢失，
正式名从 transfer.history dest 匹配兜底。

唯一有效修复：SHA1 秒传重建全新干净对象 + 删除旧对象（进回收站可恢复）。
"""

from __future__ import annotations

import glob
import json
import os
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from p115client import P115Client

from app.sdk.logging import logger

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MPStageFixer/1.0"
USER_DB = "/config/user.db"
LOG_GLOB = "/config/logs/plugins/p115strmhelper.log*"
ORPHAN_MEDIA_ROOT = "/media"
ORPHAN_MIN_AGE = 24 * 3600  # 换版进行中的新鲜边车不足 24 小时不清理
VIDEO_STEM_EXTS = (".mkv", ".mp4", ".ts", ".iso", ".rmvb", ".avi", ".wmv", ".flv", ".m2ts", ".mov")
DIR_ASSET_RE = re.compile(
    r"^(movie(\.nfo|4kbackground\.jpg)?|landscape\.jpg|thumb\.jpg|backdrop\..*|banner\..*|clearart\..*|"
    r"disc\..*|fanart\..*|logo\..*|poster\..*|tvshow\.nfo|season[a-z0-9_-]*\.(jpg|png|nfo))$", re.I
)
SIDECAR_RE = re.compile(r"(-mediainfo\.json|\.nfo|\.jpg|\.png)$", re.I)
MEDIA_EXT = re.compile(
    r"\.(mkv|mp4|ts|iso|wmv|avi|rmvb|flv|mov|m2ts|mpg|mpeg|webm)$", re.I
)
SHA_RE = re.compile(r"[0-9A-Fa-f]{40}")


def load_cookie() -> str:
    """从 P115Disk 插件配置读取 115 cookie（不打印内容）。"""
    con = sqlite3.connect(f"file:{USER_DB}?mode=ro", uri=True)
    try:
        row = con.execute(
            "select config_data from plugininstance where instance_id='P115Disk'"
        ).fetchone()
    finally:
        con.close()
    cookie = (json.loads(row[0]) if row else {}).get("cookie")
    if not cookie:
        raise RuntimeError("P115Disk 插件配置中没有 cookie")
    return cookie


def make_client() -> P115Client:
    """创建 115 客户端。"""
    return P115Client(load_cookie())


def library_root_cid(client: P115Client) -> int:
    """媒体库根目录 /最近接收 的 cid。"""
    root = client.fs_files({"cid": 0, "limit": 100, "show_dir": 1})
    for item in root.get("data") or []:
        if item.get("n") == "最近接收":
            return int(item["cid"])
    raise RuntimeError("115 根目录下未找到「最近接收」")


def walk(client: P115Client, root_cid: int, max_dirs: int = 3000) -> Tuple[List[dict], int]:
    """广度优先遍历媒体库，返回 (残留清单, 已扫描目录数)。"""
    queue: List[Tuple[str, int]] = [("/", root_cid)]
    findings: List[dict] = []
    visited = 0
    while queue and visited < max_dirs:
        path, cid = queue.pop(0)
        visited += 1
        try:
            resp = client.fs_files({"cid": cid, "limit": 1000, "show_dir": 1})
        except Exception as err:
            logger.warning(f"MPStageFixer：列目录失败 {path}: {err}")
            time.sleep(3)
            continue
        time.sleep(0.4)  # webapi 限速保护
        for item in resp.get("data") or []:
            name = item.get("n") or ""
            if item.get("fid") is None:  # 目录
                queue.append((f"{path}/{name}", int(item["cid"])))
                continue
            if "mp-stage" in name or "**" in name:
                findings.append(_finding_from_item(f"{path}/{name}", cid, item))
    return findings, visited


def resolve_dir_cid(client: P115Client, root_cid: int, dir_path: str) -> Optional[int]:
    """把 /最近接收/... 的网盘路径解析为目录 cid。"""
    rel = dir_path[len("/最近接收") :].strip("/") if dir_path.startswith("/最近接收") else dir_path.strip("/")
    cid = root_cid
    if not rel:
        return cid
    for part in rel.split("/"):
        if not part:
            continue
        resp = client.fs_files({"cid": cid, "limit": 1000, "show_dir": 1})
        time.sleep(0.4)
        found = None
        for item in resp.get("data") or []:
            if item.get("fid") is None and item.get("n") == part:
                found = int(item["cid"])
                break
        if found is None:
            return None
        cid = found
    return cid


def scan_one_dir(client: P115Client, cid: int) -> List[dict]:
    """扫描单个目录，返回残留清单。"""
    resp = client.fs_files({"cid": cid, "limit": 1000, "show_dir": 1})
    time.sleep(0.4)
    findings: List[dict] = []
    for item in resp.get("data") or []:
        name = item.get("n") or ""
        if item.get("fid") is None:
            continue
        if "mp-stage" in name or "**" in name:
            findings.append(_finding_from_item(name, cid, item))
    return findings


def _finding_from_item(path: str, cid: int, item: dict) -> dict:
    """把 webapi 文件条目整理为残留清单条目。"""
    return {
        "path": path,
        "dir_cid": cid,
        "fid": int(item.get("fid") or 0),
        "name": item.get("n") or "",
        "size": int(item.get("s") or 0),
        "pickcode": item.get("pc") or "",
        "sha": (item.get("sha") or "").strip(),
    }


def infer_clean_name(name: str) -> Optional[str]:
    """按去尾巴 stem 直接推断正式文件名。"""
    base = name.split("**", 1)[0]
    base = re.sub(r"\.mp-stage-[0-9a-fA-F]+$", "", base)
    if base and MEDIA_EXT.search(base) and base != name:
        return base
    return None


def clean_name_from_history(stem: str) -> Optional[str]:
    """丢扩展名变体兜底：按去尾巴后的 stem 匹配 MP 整理历史 dest 正式名。

    连接 MoviePilot 数据库只读查询 transferhistory，取最新一条 basename
    去扩展名后与 stem 精确相等且扩展名为媒体扩展名的记录。任何异常都
    返回 None，不影响主流程。
    """
    if not stem or MEDIA_EXT.search(stem):
        return None
    try:
        import sys as _sys

        from sqlalchemy import create_engine, text

        if "/app" not in _sys.path:
            _sys.path.insert(0, "/app")
        from app.runtime.config import settings  # pylint: disable=import-outside-toplevel

        if str(settings.DB_TYPE).lower() == "postgresql":
            engine = create_engine(settings.DB_POSTGRESQL_URL())
        else:
            engine = create_engine(f"sqlite:///{settings.CONFIG_PATH}/user.db")
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "select dest from transferhistory "
                        "where dest like :pat order by id desc limit 50"
                    ),
                    {"pat": f"%/{stem}.%"},
                ).fetchall()
        finally:
            engine.dispose()
        for (dest,) in rows:
            basename = os.path.basename(str(dest or ""))
            if not basename or "." not in basename:
                continue
            candidate_stem, ext = basename.rsplit(".", 1)
            if candidate_stem == stem and MEDIA_EXT.search(f".{ext}"):
                return basename
    except Exception:
        return None
    return None


def sha1_from_logs(fid: int) -> Optional[str]:
    """从 p115strmhelper 生活事件日志回溯文件 SHA1。"""
    pat = re.compile(r"\{'id'.*?\}")
    for logfile in sorted(glob.glob(LOG_GLOB), reverse=True):
        try:
            with open(logfile, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if str(fid) not in line or "'sha1'" not in line:
                        continue
                    m = pat.search(line)
                    if not m:
                        continue
                    try:
                        ev = eval(m.group(0), {"__builtins__": {}})  # noqa: S307
                    except Exception:
                        continue
                    if str(ev.get("file_id")) == str(fid) and SHA_RE.fullmatch(
                        str(ev.get("sha1") or "")
                    ):
                        return ev["sha1"].upper()
        except OSError:
            continue
    return None


def get_sha1(client: P115Client, finding: dict) -> Optional[str]:
    """获取残留对象 SHA1：条目自带 → 详情接口 → 生活事件日志。"""
    if SHA_RE.fullmatch(finding.get("sha") or ""):
        return finding["sha"].upper()
    try:
        detail = client.fs_file(f"https://115.com/?pickcode={finding['pickcode']}")
        data = detail.get("data") if isinstance(detail.get("data"), list) else []
        for item in data:
            if str(item.get("fid")) == str(finding["fid"]) and SHA_RE.fullmatch(
                str(item.get("sha") or "")
            ):
                return item["sha"].upper()
    except Exception:
        pass
    return sha1_from_logs(finding["fid"])


def fix_one(client: P115Client, finding: dict) -> Tuple[str, str]:
    """修复单个残留对象，返回 (结果类别, 说明)。

    类别: fixed（秒传重建）/ dedup（正式对象已在，仅清理）/ manual / failed。
    """
    name = finding["name"]
    clean = infer_clean_name(name)
    if not clean:
        stem = re.sub(r"\.mp-stage-[0-9a-fA-F]+$", "", name.split("**", 1)[0])
        clean = clean_name_from_history(stem)
        if clean:
            logger.info(f"MPStageFixer：正式名取自 transfer.history: {clean}")
    if not clean:
        return "manual", f"无法推断正式名: {name}"

    try:
        resp = client.fs_files({"cid": finding["dir_cid"], "limit": 1000, "show_dir": 1})
    except Exception as err:
        return "failed", f"列目录失败: {err}"
    siblings = {item.get("n"): item for item in resp.get("data") or []}

    existing = siblings.get(clean)
    if existing is not None:
        if int(existing.get("s") or 0) == finding["size"]:
            pass  # 正式对象已在，只需清理垃圾对象
        else:
            return "manual", f"正式名已存在但大小不同，需人工核对: {clean}"
    else:
        sha1 = get_sha1(client, finding)
        if not sha1:
            return "manual", f"取不到 SHA1（日志可能已轮转）: {name}"
        try:
            url = client.download_url(finding["fid"], user_agent=UA)
            url = url["download_url"] if isinstance(url, dict) else str(url)
            sess = requests.Session()

            def read_range(spec: str, url=url, sess=sess):
                """按 Range 分片读取直链片段（UA 必须与取链一致）。"""
                r = sess.get(
                    url,
                    headers={"Range": f"bytes={spec}", "User-Agent": UA},
                    timeout=60,
                )
                r.raise_for_status()
                return r.content

            init = client.upload_file_init(
                clean, sha1, finding["size"], pid=finding["dir_cid"],
                read_range_bytes_or_hash=read_range,
            )
        except Exception as err:
            return "failed", f"秒传异常 {clean}: {type(err).__name__}: {err}"
        if init.get("status") != 2:
            return "failed", f"秒传未成功 status={init.get('status')}: {clean}"
        time.sleep(2)
        try:
            resp = client.fs_files({"cid": finding["dir_cid"], "limit": 1000, "show_dir": 1})
        except Exception as err:
            return "failed", f"复查列目录失败: {err}"
        new_item = next(
            (i for i in resp.get("data") or [] if i.get("n") == clean), None
        )
        if not new_item or int(new_item.get("s") or 0) != finding["size"]:
            return "failed", f"新对象复核失败: {clean}"

    time.sleep(1)
    try:
        del_resp = client.fs_delete({"fid": finding["fid"]})
    except Exception as err:
        return "failed", f"删除旧对象异常 {name}: {err}"
    if not del_resp.get("state"):
        return "failed", f"删除旧对象失败: {name}"
    return ("dedup" if existing is not None else "fixed"), clean


def clean_orphan_sidecars(apply: bool = True) -> Tuple[int, int]:
    """清理换代残留的孤儿边车（本地 /media）。

    判定：边车 stem 不匹配同目录任何 strm/视频文件，且排除目录级资源
    （poster.jpg/season*.jpg 等）。超过 24 小时的才删，避免误伤换版进行中的文件。
    返回 (删除数, 防护拦截数)；apply=False 时仅统计。
    """
    now = time.time()
    deleted = guard = 0
    for dirpath, _dirs, files in os.walk(ORPHAN_MEDIA_ROOT):
        strm_stems = {f[:-5] for f in files if f.endswith(".strm")}
        video_stems = {
            os.path.splitext(f)[0] for f in files if f.lower().endswith(VIDEO_STEM_EXTS)
        }
        for f in files:
            if DIR_ASSET_RE.match(f):
                continue
            m = SIDECAR_RE.search(f)
            if not m:
                continue
            stem = f[: m.start()]
            if stem in strm_stems or stem in video_stems:
                continue
            path = os.path.join(dirpath, f)
            try:
                if now - os.path.getmtime(path) < ORPHAN_MIN_AGE:
                    guard += 1
                    continue
                if apply:
                    os.remove(path)
                    logger.info(f"MPStageFixer：孤儿边车已删除: {path}")
                deleted += 1
            except OSError:
                guard += 1
    return deleted, guard
