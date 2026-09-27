"""被动审核背压测试（fix/review-backpressure）。

ConcurrencyGate 提供有界并发门闩：并发任务数达到上限时立即拒绝
（不排队），保证高峰消息不会无界堆积后台任务。
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

# 以固定别名的命名空间包方式导入插件源码（与 tests/test_core.py 一致）。
_REPO_ROOT = Path(__file__).resolve().parent.parent
_PKG = "_plugin_under_test"
_pkg = types.ModuleType(_PKG)
_pkg.__path__ = [str(_REPO_ROOT)]  # 命名空间包
_pkg.__package__ = _PKG
sys.modules[_PKG] = _pkg
sys.path.insert(0, str(_REPO_ROOT))

from _plugin_under_test.utils.concurrency import ConcurrencyGate  # noqa: E402


class ConcurrencyGateTest(unittest.TestCase):
    def test_acquires_up_to_limit(self) -> None:
        gate = ConcurrencyGate(limit=3)
        for _ in range(3):
            self.assertTrue(gate.try_acquire())
        self.assertEqual(gate.inflight, 3)

    def test_rejects_when_full(self) -> None:
        gate = ConcurrencyGate(limit=2)
        self.assertTrue(gate.try_acquire())
        self.assertTrue(gate.try_acquire())
        self.assertFalse(gate.try_acquire())
        self.assertEqual(gate.inflight, 2)

    def test_release_frees_slot(self) -> None:
        gate = ConcurrencyGate(limit=1)
        self.assertTrue(gate.try_acquire())
        self.assertFalse(gate.try_acquire())
        gate.release()
        self.assertTrue(gate.try_acquire())

    def test_limit_change_takes_effect(self) -> None:
        gate = ConcurrencyGate(limit=1)
        self.assertTrue(gate.try_acquire())
        gate.limit = 3
        self.assertTrue(gate.try_acquire())
        self.assertTrue(gate.try_acquire())
        self.assertEqual(gate.inflight, 3)

    def test_limit_minimum_is_one(self) -> None:
        gate = ConcurrencyGate(limit=0)
        self.assertEqual(gate.limit, 1)

    def test_double_release_is_harmless(self) -> None:
        gate = ConcurrencyGate(limit=1)
        self.assertTrue(gate.try_acquire())
        gate.release()
        gate.release()
        self.assertEqual(gate.inflight, 0)


if __name__ == "__main__":
    unittest.main()
