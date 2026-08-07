"""处罚执行器（策略模式 + 流水线）。

处罚类型：warn / mute / kick / ban / blacklist。
每个处罚由若干可复用阶段（stage）按流水线顺序执行，
warn/mute/kick/ban 通过 PlatformExecutor 调用平台能力，
blacklist 走黑库适配器（适配器不可用或未启用时自动跳过）。
各策略实现见 punish_stages.py。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import safe_int
from ..models import PunishmentType, ReviewTask
from .punish_stages import (
    BanStrategy,
    BlacklistStrategy,
    KickStrategy,
    MuteStrategy,
    PunishmentStrategy,
    WarnStrategy,
)

# 默认处罚流水线：suggestion -> 有序阶段列表
DEFAULT_PIPELINES: dict[str, list[str]] = {
    PunishmentType.WARN.value: [PunishmentType.WARN.value],
    PunishmentType.MUTE.value: [PunishmentType.WARN.value, PunishmentType.MUTE.value],
    PunishmentType.KICK.value: [PunishmentType.WARN.value, PunishmentType.KICK.value],
    PunishmentType.BAN.value: [PunishmentType.WARN.value, PunishmentType.BAN.value],
    PunishmentType.BLACKLIST.value: [
        PunishmentType.WARN.value,
        PunishmentType.BLACKLIST.value,
    ],
}


@dataclass
class PunishmentResult:
    """处罚流水线的结构化结果。

    Attributes:
        failed: 任一阶段是否执行失败（平台接口报错）。调用方据此
            决定回滚任务为待处理，避免处罚失败后任务被永久记录为
            已通过。
        message: 展示给管理员的各阶段结果汇总。
    """

    failed: bool
    message: str

    def __str__(self) -> str:
        return self.message


class PlatformExecutor:
    """平台能力执行器（禁言 / 踢出 / 发送消息）。"""

    def __init__(self, context: Any) -> None:
        """初始化执行器。

        Args:
            context: AstrBot 插件 Context 对象。
        """
        self._context = context

    async def ban_user(
        self,
        platform_id: str,
        group_id: str,
        user_id: str,
        duration: int,
    ) -> str:
        """禁言用户。

        Args:
            platform_id: 平台实例 ID。
            group_id: 群号。
            user_id: 用户 ID。
            duration: 禁言时长（秒）。

        Returns:
            空字符串表示成功，否则为错误描述。
        """
        return await self._call(
            platform_id,
            "set_group_ban",
            group_id=group_id,
            user_id=user_id,
            duration=duration,
        )

    async def kick_user(
        self,
        platform_id: str,
        group_id: str,
        user_id: str,
    ) -> str:
        """踢出用户。

        Args:
            platform_id: 平台实例 ID。
            group_id: 群号。
            user_id: 用户 ID。

        Returns:
            空字符串表示成功，否则为错误描述。
        """
        return await self._call(
            platform_id,
            "set_group_kick",
            group_id=group_id,
            user_id=user_id,
        )

    async def send_message(self, session: str, text: str) -> str:
        """向指定会话发送消息。

        Args:
            session: 统一消息来源字符串（unified_msg_origin）。
            text: 消息文本。

        Returns:
            空字符串表示成功，否则为错误描述。
        """
        try:
            from astrbot.api.event import MessageChain
            from astrbot.api.message_components import Plain

            sent = await self._context.send_message(
                session, MessageChain([Plain(text)])
            )
            if sent is False:
                return f"未找到会话对应的平台: {session}"
            return ""
        except Exception as exc:
            return f"发送消息失败: {exc!s}"

    async def send_forward(
        self,
        session: str,
        items: list[tuple[str, str, str]],
    ) -> str:
        """向指定会话发送合并转发消息（聊天记录式展开，节约显示空间）。

        Args:
            session: 统一消息来源字符串（unified_msg_origin）。
            items: (昵称, QQ, 文本) 三元组列表，每个元素一条转发节点。

        Returns:
            空字符串表示成功，否则为错误描述（调用方回退文本发送）。
        """
        try:
            from astrbot.api.event import MessageChain
            from astrbot.api.message_components import Node, Nodes, Plain

            nodes = [
                Node(
                    name=name or "AI 审核",
                    uin=str(uin or "0"),
                    content=[Plain(text)],
                )
                for name, uin, text in items
                if text
            ]
            if not nodes:
                return "转发内容为空。"
            sent = await self._context.send_message(
                session, MessageChain([Nodes(nodes=nodes)])
            )
            if sent is False:
                return f"未找到会话对应的平台: {session}"
            return ""
        except Exception as exc:
            return f"合并转发发送失败: {exc!s}"

    async def is_group_moderator(
        self,
        platform_id: str,
        group_id: str,
        user_id: str,
    ) -> tuple[bool, str]:
        """Return whether the current OneBot member role can manage the group."""
        try:
            adapter = self._context.get_platform_inst(platform_id)
            if adapter is None:
                return False, f"未找到平台实例 {platform_id}"
            client = getattr(adapter, "get_client", None)
            bot = client() if callable(client) else getattr(adapter, "bot", None)
            if bot is None or not hasattr(bot, "call_action"):
                return False, f"平台 {platform_id} 不支持获取群成员信息"
            member = await bot.call_action(
                action="get_group_member_info",
                group_id=group_id,
                user_id=user_id,
                no_cache=True,
            )
        except Exception as exc:
            return False, f"获取群成员信息失败: {exc!s}"
        if not isinstance(member, dict):
            return False, "群成员信息格式错误"
        role = member.get("role")
        if role in {"owner", "admin"}:
            return True, ""
        if not isinstance(role, str):
            return False, "群成员角色缺失"
        return False, f"群成员角色无权限: {role}"

    async def _call(self, platform_id: str, action: str, **params: Any) -> str:
        """调用平台 OneBot 动作。

        Returns:
            空字符串表示成功，否则为错误描述。
        """
        adapter = self._context.get_platform_inst(platform_id)
        if adapter is None:
            return f"未找到平台实例 {platform_id}"
        client = getattr(adapter, "get_client", None)
        bot = client() if callable(client) else getattr(adapter, "bot", None)
        if bot is None or not hasattr(bot, "call_action"):
            return f"平台 {platform_id} 不支持操作 {action}"
        try:
            await bot.call_action(action=action, **params)
            return ""
        except Exception as exc:
            return f"执行 {action} 失败: {exc!s}"


class Punisher:
    """处罚执行器（流水线）。

    按任务建议的处罚类型选择流水线（有序阶段列表），依次执行并汇总结果。
    支持通过配置 punish_pipeline 覆盖默认流水线，便于扩展新的处罚流程。
    """

    def __init__(
        self,
        executor: PlatformExecutor,
        blacklist_adapter: Any = None,
        get_config: Any = None,
    ) -> None:
        """初始化处罚器。

        Args:
            executor: 平台能力执行器。
            blacklist_adapter: BlacklistAdapter 实例，可为 None。
            get_config: 返回配置的回调，用于读取 mute_duration 等。
        """
        self._executor = executor
        self._get_config = get_config
        # 固定阶段实例；mute 阶段每次按群禁言时长构造（_stage_for）
        self._stages: dict[str, PunishmentStrategy] = {
            PunishmentType.WARN.value: WarnStrategy(executor),
            PunishmentType.KICK.value: KickStrategy(executor),
            PunishmentType.BAN.value: BanStrategy(executor),
            PunishmentType.BLACKLIST.value: BlacklistStrategy(blacklist_adapter),
        }

    def _config_for(self, group_id: str = "") -> tuple[bool, int, dict[str, list[str]]]:
        """读取按群生效的处罚配置（局部值）。

        返回 (enable_blacklist, mute_duration, pipelines)。不写实例状态，
        多群交错执行时互不覆盖（W2）。
        """
        if self._get_config is None:
            return False, 600, dict(DEFAULT_PIPELINES)
        config = self._get_config(group_id)
        blacklist_enabled = bool(config.get("enable_blacklist", False))
        mute_duration = safe_int(config.get("mute_duration"), 600)
        pipelines: dict[str, list[str]] = dict(DEFAULT_PIPELINES)
        raw_pipeline = config.get("punish_pipeline") or {}
        if isinstance(raw_pipeline, dict):
            pipelines.update(
                {
                    str(key): [str(item) for item in value]
                    for key, value in raw_pipeline.items()
                    if isinstance(value, list)
                }
            )
        return blacklist_enabled, mute_duration, pipelines

    def _stage_for(self, name: str, mute_duration: int) -> PunishmentStrategy | None:
        """获取阶段实例；mute 阶段按当前群的禁言时长构造。"""
        if name == PunishmentType.MUTE.value:
            return MuteStrategy(self._executor, mute_duration)
        return self._stages.get(name)

    async def execute(self, task: ReviewTask, admin_id: str) -> PunishmentResult:
        """按任务建议的处罚类型执行整条流水线。

        Args:
            task: 已通过的审核任务。
            admin_id: 确认执行的管理员 ID。

        Returns:
            结构化结果：任一阶段失败时 failed=True（调用方应回滚任务）。
        """
        blacklist_enabled, mute_duration, pipelines = self._config_for(task.group_id)
        stage_names = pipelines.get(task.result.suggestion) or [
            task.result.suggestion
        ]
        lines = []
        failed = False
        for name in stage_names:
            if name == PunishmentType.BLACKLIST.value and not blacklist_enabled:
                lines.append("黑库同步未启用（enable_blacklist=false），跳过。")
                continue
            stage = self._stage_for(name, mute_duration)
            if stage is None:
                lines.append(f"[{name}] 未知处罚阶段，已跳过。")
                continue
            result = await stage.execute(task, admin_id)
            if not result.success:
                failed = True
            lines.append(result.message)
        return PunishmentResult(failed=failed, message="\n".join(lines))
