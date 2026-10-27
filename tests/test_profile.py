from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("profile_update", ROOT / "scripts/update_profile.py")
profile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profile)
NOW = datetime(2026, 10, 27, 8, 0, 0, tzinfo=timezone.utc)
CONFIG = json.loads((ROOT / "profile.json").read_text())


def item(number, repository="Tencent/WeKnora"):
    return {"id": number, "number": number, "repository_url": "https://api.github.com/repos/" + repository}


def repo(name, **overrides):
    owner, short = name.split("/")
    return {"name": short, "full_name": name, "owner": {"login": owner}, "private": False, "visibility": "public",
            "fork": False, "archived": False, "size": 100, "language": "Python", "stargazers_count": 9876,
            "pushed_at": "2026-10-27T00:00:00Z", **overrides}


class FakeAPI:
    def __init__(self, items=None):
        self.items = items if items is not None else [item(4502), item(4505), item(2, "panjiedev/practice")]
        self.calls = []
        self.repos = [repo("panjiedev/job-intel-agent"), repo("panjiedev/learning")]
        self.metadata = {"Tencent/WeKnora": repo("Tencent/WeKnora"),
                         "panjiedev/job-intel-agent": repo("panjiedev/job-intel-agent", stargazers_count=1)}
        self.languages = {"panjiedev/job-intel-agent": {"Python": 16698, "TypeScript": 17875, "JavaScript": 559, "CSS": 488},
                          "panjiedev/learning": {"Java": 27319}}

    def public_repository(self, name):
        result = self.metadata[name]
        if result["private"] or result["visibility"] != "public":
            raise ValueError("仅允许公开项目。")
        return result

    def get(self, path, **params):
        self.calls.append((path, params))
        if path == "search/issues":
            assert "is:public" in params["q"]
            start = (params["page"] - 1) * 100
            return {"incomplete_results": False, "total_count": len(self.items), "items": self.items[start:start + 100]}
        if path == "users/panjiedev/repos":
            return self.repos if params["page"] == 1 else []
        if path.startswith("repos/") and path.endswith("/languages"):
            return self.languages[path[len("repos/"):-len("/languages")]]
        raise AssertionError((path, params))


