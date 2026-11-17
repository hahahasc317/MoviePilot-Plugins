"""综艺正片净化单测（宿主模块以桩替代）：python3 -m unittest discover tests"""
import enum
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1] / "plugins.v3" / "varietypurifier"


def _stub(name, **attrs):
    module = sys.modules.get(name) or types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


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


_types = sys.modules.get("app.schemas.types")
MediaType = getattr(_types, "MediaType", None) or enum.Enum("MediaType", {"TV": "电视剧", "MOVIE": "电影"})
for pkg in ("app", "app.sdk", "app.schemas"):
    _stub(pkg)
_stub("app.plugins", _PluginBase=_PluginBase)
_stub("app.sdk.events", Event=object, eventmanager=NS(register=lambda *a, **k: (lambda f: f)))
_stub("app.sdk.logging", logger=NS(**{n: (lambda *a, **k: None) for n in ("info", "warning", "error", "debug")}))
_stub("app.schemas.types",
      ChainEventType=NS(ResourceSelection=1, ResourceDownload=2, TransferIntercept=3),
      EventType=NS(DownloadAdded=4), MediaType=MediaType)

_pkg_spec = importlib.util.spec_from_file_location(
    "varietypurifier", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
vp = importlib.util.module_from_spec(_pkg_spec)
sys.modules["varietypurifier"] = vp
_pkg_spec.loader.exec_module(vp)
purifier = sys.modules["varietypurifier.purifier"]
KW = purifier.DEFAULT_KEYWORDS


class KeywordTest(unittest.TestCase):
    def test_english_word_boundaries(self):
        self.assertIsNone(purifier.match_keyword("Impure.Water.S01E01.mkv", KW))
        self.assertIsNone(purifier.match_keyword("Purely.Variety.E03.mkv", KW))
        self.assertIsNone(purifier.match_keyword("Stopwatch.Show.S01E05.mkv", KW))
        self.assertEqual(purifier.match_keyword("Show.S01E05.Pure.Version.mkv", KW), "Pure")
        self.assertEqual(purifier.match_keyword("Show.E05.Extra Version.mkv", KW), "Extra.Version")
        self.assertEqual(purifier.match_keyword("Show.E05.Behind_The_Scenes.mkv", KW), "Behind.The.Scenes")
        self.assertEqual(purifier.match_keyword("Show.Watch.Party.E01.mkv", KW), "Watch")

    def test_chinese_substring(self):
        self.assertEqual(purifier.match_keyword("现在就出发.S04E05.加更版.mkv", KW), "加更")
        self.assertIsNone(purifier.match_keyword("现在就出发.S04E05.2160p.mkv", KW))


def _ctx(title, category="综艺"):
    return NS(torrent_info=NS(title=title, description=""),
              media_info=NS(type=MediaType.TV, category=category, library_category=category))


def make_plugin():
    plugin = vp.VarietyPurifier()
    plugin._enabled = True
    plugin._block_download = True
    plugin._exempt_manual = True
    plugin._scope_variety_only = True
    plugin._notify = False
    plugin._keywords = KW
    return plugin


class SelectionTest(unittest.TestCase):
    def test_drops_non_feature_and_keeps_clean(self):
        clean, extra = _ctx("Go.Now.S04E05.2160p"), _ctx("Go.Now.S04E05.加更.2160p")
        data = NS(contexts=[clean, extra], origin="Subscribe|1", updated=False, updated_contexts=None, source="")
        make_plugin().on_resource_selection(NS(event_data=data))
        self.assertTrue(data.updated)
        self.assertEqual(data.updated_contexts, [clean])

    def test_builds_on_previous_handler_result(self):
        a, b, c = _ctx("A.E01"), _ctx("B.E01.花絮"), _ctx("C.E01")
        data = NS(contexts=[a, b, c], origin="Subscribe|1", updated=True, updated_contexts=[a, b], source="x")
        make_plugin().on_resource_selection(NS(event_data=data))
        self.assertEqual(data.updated_contexts, [a])

    def test_manual_and_non_variety_untouched(self):
        extra = _ctx("Go.Now.S04E05.加更")
        data = NS(contexts=[extra], origin="Manual", updated=False, updated_contexts=None, source="")
        make_plugin().on_resource_selection(NS(event_data=data))
        self.assertFalse(data.updated)
        drama = _ctx("Drama.E05.花絮", category="国产剧")
        data = NS(contexts=[drama], origin="Subscribe|1", updated=False, updated_contexts=None, source="")
        make_plugin().on_resource_selection(NS(event_data=data))
        self.assertFalse(data.updated)


if __name__ == "__main__":
    unittest.main()
