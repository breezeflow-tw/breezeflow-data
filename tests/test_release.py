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

    def test_audit_blocks_closed_day_before_commit(self):
        self.payload["trade_date"] = "2026-10-23"
        self.data(self.a, 21)
        before = self.run_git(self.remote, "rev-parse", "main")
        with self.assertRaisesRegex(ValueError, "J3"):
            self.publish()
        self.assertEqual(self.run_git(self.remote, "rev-parse", "main"), before)

    def test_audit_blocks_all_missing_volume(self):
        payload = dict(self.payload, count=20, items=[{"code": f"TEST{i}", "price": 20} for i in range(20)])
        self.write(self.a, "web/data/latest.json", json.dumps(payload))
        with self.assertRaisesRegex(ValueError, "J3"):
            release.validate_data(self.a)

    def test_audit_reads_csv_not_stale_local_database(self):
        self.write(self.a, "data/history/daily/2026-10-16.csv", "code,date,price\n0050,2026-10-16,20\n")
        self.write(self.a, "data/history/exchange_daily/2026-10-16.csv", "code,date,close,dataset,market,source_url,first_observed_at,observed_at\n0050,2026-10-16,30,測試,上市,https://example.test,2026-10-16,2026-10-16\n")
        with self.assertRaisesRegex(ValueError, "P1"):
            release.validate_data(self.a)

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

    def test_product_override_change_refreshes_data(self):
        self.data(self.a, 21)
        self.remote_change("data/product_overrides.json", '{"00984D": {"category": "主動式"}}')
        self.publish()
        self.assertEqual(len(self.verified), 1)
        self.assertEqual(len(self.refreshed), 1)

    def test_workflow_change_requires_new_run(self):
        self.data(self.a, 21)
        self.remote_change(".github/workflows/daily.yml", "# 新工作流程\n")
        with self.assertRaisesRegex(RuntimeError, "流程本身已更新"):
            self.publish()

    def test_product_reference_change_refreshes_data(self):
        self.data(self.a, 21)
        self.remote_change("data/product_reference.json", '{"funds": {}}')
        self.publish()
        self.assertEqual(len(self.verified), 1)
        self.assertEqual(len(self.refreshed), 1)

    def test_stock_name_reference_change_refreshes_data(self):
        self.data(self.a, 21)
        self.remote_change("data/stock_name_reference.json", '{"items": {}}')
        self.publish()
        self.assertEqual(len(self.verified), 1)
        self.assertEqual(len(self.refreshed), 1)

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
        self.assertIn("data/summary.json", result["sha256"])
        detail = self.a / "web/data/etf/0050.json"
        self.assertEqual(json.loads(detail.read_text("utf-8"))["item"], self.payload["items"][0])
        lines = f"0050.json {hashlib.sha256(detail.read_bytes()).hexdigest()}\n"
        self.assertEqual(result["etf_detail"], {"count": 1, "sha256": hashlib.sha256(lines.encode("utf-8")).hexdigest()})
        with closing(sqlite3.connect(self.a / "web/data/etf.db")) as con:
            self.assertEqual(con.execute("SELECT code FROM etf").fetchone()[0], "0050")

    def test_fixture_or_empty_data_cannot_be_published(self):
        for changes in ({"source": "fixture"}, {"items": []}, {"count": 2}):
            self.write(self.a, "web/data/latest.json", json.dumps(dict(self.payload, **changes)))
            with self.assertRaises(ValueError):
                release.validate_data(self.a)

    def test_older_market_date_cannot_be_committed_or_published(self):
        before = self.run_git(self.remote, "rev-parse", "main")
        self.write(self.a, "data/history/daily/2026-10-19.csv", "code,date,price\n0050,2026-10-19,21\n")
        self.data(self.a, 21)  # 網站仍是 10/16，歷史已有 10/19
        with self.assertRaisesRegex(ValueError, "早於已保存行情 2026-10-19"):
            self.publish()
        self.assertEqual(self.run_git(self.remote, "rev-parse", "main"), before)
        self.assertEqual(self.run_git(self.a, "rev-parse", "HEAD"), before)
        with self.assertRaisesRegex(ValueError, "停止發布"):
            release.write_manifest(self.a)

    def test_same_or_newer_market_date_is_valid(self):
        self.write(self.a, "data/history/daily/2026-10-16.csv", "code,date,price\n0050,2026-10-16,20\n")
        for day in ("2026-10-16", "2026-10-19"):
            with self.subTest(day=day):
                self.write(self.a, "web/data/latest.json", json.dumps(dict(self.payload, trade_date=day)))
                self.assertEqual(release.validate_data(self.a)["trade_date"], day)

    def test_newer_history_from_remote_blocks_stale_publish(self):
        self.data(self.a, 21)
        self.remote_change("data/history/daily/2026-10-19.csv", "code,date,price\n0050,2026-10-19,22\n")
        before = self.run_git(self.remote, "rev-parse", "main")
        with self.assertRaisesRegex(ValueError, "早於已保存行情"):
            self.publish()
        self.assertEqual(self.run_git(self.remote, "rev-parse", "main"), before)


if __name__ == "__main__":
    unittest.main()
