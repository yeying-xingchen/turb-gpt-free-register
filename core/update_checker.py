# -*- coding: utf-8 -*-
"""GitHub 更新检查与一键快进更新。

设计原则：
    * 只读检查：通过 GitHub REST API 比对远端分支最新提交，不写 ``.git``、
      不改工作区，因此可以在后台长时间定时运行。
    * 一键更新：仅在用户确认后执行 ``git fetch`` + ``git merge --ff-only``，
      要求工作区干净、当前分支就是要更新的分支且能快进；任何一条不满足都
      直接拒绝，绝不产生合并提交、绝不丢弃本地改动。
    * 结论缓存到 ``run/update_state.json``，WebUI 打开即可看到上次检查结果，
      不用等下一次网络请求。

对外接口：
    local_revision()      当前代码版本（git HEAD / 分支 / 工作区状态）
    check_for_update()    立刻检查一次并返回完整结论
    get_status()          读取缓存结论（含调度器状态）
    apply_update()        一键快进更新
    start_scheduler()     WebUI 启动时拉起后台定时检查线程
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_STATE_PATH = _PROJECT_ROOT / "run" / "update_state.json"

_API_ROOT = "https://api.github.com"
_USER_AGENT = "turb-gpt-free-register-update-check"
_REQUEST_TIMEOUT = 15
_MAX_COMMITS = 20
_MIN_INTERVAL_HOURS = 0.1
_MAX_INTERVAL_HOURS = 720.0
_GIT_TIMEOUT = 30

_CHECK_LOCK = threading.Lock()
_APPLY_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
_CHECKING = threading.Event()
_STATE: dict = {}
_SCHEDULER_LOCK = threading.Lock()
_SCHEDULER_WAKE = threading.Event()
_SCHEDULER_STARTED = False
_RECHECK_REQUESTED = False
# 配置保存后若结论仍然新鲜（未换仓库/分支且刚查过），不重复消耗 GitHub 配额。
_MIN_RECHECK_SECONDS = 300.0


# ----------------------------------------------------------
# 配置
# ----------------------------------------------------------

def _settings() -> dict:
    """读取当前配置（config.update 会被 config.reload_all() 热更新）。"""
    from config import update as update_cfg

    def _text(key: str, default: str = "") -> str:
        value = getattr(update_cfg, key, default)
        return str(value).strip() if value is not None else default

    try:
        interval = float(getattr(update_cfg, "UPDATE_CHECK_INTERVAL_HOURS", 6.0) or 6.0)
    except (TypeError, ValueError):
        interval = 6.0
    return {
        "enabled": bool(getattr(update_cfg, "UPDATE_CHECK_ENABLED", True)),
        "interval_hours": max(_MIN_INTERVAL_HOURS, min(_MAX_INTERVAL_HOURS, interval)),
        "remote": _text("UPDATE_CHECK_REMOTE", "origin") or "origin",
        "branch": _text("UPDATE_CHECK_BRANCH"),
        "repo": _text("UPDATE_CHECK_REPO"),
        "token": _text("UPDATE_CHECK_GITHUB_TOKEN"),
        "proxy": _text("UPDATE_CHECK_PROXY"),
    }


def _token_hint(token: str) -> str:
    if not token:
        return ""
    return token[:4] + "…" + token[-4:] if len(token) > 8 else "已配置"


def _source_fingerprint(settings: dict) -> str:
    """检查目标的配置指纹；换了仓库/分支/远程后缓存立即失效。"""
    return f"{settings['remote']}|{settings['branch']}|{settings['repo']}"


# ----------------------------------------------------------
# git 本地状态
# ----------------------------------------------------------

def _git(args: list[str], *, timeout: int = _GIT_TIMEOUT, cwd: Path | None = None):
    """执行只读/受限 git 命令，禁用交互式凭据提示，避免后台线程挂死。"""
    env = dict(os.environ)
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "echo",
        "GIT_OPTIONAL_LOCKS": "0",
        "GCM_INTERACTIVE": "never",
        "LC_ALL": "C",
    })
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd or _PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        check=False,
    )


def git_available() -> bool:
    try:
        return _git(["--version"], timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def parse_repo_slug(url: str) -> str:
    """把 git 远程地址解析成 ``owner/repo``；无法识别时返回空串。"""
    text = str(url or "").strip()
    if not text:
        return ""
    # git@github.com:owner/repo.git / ssh://git@github.com/owner/repo.git
    match = re.search(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", text)
    if not match:
        return ""
    return f"{match.group(1)}/{match.group(2)}"


def local_revision(root: Path | None = None) -> dict:
    """当前代码版本信息；git 不可用或不是仓库时 ``available=False``。"""
    base = root or _PROJECT_ROOT
    info: dict = {
        "available": False,
        "sha": "",
        "short": "",
        "subject": "",
        "date": "",
        "branch": "",
        "remote": "",
        "remote_url": "",
        "repo": "",
        "git_dir": "",
        "dirty": 0,
        "dirty_files": [],
        "error": "",
    }
    if not git_available():
        info["error"] = "未检测到可用的 git 命令，无法检查更新"
        return info

    try:
        head = _git(["rev-parse", "--verify", "HEAD"], cwd=base)
        if head.returncode != 0:
            info["error"] = (head.stderr or "无法读取 HEAD").strip()[:300]
            return info
        info["sha"] = head.stdout.strip()
        info["available"] = True

        meta = _git(["log", "-1", "--format=%h%x1f%s%x1f%cI"], cwd=base)
        if meta.returncode == 0:
            parts = (meta.stdout.strip("\n").split("\x1f") + ["", "", ""])[:3]
            info["short"], info["subject"], info["date"] = parts[0], parts[1], _friendly_time(parts[2])

        branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=base)
        if branch.returncode == 0:
            info["branch"] = branch.stdout.strip()

        git_dir = _git(["rev-parse", "--absolute-git-dir"], cwd=base)
        if git_dir.returncode == 0:
            info["git_dir"] = git_dir.stdout.strip()

        remote = _settings()["remote"]
        remote_url = _git(["remote", "get-url", remote], cwd=base)
        if remote_url.returncode == 0:
            info["remote"] = remote
            info["remote_url"] = remote_url.stdout.strip()
            info["repo"] = parse_repo_slug(info["remote_url"])

        status = _git(["status", "--porcelain", "--untracked-files=no"], cwd=base)
        if status.returncode == 0:
            lines = [line for line in status.stdout.splitlines() if line.strip()]
            info["dirty"] = len(lines)
            info["dirty_files"] = [line[:160] for line in lines[:10]]
    except subprocess.TimeoutExpired:
        info["error"] = "git 命令超时"
    except (OSError, subprocess.SubprocessError) as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def _friendly_time(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return text


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ----------------------------------------------------------
# GitHub API
# ----------------------------------------------------------

def _api_get(path: str, settings: dict, params: dict | None = None) -> dict:
    """调用 GitHub REST API，任何失败都转成结构化结果而不是抛异常。"""
    import requests

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": _USER_AGENT,
    }
    token = settings.get("token") or ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    proxy = settings.get("proxy") or ""
    proxies = {"http": proxy, "https": proxy} if proxy else None

    try:
        response = requests.get(
            f"{_API_ROOT}{path}",
            headers=headers,
            params=params,
            proxies=proxies,
            timeout=_REQUEST_TIMEOUT,
        )
    except Exception as exc:  # requests.RequestException 及其子类
        hint = "；如网络受限可在「更新检查」中配置代理" if not proxy else ""
        return {"ok": False, "status": 0, "data": None,
                "error": f"访问 GitHub 失败：{type(exc).__name__}: {exc}{hint}", "headers": {}}

    payload = None
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if response.status_code >= 400:
        message = ""
        if isinstance(payload, dict):
            message = str(payload.get("message") or "")
        hint = ""
        if response.status_code == 404:
            hint = "；请确认仓库地址与分支名正确，私有仓库需要配置 GitHub Token"
        elif response.status_code in (401, 403):
            remaining = response.headers.get("X-RateLimit-Remaining")
            if remaining == "0":
                hint = "；未认证请求每小时 60 次，配置 GitHub Token 可提升到 5000 次/小时"
            else:
                hint = "；请检查 GitHub Token 是否有效"
        return {"ok": False, "status": response.status_code, "data": payload,
                "error": f"GitHub 返回 {response.status_code}：{message or '请求被拒绝'}{hint}",
                "headers": dict(response.headers)}

    return {"ok": True, "status": response.status_code, "data": payload, "error": "",
            "headers": dict(response.headers)}


def _commit_brief(raw: dict) -> dict:
    commit = raw.get("commit") or {}
    author = commit.get("author") or commit.get("committer") or {}
    message = str(commit.get("message") or "").strip().splitlines()
    return {
        "sha": str(raw.get("sha") or ""),
        "short": str(raw.get("sha") or "")[:7],
        "subject": message[0] if message else "",
        "author": str(author.get("name") or ""),
        "date": _friendly_time(str(author.get("date") or "")),
        "url": str(raw.get("html_url") or ""),
    }


def _rate_limit(headers: dict) -> dict:
    def _int(name: str):
        try:
            return int(headers.get(name))
        except (TypeError, ValueError):
            return None

    return {"limit": _int("X-RateLimit-Limit"), "remaining": _int("X-RateLimit-Remaining")}


# ----------------------------------------------------------
# 检查
# ----------------------------------------------------------

def _resolve_source(settings: dict, local: dict) -> dict:
    repo = settings["repo"] or local.get("repo") or ""
    repo = parse_repo_slug(repo) or repo
    branch = settings["branch"] or local.get("branch") or "main"
    return {
        "repo": repo,
        "branch": branch,
        "remote": settings["remote"],
        "remote_url": local.get("remote_url") or "",
        "html_url": f"https://github.com/{repo}" if repo else "",
        "branch_url": f"https://github.com/{repo}/tree/{branch}" if repo else "",
        "token_configured": bool(settings.get("token")),
        "token_hint": _token_hint(settings.get("token") or ""),
    }


def _check_once(settings: dict) -> dict:
    local = local_revision()
    source = _resolve_source(settings, local)
    result: dict = {
        "checked_at": time.time(),
        "checked_at_text": _now_text(),
        "enabled": settings["enabled"],
        "interval_hours": settings["interval_hours"],
        "fingerprint": _source_fingerprint(settings),
        "ok": False,
        "error": "",
        "source": source,
        "local": local,
        "remote": None,
        "release": None,
        "ahead": None,
        "behind": None,
        "compare": "unknown",
        "commits": [],
        "commits_approx": False,
        "update_available": False,
        "rate_limit": {"limit": None, "remaining": None},
        "apply": {"allowed": False, "reason": ""},
    }

    if not source["repo"]:
        result["error"] = (
            f"无法从远程 {source['remote']} 识别 GitHub 仓库，"
            "请在「更新检查」中填写仓库（owner/repo）"
        )
        return result

    head = _api_get(
        f"/repos/{source['repo']}/commits",
        settings,
        params={"sha": source["branch"], "per_page": 1},
    )
    if not head["ok"]:
        result["error"] = head["error"]
        result["rate_limit"] = _rate_limit(head.get("headers") or {})
        return result

    payload = head.get("data")
    if not isinstance(payload, list) or not payload:
        result["error"] = f"远端分支 {source['branch']} 没有提交记录"
        return result

    remote = _commit_brief(payload[0])
    result["remote"] = remote
    result["rate_limit"] = _rate_limit(head.get("headers") or {})
    result["ok"] = True

    # GitHub compare：base=本地 HEAD，head=远端分支。
    # ahead_by = 远端领先本地（即待更新数量），behind_by = 本地领先远端（未推送提交）。
    if local.get("available") and local.get("sha"):
        compare = _api_get(f"/repos/{source['repo']}/compare/{local['sha']}...{remote['sha']}", settings)
        if compare["ok"] and isinstance(compare.get("data"), dict):
            data = compare["data"]
            result["behind"] = int(data.get("ahead_by") or 0)
            result["ahead"] = int(data.get("behind_by") or 0)
            result["compare"] = str(data.get("status") or "unknown")
            result["commits"] = [_commit_brief(item) for item in (data.get("commits") or [])][-_MAX_COMMITS:]
        elif compare["status"] == 404:
            # 本地 HEAD 不在远端（本地提交未推送）或提交太旧，退化为“远端有新提交”。
            result["compare"] = "unknown"
            result["commits_approx"] = True
            result["commits"] = _recent_commits(settings, source)
        else:
            result["compare"] = "unknown"
            result["commits_approx"] = True
            result["commits"] = _recent_commits(settings, source)

    if result["behind"] is None:
        result["update_available"] = bool(
            result["remote"] and local.get("sha") and result["remote"]["sha"] != local["sha"]
        )
    else:
        result["update_available"] = result["behind"] > 0

    release = _api_get(f"/repos/{source['repo']}/releases/latest", settings)
    if release["ok"] and isinstance(release.get("data"), dict):
        data = release["data"]
        result["release"] = {
            "tag": str(data.get("tag_name") or ""),
            "name": str(data.get("name") or ""),
            "url": str(data.get("html_url") or ""),
            "published_at": _friendly_time(str(data.get("published_at") or "")),
        }

    result["apply"] = _apply_precheck(local, source)
    return result


def _recent_commits(settings: dict, source: dict) -> list[dict]:
    recent = _api_get(
        f"/repos/{source['repo']}/commits",
        settings,
        params={"sha": source["branch"], "per_page": 5},
    )
    if not recent["ok"] or not isinstance(recent.get("data"), list):
        return []
    return [_commit_brief(item) for item in recent["data"]]


def _apply_precheck(local: dict, source: dict) -> dict:
    """一键更新的前置条件，供界面提前显示「能否更新」。"""
    if not local.get("available"):
        return {"allowed": False, "reason": local.get("error") or "当前不是 git 仓库，无法自动更新"}
    if local.get("branch") in ("", "HEAD"):
        return {"allowed": False, "reason": "当前处于分离头指针状态，请先切回分支再更新"}
    if local.get("dirty"):
        return {
            "allowed": False,
            "reason": f"工作区有 {local['dirty']} 个未提交改动，请先提交或暂存后再更新",
        }
    if local.get("branch") and local["branch"] != source["branch"]:
        return {
            "allowed": False,
            "reason": f"当前分支 {local['branch']} 与更新分支 {source['branch']} 不一致",
        }
    return {"allowed": True, "reason": ""}


def check_for_update(*, trigger: str = "manual") -> dict:
    """立刻检查一次。同一时刻只允许一个检查在跑，重复调用复用缓存结论。"""
    if not _CHECK_LOCK.acquire(blocking=False):
        cached = dict(_STATE)
        cached["busy"] = True
        cached["checking"] = True
        return cached
    _CHECKING.set()
    try:
        settings = _settings()
        result = _check_once(settings)
        result["trigger"] = trigger
        result["busy"] = False
        _save_state(result)
        if result["ok"] and result["update_available"]:
            logger.info(
                "检测到 GitHub 更新：%s %s 落后 %s 个提交（%s）",
                result["source"]["repo"], result["source"]["branch"],
                result["behind"] if result["behind"] is not None else "若干",
                result["remote"]["short"] if result["remote"] else "-",
            )
        elif not result["ok"]:
            logger.warning("检查 GitHub 更新失败：%s", result["error"])
        return result
    finally:
        _CHECKING.clear()
        _CHECK_LOCK.release()


def update_check_in_progress() -> bool:
    return _CHECKING.is_set()


# ----------------------------------------------------------
# 一键更新
# ----------------------------------------------------------

def _git_error(completed) -> str:
    text = (completed.stderr or completed.stdout or "").strip()
    return text.splitlines()[-1][:300] if text else f"git 退出码 {completed.returncode}"


def apply_update() -> dict:
    """一键快进更新。失败时返回原因，不修改任何本地改动。"""
    if not _APPLY_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "已有更新任务在执行，请稍候"}

    def _run_git_step(args: list[str], *, timeout: int = 120):
        try:
            return _git(args, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("git 命令执行失败：%s", exc)
            return None

    try:
        local = local_revision()
        settings = _settings()
        source = _resolve_source(settings, local)

        if not source["repo"] and not local.get("remote"):
            return {"ok": False, "error": "未找到可用的 git 远程，无法自动更新"}
        if not local.get("available"):
            return {"ok": False, "error": local.get("error") or "当前不是 git 仓库，无法自动更新"}

        precheck = _apply_precheck(local, source)
        if not precheck["allowed"]:
            return {"ok": False, "error": precheck["reason"], "local": local}

        for marker, label in (
            ("MERGE_HEAD", "合并"), ("rebase-merge", "变基"),
            ("rebase-apply", "变基"), ("CHERRY_PICK_HEAD", "拣选"),
        ):
            if (Path(local.get("git_dir") or (_PROJECT_ROOT / ".git")) / marker).exists():
                return {"ok": False, "error": f"仓库正处于{label}过程中，请先处理完再更新"}

        remote, branch = source["remote"], source["branch"]
        log: list[str] = []

        fetch = _run_git_step(["fetch", "--no-tags", remote, branch])
        if fetch is None:
            return {"ok": False, "error": f"git fetch {remote} 超时"}
        if fetch.returncode != 0:
            return {
                "ok": False,
                "error": f"git fetch {remote} 失败：{_git_error(fetch)}",
                "log": log,
            }

        target = "FETCH_HEAD"
        fetched = _run_git_step(["rev-parse", "--verify", target])
        fetched_sha = fetched.stdout.strip() if fetched and fetched.returncode == 0 else ""
        if not fetched_sha:
            return {"ok": False, "error": "拉取成功但无法解析远端提交号", "log": log}
        log.append(f"已拉取 {remote}/{branch} → {fetched_sha[:7]}")

        if fetched_sha == local["sha"]:
            return {
                "ok": True,
                "updated": False,
                "message": "当前已是最新版本，无需更新",
                "from": local["short"],
                "to": fetched_sha[:7],
                "log": log,
            }

        # 只有远端是本地的后继时才允许快进；否则说明历史分叉，交回用户手动处理。
        ancestor = _run_git_step(["merge-base", "--is-ancestor", local["sha"], fetched_sha])
        if ancestor is None or ancestor.returncode != 0:
            return {
                "ok": False,
                "error": "远端与本地历史已分叉，无法快进更新；请手动处理（git pull --rebase 或人工合并）",
                "log": log,
            }

        merge = _run_git_step(["merge", "--ff-only", fetched_sha])
        if merge is None:
            return {"ok": False, "error": "git merge 超时", "log": log}
        if merge.returncode != 0:
            return {"ok": False, "error": f"git merge --ff-only 失败：{_git_error(merge)}", "log": log}
        log.append((merge.stdout or "").strip().splitlines()[0] if (merge.stdout or "").strip() else "已快进合并")

        after = local_revision()
        result = {
            "ok": True,
            "updated": True,
            "message": f"已更新到 {fetched_sha[:7]}，重启 WebUI 后生效",
            "from": local["short"],
            "to": (after.get("short") or fetched_sha[:7]),
            "commits": None,
            "log": log,
            "restart_hint": "./webui.sh restart",
        }
        _save_state({**(_STATE or {}), "applied_at": time.time(), "applied_to": result["to"],
                     "update_available": False, "behind": 0, "apply": {"allowed": False, "reason": "已更新，等待下次检查"}})
        return result
    finally:
        _APPLY_LOCK.release()


# ----------------------------------------------------------
# 状态缓存（供 WebUI 直接读取）
# ----------------------------------------------------------

def _load_state() -> dict:
    with _STATE_LOCK:
        if _STATE:
            return dict(_STATE)
        path = _STATE_PATH
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    _STATE.update(data)
            except (OSError, ValueError):
                logger.debug("更新检查缓存读取失败：%s", path)
        return dict(_STATE)


def _save_state(result: dict) -> None:
    with _STATE_LOCK:
        _STATE.clear()
        _STATE.update(result)
        try:
            _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = _STATE_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(_STATE_PATH)
        except OSError as exc:
            logger.debug("更新检查缓存写入失败：%s", exc)


def get_status() -> dict:
    """读取缓存结论；从未检查过时返回初始状态（不会触发网络请求）。"""
    state = _load_state()
    settings = _settings()
    state = {
        **state,
        "enabled": settings["enabled"],
        "interval_hours": settings["interval_hours"],
        "checking": update_check_in_progress(),
        "busy": False,
        "scheduler_running": _SCHEDULER_STARTED,
    }
    return state


def reset_cache() -> None:
    """清空内存缓存（测试用）。"""
    with _STATE_LOCK:
        _STATE.clear()


def cache_is_current() -> bool:
    """缓存结论是否仍对应当前配置指向的仓库/分支。"""
    state = _load_state()
    if not state.get("checked_at"):
        return False
    fingerprint = state.get("fingerprint")
    if not fingerprint:
        # 旧版本留下的缓存没有指纹，视为过期，补一次检查即可。
        return False
    return fingerprint == _source_fingerprint(_settings())


def cache_age_seconds() -> float:
    state = _load_state()
    try:
        return max(0.0, time.time() - float(state.get("checked_at") or 0))
    except (TypeError, ValueError):
        return float("inf")


# ----------------------------------------------------------
# 后台定时检查
# ----------------------------------------------------------

def _seconds_until_next(settings: dict, *, first: bool) -> float:
    if first:
        return 20.0
    return max(60.0, settings["interval_hours"] * 3600.0)


def _scheduler_loop() -> None:
    global _RECHECK_REQUESTED
    first = True
    while True:
        settings = _settings()
        delay = _seconds_until_next(settings, first=first)
        first = False
        if _SCHEDULER_WAKE.wait(timeout=delay):
            _SCHEDULER_WAKE.clear()
            if not _RECHECK_REQUESTED:
                continue
            _RECHECK_REQUESTED = False
            # 配置保存只改了无关项时沿用刚拿到的结论，避免重复消耗 GitHub 配额。
            if cache_is_current() and cache_age_seconds() < _MIN_RECHECK_SECONDS:
                continue
        settings = _settings()
        if not settings["enabled"]:
            logger.debug("自动检查更新已关闭，跳过本次")
            continue
        try:
            check_for_update(trigger="scheduled")
        except Exception:  # 后台线程必须活下去
            logger.exception("自动检查更新异常")


def start_scheduler() -> bool:
    """启动后台定时检查线程（每个进程只启动一次）。"""
    global _SCHEDULER_STARTED
    with _SCHEDULER_LOCK:
        if _SCHEDULER_STARTED:
            return False
        thread = threading.Thread(target=_scheduler_loop, name="update-check", daemon=True)
        thread.start()
        _SCHEDULER_STARTED = True
        logger.info("已启动 GitHub 更新自动检查（间隔 %.1f 小时）", _settings()["interval_hours"])
        return True


def notify_config_changed() -> None:
    """配置保存后调用：唤醒调度线程，必要时立刻按新配置复查一次。"""
    global _RECHECK_REQUESTED
    _RECHECK_REQUESTED = True
    _SCHEDULER_WAKE.set()
