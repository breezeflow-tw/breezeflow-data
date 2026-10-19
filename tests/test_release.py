"""在本機暫存 Git 遠端模擬 CI 競爭；不連線、也不推送正式儲存庫。"""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import release


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.remote = self.root / "remote.git"
        self.a, self.b = self.root / "a", self.root / "b"
        self.run_git(self.root, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.run_git(self.root, "clone", str(self.remote), str(self.a))
        self.configure(self.a)
        self.write(self.a, "web/index.html", "舊介面")
        self.write(self.a, "data/history/etf.csv", "code,name\n0050,測試基金\n")
        self.write(self.a, "scripts/fetch_etf.py", "# 資料程式初版\n")
        self.payload = {"source": "live", "count": 1, "trade_date": "2026-10-16",
                        "items": [{"code": "0050", "price": 20}]}
        self.data(self.a, 20)
        self.run_git(self.a, "add", ".")
        self.run_git(self.a, "commit", "-m", "測試初始資料")
        self.run_git(self.a, "push", "origin", "main")
        self.run_git(self.root, "clone", str(self.remote), str(self.b))
        self.configure(self.b)
        self.verified = []
        self.refreshed = []

    @staticmethod
    def run_git(root, *args):
        return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.PIPE).decode("utf-8").strip()

    def configure(self, root):
        self.run_git(root, "config", "user.name", "測試")
        self.run_git(root, "config", "user.email", "test@example.invalid")
        self.run_git(root, "config", "core.autocrlf", "false")

    @staticmethod
    def write(root, path, text):
        dest = root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, "utf-8")

    def data(self, root, price):
        payload = dict(self.payload, items=[{"code": "0050", "price": price}])
        self.write(root, "web/data/latest.json", json.dumps(payload))

    def remote_change(self, path="web/index.html", text="新介面"):
        self.write(self.b, path, text)
        self.run_git(self.b, "add", ".")
        self.run_git(self.b, "commit", "-m", "協作者更新")
        self.run_git(self.b, "push", "origin", "main")

    def refresh(self, root):
        self.refreshed.append(root)
        self.data(root, 30)

    def publish(self):
        return release.publish_data(self.a, verify_fn=self.verified.append, refresh_fn=self.refresh)

    def test_normal_publish_only_data(self):
        self.data(self.a, 21)
        sha = self.publish()
        self.assertEqual(sha, self.run_git(self.remote, "rev-parse", "main"))
        self.assertEqual(self.run_git(self.a, "show", "--pretty=", "--name-only", "HEAD"), "web/data/latest.json")
        self.assertEqual(self.verified, [])

    def test_remote_frontend_and_local_data_both_survive(self):
        self.data(self.a, 21)
        self.remote_change()
        self.publish()
        self.assertEqual((self.a / "web/index.html").read_text("utf-8"), "新介面")
        self.assertEqual(release.validate_data(self.a)["items"][0]["price"], 21)
        self.assertEqual(len(self.verified), 1)
        self.assertEqual(self.refreshed, [])

    def test_pipeline_change_refreshes_after_verification(self):
        self.data(self.a, 21)
        self.remote_change("scripts/fetch_etf.py", "# 資料程式新版\n")
        self.publish()
        self.assertEqual(len(self.verified), 1)
        self.assertEqual(len(self.refreshed), 1)
        self.assertEqual(release.validate_data(self.a)["items"][0]["price"], 30)

    def test_conflicting_data_never_overwrites_remote(self):
        self.data(self.a, 21)
        self.data(self.b, 22)
        self.remote_change("README.md", "資料另有更新")
        before = self.run_git(self.remote, "rev-parse", "main")
        with self.assertRaisesRegex(RuntimeError, "實際衝突"):
            self.publish()
        self.assertEqual(before, self.run_git(self.remote, "rev-parse", "main"))
        self.assertFalse((self.a / ".git/rebase-merge").exists())

    def test_workflow_change_requires_new_run(self):
        self.data(self.a, 21)
        self.remote_change(".github/workflows/daily.yml", "# 新工作流程\n")
        with self.assertRaisesRegex(RuntimeError, "流程本身已更新"):
            self.publish()

    def test_unrelated_local_change_is_not_committed(self):
        self.data(self.a, 21)
        self.write(self.a, "web/index.html", "未提交程式")
        before = self.run_git(self.a, "rev-parse", "HEAD")
        with self.assertRaisesRegex(ValueError, "非資料檔案"):
            self.publish()
        self.assertEqual(before, self.run_git(self.a, "rev-parse", "HEAD"))

    def test_push_race_is_safely_retried(self):
        self.data(self.a, 21)
        original = release.git
        raced = []
        def racing(root, *args, **kwargs):
            if args[0] == "push" and not raced:
                raced.append(True)
                self.remote_change()
            return original(root, *args, **kwargs)
        with patch.object(release, "git", racing):
            self.publish()
        self.assertTrue(raced)
        self.assertEqual((self.a / "web/index.html").read_text("utf-8"), "新介面")
        self.assertEqual(release.validate_data(self.a)["items"][0]["price"], 21)

    def test_manifest_matches_artifacts_and_rebuilds_database(self):
        result = release.write_manifest(self.a)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["trade_date"], "2026-10-16")
        self.assertEqual(result["commit"], self.run_git(self.a, "rev-parse", "HEAD"))
        for name, digest in result["sha256"].items():
            self.assertEqual(hashlib.sha256((self.a / "web" / name).read_bytes()).hexdigest(), digest)
        with closing(sqlite3.connect(self.a / "web/data/etf.db")) as con:
            self.assertEqual(con.execute("SELECT code FROM etf").fetchone()[0], "0050")

    def test_fixture_or_empty_data_cannot_be_published(self):
        for changes in ({"source": "fixture"}, {"items": []}, {"count": 2}):
            self.write(self.a, "web/data/latest.json", json.dumps(dict(self.payload, **changes)))
            with self.assertRaises(ValueError):
                release.validate_data(self.a)


if __name__ == "__main__":
    unittest.main()
