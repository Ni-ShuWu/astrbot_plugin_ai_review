"""AstrBot AI 审核插件入口。

装配各模块（配置/聊天缓存/Prompt/LLM/工作流/队列/处罚/黑库适配器/规则引擎），
注册群消息监听（被动审核）与管理员命令（主动审核），
并运行规则候选定时推送循环。
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .adapters.pimeng import PimengBlacklistAdapter
from .commands.config import ConfigCommandMixin
from .commands.review import ReviewCommandMixin
from .config import ConfigManager, safe_int
from .prompt import PromptManager
from .review.persistence import KVStore
from .review.history import HistoryCache
from .review.punishment import PlatformExecutor, Punisher
from .review.queue import ReviewQueue
from .review.rules import RuleEngine
from .review.stats import StatsStore
from .review.workflow import ReviewWorkflow
from .utils.llm import LLMClient
from .utils.logger import get_logger
from .utils.throttle import NotifyThrottle

logger = get_logger()

_PLUGIN_NAME = "astrbot_plugin_ai_review"
_PLUGIN_AUTHOR = "Ni-ShuWu&kelai141"
_PLUGIN_DESC = "基于 AstrBot 大模型的群聊 AI 审核助手，生成审核建议供管理员确认后执行处罚。"
_PLUGIN_VERSION = "1.22"

_PUSH_CHECK_INTERVAL = 60  # 推送循环检查间隔（秒）


@register(_PLUGIN_NAME, _PLUGIN_AUTHOR, _PLUGIN_DESC, _PLUGIN_VERSION)
class AiReviewPlugin(ReviewCommandMixin, ConfigCommandMixin, Star):
    """AI 审核插件主类。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        """初始化插件并装配各模块。

        Args:
            context: AstrBot 插件上下文。
            config: AstrBot 传入的插件配置对象。
        """
        super().__init__(context, config)
        self._bg_tasks: set[asyncio.Task] = set()
        self._terminating = False
        self._notify_throttle = NotifyThrottle()
        self._kv = KVStore(self.get_kv_data, self.put_kv_data)
        self.config = ConfigManager(config if config else {})
        get_config = self._get_config
        plugin_dir = os.path.dirname(os.path.abspath(__file__))

        self.history = HistoryCache(get_config)
        self.prompt = PromptManager(plugin_dir, get_config)
        self.queue = ReviewQueue(store=self._kv, get_config=get_config)
        self.adapter = PimengBlacklistAdapter(context)
        self.llm = LLMClient(context, get_config, notifier=self._notify_admin)
        self.stats = StatsStore(self._kv)
        self.rules = RuleEngine(self._kv, get_config, notifier=self._notify_admin)
        self.executor = PlatformExecutor(context)  # 先于 workflow：供群主/群管免审注入
        self.workflow = ReviewWorkflow(
            self.history,
            self.prompt,
            self.llm,
            self.queue,
            get_config,
            stats=self.stats,
            store=self._kv,
            rules=self.rules,
            executor=self.executor,
            blacklist=self.adapter,
        )
        self.punisher = Punisher(self.executor, self.adapter, get_config)
        self._last_push_ts = 0.0

    async def initialize(self) -> None:
        """插件激活时从 KV 恢复持久化状态并启动推送循环。"""
        await self.config.load_overrides(self._kv)
        await self.queue.load()
        await self.stats.load()
        await self.rules.load()
        await self.workflow.load_state()
        self._spawn(self._sediment_push_loop())

    async def terminate(self) -> None:
        """取消并等待插件管理的后台任务。"""
        self._terminating = True
        tasks = tuple(self._bg_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @filter.command("review")
    async def cmd_review(
        self,
        event: AstrMessageEvent,
        target: str = "",
        sub: str = "",
    ):
        """AI 审核命令入口（逻辑见 commands.review._cmd_review）。

        AstrBot 按 handler 的 __module__ 与插件主模块路径匹配来绑定插件实例，
        因此指令入口必须定义在本文件（main.py），mixin 中的逻辑经此委托。
        权限：AstrBot 管理员放行；群管模式下（regex_approval_permission=
        group_admin）本群群主/群管可审批规则候选（rule approve/deny）与
        审核任务（pass/reject）。
        """
        try:
            allowed = bool(event.is_admin())
        except Exception:
            allowed = False
        if not allowed:
            command = (target or "").strip().lower()
            parts = (
                self._rule_command_parts(event, sub)
                if command == "rule"
                else ()
            )
            allowed = await self._can_manage_rule_candidate(event, parts)
            if not allowed and command in ("pass", "reject"):
                allowed = await self._can_approve_task(event, (sub or "").strip())
        if not allowed:
            yield event.plain_result("❌ 权限不足。")
            return
        async for result in self._cmd_review(event, target, sub):
            yield result

    @filter.command("reviewconfig")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_reviewconfig(
        self,
        event: AstrMessageEvent,
        key: str = "",
        value: str = "",
    ):
        """查看或修改插件配置（逻辑见 commands.config._cmd_reviewconfig）。"""
        async for result in self._cmd_reviewconfig(event, key, value):
            yield result

    async def _sediment_push_loop(self) -> None:
        """定时向规则候选来源群推送待审批请求（每分钟检查一次）。"""
        while True:
            try:
                await asyncio.sleep(_PUSH_CHECK_INTERVAL)
                if not bool(self.config.get("regex_sediment", True)):
                    continue
                interval = safe_int(self.config.get("regex_push_interval"), 30)
                if interval <= 0:
                    continue
                now = time.time()
                if now - self._last_push_ts < interval * 60:
                    continue
                await self.rules.purge_expired_candidates()
                await self._push_rule_candidates()
                self._last_push_ts = time.time()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("[AI审核] 沉淀推送循环异常：%s", exc, exc_info=True)
                await asyncio.sleep(_PUSH_CHECK_INTERVAL)

    async def _push_rule_candidates(self) -> None:
        """按群的推送目标配置，将待审批候选推送给管理员。

        目标由该群生效的 regex_push_target 决定：
        - group：推送到来源群聊天（群内可见，审批命令仅管理员可执行）
        - admin：私聊推送给该群 regex_push_admin（留空回退全局 admin_qq）
        - off：不推送（可用 /review rule pending 查看）
        """
        candidates = self.rules.candidates()
        if not candidates:
            return
        by_destination: dict[tuple[str, str], list[Any]] = {}
        for candidate in candidates:
            platform_id = str(getattr(candidate, "platform_id", "")).strip()
            group_id = str(getattr(candidate, "group_id", "")).strip()
            if not platform_id or not group_id:
                logger.warning(
                    "[AI审核] 候选 %s 缺少平台或群信息，跳过推送。",
                    getattr(candidate, "candidate_id", "?"),
                )
                continue
            by_destination.setdefault((platform_id, group_id), []).append(candidate)
        skipped_admin_ids: set[str] = set()
        for (platform_id, group_id), group_candidates in by_destination.items():
            config = self._get_config(group_id)
            target = str(config.get("regex_push_target", "group")).lower()
            if target == "off":
                continue
            if target == "admin":
                admin_ids = config.get("regex_push_admin") or self.config.get(
                    "admin_qq", []
                )
                if not admin_ids:
                    logger.warning(
                        "[AI审核] 群 %s 推送目标为 admin 但未配置接收管理员，跳过。",
                        group_id,
                    )
                    continue
                permission = str(
                    config.get("regex_approval_permission", "astrbot_admin")
                ).lower()
                for configured_admin_id in admin_ids:
                    admin_id = str(configured_admin_id).strip()
                    session = f"{platform_id}:FriendMessage:{admin_id}"
                    if permission == "group_admin":
                        try:
                            allowed, _error = await self.executor.is_group_moderator(
                                platform_id,
                                group_id,
                                admin_id,
                            )
                        except Exception:
                            allowed = False
                    else:
                        allowed = admin_id in self._astrbot_admin_ids(session)
                    if not allowed:
                        if admin_id not in skipped_admin_ids:
                            logger.warning(
                                "[AI审核] 接收者 %s 不具备当前审批权限，跳过私聊推送。",
                                admin_id,
                            )
                            skipped_admin_ids.add(admin_id)
                        continue
                    await self._push_candidates_to(
                        session,
                        group_candidates,
                        group_id,
                        config,
                    )
            else:  # group
                await self._push_candidates_to(
                    f"{platform_id}:GroupMessage:{group_id}",
                    group_candidates,
                    group_id,
                    config,
                )

    async def _push_candidates_to(
        self,
        session: str,
        candidates: list[Any],
        group_id: str,
        config: dict[str, Any],
    ) -> None:
        """向会话推送候选审批请求。

        候选数达到 regex_forward_threshold 时打包为合并转发（节约显示空间），
        转发失败自动降级为文本。
        """
        message = self.rules.build_push_message(candidates, group_id)
        if message is None:
            return
        threshold = safe_int(config.get("regex_forward_threshold"), 3)
        if threshold > 0 and len(candidates) >= threshold:
            items = [
                ("AI 审核", "0", self.rules.format_candidate_item(candidate))
                for candidate in candidates
            ]
            err = await self.executor.send_forward(session, items)
            if not err:
                return
            logger.warning(
                "[AI审核] 合并转发推送失败，回退文本（%s）：%s", session, err
            )
        await self._send_session(session, message)

    async def _send_session(self, session: str, message: str) -> None:
        """向指定会话发送文本消息，失败仅记录日志。"""
        try:
            err = await self.executor.send_message(session, message)
            if err:
                logger.warning("[AI审核] 推送发送失败（%s）：%s", session, err)
        except Exception as exc:
            logger.warning("[AI审核] 推送发送异常（%s）：%s", session, exc)

    def _spawn(self, coro: Any) -> asyncio.Task | None:
        """以受管方式创建后台任务，避免任务被 GC 且异常静默丢失。"""
        if self._terminating:
            coro.close()
            return None
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        task.add_done_callback(self._log_task_exception)
        return task

    @staticmethod
    def _log_task_exception(task: asyncio.Task) -> None:
        """记录后台任务异常，避免 'Task exception was never retrieved'。"""
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            logger.error(
                "[AI审核] 后台任务异常（任务=%s）：%s",
                task.get_name() or "unknown",
                exc,
                exc_info=exc,
            )

    def _get_config(self, group_id: str = "") -> dict:
        """返回当前配置字典（供各模块热加载，支持按群覆盖）。"""
        return self.config.effective(group_id)

    async def _notify_admin(self, message: str) -> None:
        """向配置的管理员发送告警消息（同内容按窗口去重，防刷屏）。

        Args:
            message: 告警内容。
        """
        try:
            window = safe_int(self.config.get("notify_throttle_seconds"), 300)
            self._notify_throttle.window = window
            if not self._notify_throttle.should_notify(message):
                logger.debug("[AI审核] 相同告警在节流窗口内，已跳过：%s", message[:80])
                return
            admin_ids = [str(uid) for uid in self.config.get("admin_qq", [])]
            if not admin_ids:
                return
            from astrbot.api.event import MessageChain
            from astrbot.api.message_components import Plain

            chain = MessageChain([Plain(message)])
            platform_manager = getattr(self.context, "platform_manager", None)
            platforms = (
                platform_manager.platform_insts if platform_manager else []
            )
            for platform in platforms:
                try:
                    platform_id = platform.meta().id
                except Exception:
                    continue
                for admin_id in admin_ids:
                    try:
                        sent = await self.context.send_message(
                            f"{platform_id}:FriendMessage:{admin_id}",
                            chain,
                        )
                    except Exception:
                        continue
                    if sent is False:  # 平台未找到，告警静默丢失（S2）
                        logger.warning(
                            "[AI审核] 管理员告警发送失败：未找到平台 %s",
                            platform_id,
                        )
        except Exception as exc:
            logger.error("[AI审核] 管理员通知发送失败：%s", exc, exc_info=True)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent) -> None:
        """群消息监听：后台触发被动审核，不阻塞消息响应。

        不在此处做前置判断（含按群覆盖的 review_mode/enable_history），
        统一交给 workflow.on_message 内部判断，避免全局配置漏掉群覆盖。
        """
        self._spawn(self.workflow.on_message(event))
