"""Parked tasks retain their thread without occupying execution capacity."""
import threading
import time
import unittest
from unittest.mock import patch

from core.task_control import DynamicPool, TaskCancelled


class ParkedPoolTest(unittest.TestCase):
    def setUp(self):
        self.pools = []
        self.releases = []
        self.errors = []

    def pool(self, workers=1, max_workers=1):
        pool = DynamicPool("parked-test", workers, max_workers=max_workers)
        self.pools.append(pool)
        return pool

    def event(self):
        event = threading.Event()
        self.releases.append(event)
        return event

    def submit(self, pool, fn):
        def checked():
            try:
                fn()
            except BaseException as exc:
                self.errors.append(exc)
        self.assertTrue(pool.submit(checked))

    def wait(self, event):
        self.assertTrue(event.wait(3), "worker did not reach expected checkpoint")

    def eventually(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            threading.Event().wait(0.005)
        self.assertTrue(predicate())

    def tearDown(self):
        for event in self.releases:
            event.set()
        for pool in self.pools:
            pool.shutdown()
            self.assertTrue(pool.wait_idle(3), pool.status())
            self.eventually(lambda: pool.status()["threads"] == 0)
        self.assertEqual(self.errors, [])

    def test_single_worker_replacement_retains_thread_and_local_object(self):
        pool = self.pool()
        parked = self.event()
        finished_b = self.event()
        resumed = self.event()
        local = threading.local()
        threads = []

        def task_a():
            owner = threading.current_thread()
            obj = object()  # Stand-in for a thread-bound Playwright object.
            local.browser = obj
            with pool.park_current():
                threads.append(owner)
                parked.set()
                self.wait(finished_b)
            self.assertIs(threading.current_thread(), owner)
            self.assertIs(local.browser, obj)
            resumed.set()

        def task_b():
            threads.append(threading.current_thread())
            self.assertEqual(pool.status()["running"], 1)
            self.assertEqual(pool.status()["waiting"], 1)
            finished_b.set()

        self.submit(pool, task_a)
        self.wait(parked)
        self.assertFalse(pool.wait_idle(0.02))
        self.assertEqual(pool.status()["running"], 0)
        self.submit(pool, task_b)
        self.wait(resumed)
        self.assertTrue(pool.wait_idle(3))
        self.assertIsNot(threads[0], threads[1])
        self.assertEqual(pool.status()["completed"], 2)
        self.assertEqual(pool.status()["waiting"], 0)

    def test_limit_rejection_does_not_release_running_slot(self):
        pool = self.pool(max_workers=2)
        release = self.event()
        parked = [self.event(), self.event()]
        rejected = self.event()

        def waiter(index):
            with pool.park_current():
                parked[index].set()
                self.wait(release)

        for index in range(2):
            self.submit(pool, lambda index=index: waiter(index))
            self.wait(parked[index])

        def overflow():
            with self.assertRaisesRegex(RuntimeError, "parked waiting limit.*max_workers=2"):
                with pool.park_current():
                    self.fail("limit must reject before entering context")
            status = pool.status()
            self.assertEqual(status["waiting"], 2)
            self.assertEqual(status["running"], 1)
            self.assertLessEqual(status["threads"], 3)
            rejected.set()

        self.submit(pool, overflow)
        self.wait(rejected)
        release.set()
        self.assertTrue(pool.wait_idle(3))
        self.assertEqual(pool.status()["completed"], 3)

    def test_exception_and_cancellation_restore_counts(self):
        pool = self.pool()

        def task(error):
            with pool.park_current():
                self.assertEqual(pool.status()["waiting"], 1)
                raise error

        with self.assertLogs("core.task_control", level="INFO"):
            pool.submit(task, ValueError("verification failed"))
            self.assertTrue(pool.wait_idle(3))
            pool.submit(task, TaskCancelled("caller cancelled"))
            self.assertTrue(pool.wait_idle(3))
        status = pool.status()
        self.assertEqual((status["running"], status["waiting"]), (0, 0))
        self.assertEqual((status["completed"], status["cancelled"]), (2, 1))
        ran = self.event()
        self.submit(pool, ran.set)
        self.wait(ran)

    def test_replacement_start_failure_rolls_back_parking(self):
        pool = self.pool()
        checked = self.event()

        def task():
            with patch.object(threading.Thread, "start", side_effect=RuntimeError("cannot start thread")):
                with self.assertRaisesRegex(RuntimeError, "cannot start thread"):
                    with pool.park_current():
                        self.fail("failed replacement must not enter parked body")
            status = pool.status()
            self.assertEqual((status["running"], status["waiting"], status["threads"]), (1, 0, 1))
            checked.set()

        self.submit(pool, task)
        self.wait(checked)
        self.assertTrue(pool.wait_idle(3))
        ran = self.event()
        self.submit(pool, ran.set)
        self.wait(ran)

    def test_resume_waits_for_capacity_and_precedes_queued_work(self):
        pool = self.pool()
        parked = self.event()
        leave_park = self.event()
        running_b = self.event()
        release_b = self.event()
        resumed = self.event()
        order = []

        def task_a():
            with pool.park_current():
                parked.set()
                self.wait(leave_park)
            self.assertEqual(pool.status()["running"], 1)
            order.append("a")
            resumed.set()

        def task_b():
            running_b.set()
            self.wait(release_b)

        self.submit(pool, task_a)
        self.wait(parked)
        self.submit(pool, task_b)
        self.wait(running_b)
        self.submit(pool, lambda: order.append("c"))
        leave_park.set()
        self.eventually(lambda: pool._resuming == 1)
        self.assertFalse(resumed.is_set())
        self.assertEqual((pool.status()["running"], pool.status()["waiting"]), (1, 1))
        release_b.set()
        self.assertTrue(pool.wait_idle(3))
        self.assertEqual(order, ["a", "c"])

    def test_shrink_blocks_resume_until_all_existing_capacity_finishes(self):
        pool = self.pool(workers=2, max_workers=2)
        parked = self.event()
        leave_park = self.event()
        running = [self.event(), self.event()]
        release = [self.event(), self.event()]
        resumed = self.event()

        def task_a():
            with pool.park_current():
                parked.set()
                self.wait(leave_park)
            self.assertEqual(pool.status()["running"], 1)
            resumed.set()

        def blocker(index):
            running[index].set()
            self.wait(release[index])

        self.submit(pool, task_a)
        self.wait(parked)
        for index in range(2):
            self.submit(pool, lambda index=index: blocker(index))
            self.wait(running[index])
        self.assertEqual(pool.set_workers(1), 1)
        leave_park.set()
        self.eventually(lambda: pool._resuming == 1)
        release[0].set()
        self.eventually(lambda: pool.status()["running"] == 1)
        self.assertFalse(resumed.is_set())
        release[1].set()
        self.wait(resumed)
        self.assertTrue(pool.wait_idle(3))
        self.eventually(lambda: pool.status()["threads"] == 1)

    def test_shutdown_wait_includes_parked_and_does_not_cancel_it(self):
        pool = self.pool()
        parked = self.event()
        release = self.event()
        stopped = self.event()
        resumed = self.event()

        def task():
            with pool.park_current():
                parked.set()
                self.wait(release)
            resumed.set()

        self.submit(pool, task)
        self.wait(parked)
        shutdown_thread = threading.Thread(target=lambda: (pool.shutdown(wait=True), stopped.set()))
        shutdown_thread.start()
        self.eventually(lambda: pool.closed)
        self.assertFalse(pool.submit(lambda: None))
        self.assertFalse(stopped.wait(0.02))
        self.assertFalse(pool.wait_idle(0.02))
        self.assertEqual(pool.status()["waiting"], 1)
        release.set()
        self.wait(stopped)
        shutdown_thread.join(3)
        self.assertTrue(resumed.is_set())

    def test_shutdown_queue_can_still_run_when_current_task_parks(self):
        pool = self.pool()
        entered = self.event()
        may_park = self.event()
        finished_b = self.event()
        resumed = self.event()

        def task_a():
            entered.set()
            self.wait(may_park)
            with pool.park_current():
                self.wait(finished_b)
            resumed.set()

        self.submit(pool, task_a)
        self.wait(entered)
        self.submit(pool, finished_b.set)
        pool.shutdown()
        may_park.set()
        self.wait(resumed)
        self.assertTrue(pool.wait_idle(3))
        self.assertEqual(pool.status()["completed"], 2)

    def test_external_other_pool_and_nested_calls_are_noop(self):
        pool = self.pool()
        other = self.pool()
        before = pool.status()
        with pool.park_current():
            self.assertEqual(pool.status(), before)
        done = self.event()

        def task():
            with other.park_current():
                self.assertEqual(other.status()["waiting"], 0)
                self.assertEqual(pool.status()["running"], 1)
            with pool.park_current():
                with pool.park_current():
                    self.assertEqual(pool.status()["waiting"], 1)
                    self.assertEqual(pool.status()["running"], 0)
            self.assertEqual(pool.status()["waiting"], 0)
            self.assertEqual(pool.status()["running"], 1)
            done.set()

        self.submit(pool, task)
        self.wait(done)


if __name__ == "__main__":
    unittest.main()
