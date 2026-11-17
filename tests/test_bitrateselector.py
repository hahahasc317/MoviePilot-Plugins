"""码率优选 v1.4 补丁与巡检规划单测（宿主模块以桩替代）：python3 -m unittest discover tests"""
import enum
import importlib.util
import sys
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace as NS


def _stub(name, **attrs):
    module = sys.modules.get(name) or types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class MediaType(enum.Enum):
    TV = "电视剧"
    MOVIE = "电影"


class _PluginBase:
    def __init__(self):
        self._data = {}
        self.messages = []

    def get_data(self, key):
        return self._data.get(key)

    def save_data(self, key, value):
        self._data[key] = value

    def post_message(self, **kwargs):
        self.messages.append(kwargs)


def _norm(raw):
    return {str(int(k)): int(v) for k, v in (raw or {}).items()} if isinstance(raw, dict) else {}


def _orig_prepare(subscribe, context, priority):
    return False


def _orig_pack_gate(subscribe, priority):
    return False


priority_mod = _stub(
    "app.application.subscription.priority",
    prepare_best_version_tv_candidate=_orig_prepare,
    should_prefer_full_pack_for_episode_best_version=_orig_pack_gate,
    is_full_best_version_enabled=lambda s: bool(getattr(s, "best_version_full", 0)),
    get_best_version_target_episodes=lambda s: list(range(1, (s.total_episode or 0) + 1)),
    get_downloaded_best_version_episodes=lambda s: [int(k) for k, v in _norm(s.episode_priority).items() if v > 0],
    get_episode_priority=lambda s: _norm(s.episode_priority),
    normalize_episode_priority=_norm,
)
for pkg in ("app", "app.application", "app.application.subscription", "app.chain", "app.db",
            "app.db.oper", "app.sdk", "app.schemas", "apscheduler", "apscheduler.triggers"):
    _stub(pkg)
_stub("app.application.subscription", priority=priority_mod)
_stub("app.chain.tmdb", TmdbChain=object)
_stub("app.chain.subscribe", SubscribeChain=object)
_stub("app.db.oper.subscribe", SubscribeOper=object)
_stub("app.plugins", _PluginBase=_PluginBase)
_stub("apscheduler.triggers.cron", CronTrigger=NS(from_crontab=lambda expr: expr))
_stub("app.sdk.events", Event=object, eventmanager=NS(register=lambda *_: (lambda f: f)))
_stub("app.sdk.logging", logger=NS(**{n: (lambda *a, **k: None) for n in ("info", "warning", "error", "debug")}))
_stub("app.sdk.media", Context=NS)
_stub("app.schemas.event", ResourceDownloadEventData=object, ResourceSelectionEventData=object)
_stub("app.schemas.media", resolve_media_identity=lambda media: (media.media_source, media.media_id))
_stub("app.schemas.types", ChainEventType=NS(ResourceSelection=1, ResourceDownload=2), MediaType=MediaType)

_PATH = Path(__file__).resolve().parents[1] / "plugins.v3" / "bitrateselector" / "__init__.py"
_spec = importlib.util.spec_from_file_location("bitrateselector", _PATH)
bs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bs)


def make_plugin(**overrides):
    plugin = bs.BitrateSelector()
    plugin._enabled = True
    plugin._wash_enabled = True
    plugin._notify = True
    plugin._margin_percent = 15
    plugin._baselines = {}
    plugin._wash_marks = {}
    plugin._episode_runtimes = lambda media, season: {ep: 45 for ep in range(1, 26)}
    plugin._library_episode_minutes = lambda media, season: {}
    for key, value in overrides.items():
        setattr(plugin, key, value)
    return plugin


def make_subscribe(**kw):
    base = dict(id=124, name="玫瑰战争", year="2026", season=1, type="电视剧", best_version=1,
                best_version_full=0, total_episode=25, media_source="themoviedb", media_id="299276",
                episode_priority={str(e): 100 for e in range(1, 26)})
    base.update(kw)
    return NS(**base)


