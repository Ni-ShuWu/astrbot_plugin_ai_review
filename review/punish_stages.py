"""处罚策略实现（从 punishment.py 拆分）。

每种处罚一个策略类，通过 PlatformExecutor（平台能力）或
BlacklistAdapter（黑库）执行，供 Punisher 按流水线调度。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any

from ..models import PunishmentType, ReviewTask


@dataclass
class StageResult:
    """单个处罚阶段的结构化结果。

    Attributes:
        success: 阶段是否成功。跳过（无目标用户/缺少会话）视为成功；
            平台接口报错视为失败（调用方据此回滚任务）。
        message: 展示给管理员的执行结果描述。
    """

    success: bool
    message: str

    def __str__(self) -> str:
        return self.message


class PunishmentStrategy(abc.ABC):
    """处罚策略抽象基类。"""

    name: str

    @abc.abstractmethod
    async def execute(self, task: ReviewTask, admin_id: str) -> StageResult:
        """执行处罚。

        Args:
            task: 已通过管理员确认的审核任务。
            admin_id: 确认执行的管理员 ID。

        Returns:
            执行结果（结构化）。
        """


class WarnStrategy(PunishmentStrategy):
    """警告：向群内发送警告消息。"""

    name = PunishmentType.WARN.value

    def __init__(self, executor: Any) -> None:
        """初始化。

        Args:
            executor: 平台能力执行器。
        """
        self._executor = executor

    async def execute(self, task: ReviewTask, admin_id: str) -> StageResult:
        """发送警告消息。"""
        if not task.session_id:
            return StageResult(True, "警告未发送（缺少会话信息）。")
        text = (
            f"[AI审核] 用户 {task.nickname or task.user_id}（{task.user_id}）"
            f"已被管理员警告。原因：{task.result.reason or '无'}。"
        )
        err = await self._executor.send_message(task.session_id, text)
        if err:
            return StageResult(False, f"警告发送失败：{err}")
        return StageResult(True, "已发送警告消息。")


class MuteStrategy(PunishmentStrategy):
    """禁言：默认 10 分钟。"""

    name = PunishmentType.MUTE.value

    def __init__(self, executor: Any, duration: int = 600) -> None:
        """初始化。

        Args:
            executor: 平台能力执行器。
            duration: 禁言时长（秒），下限 60。
        """
        self._executor = executor
        self._duration = max(60, int(duration))

    async def execute(self, task: ReviewTask, admin_id: str) -> StageResult:
        """执行禁言。"""
        if not task.user_id:
            return StageResult(True, "无目标用户，跳过禁言。")
        err = await self._executor.ban_user(
            task.platform_id,
            task.group_id,
            task.user_id,
            self._duration,
        )
        if err:
            return StageResult(False, f"禁言失败：{err}")
        return StageResult(
            True,
            f"已禁言 {task.nickname or task.user_id}（{task.user_id}）{self._duration // 60} 分钟。",
        )


class KickStrategy(PunishmentStrategy):
    """踢出群聊。"""

    name = PunishmentType.KICK.value

    def __init__(self, executor: Any) -> None:
        """初始化。

        Args:
            executor: 平台能力执行器。
        """
        self._executor = executor

    async def execute(self, task: ReviewTask, admin_id: str) -> StageResult:
        """执行踢出。"""
        if not task.user_id:
            return StageResult(True, "无目标用户，跳过踢出。")
        err = await self._executor.kick_user(
            task.platform_id,
            task.group_id,
            task.user_id,
        )
        if err:
            return StageResult(False, f"踢出失败：{err}")
        return StageResult(
            True,
            f"已踢出 {task.nickname or task.user_id}（{task.user_id}）。",
        )


class BanStrategy(PunishmentStrategy):
    """拉黑：映射为长期禁言（30 天）。"""

    name = PunishmentType.BAN.value
    _BAN_DURATION = 2592000  # 30 天（秒）

    def __init__(self, executor: Any) -> None:
        """初始化。

        Args:
            executor: 平台能力执行器。
        """
        self._executor = executor

    async def execute(self, task: ReviewTask, admin_id: str) -> StageResult:
        """执行拉黑（长期禁言）。"""
        if not task.user_id:
            return StageResult(True, "无目标用户，跳过拉黑。")
        err = await self._executor.ban_user(
            task.platform_id,
            task.group_id,
            task.user_id,
            self._BAN_DURATION,
        )
        if err:
            return StageResult(False, f"拉黑失败：{err}")
        return StageResult(
            True,
            f"已拉黑（长期禁言）{task.nickname or task.user_id}（{task.user_id}）。",
        )


class BlacklistStrategy(PunishmentStrategy):
    """加入皮梦云黑库。"""

    name = PunishmentType.BLACKLIST.value

    def __init__(self, adapter: Any) -> None:
        """初始化。

        Args:
            adapter: BlacklistAdapter 实例，可为 None。
        """
        self._adapter = adapter

    async def execute(self, task: ReviewTask, admin_id: str) -> StageResult:
        """同步黑库（尽力而为：失败不视为处罚失败，避免整链回滚）。"""
        if self._adapter is None or not self._adapter.available:
            return StageResult(True, "黑库适配器不可用，跳过黑库同步。")
        return StageResult(True, await self._adapter.sync_task(task))
