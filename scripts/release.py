"""CI 的資料提交與發布紀錄；只推送資料，遇真正衝突不覆蓋遠端。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from contextlib import closing

import store
import research_history


def git(root, *args, check=True):
    return subprocess.run(["git", *args], cwd=root, check=check, text=True,
                          encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def data_path(path):
    return path == "web/data/latest.json" or path.startswith("data/history/")


def validate_data(root):
    payload = json.loads((root / "web/data/latest.json").read_text("utf-8"))
    items = payload.get("items")
    if payload.get("source") != "live" or not isinstance(items, list) or not items:
        raise ValueError("正式發布必須是非空的 live 資料。")
    codes = [item.get("code") for item in items]
    if not all(isinstance(code, str) and code for code in codes) or len(set(codes)) != len(codes):
        raise ValueError("ETF 代號缺漏或重複，停止發布。")
    if payload.get("count") != len(items):
        raise ValueError("ETF 筆數與清單不一致，停止發布。")
    published_day = date.fromisoformat(payload["trade_date"])
    history_days = [date.fromisoformat(path.stem) for path in (root / "data/history/daily").glob("*.csv")]
    if history_days and published_day < max(history_days):
        raise ValueError(f"交易日期 {published_day} 早於已保存行情 {max(history_days)}，停止發布。")
    return payload


def commit_data(root):
    validate_data(root)
    changed = set(git(root, "diff", "--name-only").stdout.splitlines())
    changed.update(git(root, "diff", "--cached", "--name-only").stdout.splitlines())
    if any(not data_path(path) for path in changed):
        raise ValueError("存在非資料檔案的未提交修改，停止自動提交。")
    git(root, "add", "--", "data/history", "web/data/latest.json")
    if git(root, "diff", "--cached", "--quiet", check=False).returncode:
        git(root, "commit", "-m", "資料更新 " + datetime.now(timezone.utc).date().isoformat())


def verify(root):
    subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=root, check=True)
    tests = sorted(str(path.relative_to(root)) for path in (root / "tests").glob("*.mjs"))
    if tests:
        subprocess.run(["node", "--test", *tests], cwd=root, check=True)


def refresh(root):
    subprocess.run([sys.executable, "scripts/fetch_etf.py"], cwd=root, check=True)


def publish_data(root, *, verify_fn=verify, refresh_fn=refresh, attempts=3):
    """限定資料提交；遠端前進時 rebase，資料程式改動則重新測試、抓取。"""
    baseline = git(root, "rev-parse", "HEAD").stdout.strip()
    commit_data(root)
    for _ in range(attempts):
        git(root, "fetch", "origin", "main")
        remote = git(root, "rev-parse", "origin/main").stdout.strip()
        if git(root, "merge-base", "--is-ancestor", remote, "HEAD", check=False).returncode:
            changed = git(root, "diff", "--name-only", baseline, remote).stdout.splitlines()
            if any(path.startswith(".github/workflows/") or path == "scripts/release.py" for path in changed):
                raise RuntimeError("發布流程本身已更新，停止舊工作；由最新 main 的新工作發布。")
            rebased = git(root, "rebase", "origin/main", check=False)
            if rebased.returncode:
                git(root, "rebase", "--abort", check=False)
                raise RuntimeError("資料與遠端有實際衝突，已停止；不選擇覆蓋任一版本。")
            verify_fn(root)
            if any(path.startswith(("scripts/", "data/research-imports/")) or path in ("data/etf_meta.csv", "data/product_overrides.json", "data/product_reference.json") for path in changed):
                refresh_fn(root)
                commit_data(root)
            baseline = remote
        validate_data(root)  # 同步可能帶回較新行情；推送前再檢查一次日期。
        pushed = git(root, "push", "origin", "HEAD:refs/heads/main", check=False)
        if pushed.returncode == 0:
            return git(root, "rev-parse", "HEAD").stdout.strip()
        # 只對推送前遠端又前進的情況重試；權限／連線失敗直接交給工作紀錄。
        if "[rejected]" not in pushed.stderr or not any(s in pushed.stderr for s in ("fetch first", "non-fast-forward")):
            raise RuntimeError("資料推送失敗：" + pushed.stderr.strip())
    raise RuntimeError("main 持續更新，三次安全同步仍未成功；停止發布。")


def write_manifest(root):
    payload = validate_data(root)
    # rebase 可能帶入遠端其他歷史 CSV；下載資料庫必須從最後提交的 CSV 重建。
    with closing(sqlite3.connect(":memory:")) as source:
        store.ensure_schema(source)
        store.load(source, root / "data/history")
        research_history.export(source, payload["items"], payload["trade_date"], root / "web/data/research")
        with closing(sqlite3.connect(root / "web/data/etf.db")) as target:
            source.backup(target)
    paths = ("web/index.html", "web/data/latest.json", "web/data/etf.db", "web/data/research/index.json")
    result = {
        "commit": git(root, "rev-parse", "HEAD").stdout.strip(),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "trade_date": payload["trade_date"], "generated_at": payload.get("generated_at"),
        "count": len(payload["items"]),
        "sha256": {path.removeprefix("web/"): hashlib.sha256((root / path).read_bytes()).hexdigest() for path in paths},
    }
    (root / "web/release.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("publish", "manifest"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.command == "publish" and (os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("GITHUB_REF") != "refs/heads/main"):
        parser.error("資料推送僅供 GitHub Actions 的 main 工作執行；本機請只跑離線測試。")
    print(publish_data(root) if args.command == "publish" else json.dumps(write_manifest(root), ensure_ascii=False))