def make_pack(size_gb, pri=100, title="War of Thorns S01 2160p Pack"):
    return NS(
        torrent_info=NS(size=size_gb * 1024 ** 3, title=title, pri_order=pri),
        meta_info=NS(begin_season=1, begin_episode=None, end_episode=None, total_episode=None, episode_list=[]),
        media_info=NS(tmdb_id=299276, title="玫瑰战争", year="2026"),
        selected_episodes=None,
        allowed_episodes=None,
    )


class PatchLifecycleTest(unittest.TestCase):
    def tearDown(self):
        make_plugin()._remove_wash_patch()
        priority_mod.should_prefer_full_pack_for_episode_best_version = _orig_pack_gate
        priority_mod.prepare_best_version_tv_candidate = _orig_prepare

    def test_apply_is_idempotent_and_removable(self):
        plugin = make_plugin()
        plugin._apply_wash_patch()
        plugin._apply_wash_patch()  # 重复加载（如升级重启）不叠加包装
        gate = priority_mod.should_prefer_full_pack_for_episode_best_version
        self.assertTrue(gate._bitrateselector_wrapper)
        self.assertIs(gate._bitrateselector_original, _orig_pack_gate)
        self.assertEqual(set(plugin._patch_status.values()), {"active"})
        plugin._remove_wash_patch()
        self.assertIs(priority_mod.should_prefer_full_pack_for_episode_best_version, _orig_pack_gate)
        self.assertIs(priority_mod.prepare_best_version_tv_candidate, _orig_prepare)

    def test_incompatible_signature_is_skipped_and_alerted_once(self):
        def changed(subscribe, priority, extra):
            return False
        priority_mod.should_prefer_full_pack_for_episode_best_version = changed
        plugin = make_plugin()
        plugin._apply_wash_patch()
        self.assertIs(priority_mod.should_prefer_full_pack_for_episode_best_version, changed)
        self.assertTrue(plugin._patch_status[bs.PATCH_PACK_GATE].startswith("incompatible"))
        self.assertEqual(plugin._patch_status[bs.PATCH_PREPARE], "active")
        plugin._apply_wash_patch()
        self.assertEqual(len(plugin.messages), 1)

    def test_watchdog_reapplies_after_override(self):
        plugin = make_plugin()
        plugin._apply_wash_patch()
        priority_mod.should_prefer_full_pack_for_episode_best_version = _orig_pack_gate
        plugin._patch_watchdog()
        self.assertTrue(priority_mod.should_prefer_full_pack_for_episode_best_version._bitrateselector_wrapper)


class PackGateTest(unittest.TestCase):
    def setUp(self):
        self.plugin = make_plugin()
        self.plugin._apply_wash_patch()

    def tearDown(self):
        self.plugin._remove_wash_patch()

    def _policy_loop(self, subscribe, contexts):
        """模拟核心 policy 循环：局部变量 context + 只传 priority 调门槛。"""
        accepted = []
        for context in contexts:
            if priority_mod.should_prefer_full_pack_for_episode_best_version(
                    subscribe, context.torrent_info.pri_order):
                accepted.append(context)
        return accepted

    def test_high_bitrate_pack_admitted_after_wash_admit(self):
        subscribe = make_subscribe(episode_priority={**{str(e): 100 for e in range(1, 26)}, "6": 99})
        self.plugin._baselines = {"themoviedb:299276:1": {"episodes": {str(e): 4.0 for e in range(1, 26)}}}
        pack = make_pack(60)  # 60GB / 25×45min ≈ 7.6Mbps，领先基线 4.0 超 15%
        self.assertTrue(priority_mod.prepare_best_version_tv_candidate(subscribe, pack, 100))
        self.assertEqual(self._policy_loop(subscribe, [pack]), [pack])

    def test_low_bitrate_pack_still_rejected(self):
        subscribe = make_subscribe()
        self.plugin._baselines = {"themoviedb:299276:1": {"episodes": {str(e): 20.0 for e in range(1, 26)}}}
        pack = make_pack(60)
        self.assertFalse(priority_mod.prepare_best_version_tv_candidate(subscribe, pack, 100))
        self.assertEqual(self._policy_loop(subscribe, [pack]), [])

    def test_unmarked_pack_falls_back_to_core(self):
        self.assertEqual(self._policy_loop(make_subscribe(), [make_pack(60)]), [])

    def test_stale_mark_with_reused_id_ignored(self):
        pack = make_pack(60)
        self.plugin._wash_marks[id(pack)] = (time.time(), "another torrent", {5})
        self.assertEqual(self._policy_loop(make_subscribe(), [pack]), [])

    def test_full_mode_never_widened(self):
        pack = make_pack(60)
        self.plugin._mark_wash_candidate(pack, [5])
        self.assertEqual(self._policy_loop(make_subscribe(best_version_full=1), [pack]), [])


