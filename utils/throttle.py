"""通知节流器。

按消息内容去重：同一内容在窗口期内只发送一次告警，避免模型故障
等场景下管理员被相同告警刷屏。独立模块（仅依赖标准库），供
main.py 的管理员告警入口使用。
"""

from __future__ import annotations

import time


class NotifyThrottle:
    """按消息内容去重的通知节流器（进程内，不持久化）。"""

    def __init__(
        self,
        window_seconds: float = 300.0,
        max_entries: int = 100,
    ) -> None:
        """初始化节流器。

        Args:
            window_seconds: 去重窗口（秒）；0 表示不节流。
            max_entries: 记录条数上限；超限时清空以控制内存。
        """
        self.window = max(0.0, float(window_seconds))
        self._max_entries = max(1, int(max_entries))
        self._sent_at: dict[str, float] = {}

    def should_notify(self, message: str) -> bool:
        """判断是否应发送该消息，并在放行时记录发送时间。

        Args:
            message: 告警内容。

        Returns:
            应发送返回 True；窗口期内已发送过相同内容返回 False。
        """
        if self.window <= 0:
            return True
        key = (message or "").strip()
        if not key:
            return True
        now = time.time()
        last = self._sent_at.get(key)
        if last is not None and now - last < self.window:
            return False
        if len(self._sent_at) >= self._max_entries:
            self._sent_at.clear()
        self._sent_at[key] = now
        return True
