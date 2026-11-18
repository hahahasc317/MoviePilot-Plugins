"""码率优选的模块级常量。"""

import re

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
# 播种时按 strm 实际文件核实码率：边车码率低于此值或该集有防降级下限时核实
SEED_VERIFY_BELOW_MBPS = 8.0
# 核实值与既有基线相差超过该比例即以核实值为准
SEED_VERIFY_TOLERANCE = 0.15
# 单次播种最多核实的集数与核实间隔，防止 115 直链请求过密
SEED_VERIFY_MAX_PER_RUN = 300
SEED_VERIFY_INTERVAL_SECONDS = 0.3
# 网络源（WEB-DL 等）合理码率上限，超出视为集数识别错误；原盘/REMUX 不受限
IMPLAUSIBLE_WEB_MBPS = 50.0
DISC_SOURCE_RE = re.compile(r"remux|blu-?ray|bdmv|uhd\s*bd|原盘", re.IGNORECASE)
# 标题声明的集数范围：E01-E30 / EP01~EP30 / 第1-30集；全30集 / 共30集
TITLE_EP_RANGE_RE = re.compile(
    r"(?:\bE[Pp]?|第)\s*(\d{1,4})\s*(?:-|~|～|—|至)\s*(?:E[Pp]?)?\s*(\d{1,4})", re.IGNORECASE
)
TITLE_EP_TOTAL_RE = re.compile(r"[全共]\s*(\d{1,4})\s*集")
# 库内实测与基线相差超过该比例即重新校准（统一为含音轨的整体码率口径）
SEED_RECALIBRATE_TOLERANCE = 0.05
# 洗版毕业：码率达标且观察期内未洗到明显更好版本的集，标记为规则满分停止洗版
GRADUATE_MIN_MBPS = 15.0
GRADUATE_WATCH_SECONDS = 3 * 86400
GRADUATION_KEY = "graduation_watch"
# 本地插件副本告警去重记录
LOCAL_SHADOW_KEY = "local_shadow_alert"
# 入库实测：视频文件最小体积与扩展名
TRANSFER_MIN_VIDEO_BYTES = 50 * 1024 * 1024
VIDEO_EXTS = (".mkv", ".mp4", ".ts", ".m2ts", ".avi", ".mov", ".wmv", ".flv", ".iso", ".rmvb", ".webm")
# 旧版虚高下限一次性清理标记
FLOOR_MIGRATION_KEY = "floor_migration_147"
# 选种淘汰通知去重记录的存储键与去重时长
SELECTION_NOTICE_KEY = "selection_notices"
SELECTION_NOTICE_TTL_SECONDS = 24 * 3600
# 补丁不兼容告警去重记录的存储键
PATCH_ALERT_KEY = "patch_alert"
