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
_stub("app.schemas.types", ChainEventType=NS(ResourceSelection=1, ResourceDownload=2), EventType=NS(PluginAction=5, TransferComplete=6), MediaType=MediaType)

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


class InspectCommandTest(unittest.TestCase):
    def test_command_runs_inspect_and_reports_clean(self):
        plugin = make_plugin(_inspect_enabled=True, _inspect_days=30, _inspect_min_mbps=8.0)
        plugin._safe_low_bitrate_inspect = lambda: {"success": True, "low": 0, "triggered": []}
        threads = []
        original = bs.threading.Thread

        class _Sync:
            def __init__(self, target, **_):
                self.target = target

            def start(self):
                threads.append(self)
                self.target()

        bs.threading.Thread = _Sync
        try:
            plugin.on_plugin_action(NS(event_data={"action": "bitrate_inspect"}))
        finally:
            bs.threading.Thread = original
        self.assertEqual(len(threads), 1)
        self.assertIn("无需处理", plugin.messages[-1]["text"])
        self.assertFalse(plugin._inspect_lock.locked())

    def test_other_action_ignored_and_disabled_reported(self):
        plugin = make_plugin(_inspect_enabled=False)
        plugin.on_plugin_action(NS(event_data={"action": "mpstage_fix"}))
        self.assertEqual(plugin.messages, [])
        plugin.on_plugin_action(NS(event_data={"action": "bitrate_inspect"}))
        self.assertIn("未启用", plugin.messages[-1]["text"])


class SeedVerifyTest(unittest.TestCase):
    """复现玫瑰战争：换版后边车 mediainfo 未刷新，基线停在 4Mbps、下限被误清。"""

    def setUp(self):
        import json
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.season = Path(self.tmp.name) / "玫瑰战争 (2026)" / "Season 1"
        self.season.mkdir(parents=True)
        runtime_ticks = 45 * 60 * 10_000_000
        for ep, sidecar_mbps in ((5, 5.18), (14, 14.72), (22, 4.03)):
            stem = f"玫瑰战争 - S01E{ep:02d} - 第 {ep} 集"
            (self.season / f"{stem}-mediainfo.json").write_text(json.dumps([{"MediaSourceInfo": {
                "RunTimeTicks": runtime_ticks,
                "MediaStreams": [{"Type": "Video", "BitRate": int(sidecar_mbps * 1_000_000)}]}}]))
            (self.season / f"{stem}.strm").write_text(f"http://nas/{ep}")
        # E5、E22 实际已是约 15Mbps 的新版本；E14 未换版
        self.sizes = {"5": 15.2, "22": 15.0, "14": 14.72}

    def tearDown(self):
        self.tmp.cleanup()

    def _plugin(self):
        plugin = make_plugin()
        plugin._baselines = {"themoviedb:299276:1": {
            "episodes": {"5": 5.18, "14": 14.72, "22": 4.03},
            "ep_floors": {"22": [15.01, time.time() - 3600]},
        }}
        sizes = self.sizes

        def remote_size(info_path):
            ep = str(int(info_path.name.split("S01E")[1][:2]))
            return True, int(sizes[ep] * 1_000_000 / 8 * 45 * 60)

        plugin._strm_remote_size = remote_size
        plugin._seed_verify_budget = 10
        bs.SEED_VERIFY_INTERVAL_SECONDS = 0
        return plugin

    def test_stale_sidecar_corrected_by_actual_file(self):
        plugin = self._plugin()
        seeded, corrected = plugin._seed_subscription_baseline(make_subscribe(), Path(self.tmp.name))
        entry = plugin._baselines["themoviedb:299276:1"]
        self.assertAlmostEqual(entry["episodes"]["5"], 15.2, places=1)
        self.assertAlmostEqual(entry["episodes"]["22"], 15.0, places=1)
        self.assertEqual(entry["episodes"]["14"], 14.72)
        self.assertNotIn("22", entry["ep_floors"])
        self.assertEqual((seeded, corrected), (0, 2))

    def test_floor_kept_when_verification_fails(self):
        plugin = self._plugin()
        plugin._strm_remote_size = lambda info_path: (True, None)
        plugin._seed_subscription_baseline(make_subscribe(), Path(self.tmp.name))
        entry = plugin._baselines["themoviedb:299276:1"]
        self.assertIn("22", entry["ep_floors"])
        self.assertEqual(entry["episodes"]["22"], 4.03)


