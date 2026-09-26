"""Тесты файлового протокола воркера: стейт, стоп-файл, pid, отказ двойного
запуска. WORKER_DATA_DIR подменяется до импорта — файлы живут во временной
папке, живые data/ не трогаются."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

# ВАЖНО: до импорта runtime — модуль читает WORKER_DATA_DIR на импорте.
_tmp_data = tempfile.mkdtemp(prefix="worker-test-")
os.environ["WORKER_DATA_DIR"] = _tmp_data

from src.workers.runtime import (                        # noqa: E402
    WorkerRuntime,
    pid_alive,
    pid_path,
    read_state,
    request_stop,
    state_path,
    stop_path,
)


def cleanup(marketplace: str) -> None:
    for path in (pid_path(marketplace), state_path(marketplace), stop_path(marketplace)):
        Path(path).unlink(missing_ok=True)


class WorkerRuntimeTests(unittest.TestCase):
    MP = "wb"

    def setUp(self):
        cleanup(self.MP)
        self.runtime = WorkerRuntime(self.MP)

    def tearDown(self):
        self.runtime.stop_event.set()
        cleanup(self.MP)

    def test_acquire_writes_pid_and_state(self):
        self.runtime.acquire()
        self.assertTrue(pid_alive(self.MP))
        state = read_state(self.MP)
        self.assertEqual(state["marketplace"], self.MP)
        self.assertEqual(state["pid"], os.getpid())
        self.assertIsNone(state["exit_status"])

    def test_acquire_refuses_second_run(self):
        self.runtime.acquire()
        with self.assertRaises(RuntimeError):
            WorkerRuntime(self.MP).acquire()

    def test_update_merges_fields_atomically(self):
        self.runtime.acquire()
        self.runtime.update(phase="Сбор", done=2, total=5)
        state = read_state(self.MP)
        self.assertEqual(state["phase"], "Сбор")
        self.assertEqual(state["done"], 2)
        self.assertEqual(state["total"], 5)
        self.assertTrue(state["updated_at"])
        # Файл валидный JSON целиком (атомарная запись не оставляет обрезков).
        json.loads(state_path(self.MP).read_text(encoding="utf-8"))

    def test_stop_file_sets_event(self):
        self.runtime.acquire()
        self.assertFalse(self.runtime.stop_requested())
        self.assertTrue(request_stop(self.MP))
        deadline = time.time() + 5
        while not self.runtime.stop_event.is_set() and time.time() < deadline:
            time.sleep(0.1)
        self.assertTrue(self.runtime.stop_event.is_set())

    def test_request_stop_without_worker_is_false(self):
        self.assertFalse(request_stop(self.MP))
        self.assertFalse(stop_path(self.MP).exists())

    def test_finish_removes_pid_and_sets_status(self):
        self.runtime.acquire()
        self.runtime.finish("ok", "Готово")
        self.assertFalse(pid_alive(self.MP))
        self.assertFalse(pid_path(self.MP).exists())
        state = read_state(self.MP)
        self.assertEqual(state["exit_status"], "ok")
        self.assertEqual(state["last_log"], "Готово")
        self.assertIsNotNone(state["finished_at"])

    def test_mark_blocked_flags_state(self):
        self.runtime.acquire()
        self.runtime.mark_blocked("WB закрыл доступ (HTTP 403)")
        self.assertTrue(read_state(self.MP)["blocked"])


if __name__ == "__main__":
    unittest.main()
