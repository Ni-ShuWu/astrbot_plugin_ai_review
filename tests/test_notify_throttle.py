"""通知节流测试（fix/notify-throttle）。

同一内容在窗口期内只发送一次；不同内容互不影响；窗口过期后可
再次发送；窗口为 0 时不节流。
"""

from __future__ import annotations

import sys
import time
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

from _plugin_under_test.utils.throttle import NotifyThrottle  # noqa: E402


class NotifyThrottleTest(unittest.TestCase):
    def test_same_message_throttled_within_window(self) -> None:
        throttle = NotifyThrottle(window_seconds=60)
        self.assertTrue(throttle.should_notify("模型失败 A"))
        self.assertFalse(throttle.should_notify("模型失败 A"))
        self.assertFalse(throttle.should_notify("模型失败 A"))

    def test_different_messages_not_throttled(self) -> None:
        throttle = NotifyThrottle(window_seconds=60)
        self.assertTrue(throttle.should_notify("消息一"))
        self.assertTrue(throttle.should_notify("消息二"))

    def test_allows_again_after_window_expires(self) -> None:
        throttle = NotifyThrottle(window_seconds=0.02)
        self.assertTrue(throttle.should_notify("模型失败 B"))
        self.assertFalse(throttle.should_notify("模型失败 B"))
        time.sleep(0.05)
        self.assertTrue(throttle.should_notify("模型失败 B"))

    def test_zero_window_disables_throttling(self) -> None:
        throttle = NotifyThrottle(window_seconds=0)
        for _ in range(3):
            self.assertTrue(throttle.should_notify("重复消息"))

    def test_blank_message_not_throttled(self) -> None:
        throttle = NotifyThrottle(window_seconds=60)
        self.assertTrue(throttle.should_notify(""))
        self.assertTrue(throttle.should_notify(""))

    def test_entries_capped_to_prevent_growth(self) -> None:
        throttle = NotifyThrottle(window_seconds=60, max_entries=2)
        self.assertTrue(throttle.should_notify("a"))
        self.assertTrue(throttle.should_notify("b"))
        # 容量已满：新消息触发清理，旧键失效
        self.assertTrue(throttle.should_notify("c"))
        self.assertTrue(throttle.should_notify("a"))


if __name__ == "__main__":
    unittest.main()