class RecordBaselineTest(unittest.TestCase):
    """复现多喜一家人 E06：10.4GB 整季包只为 E06 下载，旧版按单集估出 30Mbps 虚高下限。"""

    def test_pack_for_single_episode_uses_pack_coverage(self):
        plugin = make_plugin()
        plugin._baselines = {"themoviedb:1:1": {"episodes": {"6": 1.5}}}
        size = int(10.4 * 1024 ** 3)
        ctx = NS(
            torrent_info=NS(size=size, title="Still.a.Family.S01.HHWEB", pri_order=100),
            meta_info=NS(begin_season=1, episode_list=[]),
            media_info=NS(media_source="themoviedb", media_id="1", tmdb_id=1, title="多喜一家人", year="2026"),
        )
        plugin._record_wash_baseline(NS(context=ctx, episodes={6}))
        entry = plugin._baselines["themoviedb:1:1"]
        pack_rate = size * 8 / (25 * 45 * 60) / 1_000_000   # 全季 25 集各 45 分钟
        self.assertAlmostEqual(entry["episodes"]["6"], round(pack_rate, 2), places=2)
        self.assertFalse(entry.get("ep_floors"))
        single_ep_rate = size * 8 / (45 * 60) / 1_000_000
        self.assertGreater(single_ep_rate, 30)   # 旧算法的虚高值

    def test_old_floors_migrated_once(self):
        plugin = make_plugin()
        plugin._baselines = {"k": {"episodes": {"6": 8.0}, "ep_floors": {"6": [30.27, time.time()]},
                                   "full_floor": [20.0, time.time()]}}
        plugin._migrate_inflated_floors()
        self.assertEqual(plugin._baselines["k"]["ep_floors"], {})
        self.assertNotIn("full_floor", plugin._baselines["k"])
        plugin._baselines["k"]["ep_floors"] = {"7": [12.0, time.time()]}
        plugin._migrate_inflated_floors()   # 第二次不再清除新记录的下限
        self.assertIn("7", plugin._baselines["k"]["ep_floors"])


class MisparsedPackTest(unittest.TestCase):
    """复现谷雨：20GB 整季包被识别为仅 E01，估出 62.6Mbps 后以「洗版升级」下载。"""

    TITLE = "[TV Series/HD]The Silent Storm S01 2026 2160p YK WEB-DL H265 AAC-ADWeb"
    DESC = "谷雨/The Silent Storm | 4K | 集数：E01-E30（共30集）"

    def _ctx(self, title=None, desc=None, size_gb=20.12, eps=(1,)):
        return NS(
            torrent_info=NS(size=int(size_gb * 1024 ** 3), title=title or self.TITLE,
                            description=self.DESC if desc is None else desc, pri_order=100),
            meta_info=NS(begin_season=1, begin_episode=1, end_episode=None, total_episode=None,
                         episode_list=list(eps)),
            media_info=NS(tmdb_id=279155, title="谷雨", year="2026",
                          media_source="themoviedb", media_id="279155"),
            selected_episodes=None, allowed_episodes=None,
        )

    def test_title_range_parsing(self):
        rng = bs.BitrateSelector._title_episode_range
        self.assertEqual(rng(NS(title="x", description="集数：E01-E30（共30集）")), set(range(1, 31)))
        self.assertEqual(rng(NS(title="Show 全24集", description="")), set(range(1, 25)))
        self.assertEqual(rng(NS(title="Show 第1-12集", description="")), set(range(1, 13)))
        self.assertEqual(rng(NS(title="Show S01E05 2160p WEB-DL H265-ADWeb", description="")), set())

    def test_estimate_uses_title_range(self):
        plugin = make_plugin()
        plugin._episode_runtimes = lambda media, season: {ep: 45 for ep in range(1, 31)}
        mbps, _ = plugin._estimate_bitrate_ex(self._ctx(), {1})
        self.assertAlmostEqual(mbps, 20.12 * 1024 ** 3 * 8 / (30 * 45 * 60) / 1e6, places=2)

    def test_wash_rejects_misparsed_pack(self):
        plugin = make_plugin()
        plugin._episode_runtimes = lambda media, season: {ep: 45 for ep in range(1, 31)}
        plugin._baselines = {"themoviedb:279155:1": {"episodes": {str(e): 16.0 for e in range(1, 31)}}}
        subscribe = make_subscribe(id=122, name="谷雨", total_episode=30, media_id="279155",
                                   episode_priority={str(e): 96 for e in range(1, 31)})
        self.assertFalse(plugin._wash_admit(subscribe, self._ctx(), 100, lambda *a: True))

    def test_implausible_estimate_rejected_not_fallback(self):
        plugin = make_plugin()
        plugin._episode_runtimes = lambda media, season: {ep: 45 for ep in range(1, 31)}
        plugin._baselines = {"themoviedb:279155:1": {"episodes": {str(e): 16.0 for e in range(1, 31)}}}
        subscribe = make_subscribe(id=122, name="谷雨", total_episode=30, media_id="279155",
                                   episode_priority={str(e): 96 for e in range(1, 31)})
        ctx = self._ctx(desc="")  # 无任何范围线索
        mbps, source = plugin._estimate_bitrate_ex(ctx, {1})
        self.assertEqual((mbps, source), (None, "implausible"))
        self.assertFalse(plugin._wash_admit(subscribe, ctx, 100, lambda *a: True))

    def test_remux_not_capped(self):
        plugin = make_plugin()
        ctx = self._ctx(title="Blue.Planet.II.S01E01.2160p.BluRay.REMUX.HEVC", desc="", size_gb=40)
        mbps, source = plugin._estimate_bitrate_ex(ctx, {1})
        self.assertGreater(mbps, 50)
        self.assertNotEqual(source, "implausible")


