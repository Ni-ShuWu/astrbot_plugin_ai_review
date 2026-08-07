"""审核工作流。

职责：消息缓存与过滤、正则规则层预筛（命中跳过 LLM）、Prompt 组装、
LLM 调用、结果解析（含重试）、阈值判断、可选二次审核、审核任务入队、
结构化日志、违规规则沉淀。过滤/冷却/裁剪等辅助见 filters.py。
可选：审核前查询皮梦云黑库，命中则加重风险判定。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TYPE_CHECKING

from ..config import safe_int
from ..models import ChatRecord, ReviewLog, ReviewResult, ReviewTask
from ..prompt import PromptManager
from ..utils.logger import get_logger, log_event, log_review, review_context
from ..utils.parser import parse_with_llm_retry
from .filters import CooldownManager, MessageFilters, to_record, trim_records, user_content
from .history import HistoryCache
from .persistence import KVStore
from .queue import ReviewQueue
from .stats import StatsStore

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

logger = get_logger()

# 插件自身命令前缀：此类消息不进入聊天缓存，避免污染审核上下文。
_COMMAND_PREFIX = "/review"


class ReviewWorkflow:
    """审核工作流编排器。

    依赖注入 HistoryCache / PromptManager / LLMClient / ReviewQueue /
    RuleEngine（可选）。
    """

    def __init__(
        self,
        history: HistoryCache,
        prompt: PromptManager,
        llm: Any,
        queue: ReviewQueue,
        get_config: Callable[[str], dict[str, Any]],
        stats: StatsStore | None = None,
        store: KVStore | None = None,
        rules: Any | None = None,
        executor: Any | None = None,
        blacklist: Any | None = None,
    ) -> None:
        """初始化工作流。

        Args:
            history: 聊天记录缓存。
            prompt: Prompt 组装器。
            llm: LLMClient 实例。
            queue: 审核任务队列。
            get_config: 返回当前插件配置字典的回调，可接受群号参数。
            stats: 违规统计存储（可选）。
            store: KV 持久化存储（用于冷却表，可选）。
            rules: 正则规则引擎（可选，未配置时跳过规则层）。
            executor: 平台执行器（用于群主/群管过滤，可选）。
            blacklist: 黑库适配器（可选，审核前查询云黑加重判定）。
        """
        self.history = history
        self.prompt = prompt
        self.llm = llm
        self.queue = queue
        self._get_config = get_config
        self._stats = stats
        self._rules = rules
        self._blacklist = blacklist
        self._cooldown = CooldownManager(get_config, store)
        self.filters = MessageFilters(get_config, self._cooldown, executor)

    async def load_state(self) -> None:
        """从 KV 恢复冷却表。"""
        await self._cooldown.load_state()

    # ---------- 公共入口 ----------

    async def on_message(self, event: "AstrMessageEvent") -> None:
        """收到群消息后的被动审核入口。

        流程：缓存记录 → 判断触发方式 → 过滤 → 正则规则层预筛 → 审核。
        建议由外部以后台任务（asyncio.create_task）调用，避免阻塞消息响应。

        Args:
            event: AstrBot 消息事件。
        """
        group_id = event.get_group_id()
        if not group_id:
            return
        record = to_record(event, group_id)
        # 机器人消息与插件自身命令不缓存，避免污染审核上下文
        if not record.user_id or record.user_id == event.get_self_id():
            return
        if (record.content or "").strip().startswith(_COMMAND_PREFIX):
            return
        self.history.add(record)
        config = self._get_config(group_id)
        if not bool(config.get("enable_passive_review", True)):
            return
        if self.filters.review_mode(group_id) not in ("passive", "both"):
            return
        skip, reason = await self.filters.should_skip(event)
        if skip:
            logger.debug(
                "[AI审核] 消息被过滤：%s (群=%s 用户=%s)",
                reason,
                group_id,
                event.get_sender_id(),
            )
            return
        with review_context(
            group_id=group_id,
            user_id=event.get_sender_id(),
            provider=self.llm.last_provider_id,
        ):
            log_event("message_received", content=record.content[:80])
            # 正则规则层：命中已激活规则直接生成任务，跳过 LLM 调用
            if await self._try_rule_prefilter(event, record, config):
                return
            await self._run_review(
                event,
                target_user_id=event.get_sender_id(),
                target_nickname=event.get_sender_name(),
                current_record=record,
            )

    async def review_target(
        self,
        event: "AstrMessageEvent",
        target_user_id: str,
        target_nickname: str,
    ) -> ReviewTask | None:
        """主动审核指定用户（/review @成员 或 /review uid）。

        Args:
            event: 触发命令的消息事件。
            target_user_id: 目标用户 ID。
            target_nickname: 目标用户昵称。

        Returns:
            生成的审核任务；未触发时返回 None。
        """
        group_id = event.get_group_id()
        with review_context(
            group_id=group_id or "",
            user_id=target_user_id,
            provider=self.llm.last_provider_id,
        ):
            log_event("manual_review", target=target_user_id or "(整体)")
            task, _outcome = await self._run_review(
                event,
                target_user_id=target_user_id,
                target_nickname=target_nickname,
            )
            return task

    async def review_recent(self, event: "AstrMessageEvent") -> ReviewTask | None:
        """主动审核最近聊天记录整体（/review recent）。

        Args:
            event: 触发命令的消息事件。

        Returns:
            生成的审核任务；未触发时返回 None。
        """
        with review_context(
            group_id=event.get_group_id() or "",
            provider=self.llm.last_provider_id,
        ):
            log_event("manual_review", target="(整体)")
            task, _outcome = await self._run_review(
                event, target_user_id="", target_nickname=""
            )
            return task

    # ---------- 规则层 ----------

    async def _try_rule_prefilter(
        self,
        event: "AstrMessageEvent",
        record: ChatRecord,
        config: dict[str, Any],
    ) -> bool:
        """正则规则层预筛：命中激活规则直接生成任务；返回是否已处理。

        观察期规则命中时不拦截，仍走 LLM，并记录判定一致性。
        """
        if self._rules is None or not bool(
            config.get("enable_regex_prefilter", True)
        ):
            return False
        hits = self._rules.match(record.content)
        if hits:
            result = self._rules.build_result(hits[0], record.content)
            task = await self._enqueue_task(
                event,
                [record],
                record.user_id,
                record.nickname,
                result,
                event.get_group_id(),
                config,
                rule_id=hits[0].rule_id,
            )
            if task is not None:
                await self._rules.record_hit(hits[0].rule_id)
            return True
        observing = self._rules.match_observing(record.content)
        task, outcome = await self._run_review(
            event,
            target_user_id=record.user_id,
            target_nickname=record.nickname,
            current_record=record,
        )
        if observing and outcome in ("task_created", "no_violation"):
            for rule in observing:
                await self._rules.record_observation(
                    rule.rule_id, outcome == "task_created"
                )
        return True

    # ---------- 内部实现 ----------

    async def _run_review(
        self,
        event: "AstrMessageEvent",
        target_user_id: str,
        target_nickname: str,
        current_record: ChatRecord | None = None,
    ) -> tuple[ReviewTask | None, str]:
        """执行一次完整 LLM 审核。

        Args:
            event: 消息事件。
            target_user_id: 目标用户 ID；为空表示审核整体聊天记录。
            target_nickname: 目标用户昵称。

        Returns:
            (审核任务, 结果分类)。分类用于观察期规则统计区分真实判定
            与故障路径（W4）：skipped / llm_failed / parse_failed /
            no_violation / task_created / queue_rejected。
        """
        group_id = event.get_group_id()
        if not group_id:
            return None, "skipped"
        config = self._get_config(group_id)
        threshold = safe_int(config.get("risk_threshold"), 80)

        if target_user_id:
            skip, reason = self.filters.should_skip_target(event, target_user_id)
            if skip:
                logger.debug("[AI审核] 主动审核被过滤：%s", reason)
                return None, "skipped"
            # 审核开始即记录冷却起点（LLM 调用前），避免调用窗口内
            # 新消息绕过冷却检查导致并发触发多次模型调用（W1）
            await self._cooldown.touch(group_id, target_user_id)
        records = self.history.get_recent(
            group_id,
            safe_int(config.get("history_count"), 50),
        )
        if not records:
            if current_record is not None:
                records = [current_record]
            else:
                logger.info("[AI审核] 群=%s 暂无聊天记录，本次审核跳过。", group_id)
                return None, "skipped"
        records = trim_records(
            records,
            safe_int(config.get("max_chat_chars"), 3000),
            safe_int(config.get("max_msg_chars"), 200),
        )

        target_desc = ""
        blacklist_hit: dict[str, Any] | None = None
        if target_user_id:
            target_desc = (
                f"本次审核对象：{target_nickname or target_user_id}（{target_user_id}）。"
                "请重点分析该用户的发言。"
            )
            blacklist_hit = await self._check_blacklist(
                group_id, target_user_id, config
            )
            if blacklist_hit:
                level = blacklist_hit.get("level", "")
                reason = blacklist_hit.get("reason", "")
                target_desc += (
                    f"\n⚠️ 注意：该用户已在云黑库中（等级 {level}，"
                    f"原因：{reason or '未知'}）。请从严审核并确认违规。"
                )

        system = self.prompt.build_system()
        user = self.prompt.build_user(records, target_desc)
        output = self.prompt.build_output()
        umo = event.unified_msg_origin

        text, first_provider = await self.llm.chat_ex(system, user, output, umo)
        if text is None:
            log_event("llm_call_failed", group_id=group_id)
            return None, "llm_failed"
        result = await parse_with_llm_retry(self.llm, system, user, output, umo, text)
        if result is None:
            log_event("parse_failed", group_id=group_id)
            return None, "parse_failed"

        if not result.illegal or result.risk < threshold:
            if blacklist_hit:
                # 黑库命中用户从严处理：强制视为违规并入队（提升至阈值）
                result.risk = max(result.risk, threshold)
                result.illegal = True
                log_event(
                    "blacklist_forced_violation",
                    group_id=group_id,
                    user_id=target_user_id,
                    level=blacklist_hit.get("level"),
                )
                logger.info(
                    "[AI审核] 群=%s 用户=%s 命中云黑库，从严判定违规（risk=%d）。",
                    group_id,
                    target_user_id or "(整体)",
                    result.risk,
                )
            else:
                logger.info(
                    "[AI审核] 群=%s 用户=%s 判定无违规（risk=%d < %d），结束。",
                    group_id,
                    target_user_id or "(整体)",
                    result.risk,
                    threshold,
                )
                log_event("review_clean", group_id=group_id, risk=result.risk)
                return None, "no_violation"

        # 二次审核（可选）：初次判定违规后，用指定模型复核同一批记录，
        # 二次也判定违规才生成任务；二次失败回退初次判定（不中断审核）。
        second_provider, second_outcome = await self._run_second_review(
            event,
            records,
            target_user_id,
            target_nickname,
            config,
            result,
        )
        if second_outcome == "clean":
            logger.info(
                "[AI审核] 群=%s 用户=%s 二次审核判定无违规，本次结束。",
                group_id,
                target_user_id or "(整体)",
            )
            log_event(
                "second_review_clean",
                group_id=group_id,
                provider=second_provider,
                first_risk=result.risk,
            )
            return None, "no_violation"

        task = await self._enqueue_task(
            event,
            records,
            target_user_id,
            target_nickname,
            result,
            group_id,
            config,
            llm_provider=first_provider,
            second_llm_provider=second_provider,
        )
        if task is None:
            return None, "queue_rejected"
        return task, "task_created"

    async def _run_second_review(
        self,
        event: "AstrMessageEvent",
        records: list[ChatRecord],
        target_user_id: str,
        target_nickname: str,
        config: dict[str, Any],
        first_result: ReviewResult,
    ) -> tuple[str, str]:
        """执行二次审核：初次判定违规后用第二模型复核。

        Args:
            event: 消息事件。
            records: 聊天上下文记录。
            target_user_id: 目标用户 ID。
            target_nickname: 目标用户昵称。
            config: 当前生效配置。
            first_result: 初次审核结果（违规）。

        Returns:
            (二次审核使用的 Provider ID, 结果分类)。
            分类：confirmed=二次判定违规；clean=二次判定无违规；
            skipped=未启用二次审核或无需复核；failed=调用/解析失败
            （回退初次判定，由调用方按 confirmed 处理）。
        """
        if not bool(config.get("enable_second_review", False)):
            return "", "skipped"
        second_provider_id = str(
            config.get("second_review_provider_id", "") or ""
        ).strip()
        target_desc = ""
        if target_user_id:
            target_desc = (
                f"本次复核对象：{target_nickname or target_user_id}（{target_user_id}）。"
                "请独立判断该用户的发言是否违规。"
            )
        system = self.prompt.build_system()
        user = self.prompt.build_user(records, target_desc)
        output = self.prompt.build_output()
        umo = event.unified_msg_origin
        threshold = safe_int(config.get("risk_threshold"), 80)

        log_event(
            "second_review_start",
            provider=second_provider_id or "(会话默认)",
            first_risk=first_result.risk,
        )
        text, used_provider = await self.llm.chat_ex(
            system, user, output, umo, second_provider_id
        )
        if text is None:
            log_event("second_review_failed", provider=second_provider_id)
            return "", "failed"
        second = await parse_with_llm_retry(
            self.llm, system, user, output, umo, text, second_provider_id
        )
        if second is None:
            log_event("second_review_parse_failed", provider=second_provider_id)
            return "", "failed"
        log_event(
            "second_review_done",
            provider=used_provider,
            illegal=second.illegal,
            risk=second.risk,
        )
        if not second.illegal or second.risk < threshold:
            return used_provider, "clean"
        return used_provider, "confirmed"

    async def _check_blacklist(
        self,
        group_id: str,
        user_id: str,
        config: dict[str, Any],
    ) -> dict[str, Any] | None:
        """审核前查询皮梦云黑库（皮梦云 → AI 审核 通信方向）。

        仅在启用 ``enable_blacklist_check`` 且黑库适配器可用时查询；
        查询失败不影响审核（返回 None）。

        Args:
            group_id: 群号。
            user_id: 目标用户 ID。
            config: 当前生效配置。

        Returns:
            命中时返回黑库记录字典，否则返回 None。
        """
        if not bool(config.get("enable_blacklist_check", False)):
            return None
        if self._blacklist is None or not self._blacklist.available:
            return None
        try:
            hit = await self._blacklist.check_user(user_id)
        except Exception as exc:
            logger.warning("[AI审核] 黑库查询异常（群=%s 用户=%s）：%s", group_id, user_id, exc)
            return None
        if hit:
            log_event(
                "blacklist_hit",
                group_id=group_id,
                user_id=user_id,
                level=hit.get("level"),
            )
        return hit

    async def _enqueue_task(
        self,
        event: "AstrMessageEvent",
        records: list[ChatRecord],
        target_user_id: str,
        target_nickname: str,
        result: ReviewResult,
        group_id: str,
        config: dict[str, Any],
        rule_id: str = "",
        llm_provider: str = "",
        second_llm_provider: str = "",
    ) -> ReviewTask | None:
        """创建并加入审核任务，记录冷却/统计/日志。

        Returns:
            入队成功返回任务；被队列拒绝返回 None。
        """
        task = ReviewTask.create(
            group_id=group_id,
            user_id=target_user_id,
            nickname=target_nickname,
            result=result,
            context=records,
            timeout=float(safe_int(config.get("review_timeout"), 300)),
            platform_id=event.get_platform_id(),
            session_id=event.unified_msg_origin,
            rule_id=rule_id,
            llm_provider=llm_provider,
            second_llm_provider=second_llm_provider,
        )
        if not await self.queue.add(task):
            logger.warning(
                "[AI审核] 任务入队被拒绝（队列已满或该用户待处理任务过多）：群=%s 用户=%s",
                group_id,
                target_user_id or "(整体)",
            )
            return None
        if target_user_id:
            # 注意：此处为冷却"二次写入"（R1）。LLM 路径的冷却已在
            # _run_review 开头记录；此处不可删除——规则命中路径
            # （_try_rule_prefilter 命中分支）不经过 _run_review，
            # 仅靠此处的 touch 设置冷却。
            await self._cooldown.touch(group_id, target_user_id)
        if self._stats is not None:
            await self._stats.record_violation(group_id, target_user_id, result.type)
        log_review(
            ReviewLog(
                group_id=group_id,
                user_id=target_user_id,
                content=user_content(records, target_user_id),
                risk=result.risk,
                review_status="pending",
                task_id=task.task_id,
                llm_provider=llm_provider,
            )
        )
        logger.info(
            "[AI审核] 群=%s 用户=%s 生成审核任务 %s（risk=%d 类型=%s 建议=%s%s）。",
            group_id,
            target_user_id or "(整体)",
            task.task_id,
            result.risk,
            result.type,
            result.suggestion,
            " 规则=" + rule_id if rule_id else "",
        )
        log_event(
            "review_created",
            task_id=task.task_id,
            user_id=target_user_id,
            risk=result.risk,
            type=result.type,
            suggestion=result.suggestion,
            rule_id=rule_id,
            provider=llm_provider,
        )
        return task
