"""审核插件数据模型。

定义插件内部使用的全部数据模型，供其他模块引用。
所有模型均为 dataclass，保证类型清晰、可序列化。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


def _parse_bool(value: Any) -> bool:
    """兼容 LLM 返回字符串布尔值（如 "false"/"true"）的情况。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return False


class ReviewStatus(str, Enum):
    """审核任务状态。"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class PunishmentType(str, Enum):
    """处罚类型。"""

    WARN = "warn"
    MUTE = "mute"
    KICK = "kick"
    BAN = "ban"
    BLACKLIST = "blacklist"


class RuleStatus(str, Enum):
    """正则规则状态。

    OBSERVING: 观察期（命中仍走 LLM，统计规则判定与 LLM 判定的一致性）；
    ACTIVE: 已激活（命中直接生成审核任务，跳过 LLM）；
    DISABLED: 已停用（熔断或管理员手动停用）。
    """

    OBSERVING = "observing"
    ACTIVE = "active"
    DISABLED = "disabled"


@dataclass(slots=True)
class ChatRecord:
    """一条群聊记录。

    Attributes:
        timestamp: 消息时间戳（Unix 秒）。
        nickname: 发送者昵称。
        user_id: 发送者 QQ/平台 ID。
        content: 消息纯文本内容。
        group_id: 所属群号。
    """

    timestamp: float
    nickname: str
    user_id: str
    content: str
    group_id: str = ""

    def to_prompt_line(self, index: int) -> str:
        """将记录格式化为 Prompt 中的一行。

        Args:
            index: 记录序号（从 1 开始）。

        Returns:
            格式化后的文本行。
        """
        time_str = time.strftime("%m-%d %H:%M", time.localtime(self.timestamp))
        return f"{index}. [{time_str}] {self.nickname}({self.user_id}): {self.content}"

    def to_dict(self) -> dict:
        """序列化为字典（用于 KV 持久化）。"""
        return {
            "timestamp": self.timestamp,
            "nickname": self.nickname,
            "user_id": self.user_id,
            "content": self.content,
            "group_id": self.group_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ChatRecord":
        """从字典恢复记录。"""
        return cls(
            timestamp=float(data.get("timestamp", 0)),
            nickname=str(data.get("nickname", "")),
            user_id=str(data.get("user_id", "")),
            content=str(data.get("content", "")),
            group_id=str(data.get("group_id", "")),
        )


@dataclass(slots=True)
class ReviewResult:
    """AI 审核结果（对应 AI 返回的 JSON）。

    Attributes:
        illegal: 是否违规。
        risk: 风险值 0~100。
        type: 违规类型。
        reason: 违规原因。
        evidence: 违规证据片段列表。
        suggestion: 建议的处罚类型。
    """

    illegal: bool
    risk: int
    type: str
    reason: str
    evidence: list[str] = field(default_factory=list)
    suggestion: str = PunishmentType.WARN.value

    @classmethod
    def from_dict(cls, data: dict) -> "ReviewResult":
        """从解析后的字典构建审核结果。

        Args:
            data: 由 AI 返回并解析的 JSON 字典。

        Returns:
            审核结果对象。

        Raises:
            ValueError: 当缺少必要字段或字段类型非法时。
        """
        illegal = _parse_bool(data.get("illegal", False))
        raw_risk = data.get("risk", 0)
        try:
            risk = int(raw_risk)
        except (TypeError, ValueError):
            raise ValueError(f"risk 字段非法: {raw_risk!r}")
        risk = max(0, min(100, risk))
        raw_evidence = data.get("evidence", [])
        if not isinstance(raw_evidence, list):
            raw_evidence = []
        evidence = [str(item) for item in raw_evidence]
        suggestion = str(
            data.get("suggestion", PunishmentType.WARN.value)
        ).lower()
        return cls(
            illegal=illegal,
            risk=risk,
            type=str(data.get("type", "")),
            reason=str(data.get("reason", "")),
            evidence=evidence,
            suggestion=suggestion,
        )

    def to_dict(self) -> dict:
        """序列化为字典（用于 KV 持久化）。"""
        return {
            "illegal": self.illegal,
            "risk": self.risk,
            "type": self.type,
            "reason": self.reason,
            "evidence": list(self.evidence),
            "suggestion": self.suggestion,
        }


@dataclass(slots=True)
class ReviewTask:
    """一条待管理员确认的审核任务。

    Attributes:
        task_id: 任务唯一 ID。
        group_id: 群号。
        user_id: 被审核用户 ID。
        nickname: 被审核用户昵称。
        result: AI 审核结果。
        context: 相关的聊天上下文记录。
        created_at: 创建时间戳。
        expires_at: 过期时间戳（超时自动失效）。
        status: 当前状态。
        admin_id: 处理该任务的管理员 ID（未处理为空）。
        decided_at: 处理时间戳（未处理为 None）。
    """

    task_id: str
    group_id: str
    user_id: str
    nickname: str
    result: ReviewResult
    context: list[ChatRecord] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    status: ReviewStatus = ReviewStatus.PENDING
    admin_id: str = ""
    decided_at: float | None = None
    platform_id: str = ""
    session_id: str = ""
    rule_id: str = ""
    llm_provider: str = ""
    second_llm_provider: str = ""

    @classmethod
    def create(
        cls,
        group_id: str,
        user_id: str,
        nickname: str,
        result: ReviewResult,
        context: list[ChatRecord],
        timeout: float,
        platform_id: str = "",
        session_id: str = "",
        rule_id: str = "",
        llm_provider: str = "",
        second_llm_provider: str = "",
    ) -> "ReviewTask":
        """创建审核任务。

        Args:
            group_id: 群号。
            user_id: 被审核用户 ID。
            nickname: 被审核用户昵称。
            result: AI 审核结果。
            context: 聊天上下文。
            timeout: 超时秒数。
            rule_id: 命中的正则规则 ID（规则层任务使用）。

        Returns:
            新创建的审核任务。
        """
        now = time.time()
        return cls(
            task_id=uuid.uuid4().hex[:12],
            group_id=group_id,
            user_id=user_id,
            nickname=nickname,
            result=result,
            context=context,
            created_at=now,
            expires_at=now + timeout,
            platform_id=platform_id,
            session_id=session_id,
            rule_id=rule_id,
            llm_provider=llm_provider,
            second_llm_provider=second_llm_provider,
        )

    def to_dict(self) -> dict:
        """序列化为字典（用于 KV 持久化）。"""
        return {
            "task_id": self.task_id,
            "group_id": self.group_id,
            "user_id": self.user_id,
            "nickname": self.nickname,
            "result": self.result.to_dict(),
            "context": [record.to_dict() for record in self.context],
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status.value,
            "admin_id": self.admin_id,
            "decided_at": self.decided_at,
            "platform_id": self.platform_id,
            "session_id": self.session_id,
            "rule_id": self.rule_id,
            "llm_provider": self.llm_provider,
            "second_llm_provider": self.second_llm_provider,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ReviewTask":
        """从字典恢复任务（异常字段按默认值兜底）。"""
        raw_status = data.get("status", ReviewStatus.PENDING.value)
        try:
            status = ReviewStatus(raw_status)
        except ValueError:
            status = ReviewStatus.PENDING
        raw_result = data.get("result")
        if isinstance(raw_result, dict):
            result = ReviewResult.from_dict(raw_result)
        else:
            result = ReviewResult.from_dict(
                {"illegal": False, "risk": 0, "type": "", "reason": ""}
            )
        raw_context = data.get("context") or []
        context = [
            ChatRecord.from_dict(item)
            for item in raw_context
            if isinstance(item, dict)
        ]
        raw_decided = data.get("decided_at")
        return cls(
            task_id=str(data.get("task_id", "")),
            group_id=str(data.get("group_id", "")),
            user_id=str(data.get("user_id", "")),
            nickname=str(data.get("nickname", "")),
            result=result,
            context=context,
            created_at=float(data.get("created_at", 0)),
            expires_at=float(data.get("expires_at", 0)),
            status=status,
            admin_id=str(data.get("admin_id", "")),
            decided_at=float(raw_decided) if raw_decided is not None else None,
            platform_id=str(data.get("platform_id", "")),
            session_id=str(data.get("session_id", "")),
            rule_id=str(data.get("rule_id", "")),
            llm_provider=str(data.get("llm_provider", "")),
            second_llm_provider=str(data.get("second_llm_provider", "")),
        )

    @property
    def is_expired(self) -> bool:
        """任务是否已超时失效（仅针对待处理状态）。"""
        return self.status == ReviewStatus.PENDING and time.time() >= self.expires_at

    def approve(self, admin_id: str) -> None:
        """标记任务为通过。

        Args:
            admin_id: 处理的管理员 ID。
        """
        self.status = ReviewStatus.APPROVED
        self.admin_id = admin_id
        self.decided_at = time.time()

    def reject(self, admin_id: str) -> None:
        """标记任务为拒绝。

        Args:
            admin_id: 处理的管理员 ID。
        """
        self.status = ReviewStatus.REJECTED
        self.admin_id = admin_id
        self.decided_at = time.time()

    def mark_expired(self) -> None:
        """将任务标记为已失效。"""
        self.status = ReviewStatus.EXPIRED

    def revert_to_pending(self) -> None:
        """将已通过的任务恢复为待处理（处罚失败重试路径）。

        清空处理人/处理时间，并顺延过期时间（保持原任务时长，
        至少 60 秒），避免恢复后立即过期。
        """
        if self.status is not ReviewStatus.APPROVED:
            return
        duration = self.expires_at - self.created_at
        self.status = ReviewStatus.PENDING
        self.admin_id = ""
        self.decided_at = None
        self.expires_at = time.time() + max(duration, 60)


@dataclass(slots=True)
class ReviewLog:
    """审核日志记录（仅内存日志，不落库）。

    Attributes:
        timestamp: 日志时间戳。
        group_id: 群号。
        user_id: 用户 ID。
        content: 聊天内容。
        risk: AI 风险值。
        review_status: 审核结果。
        admin_id: 处理的管理员。
        punishment: 处罚类型。
        blacklist_sync: 黑库同步状态。
    """

    timestamp: float = field(default_factory=time.time)
    group_id: str = ""
    user_id: str = ""
    content: str = ""
    risk: int = 0
    review_status: str = ""
    admin_id: str = ""
    punishment: str = ""
    task_id: str = ""
    llm_provider: str = ""


@dataclass(slots=True)
class RuleRecord:
    """一条正则审核规则（KV 持久化）。

    Attributes:
        rule_id: 规则唯一 ID。
        pattern: 正则表达式原文。
        source: 来源（auto=AI 沉淀，manual=管理员添加）。
        note: 规则说明。
        level: 违规等级 1~3（决定风险值与建议处罚）。
        status: 规则状态（观察/激活/停用）。
        hits: 命中次数（观察期表示判定一致次数）。
        observed: 观察期参与判定的总次数。
        approved: 命中生成任务后被管理员通过数。
        rejected: 命中生成任务后被管理员拒绝数。
        created_at: 创建时间戳。
        last_ts: 最近命中时间戳。
    """

    rule_id: str
    pattern: str
    source: str = "manual"
    note: str = ""
    level: int = 1
    status: RuleStatus = RuleStatus.ACTIVE
    hits: int = 0
    observed: int = 0
    approved: int = 0
    rejected: int = 0
    created_at: float = field(default_factory=time.time)
    last_ts: float = 0.0

    @property
    def accuracy(self) -> float:
        """管理员确认准确率（无确认记录时返回 1.0 避免除零）。"""
        decided = self.approved + self.rejected
        if decided <= 0:
            return 1.0
        return self.approved / decided

    def to_dict(self) -> dict:
        """序列化为字典（用于 KV 持久化）。"""
        return {
            "rule_id": self.rule_id,
            "pattern": self.pattern,
            "source": self.source,
            "note": self.note,
            "level": self.level,
            "status": self.status.value,
            "hits": self.hits,
            "observed": self.observed,
            "approved": self.approved,
            "rejected": self.rejected,
            "created_at": self.created_at,
            "last_ts": self.last_ts,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RuleRecord":
        """从字典恢复规则（异常字段按默认值兜底）。"""
        raw_status = data.get("status", RuleStatus.ACTIVE.value)
        try:
            status = RuleStatus(raw_status)
        except ValueError:
            status = RuleStatus.ACTIVE
        return cls(
            rule_id=str(data.get("rule_id", "")),
            pattern=str(data.get("pattern", "")),
            source=str(data.get("source", "manual")),
            note=str(data.get("note", "")),
            level=int(data.get("level", 1) or 1),
            status=status,
            hits=int(data.get("hits", 0) or 0),
            observed=int(data.get("observed", 0) or 0),
            approved=int(data.get("approved", 0) or 0),
            rejected=int(data.get("rejected", 0) or 0),
            created_at=float(data.get("created_at", 0)),
            last_ts=float(data.get("last_ts", 0) or 0),
        )


@dataclass(slots=True)
class RuleCandidate:
    """一条待管理员审批的规则候选（AI 沉淀，KV 持久化）。

    Attributes:
        candidate_id: 候选唯一 ID。
        pattern: 提炼出的正则表达式。
        note: 规则说明。
        level: 违规等级 1~3。
        group_id: 来源群号。
        user_id: 被审核用户 ID。
        session_id: 来源消息会话（用于向该群推送审批请求）。
        source_task_id: 来源审核任务 ID。
        created_at: 创建时间戳。
    """

    candidate_id: str
    pattern: str
    note: str = ""
    level: int = 1
    group_id: str = ""
    user_id: str = ""
    platform_id: str = ""
    session_id: str = ""
    source_task_id: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        """序列化为字典（用于 KV 持久化）。"""
        return {
            "candidate_id": self.candidate_id,
            "pattern": self.pattern,
            "note": self.note,
            "level": self.level,
            "group_id": self.group_id,
            "user_id": self.user_id,
            "platform_id": self.platform_id,
            "session_id": self.session_id,
            "source_task_id": self.source_task_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RuleCandidate":
        """从字典恢复候选。"""
        return cls(
            candidate_id=str(data.get("candidate_id", "")),
            pattern=str(data.get("pattern", "")),
            note=str(data.get("note", "")),
            level=int(data.get("level", 1) or 1),
            group_id=str(data.get("group_id", "")),
            user_id=str(data.get("user_id", "")),
            platform_id=str(data.get("platform_id", "")),
            session_id=str(data.get("session_id", "")),
            source_task_id=str(data.get("source_task_id", "")),
            created_at=float(data.get("created_at", 0)),
        )
