# -*- coding: utf-8 -*-
"""更新检查：git 状态解析、GitHub 比对、一键快进更新与接口鉴权。

所有出站 HTTP 都被替换成假响应；涉及 git 的用例使用临时仓库，不触碰项目本身。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time

import pytest
import requests

from core import update_checker
from webui import config_editor
from webui.app import create_app

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git 命令")

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "tester",
    "GIT_AUTHOR_EMAIL": "tester@example.com",
    "GIT_COMMITTER_NAME": "tester",
    "GIT_COMMITTER_EMAIL": "tester@example.com",
    # 屏蔽用户/系统级 git 配置（签名、hooks、默认分支）对用例的干扰。
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
}


def _git(repo, *args):
    done = subprocess.run(
        ["git", *args], cwd=str(repo), env={**os.environ, **_GIT_ENV},
        capture_output=True, text=True,
    )
    assert done.returncode == 0, f"git {' '.join(args)} 失败：{done.stderr}"
    return done.stdout.strip()


def _make_repo(tmp_path, name="repo", remote="git@github.com:owner/repo.git"):
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "a.txt").write_text("1", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "init")
    if remote:
        _git(repo, "remote", "add", "origin", remote)
    return repo


class _Response:
    def __init__(self, payload, status=200, headers=None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


def _fake_github(monkeypatch, *, commits=None, compare=None, release=None,
                 status=200, headers=None):
    """替换 requests.get，按 URL 分派 GitHub 假响应。"""
    calls: list[str] = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if "/compare/" in url:
            return _Response(compare, status=status, headers=headers)
        if url.endswith("/releases/latest"):
            return _Response(release, status=200 if release is not None else 404)
        return _Response(commits, status=status, headers=headers)

    monkeypatch.setattr(requests, "get", fake_get)
    return calls


_REMOTE_HEAD = {
    "sha": "f" * 40,
    "commit": {
        "message": "feat: 远端新功能\n\n详情",
        "author": {"name": "upstream", "date": "2026-10-07T10:00:00Z"},
    },
    "html_url": "https://github.com/owner/repo/commit/" + "f" * 40,
}


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """检查结论缓存与项目根都指向临时位置，避免污染真实仓库。"""
    monkeypatch.setattr(update_checker, "_STATE_PATH", tmp_path / "update_state.json")
    monkeypatch.setattr(update_checker, "_SCHEDULER_STARTED", False)
    monkeypatch.setattr(update_checker, "_RECHECK_REQUESTED", False)
    update_checker.reset_cache()
    update_checker._SCHEDULER_WAKE.clear()
    # config_editor.update_config 会直接写 os.environ，用后必须还原。
    saved_env = {k: v for k, v in os.environ.items() if k.startswith("UPDATE_CHECK_")}
    yield
    for key in [k for k in os.environ if k.startswith("UPDATE_CHECK_")]:
        os.environ.pop(key, None)
    os.environ.update(saved_env)
    update_checker.reset_cache()
    update_checker._SCHEDULER_WAKE.clear()


# ----------------------------------------------------------
# 远程地址解析
# ----------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("git@github.com:owner/repo.git", "owner/repo"),
    ("git@github.com:owner/repo", "owner/repo"),
    ("https://github.com/owner/repo.git", "owner/repo"),
    ("https://github.com/owner/repo", "owner/repo"),
    ("ssh://git@github.com/owner/repo.git", "owner/repo"),
    ("https://gitee.com/owner/repo.git", ""),
    ("", ""),
])
def test_parse_repo_slug(url, expected):
    assert update_checker.parse_repo_slug(url) == expected


# ----------------------------------------------------------
# 本地 git 状态
# ----------------------------------------------------------

@needs_git
def test_local_revision_reports_repo_state(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(update_checker, "_PROJECT_ROOT", repo)

    info = update_checker.local_revision()

    assert info["available"] is True
    assert len(info["sha"]) == 40
    assert info["branch"] == "main"
    assert info["repo"] == "owner/repo"
    assert info["remote"] == "origin"
    assert info["dirty"] == 0
    assert "init" in info["subject"]


@needs_git
def test_local_revision_counts_tracked_changes(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    (repo / "a.txt").write_text("changed", encoding="utf-8")
    (repo / "untracked.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(update_checker, "_PROJECT_ROOT", repo)

    info = update_checker.local_revision()

    # 只统计已跟踪文件的改动，未跟踪文件不阻断一键更新。
    assert info["dirty"] == 1
    assert any("a.txt" in item for item in info["dirty_files"])


def test_local_revision_without_git_repo(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setattr(update_checker, "_PROJECT_ROOT", plain)

    info = update_checker.local_revision()

    assert info["available"] is False
    assert info["error"]


# ----------------------------------------------------------
# 检查逻辑
# ----------------------------------------------------------

@needs_git
def test_check_reports_available_update(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(update_checker, "_PROJECT_ROOT", repo)
    _fake_github(
        monkeypatch,
        commits=[_REMOTE_HEAD],
        compare={"status": "behind", "ahead_by": 2, "behind_by": 0, "commits": [_REMOTE_HEAD]},
        headers={"X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "58"},
    )

    result = update_checker.check_for_update()

    assert result["ok"] is True
    assert result["behind"] == 2
    assert result["ahead"] == 0
    assert result["update_available"] is True
    assert result["compare"] == "behind"
    assert result["remote"]["short"] == "f" * 7
    assert result["commits"][0]["subject"] == "feat: 远端新功能"
    assert result["source"]["repo"] == "owner/repo"
    assert result["source"]["branch"] == "main"
    assert result["rate_limit"] == {"limit": 60, "remaining": 58}
    # 工作区干净且分支一致时允许一键更新。
    assert result["apply"] == {"allowed": True, "reason": ""}


@needs_git
def test_check_reports_up_to_date(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(update_checker, "_PROJECT_ROOT", repo)
    _fake_github(
        monkeypatch,
        commits=[{**_REMOTE_HEAD, "sha": head}],
        compare={"status": "identical", "ahead_by": 0, "behind_by": 0, "commits": []},
    )

    result = update_checker.check_for_update()

    assert result["ok"] is True
    assert result["update_available"] is False
    assert result["behind"] == 0
    assert result["compare"] == "identical"


@needs_git
def test_check_blocks_apply_on_dirty_worktree(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    (repo / "a.txt").write_text("changed", encoding="utf-8")
    monkeypatch.setattr(update_checker, "_PROJECT_ROOT", repo)
    _fake_github(
        monkeypatch,
        commits=[_REMOTE_HEAD],
        compare={"status": "behind", "ahead_by": 1, "behind_by": 0, "commits": [_REMOTE_HEAD]},
    )

    result = update_checker.check_for_update()

    assert result["update_available"] is True
    assert result["apply"]["allowed"] is False
    assert "未提交改动" in result["apply"]["reason"]


@needs_git
def test_check_falls_back_when_compare_unknown(tmp_path, monkeypatch):
    """本地 HEAD 不在远端（未推送提交）时退化为按提交号判断。"""
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(update_checker, "_PROJECT_ROOT", repo)
    calls: list[str] = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if "/compare/" in url:
            return _Response({"message": "Not Found"}, status=404)
        if url.endswith("/releases/latest"):
            return _Response(None, status=404)
        return _Response([_REMOTE_HEAD], status=200)

    monkeypatch.setattr(requests, "get", fake_get)

    result = update_checker.check_for_update()

    assert result["ok"] is True
    assert result["compare"] == "unknown"
    assert result["behind"] is None
    assert result["update_available"] is True
    assert result["commits_approx"] is True


def test_check_reports_github_error(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setattr(update_checker, "_PROJECT_ROOT", plain)
    monkeypatch.setattr(update_checker, "_settings", lambda: {
        "enabled": True, "interval_hours": 6.0, "remote": "origin",
        "branch": "main", "repo": "owner/repo", "token": "", "proxy": "",
    })
    _fake_github(monkeypatch, status=403, headers={"X-RateLimit-Remaining": "0"},
                 commits={"message": "API rate limit exceeded"})

    result = update_checker.check_for_update()

    assert result["ok"] is False
    assert result["update_available"] is False
    assert "403" in result["error"]
    assert "Token" in result["error"]


def test_check_requires_github_repo(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setattr(update_checker, "_PROJECT_ROOT", plain)

    result = update_checker.check_for_update()

    assert result["ok"] is False
    assert "仓库" in result["error"]


def test_status_reads_cached_state_without_network(tmp_path, monkeypatch):
    def _no_network(*args, **kwargs):
        raise AssertionError("读取状态不应发起网络请求")

    monkeypatch.setattr(requests, "get", _no_network)
    update_checker._save_state({"checked_at": 1.0, "ok": True, "update_available": True, "behind": 3})

    status = update_checker.get_status()

    assert status["behind"] == 3
    assert status["enabled"] is True
    assert status["checking"] is False


def test_cache_is_current_tracks_check_target(monkeypatch):
    from config import update as update_cfg

    monkeypatch.setattr(update_cfg, "UPDATE_CHECK_REPO", "owner/repo")
    update_checker._save_state({
        "checked_at": time.time(), "ok": True,
        "fingerprint": update_checker._source_fingerprint(update_checker._settings()),
    })
    assert update_checker.cache_is_current() is True

    # 换了仓库（或分支/远程）后旧结论必须失效，触发一次新的检查。
    monkeypatch.setattr(update_cfg, "UPDATE_CHECK_REPO", "other/repo")
    assert update_checker.cache_is_current() is False

    # 没有指纹的历史缓存同样视为过期。
    update_checker._save_state({"checked_at": time.time(), "ok": True})
    assert update_checker.cache_is_current() is False


def test_kick_background_check_skips_fresh_cache(monkeypatch):
    from webui import update_routes

    started: list[str] = []

    class _FakeThread:
        def __init__(self, target=None, name=None, daemon=None, **kwargs):
            started.append(name or "")

        def start(self):
            return None

    monkeypatch.setattr(update_routes.threading, "Thread", _FakeThread)
    monkeypatch.setattr(update_checker, "get_status", lambda: {"enabled": True, "checked_at": time.time()})
    monkeypatch.setattr(update_checker, "cache_is_current", lambda: True)
    update_routes._kick_background_check()
    assert started == []

    monkeypatch.setattr(update_checker, "cache_is_current", lambda: False)
    update_routes._kick_background_check()
    assert started == ["update-check-initial"]


def test_kick_background_check_respects_disabled(monkeypatch):
    from webui import update_routes

    started: list[str] = []

    class _FakeThread:
        def __init__(self, target=None, name=None, daemon=None):
            started.append(name or "")

        def start(self):
            return None

    monkeypatch.setattr(update_routes.threading, "Thread", _FakeThread)
    monkeypatch.setattr(update_checker, "get_status", lambda: {"enabled": False})
    update_routes._kick_background_check()
    assert started == []


# ----------------------------------------------------------
# 一键更新
# ----------------------------------------------------------

def _make_remote_pair(tmp_path):
    """seed 推到裸仓库，再克隆出 work / other 两个工作副本。"""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git(bare, "init", "--bare", "-b", "main")

    seed = _make_repo(tmp_path, name="seed", remote="")
    _git(seed, "remote", "add", "origin", str(bare))
    _git(seed, "push", "-u", "origin", "main")

    work = tmp_path / "work"
    _git(tmp_path, "clone", str(bare), str(work))
    other = tmp_path / "other"
    _git(tmp_path, "clone", str(bare), str(other))
    return bare, work, other


@needs_git
def test_apply_update_fast_forwards(tmp_path, monkeypatch):
    bare, work, other = _make_remote_pair(tmp_path)
    monkeypatch.setattr(update_checker, "_PROJECT_ROOT", work)

    (other / "b.txt").write_text("upstream", encoding="utf-8")
    _git(other, "add", "b.txt")
    _git(other, "commit", "-m", "feat: 上游新提交")
    _git(other, "push", "origin", "main")
    upstream_head = _git(other, "rev-parse", "HEAD")

    result = update_checker.apply_update()

    assert result["ok"] is True
    assert result["updated"] is True
    assert _git(work, "rev-parse", "HEAD") == upstream_head
    assert (work / "b.txt").read_text(encoding="utf-8") == "upstream"
    assert result["restart_hint"]


@needs_git
def test_apply_update_noop_when_up_to_date(tmp_path, monkeypatch):
    bare, work, other = _make_remote_pair(tmp_path)
    monkeypatch.setattr(update_checker, "_PROJECT_ROOT", work)
    before = _git(work, "rev-parse", "HEAD")

    result = update_checker.apply_update()

    assert result["ok"] is True
    assert result["updated"] is False
    assert _git(work, "rev-parse", "HEAD") == before


@needs_git
def test_apply_update_refuses_dirty_worktree(tmp_path, monkeypatch):
    bare, work, other = _make_remote_pair(tmp_path)
    monkeypatch.setattr(update_checker, "_PROJECT_ROOT", work)
    (other / "b.txt").write_text("upstream", encoding="utf-8")
    _git(other, "add", "b.txt")
    _git(other, "commit", "-m", "feat: 上游新提交")
    _git(other, "push", "origin", "main")

    (work / "a.txt").write_text("local edit", encoding="utf-8")
    before = _git(work, "rev-parse", "HEAD")

    result = update_checker.apply_update()

    assert result["ok"] is False
    assert "未提交改动" in result["error"]
    assert _git(work, "rev-parse", "HEAD") == before
    assert (work / "a.txt").read_text(encoding="utf-8") == "local edit"


@needs_git
def test_apply_update_refuses_diverged_history(tmp_path, monkeypatch):
    bare, work, other = _make_remote_pair(tmp_path)
    monkeypatch.setattr(update_checker, "_PROJECT_ROOT", work)

    (work / "local.txt").write_text("local", encoding="utf-8")
    _git(work, "add", "local.txt")
    _git(work, "commit", "-m", "本地提交")
    local_head = _git(work, "rev-parse", "HEAD")

    (other / "b.txt").write_text("upstream", encoding="utf-8")
    _git(other, "add", "b.txt")
    _git(other, "commit", "-m", "feat: 上游新提交")
    _git(other, "push", "origin", "main")

    result = update_checker.apply_update()

    assert result["ok"] is False
    assert "分叉" in result["error"]
    assert _git(work, "rev-parse", "HEAD") == local_head


# ----------------------------------------------------------
# 接口
# ----------------------------------------------------------

@pytest.fixture
def client():
    app = create_app(auth_code="test-auth")
    test_client = app.test_client()
    test_client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
    return test_client


def test_update_routes_require_auth():
    assert create_app(auth_code="test-auth").test_client().get("/api/update/status").status_code == 401
    assert create_app(auth_code="test-auth").test_client().post("/api/update/check").status_code == 401
    assert create_app(auth_code="test-auth").test_client().post("/api/update/apply").status_code == 401


def test_update_status_route(client, monkeypatch):
    monkeypatch.setattr("webui.update_routes._kick_background_check", lambda: None)
    monkeypatch.setattr(update_checker, "get_status", lambda: {
        "checked_at": 1.0, "ok": True, "update_available": True, "behind": 2,
        "enabled": True, "interval_hours": 6.0, "checking": False,
    })

    response = client.get("/api/update/status")

    assert response.status_code == 200
    assert response.get_json()["behind"] == 2


def test_update_check_route_returns_reason_on_failure(client, monkeypatch):
    monkeypatch.setattr(update_checker, "check_for_update", lambda **kwargs: {
        "ok": False, "error": "访问 GitHub 失败：Timeout", "update_available": False,
    })

    response = client.post("/api/update/check")

    # 检查失败属于业务结果而不是接口错误，前端照常展示原因。
    assert response.status_code == 200
    assert response.get_json()["error"].startswith("访问 GitHub 失败")


def test_update_apply_route_rejects_and_succeeds(client, monkeypatch):
    monkeypatch.setattr(update_checker, "apply_update", lambda: {"ok": False, "error": "工作区有 1 个未提交改动"})
    rejected = client.post("/api/update/apply")
    assert rejected.status_code == 400
    assert "未提交改动" in rejected.get_json()["error"]

    monkeypatch.setattr(update_checker, "apply_update", lambda: {"ok": True, "updated": True, "message": "已更新"})
    monkeypatch.setattr(update_checker, "check_for_update", lambda **kwargs: {"ok": True})
    accepted = client.post("/api/update/apply")
    assert accepted.status_code == 200
    assert accepted.get_json()["message"] == "已更新"


# ----------------------------------------------------------
# 配置与调度
# ----------------------------------------------------------

def test_config_fields_are_editable():
    fields = {f["key"]: f for f in config_editor.get_config() if f["key"].startswith("UPDATE_CHECK_")}

    assert set(fields) == {
        "UPDATE_CHECK_ENABLED", "UPDATE_CHECK_INTERVAL_HOURS", "UPDATE_CHECK_REMOTE",
        "UPDATE_CHECK_BRANCH", "UPDATE_CHECK_REPO", "UPDATE_CHECK_GITHUB_TOKEN",
        "UPDATE_CHECK_PROXY",
    }
    assert all(f["group"] == "更新检查" for f in fields.values())
    assert fields["UPDATE_CHECK_ENABLED"]["value"] is True
    assert fields["UPDATE_CHECK_INTERVAL_HOURS"]["value"] == 6.0
    assert fields["UPDATE_CHECK_GITHUB_TOKEN"]["secret"] is True


def test_config_round_trip_reaches_update_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("TURB_ENV_FILE", str(tmp_path / ".env"))
    result = config_editor.update_config({
        "UPDATE_CHECK_ENABLED": False,
        "UPDATE_CHECK_INTERVAL_HOURS": 3.5,
        "UPDATE_CHECK_REPO": "myfanhua/turb-gpt-free-register",
    })
    assert "UPDATE_CHECK_ENABLED" in result["updated"]

    values = {f["key"]: f["value"] for f in config_editor.get_config()}
    assert values["UPDATE_CHECK_ENABLED"] is False
    assert values["UPDATE_CHECK_INTERVAL_HOURS"] == 3.5
    assert values["UPDATE_CHECK_REPO"] == "myfanhua/turb-gpt-free-register"


def test_settings_clamp_interval(monkeypatch):
    from config import update as update_cfg
    monkeypatch.setattr(update_cfg, "UPDATE_CHECK_INTERVAL_HOURS", 0.0001)
    assert update_checker._settings()["interval_hours"] == update_checker._MIN_INTERVAL_HOURS
    monkeypatch.setattr(update_cfg, "UPDATE_CHECK_INTERVAL_HOURS", 99999)
    assert update_checker._settings()["interval_hours"] == update_checker._MAX_INTERVAL_HOURS


def test_scheduler_starts_once_and_wakes(monkeypatch):
    started: list[str] = []

    class _FakeThread:
        def __init__(self, target=None, name=None, daemon=None):
            started.append(name or "")

        def start(self):
            return None

    monkeypatch.setattr(update_checker.threading, "Thread", _FakeThread)

    assert update_checker.start_scheduler() is True
    assert update_checker.start_scheduler() is False
    assert started == ["update-check"]

    update_checker.notify_config_changed()
    assert update_checker._SCHEDULER_WAKE.is_set()