class TransferBaselineTest(unittest.TestCase):
    """入库后按实际文件大小记录基线：复现谷雨 E01 被 801MB 低码版覆盖后的修正。"""

    def _event(self, size, name="谷雨 - S01E01 - 第 1 集.mkv", eps=(1,), file_count=1, success=True):
        return NS(
            transferinfo=NS(success=success, file_count=file_count, total_size=size,
                            target_item=NS(size=size, name=name)),
            meta=NS(begin_season=1, begin_episode=eps[0] if eps else None, episode_list=list(eps)),
            mediainfo=NS(type=MediaType.TV, tmdb_id=279155, media_source="themoviedb", media_id="279155"),
        )

    def _plugin(self):
        plugin = make_plugin()
        plugin._episode_runtimes = lambda media, season: {ep: 45 for ep in range(1, 31)}
        plugin._baselines = {"themoviedb:279155:1": {
            "episodes": {"1": 17.47, "2": 15.72},
            "ep_floors": {"1": [62.61, time.time()]},
        }}
        return plugin

    def test_actual_file_overrides_stale_baseline_and_floor(self):
        plugin = self._plugin()
        size = int(801.62 * 1024 ** 2)
        key, eps, mbps = plugin._record_transferred_baseline(self._event(size))
        self.assertEqual((key, eps), ("themoviedb:279155:1", [1]))
        self.assertAlmostEqual(mbps, size * 8 / (45 * 60) / 1e6, places=2)
        entry = plugin._baselines[key]
        self.assertEqual(entry["episodes"]["1"], mbps)
        self.assertEqual(entry["episodes"]["2"], 15.72)
        self.assertNotIn("1", entry["ep_floors"])
        self.assertIs(plugin.get_data(bs.WASH_BASELINES_KEY), plugin._baselines)

    def test_skips_unusable_events(self):
        plugin = self._plugin()
        big = 6 * 1024 ** 3
        self.assertIsNone(plugin._record_transferred_baseline(self._event(big, file_count=4)))
        self.assertIsNone(plugin._record_transferred_baseline(self._event(2048, name="x.strm")))
        self.assertIsNone(plugin._record_transferred_baseline(self._event(big, success=False)))
        self.assertIsNone(plugin._record_transferred_baseline(self._event(big, eps=())))
        self.assertIsNone(plugin._record_transferred_baseline(None))
        self.assertEqual(plugin._baselines["themoviedb:279155:1"]["episodes"]["1"], 17.47)

    def test_handler_respects_switches(self):
        plugin = self._plugin()
        plugin._wash_enabled = False
        plugin.on_transfer_complete(NS(event_data=self._event(6 * 1024 ** 3)))
        self.assertEqual(plugin._baselines["themoviedb:279155:1"]["episodes"]["1"], 17.47)
        plugin._wash_enabled = True
        plugin.on_transfer_complete(NS(event_data=self._event(6 * 1024 ** 3)))
        self.assertAlmostEqual(plugin._baselines["themoviedb:279155:1"]["episodes"]["1"],
                               round(6 * 1024 ** 3 * 8 / 2700 / 1e6, 2))


