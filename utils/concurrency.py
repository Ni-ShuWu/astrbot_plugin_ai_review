"""有界并发门闩。

用于被动审核背压：并发任务数达到上限时立即拒绝（不排队），
避免高峰消息无界堆积后台任务。独立模块（仅依赖标准库），
供 main.py 的群消息监听入口使用。
"""

from __future__ import annotations


class ConcurrencyGate:
    """有界并发门闩（进程内计数，非阻塞拒绝）。"""

    def __init__(self, limit: int = 10) -> None:
        """初始化门闩。

        Args:
            limit: 并发上限（至少为 1）。
        """
        self.limit = max(1, int(limit))
        self._inflight = 0

    @property
    def inflight(self) -> int:
        """当前进行中的任务数。"""
        return self._inflight

    def try_acquire(self) -> bool:
        """尝试占用一个并发槽位；已满时立即返回 False。"""
        if self._inflight >= self.limit:
            return False
        self._inflight += 1
        return True

    def release(self) -> None:
        """释放一个并发槽位（多余释放无副作用）。"""
        if self._inflight > 0:
            self._inflight -= 1