class ProfileTests(unittest.TestCase):
    def test_external_merged_prs_and_project_stars_share_one_snapshot(self):
        data = profile.collect(FakeAPI(), CONFIG, NOW)
        self.assertEqual(len(data["contributions"]), 1)
        self.assertEqual(data["contributions"][0]["merged_prs"], 2)
        self.assertEqual(data["contributions"][0]["stars"], 9876)
        self.assertEqual(data["featured"][0]["stars"], 1)
        self.assertEqual(data["featured"][1]["stars"], 9876)

    def test_pr_pagination_continues_beyond_first_hundred(self):
        api = FakeAPI([item(n) for n in range(101)])
        groups = profile.merged_pull_requests(api, "panjiedev", NOW)
        self.assertEqual(groups[0]["merged_prs"], 101)
        self.assertEqual([params["page"] for path, params in api.calls], [1, 2])

    def test_large_search_is_split_by_creation_time(self):
        class SplitAPI(FakeAPI):
            def get(self, path, **params):
                if "created:" not in params["q"]:
                    return {"incomplete_results": False, "total_count": 1001, "items": []}
                start, end = params["q"].split("created:")[1].split("..")
                lower = datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp()
                upper = datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp()
                values = [item(n) for n in range(1001) if lower <= n <= upper]
                offset = (params["page"] - 1) * 100
                return {"incomplete_results": False, "total_count": len(values), "items": values[offset:offset + 100]}
        groups = profile.merged_pull_requests(SplitAPI(), "panjiedev", datetime.fromtimestamp(1001, timezone.utc))
        self.assertEqual(groups[0]["merged_prs"], 1001)

    def test_incomplete_search_is_rejected(self):
        class Incomplete(FakeAPI):
            def get(self, *args, **kwargs):
                return {"incomplete_results": True, "total_count": 5, "items": []}
        with self.assertRaisesRegex(ValueError, "不完整"):
            profile.collect(Incomplete(), CONFIG, NOW)

    def test_duplicate_pagination_is_rejected(self):
        api = FakeAPI([item(1), item(1)])
        with self.assertRaisesRegex(ValueError, "重复"):
            profile.collect(api, CONFIG, NOW)

    def test_language_scope_excludes_private_forks_courses_empty_and_profile(self):
        api = FakeAPI()
        api.repos += [repo("panjiedev/private", private=True), repo("panjiedev/fork", fork=True),
                      repo("panjiedev/archived", archived=True), repo("panjiedev/skills-training"),
                      repo("panjiedev/empty", size=0), repo("panjiedev/panjiedev"), repo("someone/foreign")]
        data = profile.collect(api, CONFIG, NOW)
        self.assertEqual(data["languages"]["total_bytes"], 62939)
        self.assertEqual(data["languages"]["included_repositories"], ["panjiedev/job-intel-agent", "panjiedev/learning"])
        self.assertEqual([r["percent"] for r in data["languages"]["shares"]], [43.4, 28.4, 26.5, .9, .8])

    def test_language_rounding_preserves_tail_and_one_hundred_percent(self):
        shares = profile.language_percentages({f"L{n}": n + 1 for n in range(12)})
        self.assertEqual(len(shares), 6)
        self.assertEqual(shares[-1]["name"], "其他")
        self.assertAlmostEqual(sum(r["percent"] for r in shares), 100)
        self.assertEqual(sum(r["bytes"] for r in shares), sum(range(1, 13)))
        self.assertEqual(profile.language_percentages({}), [])

    def test_private_featured_repository_cannot_be_published(self):
        api = FakeAPI()
        api.metadata["panjiedev/job-intel-agent"]["private"] = True
        with self.assertRaisesRegex(ValueError, "公开"):
            profile.collect(api, CONFIG, NOW)

    def test_rendered_svg_is_valid_and_external_text_is_escaped(self):
        data = profile.collect(FakeAPI(), CONFIG, NOW)
        data["featured"][0]["description"] = '<script>alert("x")</script> & 安全'
        outputs = profile.render_outputs(CONFIG, data)
        self.assertEqual(len([p for p in outputs if p.endswith(".svg")]), 8)
        for path, source in outputs.items():
            if path.endswith(".svg"):
                ET.fromstring(source)
                self.assertNotIn("<script>", source)
        for theme in ("light", "dark"):
            self.assertIn("Star 9,876", outputs[f"assets/project-2-{theme}.svg"])
            self.assertIn("9,876", outputs[f"assets/contributions-{theme}.svg"])
        self.assertIn("UTC+8", outputs["README.md"])
        self.assertIn("2026-10-27 16:00", outputs["README.md"])
        self.assertIn('width="400"', outputs["README.md"])

    def test_fetch_failure_preserves_all_previous_output(self):
        class Failure(FakeAPI):
            def get(self, *args, **kwargs):
                raise RuntimeError("模拟 API 失败")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "profile.json").write_text(json.dumps(CONFIG))
            (root / "README.md").write_text("previous readme")
            (root / "assets").mkdir()
            (root / "assets/data.json").write_text("previous data")
            with self.assertRaises(RuntimeError):
                profile.update(root, Failure(), NOW)
            self.assertEqual((root / "README.md").read_text(), "previous readme")
            self.assertEqual((root / "assets/data.json").read_text(), "previous data")

    def test_successful_update_uses_same_data_in_all_outputs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "profile.json").write_text(json.dumps(CONFIG))
            data = profile.update(root, FakeAPI(), NOW)
            self.assertEqual(json.loads((root / "assets/data.json").read_text()), data)
            self.assertIn("9,876", (root / "assets/contributions-light.svg").read_text())
            self.assertIn("Star 9,876", (root / "assets/project-2-light.svg").read_text())

    def test_daily_schedule_is_beijing_half_past_midnight(self):
        workflow = (ROOT / ".github/workflows/update-profile.yml").read_text()
        self.assertIn("cron: '30 16 * * *'", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("needs: validate", workflow)
        self.assertNotIn("continue-on-error", workflow)
        self.assertEqual(datetime(2026, 10, 27, 16, 30, tzinfo=timezone.utc).astimezone(profile.BEIJING).strftime("%Y-%m-%d %H:%M"), "2026-10-28 00:30")


if __name__ == "__main__":
    unittest.main()