if __name__ == "__main__":
    unittest.main()


def _notice(media="玫瑰战争 S01", drop_ver="OurTV · Vivid HDR", drop_rate=4.8,
            keep_ver="ADWeb · HDR10", keep_rate=14.9):
    return {"media": media, "drop_ver": drop_ver, "drop_rate": drop_rate,
            "keep_ver": keep_ver, "keep_rate": keep_rate}


class NoticeTest(unittest.TestCase):
    def test_same_pair_notified_once_despite_bitrate_jitter(self):
        plugin = make_plugin()
        self.assertEqual(len(plugin._dedupe_notices([_notice()])), 1)
        self.assertEqual(plugin._dedupe_notices([_notice(keep_rate=14.8)]), [])
        self.assertEqual(len(plugin._dedupe_notices([_notice(media="折月亮 S01")])), 1)

    def test_expired_entry_notifies_again(self):
        plugin = make_plugin()
        key = "玫瑰战争 S01|OurTV · Vivid HDR|ADWeb · HDR10"
        plugin.save_data(bs.SELECTION_NOTICE_KEY, {key: time.time() - bs.SELECTION_NOTICE_TTL_SECONDS - 1})
        self.assertEqual(len(plugin._dedupe_notices([_notice()])), 1)

    def test_render_grouped(self):
        text = bs.BitrateSelector._render_selection_notice([
            _notice(), _notice(drop_ver="CHDWEB", drop_rate=7.0),
            _notice(media="折月亮 S01", drop_ver="A", drop_rate=3.0, keep_ver="B", keep_rate=9.0),
        ])
        self.assertEqual(text, (
            "玫瑰战争 S01\n"
            "✅ 保留 ADWeb · HDR10　14.9 Mbps\n"
            "⛔ 淘汰 OurTV · Vivid HDR　4.8 Mbps（低 68%）\n"
            "⛔ 淘汰 CHDWEB　7.0 Mbps（低 53%）\n\n"
            "折月亮 S01\n"
            "✅ 保留 B　9.0 Mbps\n"
            "⛔ 淘汰 A　3.0 Mbps（低 67%）"))

    def test_effect_tokens_deduped(self):
        self.assertEqual(bs.BitrateSelector._dedupe_effect_tokens("HDR10 HDR"), "HDR10")
        self.assertEqual(bs.BitrateSelector._dedupe_effect_tokens("Vivid HDR"), "Vivid HDR")
        self.assertEqual(bs.BitrateSelector._dedupe_effect_tokens("DV HDR10 HDR"), "DV HDR10")
        label = bs.BitrateSelector._notify_version_label(
            NS(meta_info=NS(resource_team="ADWeb", resource_effect="HDR10 HDR"), torrent_info=NS(title="x")))
        self.assertEqual(label, "ADWeb · HDR10")