class InspectPlanningTest(unittest.TestCase):
    def test_plan_demotions(self):
        now = 1_000_000
        priority = {"1": 100, "2": 100, "3": 94, "4": 100, "5": 100}
        baselines = {"1": 5.0, "2": 5.0, "3": 5.0, "5": 5.0}
        records = {"5": {"cooldown": now + 10}}
        demote, pending, skipped = bs.BitrateSelector._plan_demotions(
            priority, [1, 2, 3, 4, 5, 9], baselines, records, now, budget=1)
        self.assertEqual(demote, [1])          # 预算 1
        self.assertEqual(pending, [3])         # 已在待洗
        self.assertEqual(skipped, [2, 4, 5, 9])  # 超预算 / 缺基线 / 冷却 / 未下载

    def test_plan_restores(self):
        now = 1_000_000
        ttl = bs.INSPECT_DEMOTE_TTL_SECONDS
        records = {
            "1": {"ts": now - ttl - 1},   # 超时仍 99 → 恢复
            "2": {"ts": now - 60},        # 未超时 → 保留
            "3": {"ts": now - 60},        # 已洗成 100 → 清理
            "4": {"cooldown": now - 1},   # 冷却到期 → 清理
            "5": {"cooldown": now + 99},  # 冷却中 → 保留
        }
        restore, finished = bs.BitrateSelector._plan_restores(
            {"1": 99, "2": 99, "3": 100, "4": 100, "5": 100}, records, now)
        self.assertEqual(restore, [1])
        self.assertEqual(sorted(finished), [3, 4])


class MatchSubscribeTest(unittest.TestCase):
    def test_year_and_exact_match(self):
        plugin = make_plugin()
        subs = [make_subscribe(id=1, name="遮天", year="2023"), make_subscribe(id=2, name="遮天", year="2026")]
        self.assertEqual(plugin._match_subscribe_id("遮天 (2026)", 1, subs).id, 2)

    def test_ambiguous_fuzzy_rejected(self):
        plugin = make_plugin()
        subs = [make_subscribe(id=1, name="蓝色星球", year=""), make_subscribe(id=2, name="蓝色星球2", year="")]
        self.assertEqual(plugin._match_subscribe_id("蓝色星球 (2001)", 1, subs).id, 1)
        self.assertIsNone(plugin._match_subscribe_id("星球", 1, subs))


if __name__ == "__main__":
    unittest.main()


class NoticeDedupeTest(unittest.TestCase):
    def test_same_pair_notified_once_despite_bitrate_jitter(self):
        plugin = make_plugin()
        first = plugin._dedupe_notices(["玫瑰战争 S01：淘汰 OurTV 4.8Mbps → 保留 ADWeb 14.9Mbps"])
        again = plugin._dedupe_notices(["玫瑰战争 S01：淘汰 OurTV 4.8Mbps → 保留 ADWeb 14.8Mbps"])
        other = plugin._dedupe_notices(["折月亮 S01：淘汰 A 3.0Mbps → 保留 B 9.0Mbps"])
        self.assertEqual(len(first), 1)
        self.assertEqual(again, [])
        self.assertEqual(len(other), 1)

    def test_expired_entry_notifies_again(self):
        plugin = make_plugin()
        key = "玫瑰战争 S01：淘汰 OurTV  → 保留 ADWeb "
        plugin.save_data(bs.SELECTION_NOTICE_KEY, {key: time.time() - bs.SELECTION_NOTICE_TTL_SECONDS - 1})
        self.assertEqual(len(plugin._dedupe_notices(["玫瑰战争 S01：淘汰 OurTV 4.8Mbps → 保留 ADWeb 14.9Mbps"])), 1)
