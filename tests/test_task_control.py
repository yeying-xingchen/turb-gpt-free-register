# -*- coding: utf-8 -*-
"""统一任务控制层：暂停/恢复/取消检查点与运行中动态并发。"""
import threading
import time
import unittest

from core import task_control


class ControlStateTest(unittest.TestCase):
    def test_control_lifecycle(self):
        handle = task_control.Control("live_check", 7)
        self.assertEqual(handle.state(), "running")
        self.assertFalse(handle.paused)

        self.assertTrue(handle.pause())
        self.assertFalse(handle.pause())
        self.assertEqual(handle.state(), "paused")
        self.assertTrue(handle.paused)

        self.assertTrue(handle.resume())
        self.assertFalse(handle.resume())
        self.assertEqual(handle.state(), "running")

        self.assertTrue(handle.cancel())
        self.assertFalse(handle.cancel())
        self.assertEqual(handle.state(), "cancelled")
        self.assertTrue(handle.cancelled)
        # 取消后再暂停/恢复都不生效
        self.assertFalse(handle.pause())
        self.assertFalse(handle.resume())

    def test_checkpoint_blocks_until_resume(self):
        handle = task_control.Control("plan_check", 3)
        handle.pause()
        entered = threading.Event()
        left = threading.Event()

        def worker():
            entered.set()
            handle.checkpoint()
            left.set()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        self.assertTrue(entered.wait(2))
        self.assertFalse(left.wait(0.2))
        handle.resume()
        self.assertTrue(left.wait(2))

    def test_checkpoint_raises_after_cancel(self):
        handle = task_control.Control("plan_check", 3)
        handle.pause()
        raised = []

        def worker():
            try:
                handle.checkpoint()
            except task_control.TaskCancelled:
                raised.append(True)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        time.sleep(0.05)
        handle.cancel()
        thread.join(2)
        self.assertEqual(raised, [True])

    def test_wait_is_interruptible_and_cancellable(self):
        handle = task_control.Control("live_check", 1)
        started = time.monotonic()
        raised = []

        def worker():
            try:
                handle.wait(5)
            except task_control.TaskCancelled:
                raised.append(True)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        time.sleep(0.05)
        handle.cancel()
        thread.join(2)
        self.assertEqual(raised, [True])
        self.assertLess(time.monotonic() - started, 2)


class RegistryTest(unittest.TestCase):
    def tearDown(self):
        for name in task_control.pool_names():
            task_control.remove_pool(name)
        task_control.clear_controls()

    def test_control_registry_and_actions(self):
        self.assertIsNone(task_control.get_control("live_check", 5))
        handle = task_control.control("live_check", 5)
        self.assertIs(task_control.get_control("live_check", 5), handle)
        # 整型与字符串 key 归一化为同一个任务
        self.assertIs(task_control.control("live_check", "5"), handle)

        result = task_control.apply_action("pause", "live_check", 5)
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "paused")
        self.assertEqual(task_control.control_state("live_check", 5), "paused")

        self.assertTrue(task_control.apply_action("resume", "live_check", 5)["ok"])
        self.assertEqual(task_control.control_state("live_check", 5), "running")

        self.assertTrue(task_control.apply_action("cancel", "live_check", 5)["ok"])
        self.assertEqual(task_control.control_state("live_check", 5), "cancelled")

        task_control.release("live_check", 5)
        self.assertIsNone(task_control.control_state("live_check", 5))
        self.assertFalse(task_control.apply_action("pause", "live_check", 5)["ok"])

    def test_checkpoint_without_control_is_noop(self):
        task_control.checkpoint("live_check", 999)  # 不应阻塞或抛错
        started = time.monotonic()
        task_control.sleep("live_check", 999, 0.05)
        self.assertGreaterEqual(time.monotonic() - started, 0.04)


class DynamicPoolTest(unittest.TestCase):
    def tearDown(self):
        for name in task_control.pool_names():
            task_control.remove_pool(name)

    def test_pool_runs_queued_work(self):
        pool = task_control.register_pool("unit-basic", 2)
        done = []
        lock = threading.Lock()

        def job(index):
            with lock:
                done.append(index)

        for index in range(6):
            self.assertTrue(pool.submit(job, index))
        self.assertTrue(pool.wait_idle(timeout=5))
        self.assertEqual(sorted(done), list(range(6)))

    def test_set_workers_scales_running_batch(self):
        pool = task_control.register_pool("unit-scale", 1)
        release = threading.Event()
        started = []
        lock = threading.Lock()

        def job(index):
            with lock:
                started.append(index)
            release.wait(5)

        for index in range(4):
            pool.submit(job, index)
        deadline = time.monotonic() + 3
        while len(started) < 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(started), 1)

        # 运行中调大并发：排队任务立刻被新线程接手
        self.assertEqual(pool.set_workers(4), 4)
        deadline = time.monotonic() + 3
        while len(started) < 4 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(started), 4)

        # 调小并发：多余线程在完成当前任务后退出
        self.assertEqual(pool.set_workers(2), 2)
        release.set()
        self.assertTrue(pool.wait_idle(timeout=5))
        deadline = time.monotonic() + 3
        while pool.status()["threads"] > 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(pool.status()["threads"], 2)

    def test_paused_task_does_not_start_until_resumed(self):
        pool = task_control.register_pool("unit-pause", 2)
        handle = task_control.control("unit-pause-kind", 1)
        handle.pause()
        ran = threading.Event()
        pool.submit(ran.set, control=handle)
        self.assertFalse(ran.wait(0.3))
        handle.resume()
        self.assertTrue(ran.wait(3))

    def test_cancelled_pending_task_is_skipped_with_callback(self):
        pool = task_control.register_pool("unit-cancel", 1)
        blocker = threading.Event()
        pool.submit(blocker.wait, 5)
        handle = task_control.control("unit-cancel-kind", 2)
        calls = []
        pool.submit(calls.append, "ran", control=handle, on_cancel=lambda: calls.append("cancelled"))
        handle.cancel()
        blocker.set()
        deadline = time.monotonic() + 5
        while "cancelled" not in calls and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(calls, ["cancelled"])

    def test_running_task_observes_cancel_at_checkpoint(self):
        pool = task_control.register_pool("unit-running-cancel", 1)
        handle = task_control.control("unit-running-cancel-kind", 3)
        seen = []

        def job():
            try:
                for _ in range(200):
                    handle.checkpoint(0.05)
                    time.sleep(0.01)
            except task_control.TaskCancelled:
                seen.append("cancelled")

        pool.submit(job, control=handle)
        time.sleep(0.1)
        handle.cancel()
        self.assertTrue(pool.wait_idle(timeout=5))
        self.assertEqual(seen, ["cancelled"])

    def test_pool_rejects_after_shutdown(self):
        pool = task_control.register_pool("unit-closed", 1)
        pool.shutdown()
        self.assertFalse(pool.submit(lambda: None))

    def test_clamp_workers_bounds(self):
        self.assertEqual(task_control.clamp_workers(0), 1)
        self.assertEqual(task_control.clamp_workers(-4), 1)
        self.assertEqual(task_control.clamp_workers(999), 16)
        self.assertEqual(task_control.clamp_workers("bad", 3), 3)


if __name__ == "__main__":
    unittest.main()