class InspectCommandTest(unittest.TestCase):
    def test_command_runs_inspect_and_reports_clean(self):
        plugin = make_plugin(_inspect_enabled=True, _inspect_days=30, _inspect_min_mbps=8.0)
        plugin._safe_low_bitrate_inspect = lambda: {"success": True, "low": 0, "triggered": []}
        threads = []
        original = bs.threading.Thread

        class _Sync:
            def __init__(self, target, **_):
                self.target = target

            def start(self):
                threads.append(self)
                self.target()

        bs.threading.Thread = _Sync
        try:
            plugin.on_plugin_action(NS(event_data={"action": "bitrate_inspect"}))
        finally:
            bs.threading.Thread = original
        self.assertEqual(len(threads), 1)
        self.assertIn("无需处理", plugin.messages[-1]["text"])
        self.assertFalse(plugin._inspect_lock.locked())

    def test_other_action_ignored_and_disabled_reported(self):
        plugin = make_plugin(_inspect_enabled=False)
        plugin.on_plugin_action(NS(event_data={"action": "mpstage_fix"}))
        self.assertEqual(plugin.messages, [])
        plugin.on_plugin_action(NS(event_data={"action": "bitrate_inspect"}))
        self.assertIn("未启用", plugin.messages[-1]["text"])


class SeedVerifyTest(unittest.TestCase):
    """复现玫瑰战争：换版后边车 mediainfo 未刷新，基线停在 4Mbps、下限被误清。"""

    def setUp(self):
        import json
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.season = Path(self.tmp.name) / "玫瑰战争 (2026)" / "Season 1"
        self.season.mkdir(parents=True)
        runtime_ticks = 45 * 60 * 10_000_000
        for ep, sidecar_mbps in ((5, 5.18), (14, 14.72), (22, 4.03)):
            stem = f"玫瑰战争 - S01E{ep:02d} - 第 {ep} 集"
            (self.season / f"{stem}-mediainfo.json").write_text(json.dumps([{"MediaSourceInfo": {
                "RunTimeTicks": runtime_ticks,
                "MediaStreams": [{"Type": "Video", "BitRate": int(sidecar_mbps * 1_000_000)}]}}]))
            (self.season / f"{stem}.strm").write_text(f"http://nas/{ep}")
        # E5、E22 实际已是约 15Mbps 的新版本；E14 未换版
        self.sizes = {"5": 15.2, "22": 15.0, "14": 14.72}

    def tearDown(self):
        self.tmp.cleanup()

    def _plugin(self):
        plugin = make_plugin()
        plugin._baselines = {"themoviedb:299276:1": {
            "episodes": {"5": 5.18, "14": 14.72, "22": 4.03},
            "ep_floors": {"22": [15.01, time.time() - 3600]},
        }}
        sizes = self.sizes

        def remote_size(info_path):
            ep = str(int(info_path.name.split("S01E")[1][:2]))
            return True, int(sizes[ep] * 1_000_000 / 8 * 45 * 60)

        plugin._strm_remote_size = remote_size
        plugin._seed_verify_budget = 10
        bs.SEED_VERIFY_INTERVAL_SECONDS = 0
        return plugin

    def test_stale_sidecar_corrected_by_actual_file(self):
        plugin = self._plugin()
        seeded, corrected = plugin._seed_subscription_baseline(make_subscribe(), Path(self.tmp.name))
        entry = plugin._baselines["themoviedb:299276:1"]
        self.assertAlmostEqual(entry["episodes"]["5"], 15.2, places=1)
        self.assertAlmostEqual(entry["episodes"]["22"], 15.0, places=1)
        self.assertEqual(entry["episodes"]["14"], 14.72)
        self.assertNotIn("22", entry["ep_floors"])
        self.assertEqual((seeded, corrected), (0, 2))

    def test_floor_kept_when_verification_fails(self):
        plugin = self._plugin()
        plugin._strm_remote_size = lambda info_path: (True, None)
        plugin._seed_subscription_baseline(make_subscribe(), Path(self.tmp.name))
        entry = plugin._baselines["themoviedb:299276:1"]
        self.assertIn("22", entry["ep_floors"])
        self.assertEqual(entry["episodes"]["22"], 4.03)


