# -*- coding: utf-8 -*-
"""统一后台任务控制层：暂停 / 恢复 / 取消 + 运行中动态并发。

设计要点：
    - :class:`DynamicPool` 取代固定大小的 ``ThreadPoolExecutor``。线程数可以在
      运行中增减：调大立刻为新排队任务加线程，调小让多余线程在完成当前任务后退出。
      已排队但尚未开始的任务会立即跟随新的并发数，不需要等旧批次跑完。
    - :class:`Control` 表示单个任务（``job_type`` + 账号/任务 key）的运行状态。
      工作线程在安全位置调用 :func:`checkpoint`：暂停时阻塞等待，取消时抛出
      :class:`TaskCancelled`。
    - 队列满/占用等业务判定仍由各服务自己负责；本模块只做调度与控制信号，
      不写业务状态。服务收到 :class:`TaskCancelled` 后自行写回终态。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque

logger = logging.getLogger(__name__)

MIN_WORKERS = 1
MAX_WORKERS = 16

_STATE_RUNNING = "running"
_STATE_PAUSED = "paused"
_STATE_CANCELLED = "cancelled"


class TaskCancelled(RuntimeError):
    """任务在检查点被用户取消。"""


def clamp_workers(value, default: int = MIN_WORKERS) -> int:
    """把外部传入的并发数收敛到 1–16。"""
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        number = int(default)
    return max(MIN_WORKERS, min(MAX_WORKERS, number))


class Control:
    """单个任务的暂停/取消状态，可跨线程安全访问。"""

    __slots__ = ("kind", "key", "_condition", "_paused", "_cancelled")

    def __init__(self, kind: str, key):
        self.kind = str(kind)
        self.key = key
        self._condition = threading.Condition()
        self._paused = False
        self._cancelled = False

    # ---- 状态查询 ----------------------------------------------------
    @property
    def cancelled(self) -> bool:
        with self._condition:
            return self._cancelled

    @property
    def paused(self) -> bool:
        with self._condition:
            return self._paused

    def state(self) -> str:
        with self._condition:
            if self._cancelled:
                return _STATE_CANCELLED
            if self._paused:
                return _STATE_PAUSED
            return _STATE_RUNNING

    # ---- 状态变更 ----------------------------------------------------
    def pause(self) -> bool:
        """请求暂停；已经暂停或已取消时返回 False。"""
        with self._condition:
            if self._cancelled or self._paused:
                return False
            self._paused = True
            self._condition.notify_all()
            return True

    def resume(self) -> bool:
        """恢复暂停；不在暂停状态时返回 False。"""
        with self._condition:
            if self._cancelled or not self._paused:
                return False
            self._paused = False
            self._condition.notify_all()
            return True

    def cancel(self) -> bool:
        """请求取消；重复取消返回 False。取消会同时唤醒暂停中的线程。"""
        with self._condition:
            if self._cancelled:
                return False
            self._cancelled = True
            self._paused = False
            self._condition.notify_all()
            return True

    # ---- 工作线程侧 --------------------------------------------------
    def checkpoint(self, timeout: float = 0.5) -> None:
        """安全检查点：暂停时阻塞，取消时抛出 :class:`TaskCancelled`。"""
        while True:
            with self._condition:
                if self._cancelled:
                    raise TaskCancelled(f"{self.kind} 任务已取消")
                if not self._paused:
                    return
                self._condition.wait(timeout)

    def wait(self, seconds: float, timeout: float = 0.5) -> None:
        """可中断等待：暂停时挂起直到恢复，取消时立刻抛出。"""
        deadline = time.monotonic() + max(0.0, float(seconds or 0.0))
        while True:
            self.checkpoint(timeout)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            with self._condition:
                if self._cancelled or self._paused:
                    continue
                self._condition.wait(min(timeout, remaining))


class _WorkItem:
    __slots__ = ("fn", "args", "kwargs", "control", "on_cancel")

    def __init__(self, fn, args, kwargs, control, on_cancel):
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.control = control
        self.on_cancel = on_cancel


class GatedCall:
    """把任务函数和它的暂停/取消信号绑在一起提交。

    ``ThreadPoolExecutor.submit`` 只接受 ``(fn, *args, **kwargs)``，而测试和业务
    代码经常把 ``submit`` 换成自己的假执行器。把控制对象放在可调用包装里，提交
    参数就保持原样，假执行器直接调用也能正常工作。
    """

    __slots__ = ("fn", "control", "on_cancel", "__name__", "__qualname__")

    def __init__(self, fn, control: Control | None = None, on_cancel=None):
        self.fn = fn
        self.control = control
        self.on_cancel = on_cancel
        self.__name__ = getattr(fn, "__name__", "gated_task")
        self.__qualname__ = getattr(fn, "__qualname__", self.__name__)

    def __call__(self, *args, **kwargs):
        return self.fn(*args, **kwargs)


def gated(fn, control: Control | None = None, on_cancel=None) -> GatedCall:
    """包装任务函数：线程池据此在开始前判断暂停/取消。"""
    if isinstance(fn, GatedCall):
        if control is None:
            control = fn.control
        if on_cancel is None:
            on_cancel = fn.on_cancel
        fn = fn.fn
    return GatedCall(fn, control, on_cancel)


class DynamicPool:
    """线程数可在运行中调整的任务池，兼容 ``ThreadPoolExecutor.submit`` 调用形式。

    线程按需启动：注册池本身不创建线程，第一次提交任务或显式调整并发时才拉起，
    因此“声明一个任务类型”不会在启动阶段占用系统资源。
    """

    def __init__(self, name: str, workers: int = MIN_WORKERS, *, max_workers: int = MAX_WORKERS):
        self.name = str(name)
        self._max_workers = max(MIN_WORKERS, min(MAX_WORKERS, int(max_workers)))
        self._target = min(clamp_workers(workers), self._max_workers)
        self._condition = threading.Condition()
        self._queue: deque[_WorkItem] = deque()
        self._threads: set[threading.Thread] = set()
        self._closed = False
        self._running = 0
        self._completed = 0
        self._cancelled = 0
        self._rejected = 0
        self._generation = 0

    # ---- 观测 --------------------------------------------------------
    @property
    def workers(self) -> int:
        """目标并发数（正在使用的线程上限）。"""
        with self._condition:
            return self._target

    @property
    def max_workers(self) -> int:
        return self._max_workers

    @property
    def active(self) -> int:
        with self._condition:
            return self._running

    @property
    def pending(self) -> int:
        with self._condition:
            return len(self._queue)

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def status(self) -> dict:
        with self._condition:
            return {
                "name": self.name,
                "workers": self._target,
                "max_workers": self._max_workers,
                "threads": len(self._threads),
                "running": self._running,
                "pending": len(self._queue),
                "completed": self._completed,
                "cancelled": self._cancelled,
            }

    # ---- 调度 --------------------------------------------------------
    def submit(self, fn, *args, control: Control | None = None, on_cancel=None, **kwargs) -> bool:
        """提交一个任务；``control`` 为该任务的暂停/取消信号。

        ``on_cancel`` 只在任务尚未开始就被取消时调用（服务用它释放占用并写回终态）；
        已经在运行的取消由工作线程在检查点抛出的 :class:`TaskCancelled` 处理。

        ``fn`` 也可以是 :func:`gated` 包装过的可调用对象，此时控制信号从包装里取。
        """
        if isinstance(fn, GatedCall):
            control = fn.control if control is None else control
            on_cancel = fn.on_cancel if on_cancel is None else on_cancel
            fn = fn.fn
        item = _WorkItem(fn, args, kwargs, control, on_cancel)
        with self._condition:
            if self._closed:
                self._rejected += 1
                return False
            self._queue.append(item)
            self._spawn_locked()
            self._condition.notify_all()
            return True

    def set_workers(self, workers: int) -> int:
        """调整目标并发数并立即生效（调大马上加线程，调小只回收空闲线程）。"""
        with self._condition:
            self.configure_workers(workers)
            self._spawn_locked()
            self._condition.notify_all()
            return self._target

    def configure_workers(self, workers: int) -> int:
        """只记录目标并发数，不提前创建线程（注册任务类型时使用）。"""
        with self._condition:
            self._target = min(clamp_workers(workers, self._target), self._max_workers)
            self._condition.notify_all()
            return self._target

    def shutdown(self, wait: bool = False) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        if wait:
            self.wait_idle()

    def wait_idle(self, timeout: float | None = None) -> bool:
        """等待队列清空且没有任务在执行。"""
        deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            while self._queue or self._running:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(0.2 if remaining is None else min(0.2, remaining))
            return True

    # ---- 内部实现 ----------------------------------------------------
    def _spawn_locked(self) -> None:
        while not self._closed and len(self._threads) < self._target:
            self._generation += 1
            thread = threading.Thread(
                target=self._worker,
                name=f"{self.name}-worker-{self._generation}",
                daemon=True,
            )
            self._threads.add(thread)
            thread.start()

    def _take_locked(self, me: threading.Thread) -> _WorkItem | None:
        """取一个待执行任务；返回 None 表示本线程应当退出。"""
        with self._condition:
            while True:
                if len(self._threads) > self._target or (self._closed and not self._queue):
                    self._threads.discard(me)
                    return None
                if self._queue:
                    item = self._queue.popleft()
                    self._running += 1
                    return item
                self._condition.wait(0.25)

    def _defer_locked(self, item: _WorkItem) -> None:
        with self._condition:
            self._running = max(0, self._running - 1)
            if not self._closed:
                self._queue.append(item)
            self._condition.wait(0.25)

    def _finish(self, *, cancelled: bool) -> None:
        with self._condition:
            self._running = max(0, self._running - 1)
            self._completed += 1
            if cancelled:
                self._cancelled += 1
            self._condition.notify_all()

    def _notify_cancel(self, item: _WorkItem) -> None:
        if item.on_cancel is None:
            return
        try:
            item.on_cancel()
        except Exception:
            logger.exception("[TaskControl] %s 取消回调失败", self.name)

    def _worker(self) -> None:
        me = threading.current_thread()
        while True:
            item = self._take_locked(me)
            if item is None:
                return
            state = _STATE_RUNNING if item.control is None else item.control.state()
            if state == _STATE_CANCELLED:
                self._finish(cancelled=True)
                self._notify_cancel(item)
                continue
            if state == _STATE_PAUSED:
                self._defer_locked(item)
                continue
            try:
                item.fn(*item.args, **item.kwargs)
            except TaskCancelled:
                logger.info("[TaskControl] %s 任务已在检查点取消", self.name)
                self._finish(cancelled=True)
            except Exception:
                logger.exception("[TaskControl] %s 任务执行异常", self.name)
                self._finish(cancelled=False)
            else:
                self._finish(cancelled=False)


# ============================================================
# 注册表
# ============================================================

_LOCK = threading.RLock()
_POOLS: dict[str, DynamicPool] = {}
_CONTROLS: dict[tuple[str, str], Control] = {}


def _control_key(kind, key) -> tuple[str, str]:
    return (str(kind), str(key))


def register_pool(name: str, workers: int = MIN_WORKERS, *, max_workers: int = MAX_WORKERS) -> DynamicPool:
    """按名字注册（或复用）一个任务池。"""
    with _LOCK:
        pool = _POOLS.get(str(name))
        if pool is None or pool.closed:
            pool = DynamicPool(str(name), workers, max_workers=max_workers)
            _POOLS[str(name)] = pool
        else:
            pool.configure_workers(workers)
        return pool


def get_pool(name: str) -> DynamicPool | None:
    with _LOCK:
        return _POOLS.get(str(name))


def remove_pool(name: str) -> None:
    """仅供测试/停机使用：移除注册并关闭线程池。"""
    with _LOCK:
        pool = _POOLS.pop(str(name), None)
    if pool is not None:
        pool.shutdown(wait=False)


def pool_names() -> list[str]:
    with _LOCK:
        return sorted(_POOLS)


def set_pool_workers(name: str, workers: int) -> dict:
    pool = get_pool(name)
    if pool is None:
        return {"ok": False, "error": f"未知任务类型：{name}"}
    applied = pool.set_workers(workers)
    return {"ok": True, "name": str(name), "workers": applied}


def pool_status(name: str) -> dict | None:
    pool = get_pool(name)
    return pool.status() if pool is not None else None


def pools_status() -> list[dict]:
    with _LOCK:
        pools = list(_POOLS.values())
    return [pool.status() for pool in pools]


# ---- 单任务控制 --------------------------------------------------

def control(kind, key) -> Control:
    """获取或创建任务控制对象。"""
    with _LOCK:
        return _CONTROLS.setdefault(_control_key(kind, key), Control(kind, key))


def get_control(kind, key) -> Control | None:
    with _LOCK:
        return _CONTROLS.get(_control_key(kind, key))


def release(kind, key) -> None:
    """任务结束后释放控制对象，任务中心的按钮随之消失。"""
    with _LOCK:
        _CONTROLS.pop(_control_key(kind, key), None)


def control_state(kind, key) -> str | None:
    handle = get_control(kind, key)
    return handle.state() if handle is not None else None


def clear_controls() -> None:
    """丢弃全部任务控制对象（停机或测试隔离使用）。"""
    with _LOCK:
        _CONTROLS.clear()


def active_control_count(kind: str | None = None) -> int:
    with _LOCK:
        if kind is None:
            return len(_CONTROLS)
        return sum(1 for name, _ in _CONTROLS if name == str(kind))


def apply_action(action: str, kind, key) -> dict:
    """对单个任务执行 pause / resume / cancel。"""
    handle = get_control(kind, key)
    if handle is None:
        return {"ok": False, "error": "任务不在可控制状态（可能已结束或未在执行队列中）", "status": 409}
    if action == "pause":
        changed = handle.pause()
        state = handle.state()
        if state == _STATE_CANCELLED:
            return {"ok": False, "error": "任务已取消，无法暂停", "status": 409}
        return {"ok": True, "message": "任务已暂停" if changed else "任务已经处于暂停状态",
                "state": state, "changed": changed}
    if action == "resume":
        changed = handle.resume()
        state = handle.state()
        if state == _STATE_CANCELLED:
            return {"ok": False, "error": "任务已取消，无法恢复", "status": 409}
        if state != _STATE_RUNNING:
            return {"ok": False, "error": "任务当前不支持恢复", "status": 409}
        return {"ok": True, "message": "任务已恢复" if changed else "任务未处于暂停状态",
                "state": state, "changed": changed}
    if action == "cancel":
        changed = handle.cancel()
        return {"ok": True, "message": "已发送取消信号" if changed else "任务已经取消",
                "state": _STATE_CANCELLED, "changed": changed}
    return {"ok": False, "error": "不支持的任务操作", "status": 404}


def checkpoint(kind, key) -> None:
    """工作线程安全检查点；没有控制对象时不做任何事。"""
    handle = get_control(kind, key)
    if handle is not None:
        handle.checkpoint()


def sleep(kind, key, seconds: float) -> None:
    """可中断等待；没有控制对象时退化为普通 ``time.sleep``。"""
    seconds = max(0.0, float(seconds or 0.0))
    handle = get_control(kind, key)
    if handle is None:
        if seconds:
            time.sleep(seconds)
        return
    handle.wait(seconds)


def cancel_matching(kind: str, keys) -> int:
    """批量取消同一类型下的多个任务，返回实际发出取消信号的数量。"""
    wanted = {str(key) for key in keys}
    if not wanted:
        return 0
    with _LOCK:
        handles = [handle for (name, key), handle in _CONTROLS.items() if name == str(kind) and key in wanted]
    return sum(1 for handle in handles if handle.cancel())
