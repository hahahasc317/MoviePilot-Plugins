"""MPStageFixer stagefix 纯逻辑单测（宿主模块以桩替代）：python3 -m unittest discover tests"""
import os
import sys
import tempfile
import time
import types
import unittest
import importlib.util
from pathlib import Path

for name in ("p115client", "app", "app.sdk", "app.sdk.logging"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["p115client"].P115Client = object
sys.modules["app.sdk.logging"].logger = types.SimpleNamespace(
    info=lambda *a, **k: None, warning=lambda *a, **k: None, error=lambda *a, **k: None)
sys.modules.setdefault("requests", types.ModuleType("requests"))

_PATH = Path(__file__).resolve().parents[1] / "plugins.v3" / "mpstagefixer" / "stagefix.py"
_spec = importlib.util.spec_from_file_location("stagefix", _PATH)
stagefix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stagefix)

SHA = "a" * 40


class ParseLogSha1Test(unittest.TestCase):
    def test_extracts_matching_event(self):
        line = f"INFO {{'id': 1, 'file_id': 123, 'file_name': 'x.mkv', 'sha1': '{SHA}'}}"
        self.assertEqual(stagefix.parse_log_sha1(line, 123), SHA.upper())

    def test_quoted_file_id(self):
        line = f"{{'id': '1', 'file_id': '123', 'sha1': '{SHA}'}}"
        self.assertEqual(stagefix.parse_log_sha1(line, 123), SHA.upper())

    def test_other_fid_ignored(self):
        line = f"{{'id': 1, 'file_id': 1234, 'sha1': '{SHA}'}}"
        self.assertIsNone(stagefix.parse_log_sha1(line, 123))

    def test_never_evaluates(self):
        marker = os.path.join(tempfile.gettempdir(), "mpstage_eval_marker")
        if os.path.exists(marker):
            os.remove(marker)
        payload = ("{'id': [c for c in ().__class__.__base__.__subclasses__() "
                   f"if c.__name__=='x'], 'file_id': 1, 'sha1': '{SHA}'}}")
        stagefix.parse_log_sha1(payload, 1)
        self.assertFalse(os.path.exists(marker))
        self.assertNotIn("eval(", _PATH.read_text(encoding="utf-8"))


class FreshnessTest(unittest.TestCase):
    def test_item_mtime_picks_latest(self):
        self.assertEqual(stagefix._item_mtime({"tp": "100", "te": "200", "t": "2026-11-17 20:00"}), 200)
        self.assertIsNone(stagefix._item_mtime({"t": "2026-11-17 20:00"}))

    def test_is_fresh(self):
        now = 10_000
        self.assertTrue(stagefix.is_fresh({"mtime": now - 60}, now))
        self.assertFalse(stagefix.is_fresh({"mtime": now - 3600}, now))
        self.assertFalse(stagefix.is_fresh({"mtime": None}, now))

    def test_fix_one_skips_fresh(self):
        finding = {"name": "a.mkv**.mp-stage-ab", "mtime": int(time.time())}
        result, _ = stagefix.fix_one(None, finding)
        self.assertEqual(result, "fresh")


class OrphanSidecarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.old = time.time() - 3 * 86400

    def tearDown(self):
        self.tmp.cleanup()

    def _touch(self, rel, old=True):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_text("x")
        if old:
            os.utime(path, (self.old, self.old))
        return path

    def test_thumb_kept_and_real_orphan_found(self):
        self._touch("Show/Season 1/S01E01.strm")
        self._touch("Show/Season 1/S01E01-thumb.jpg")
        self._touch("Show/Season 1/S01E01-mediainfo.json")
        self._touch("Show/Season 1/poster.jpg")
        orphan = self._touch("Show/Season 1/S01E02-mediainfo.json")
        candidates, _ = stagefix.find_orphan_sidecars(self.root)
        self.assertEqual(candidates, [orphan])

    def test_dir_without_any_strm_is_skipped(self):
        self._touch("Show/Season 2/S02E01-mediainfo.json")
        self._touch("Show/Season 2/S02E01.nfo")
        candidates, guard = stagefix.find_orphan_sidecars(self.root)
        self.assertEqual(candidates, [])
        self.assertEqual(guard, 2)

    def test_fresh_orphan_guarded(self):
        self._touch("Show/Season 1/S01E01.strm")
        self._touch("Show/Season 1/S01E09.nfo", old=False)
        candidates, guard = stagefix.find_orphan_sidecars(self.root)
        self.assertEqual((candidates, guard), ([], 1))

    def test_brake_deletes_nothing(self):
        self._touch("Show/Season 1/S01E01.strm")
        paths = [self._touch(f"Show/Season 1/S01E{n:02d}.nfo") for n in range(2, 8)]
        original = stagefix.ORPHAN_MEDIA_ROOT
        stagefix.ORPHAN_MEDIA_ROOT = self.root
        try:
            deleted, _, braked = stagefix.clean_orphan_sidecars(apply=True, max_delete=3)
        finally:
            stagefix.ORPHAN_MEDIA_ROOT = original
        self.assertEqual((deleted, braked), (0, True))
        self.assertTrue(all(os.path.exists(p) for p in paths))

    def test_within_limit_deletes(self):
        self._touch("Show/Season 1/S01E01.strm")
        orphan = self._touch("Show/Season 1/S01E05.nfo")
        original = stagefix.ORPHAN_MEDIA_ROOT
        stagefix.ORPHAN_MEDIA_ROOT = self.root
        try:
            deleted, _, braked = stagefix.clean_orphan_sidecars(apply=True, max_delete=3)
        finally:
            stagefix.ORPHAN_MEDIA_ROOT = original
        self.assertEqual((deleted, braked), (1, False))
        self.assertFalse(os.path.exists(orphan))


if __name__ == "__main__":
    unittest.main()