class RecordBaselineTest(unittest.TestCase):
    """复现多喜一家人 E06：10.4GB 整季包只为 E06 下载，旧版按单集估出 30Mbps 虚高下限。"""

    def test_pack_for_single_episode_uses_pack_coverage(self):
        plugin = make_plugin()
        plugin._baselines = {"themoviedb:1:1": {"episodes": {"6": 1.5}}}
        size = int(10.4 * 1024 ** 3)
        ctx = NS(
            torrent_info=NS(size=size, title="Still.a.Family.S01.HHWEB", pri_order=100),
            meta_info=NS(begin_season=1, episode_list=[]),
            media_info=NS(media_source="themoviedb", media_id="1", tmdb_id=1, title="多喜一家人", year="2026"),
        )
        plugin._record_wash_baseline(NS(context=ctx, episodes={6}))
        entry = plugin._baselines["themoviedb:1:1"]
        pack_rate = size * 8 / (25 * 45 * 60) / 1_000_000   # 全季 25 集各 45 分钟
        self.assertAlmostEqual(entry["episodes"]["6"], round(pack_rate, 2), places=2)
        self.assertFalse(entry.get("ep_floors"))
        single_ep_rate = size * 8 / (45 * 60) / 1_000_000
        self.assertGreater(single_ep_rate, 30)   # 旧算法的虚高值

    def test_old_floors_migrated_once(self):
        plugin = make_plugin()
        plugin._baselines = {"k": {"episodes": {"6": 8.0}, "ep_floors": {"6": [30.27, time.time()]},
                                   "full_floor": [20.0, time.time()]}}
        plugin._migrate_inflated_floors()
        self.assertEqual(plugin._baselines["k"]["ep_floors"], {})
        self.assertNotIn("full_floor", plugin._baselines["k"])
        plugin._baselines["k"]["ep_floors"] = {"7": [12.0, time.time()]}
        plugin._migrate_inflated_floors()   # 第二次不再清除新记录的下限
        self.assertIn("7", plugin._baselines["k"]["ep_floors"])


class MisparsedPackTest(unittest.TestCase):
    """复现谷雨：20GB 整季包被识别为仅 E01，估出 62.6Mbps 后以「洗版升级」下载。"""

    TITLE = "[TV Series/HD]The Silent Storm S01 2026 2160p YK WEB-DL H265 AAC-ADWeb"
    DESC = "谷雨/The Silent Storm | 4K | 集数：E01-E30（共30集）"

    def _ctx(self, title=None, desc=None, size_gb=20.12, eps=(1,)):
        return NS(
            torrent_info=NS(size=int(size_gb * 1024 ** 3), title=title or self.TITLE,
                            description=self.DESC if desc is None else desc, pri_order=100),
            meta_info=NS(begin_season=1, begin_episode=1, end_episode=None, total_episode=None,
                         episode_list=list(eps)),
            media_info=NS(tmdb_id=279155, title="谷雨", year="2026",
                          media_source="themoviedb", media_id="279155"),
            selected_episodes=None, allowed_episodes=None,
        )

    def test_title_range_parsing(self):
        rng = bs.BitrateSelector._title_episode_range
        self.assertEqual(rng(NS(title="x", description="集数：E01-E30（共30集）")), set(range(1, 31)))
        self.assertEqual(rng(NS(title="Show 全24集", description="")), set(range(1, 25)))
        self.assertEqual(rng(NS(title="Show 第1-12集", description="")), set(range(1, 13)))
        self.assertEqual(rng(NS(title="Show S01E05 2160p WEB-DL H265-ADWeb", description="")), set())

    def test_estimate_uses_title_range(self):
        plugin = make_plugin()
        plugin._episode_runtimes = lambda media, season: {ep: 45 for ep in range(1, 31)}
        mbps, _ = plugin._estimate_bitrate_ex(self._ctx(), {1})
        self.assertAlmostEqual(mbps, 20.12 * 1024 ** 3 * 8 / (30 * 45 * 60) / 1e6, places=2)

    def test_wash_rejects_misparsed_pack(self):
        plugin = make_plugin()
        plugin._episode_runtimes = lambda media, season: {ep: 45 for ep in range(1, 31)}
        plugin._baselines = {"themoviedb:279155:1": {"episodes": {str(e): 16.0 for e in range(1, 31)}}}
        subscribe = make_subscribe(id=122, name="谷雨", total_episode=30, media_id="279155",
                                   episode_priority={str(e): 96 for e in range(1, 31)})
        self.assertFalse(plugin._wash_admit(subscribe, self._ctx(), 100, lambda *a: True))

    def test_implausible_estimate_rejected_not_fallback(self):
        plugin = make_plugin()
        plugin._episode_runtimes = lambda media, season: {ep: 45 for ep in range(1, 31)}
        plugin._baselines = {"themoviedb:279155:1": {"episodes": {str(e): 16.0 for e in range(1, 31)}}}
        subscribe = make_subscribe(id=122, name="谷雨", total_episode=30, media_id="279155",
                                   episode_priority={str(e): 96 for e in range(1, 31)})
        ctx = self._ctx(desc="")  # 无任何范围线索
        mbps, source = plugin._estimate_bitrate_ex(ctx, {1})
        self.assertEqual((mbps, source), (None, "implausible"))
        self.assertFalse(plugin._wash_admit(subscribe, ctx, 100, lambda *a: True))

    def test_remux_not_capped(self):
        plugin = make_plugin()
        ctx = self._ctx(title="Blue.Planet.II.S01E01.2160p.BluRay.REMUX.HEVC", desc="", size_gb=40)
        mbps, source = plugin._estimate_bitrate_ex(ctx, {1})
        self.assertGreater(mbps, 50)
        self.assertNotEqual(source, "implausible")


class TransferBaselineTest(unittest.TestCase):
    """入库后按实际文件大小记录基线：复现谷雨 E01 被 801MB 低码版覆盖后的修正。"""

    def _event(self, size, name="谷雨 - S01E01 - 第 1 集.mkv", eps=(1,), file_count=1, success=True):
        return NS(
            transferinfo=NS(success=success, file_count=file_count, total_size=size,
                            target_item=NS(size=size, name=name)),
            meta=NS(begin_season=1, begin_episode=eps[0] if eps else None, episode_list=list(eps)),
            mediainfo=NS(type=MediaType.TV, tmdb_id=279155, media_source="themoviedb", media_id="279155"),
        )

    def _plugin(self):
        plugin = make_plugin()
        plugin._episode_runtimes = lambda media, season: {ep: 45 for ep in range(1, 31)}
        plugin._baselines = {"themoviedb:279155:1": {
            "episodes": {"1": 17.47, "2": 15.72},
            "ep_floors": {"1": [62.61, time.time()]},
        }}
        return plugin

    def test_actual_file_overrides_stale_baseline_and_floor(self):
        plugin = self._plugin()
        size = int(801.62 * 1024 ** 2)
        key, eps, mbps = plugin._record_transferred_baseline(self._event(size))
        self.assertEqual((key, eps), ("themoviedb:279155:1", [1]))
        self.assertAlmostEqual(mbps, size * 8 / (45 * 60) / 1e6, places=2)
        entry = plugin._baselines[key]
        self.assertEqual(entry["episodes"]["1"], mbps)
        self.assertEqual(entry["episodes"]["2"], 15.72)
        self.assertNotIn("1", entry["ep_floors"])
        self.assertIs(plugin.get_data(bs.WASH_BASELINES_KEY), plugin._baselines)

    def test_skips_unusable_events(self):
        plugin = self._plugin()
        big = 6 * 1024 ** 3
        self.assertIsNone(plugin._record_transferred_baseline(self._event(big, file_count=4)))
        self.assertIsNone(plugin._record_transferred_baseline(self._event(2048, name="x.strm")))
        self.assertIsNone(plugin._record_transferred_baseline(self._event(big, success=False)))
        self.assertIsNone(plugin._record_transferred_baseline(self._event(big, eps=())))
        self.assertIsNone(plugin._record_transferred_baseline(None))
        self.assertEqual(plugin._baselines["themoviedb:279155:1"]["episodes"]["1"], 17.47)

    def test_handler_respects_switches(self):
        plugin = self._plugin()
        plugin._wash_enabled = False
        plugin.on_transfer_complete(NS(event_data=self._event(6 * 1024 ** 3)))
        self.assertEqual(plugin._baselines["themoviedb:279155:1"]["episodes"]["1"], 17.47)
        plugin._wash_enabled = True
        plugin.on_transfer_complete(NS(event_data=self._event(6 * 1024 ** 3)))
        self.assertAlmostEqual(plugin._baselines["themoviedb:279155:1"]["episodes"]["1"],
                               round(6 * 1024 ** 3 * 8 / 2700 / 1e6, 2))


if __name__ == "__main__":
    unittest.main()
