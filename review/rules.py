"""正则规则引擎（KV 持久化）。

规则层在 LLM 之前拦截：命中已激活规则的群消息直接生成审核任务，
跳过模型调用以节省 token。规则生命周期：

- OBSERVING（观察期）：AI 沉淀的新规则先进观察期，命中仍走 LLM，
  统计规则判定与 LLM 判定的一致性；累计 regex_min_hits 次后按准确率
  决定激活或删除（灰度）。
- ACTIVE（激活）：命中直接生成审核任务，管理员 pass/reject 作为反馈，
  准确率低于 regex_min_accuracy 时自动熔断停用并通知管理员。
- DISABLED（停用）：熔断或管理员手动停用，不参与匹配。

规则库整体序列化到 KV 的 review_rules 键，重启不丢。
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid

try:
    import re._parser as _sre_parse  # Python 3.11+
except ImportError:  # pragma: no cover - Python 3.10 及更早
    import re.sre_parse as _sre_parse  # type: ignore[no-redef]
from collections.abc import Awaitable, Callable
from typing import Any

from ..config import safe_int
from ..models import PunishmentType, RuleCandidate, RuleRecord, RuleStatus
from ..utils.logger import get_logger
from ..utils.parser import extract_json_object
from .persistence import KVStore

logger = get_logger()

# 规则等级 -> 风险值与建议处罚（生成任务时使用）
_LEVEL_TO_RISK = {1: 85, 2: 92, 3: 96}
_LEVEL_TO_SUGGESTION = {
    1: PunishmentType.WARN.value,
    2: PunishmentType.MUTE.value,
    3: PunishmentType.KICK.value,
}

_MAX_PATTERN_LEN = 200
_KV_KEY = "review_rules"

# sre_parse 中的重复节点类型（+ * ? {m,n} 及懒量词）
_REPEAT_OPS = (
    _sre_parse.REPEAT,
    _sre_parse.REPEAT_ONE,
    _sre_parse.MAX_REPEAT,
    _sre_parse.MIN_REPEAT,
)


def _contains_repeat(node: object) -> bool:
    """递归检查解析树节点内是否含重复节点（用于嵌套量词检测）。"""
    if isinstance(node, tuple) and node:
        if node[0] in _REPEAT_OPS:
            return True
        return any(_contains_repeat(part) for part in node)
    if isinstance(node, list):
        return any(_contains_repeat(part) for part in node)
    if isinstance(node, _sre_parse.SubPattern):  # Python 3.13+ parse 顶层
        return any(_contains_repeat(part) for part in node.data)
    return False


def _walk(node: object):
    """深度遍历解析树，产出全部节点（含嵌套子结构）。"""
    if isinstance(node, tuple) and node:
        yield node
        for part in node[1:]:
            yield from _walk(part)
    elif isinstance(node, list):
        for part in node:
            yield from _walk(part)
    elif isinstance(node, _sre_parse.SubPattern):  # Python 3.13+ parse 顶层
        for part in node.data:
            yield from _walk(part)


def _reject_catastrophic(pattern: str) -> None:
    """拒绝嵌套量词（如 `(a+)+`、`(a*)*`）——灾难性回溯风险（W5）。

    Args:
        pattern: 正则原文。

    Raises:
        ValueError: 检测到嵌套量词时。
    """
    try:
        parsed = _sre_parse.parse(pattern)
    except re.error as exc:
        raise ValueError(f"正则表达式非法: {exc}") from exc
    for node in _walk(parsed):
        if not (isinstance(node, tuple) and node):
            continue
        op = node[0]
        if op not in _REPEAT_OPS:
            continue
        args = node[1] if len(node) > 1 else None
        inner = args[2] if isinstance(args, tuple) and len(args) > 2 else None
        if inner is not None and _contains_repeat(inner):
            raise ValueError(
                "正则包含嵌套量词（如 (a+)+），存在灾难性回溯风险，已拒绝。"
            )
_CANDIDATE_KV_KEY = "rule_candidates"
_DAYS_TO_SECONDS = 86400

Notifier = Callable[[str], Awaitable[None]]


class RuleEngine:
    """正则规则库：增删改查、预编译匹配、命中/判定统计、熔断与沉淀。"""

    def __init__(
        self,
        store: KVStore,
        get_config: Callable[[str], dict[str, Any]],
        notifier: Notifier | None = None,
    ) -> None:
        """初始化规则引擎。

        Args:
            store: KV 持久化存储。
            get_config: 返回当前配置字典的回调，可接受群号参数。
            notifier: 异常/熔断告警通知回调，可为空。
        """
        self._store = store
        self._get_config = get_config
        self._notifier = notifier
        self._rules: dict[str, RuleRecord] = {}
        self._compiled: dict[str, re.Pattern] = {}
        self._candidates: dict[str, RuleCandidate] = {}
        self._lock = asyncio.Lock()

    # ---------- 持久化 ----------

    async def load(self) -> None:
        """从 KV 恢复规则库与候选池，并预编译有效规则。"""
        raw = await self._store.get(_KV_KEY, {})
        if isinstance(raw, dict):
            async with self._lock:
                rules: dict[str, RuleRecord] = {}
                for rule_id, data in raw.items():
                    if not isinstance(data, dict):
                        continue
                    try:
                        rule = RuleRecord.from_dict(data)
                    except Exception:
                        continue
                    if rule.rule_id and rule.pattern:
                        rules[rule.rule_id] = rule
                self._rules = rules
                self._rebuild_compiled()
        raw_candidates = await self._store.get(_CANDIDATE_KV_KEY, {})
        if isinstance(raw_candidates, dict):
            async with self._lock:
                candidates: dict[str, RuleCandidate] = {}
                for candidate_id, data in raw_candidates.items():
                    if not isinstance(data, dict):
                        continue
                    try:
                        candidate = RuleCandidate.from_dict(data)
                    except Exception:
                        continue
                    if candidate.candidate_id and candidate.pattern:
                        candidates[candidate.candidate_id] = candidate
                self._candidates = candidates

    async def _save(self) -> None:
        snapshot = {rule_id: rule.to_dict() for rule_id, rule in self._rules.items()}
        await self._store.put(_KV_KEY, snapshot)

    async def _save_candidates(self) -> None:
        snapshot = {
            candidate_id: candidate.to_dict()
            for candidate_id, candidate in self._candidates.items()
        }
        await self._store.put(_CANDIDATE_KV_KEY, snapshot)

    def _rebuild_compiled(self) -> None:
        """为启用的规则重建预编译缓存（编译失败静默跳过）。"""
        self._compiled = {}
        for rule_id, rule in self._rules.items():
            if rule.status is RuleStatus.DISABLED:
                continue
            try:
                self._compiled[rule_id] = re.compile(rule.pattern)
            except re.error:
                logger.warning("[AI审核] 规则 %s 编译失败，已跳过匹配：%s", rule_id, rule.pattern)

    def _compile(self, pattern: str) -> re.Pattern:
        """编译正则表达式（含嵌套量词 ReDoS 校验）。

        Args:
            pattern: 正则原文。

        Returns:
            编译后的 Pattern。

        Raises:
            ValueError: 表达式非法、超出长度限制或含嵌套量词。
        """
        if not pattern or len(pattern) > _MAX_PATTERN_LEN:
            raise ValueError(f"正则长度需在 1~{_MAX_PATTERN_LEN} 字符之间")
        _reject_catastrophic(pattern)
        try:
            return re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"正则表达式非法: {exc}") from exc

    # ---------- 匹配 ----------

    def match(self, text: str) -> list[RuleRecord]:
        """匹配已激活的规则，按创建时间返回命中列表。"""
        if not text:
            return []
        hits = []
        for rule_id, rule in self._rules.items():
            if rule.status is not RuleStatus.ACTIVE:
                continue
            compiled = self._compiled.get(rule_id)
            if compiled is not None and compiled.search(text):
                hits.append(rule)
        hits.sort(key=lambda rule: rule.created_at)
        return hits

    def match_observing(self, text: str) -> list[RuleRecord]:
        """匹配观察期规则（命中仍走 LLM，仅用于统计）。"""
        if not text:
            return []
        hits = []
        for rule_id, rule in self._rules.items():
            if rule.status is not RuleStatus.OBSERVING:
                continue
            compiled = self._compiled.get(rule_id)
            if compiled is not None and compiled.search(text):
                hits.append(rule)
        return hits

    def build_result(self, rule: RuleRecord, content: str) -> Any:
        """根据命中的规则构造审核结果（供生成任务使用）。"""
        from ..models import ReviewResult

        return ReviewResult(
            illegal=True,
            risk=_LEVEL_TO_RISK.get(rule.level, 85),
            type=rule.note or "规则命中",
            reason=f"命中规则「{rule.note or rule.pattern}」",
            evidence=[content],
            suggestion=_LEVEL_TO_SUGGESTION.get(rule.level, PunishmentType.WARN.value),
        )

    # ---------- 规则管理 ----------

    async def add(
        self,
        pattern: str,
        source: str = "manual",
        note: str = "",
        level: int = 1,
    ) -> tuple[bool, str, RuleRecord | None]:
        """添加一条规则；auto 来源进入观察期，manual 直接激活。

        Returns:
            (是否成功, 提示信息, 新规则)。
        """
        pattern = (pattern or "").strip()
        try:
            self._compile(pattern)
        except ValueError as exc:
            return False, str(exc), None
        level = max(1, min(3, safe_int(level, 1)))
        note = (note or "").strip()[:50]
        async with self._lock:
            return await self._add_locked(pattern, source, note, level)

    async def _add_locked(self, pattern: str, source: str, note: str, level: int):
        """在锁内插入一条规则（供 add 与 approve_candidate 复用）。

        调用方必须持有 self._lock。
        """
        max_rules = safe_int(self._get_config().get("regex_max_rules"), 200)
        if len(self._rules) >= max_rules:
            return False, f"规则数量已达上限（{max_rules}），请先清理。", None
        for rule in self._rules.values():
            if rule.pattern == pattern:
                return False, "相同正则已存在，请勿重复添加。", None
        rule = RuleRecord(
            rule_id=uuid.uuid4().hex[:12],
            pattern=pattern,
            source=source,
            note=note,
            level=level,
            status=RuleStatus.OBSERVING if source == "auto" else RuleStatus.ACTIVE,
        )
        self._rules[rule.rule_id] = rule
        self._rebuild_compiled()
        await self._save()
        return True, f"已添加规则 {rule.rule_id}（{rule.status.value}）。", rule

    async def delete(self, rule_id: str) -> bool:
        """删除一条规则。"""
        async with self._lock:
            if rule_id not in self._rules:
                return False
            del self._rules[rule_id]
            self._compiled.pop(rule_id, None)
            await self._save()
            return True

    async def set_enabled(self, rule_id: str, enabled: bool) -> tuple[bool, str]:
        """启用/停用一条规则（观察期规则不可手动停用）。"""
        async with self._lock:
            rule = self._rules.get(rule_id)
            if rule is None:
                return False, "规则不存在。"
            if rule.status is RuleStatus.OBSERVING:
                return False, "观察期规则自动流转，无需手动停用。"
            rule.status = (
                RuleStatus.ACTIVE if enabled else RuleStatus.DISABLED
            )
            self._rebuild_compiled()
            await self._save()
        return True, f"规则 {rule_id} 已{'启用' if enabled else '停用'}。"

    def list(self) -> list[RuleRecord]:
        """返回全部规则（按创建时间排序）。"""
        return sorted(self._rules.values(), key=lambda rule: rule.created_at)

    # ---------- 统计与反馈 ----------

    async def record_hit(self, rule_id: str) -> None:
        """记录一次激活规则的命中（生成任务时调用）。"""
        async with self._lock:
            rule = self._rules.get(rule_id)
            if rule is None or rule.status is not RuleStatus.ACTIVE:
                return
            rule.hits += 1
            rule.last_ts = time.time()
            await self._save()

    async def record_observation(self, rule_id: str, correct: bool) -> None:
        """记录观察期规则的一次判定对比（LLM 判定一致为 correct）。

        累计足够次数后按准确率自动激活或删除。
        """
        async with self._lock:
            rule = self._rules.get(rule_id)
            if rule is None or rule.status is not RuleStatus.OBSERVING:
                return
            rule.observed += 1
            if correct:
                rule.hits += 1
            rule.last_ts = time.time()
            min_hits = safe_int(self._get_config().get("regex_min_hits"), 5)
            if rule.observed < min_hits:
                await self._save()
                return
            accuracy = rule.hits / rule.observed
            min_accuracy = self._accuracy_threshold()
            if accuracy >= min_accuracy:
                rule.status = RuleStatus.ACTIVE
                logger.info(
                    "[AI审核] 规则 %s 观察期通过（准确率 %.0f%%），已激活。",
                    rule.rule_id,
                    accuracy * 100,
                )
            else:
                self._rules.pop(rule.rule_id, None)
                self._compiled.pop(rule.rule_id, None)
                logger.info(
                    "[AI审核] 规则 %s 观察期准确率 %.0f%% 不足，已删除。",
                    rule.rule_id,
                    accuracy * 100,
                )
            await self._save()

    async def record_decision(self, rule_id: str, approved: bool) -> None:
        """记录激活规则命中任务的最终处理结果，并检查熔断。"""
        async with self._lock:
            rule = self._rules.get(rule_id)
            if rule is None or rule.status is not RuleStatus.ACTIVE:
                return
            if approved:
                rule.approved += 1
            else:
                rule.rejected += 1
            await self._save()
            min_hits = safe_int(self._get_config().get("regex_min_hits"), 5)
            if rule.approved + rule.rejected < min_hits:
                return
            if rule.accuracy < self._accuracy_threshold():
                rule.status = RuleStatus.DISABLED
                self._compiled.pop(rule_id, None)
                await self._save()
                message = (
                    f"[AI审核] 正则规则「{rule.note or rule.pattern}」"
                    f"准确率 {rule.accuracy * 100:.0f}%（通过 {rule.approved}/"
                    f"拒绝 {rule.rejected}）低于阈值，已自动停用。"
                )
                logger.warning(message)
                if self._notifier is not None:
                    try:
                        await self._notifier(message)
                    except Exception:
                        logger.warning("规则熔断通知发送失败。", exc_info=True)

    def _accuracy_threshold(self) -> float:
        try:
            return float(self._get_config().get("regex_min_accuracy", 0.7))
        except (TypeError, ValueError):
            return 0.7

    # ---------- AI 沉淀（候选池 + 管理员审批） ----------

    async def collect_candidate(self, llm: Any, prompt: Any, task: Any) -> None:
        """管理员通过任务后提炼规则候选（供命令层异步调用）。

        候选进入待审批池，由定时推送提醒管理员用命令审批；
        提炼或入库失败仅记录日志，不影响本次处理流程。
        """
        try:
            config = self._get_config(task.group_id)
            if not bool(config.get("regex_sediment", True)):
                return
            if task.rule_id or not task.result.evidence:
                return
            refined = await self._refine(llm, prompt, task)
            if refined is None:
                return
            pattern, note, level = refined
            async with self._lock:
                for candidate in self._candidates.values():
                    if candidate.pattern == pattern:
                        logger.info(
                            "[AI审核] 候选已存在，跳过：%s（任务 %s）",
                            pattern,
                            task.task_id,
                        )
                        return
                candidate = RuleCandidate(
                    candidate_id=uuid.uuid4().hex[:12],
                    pattern=pattern,
                    note=note,
                    level=level,
                    group_id=task.group_id,
                    user_id=task.user_id,
                    platform_id=task.platform_id,
                    session_id=task.session_id,
                    source_task_id=task.task_id,
                )
                self._candidates[candidate.candidate_id] = candidate
                await self._save_candidates()
            logger.info(
                "[AI审核] 已生成规则候选 %s（任务 %s）：%s",
                candidate.candidate_id,
                task.task_id,
                pattern,
            )
        except Exception as exc:
            logger.warning("[AI审核] 规则候选收集失败：%s", exc, exc_info=True)

    async def _refine(
        self,
        llm: Any,
        prompt: Any,
        task: Any,
    ) -> tuple[str, str, int] | None:
        """调用模型将已确认的违规任务提炼为 (pattern, note, level)。

        Returns:
            提炼结果；任何一步失败返回 None。
        """
        try:
            user_prompt = prompt.build_rule(task)
        except Exception:
            return None
        if not user_prompt:
            return None
        try:
            text = await llm.chat("", user_prompt, "", task.session_id)
        except Exception as exc:
            logger.warning("[AI审核] 规则提炼调用失败：%s", exc)
            return None
        if not text:
            return None
        try:
            data = extract_json_object(text, required_key="pattern")
        except ValueError:
            logger.warning("[AI审核] 规则提炼结果解析失败。")
            return None
        pattern = str(data.get("pattern", "")).strip()
        if not pattern:
            return None
        try:
            level = max(1, min(3, int(data.get("level", 1) or 1)))
        except (TypeError, ValueError):
            level = 1
        note = str(data.get("note", ""))[:50]
        return pattern, note, level

    def candidates(self) -> list[RuleCandidate]:
        """返回全部待审批候选（按创建时间排序）。"""
        return sorted(
            self._candidates.values(),
            key=lambda candidate: candidate.created_at,
        )

    async def approve_candidate(self, candidate_id: str) -> tuple[bool, str]:
        """批准候选：转入观察期规则并删除候选（同一临界区完成）。

        检查存在性、创建规则、消费候选在同一把锁内完成，杜绝并发
        approve/deny 交错导致"拒绝后仍建规则"或重复提示。

        Returns:
            (是否成功, 提示信息)。
        """
        async with self._lock:
            candidate = self._candidates.get(candidate_id)
            if candidate is None:
                return False, "候选不存在或已被处理。"
            ok, message, rule = await self._add_locked(
                candidate.pattern,
                source="auto",
                note=candidate.note,
                level=candidate.level,
            )
            if not ok:
                return False, f"批准失败：{message}"
            self._candidates.pop(candidate_id, None)
            await self._save_candidates()
        if rule is not None and rule.status is RuleStatus.OBSERVING:
            logger.info(
                "[AI审核] 新规则 %s 进入观察期：%s", rule.rule_id, rule.pattern
            )
        logger.info(
            "[AI审核] 候选 %s 已批准，规则进入观察期：%s",
            candidate_id,
            candidate.pattern,
        )
        return True, f"已批准候选 {candidate_id}，规则进入观察期。"

    async def deny_candidate(self, candidate_id: str) -> bool:
        """拒绝候选：直接丢弃。"""
        async with self._lock:
            if candidate_id not in self._candidates:
                return False
            del self._candidates[candidate_id]
            await self._save_candidates()
        logger.info("[AI审核] 候选 %s 已被拒绝丢弃。", candidate_id)
        return True

    async def purge_expired_candidates(self) -> int:
        """清理超过保留天数的候选。

        Returns:
            清理的候选数量。
        """
        ttl_days = safe_int(self._get_config().get("regex_candidate_ttl"), 3)
        cutoff = time.time() - ttl_days * _DAYS_TO_SECONDS
        expired = [
            candidate_id
            for candidate_id, candidate in self._candidates.items()
            if candidate.created_at < cutoff
        ]
        if not expired:
            return 0
        async with self._lock:
            for candidate_id in expired:
                self._candidates.pop(candidate_id, None)
            await self._save_candidates()
        logger.info("[AI审核] 已清理 %d 条过期规则候选。", len(expired))
        return len(expired)

    @staticmethod
    def format_candidate_item(candidate: RuleCandidate) -> str:
        """格式化单条候选为审批文本（含批准/拒绝命令）。"""
        return (
            f"#{candidate.candidate_id} [{candidate.note or candidate.pattern}]"
            f" L{candidate.level}（来源群 {candidate.group_id}）\n"
            f"✅ 批准：/review rule approve {candidate.candidate_id}\n"
            f"❌ 拒绝：/review rule deny {candidate.candidate_id}"
        )

    def build_push_message(
        self,
        candidates: list[RuleCandidate] | None = None,
        group_id: str = "",
    ) -> str | None:
        """构建沉淀推送消息（列出全部待审批候选）。

        Args:
            candidates: 候选列表；默认取全部。
            group_id: 群号，非空时在首行标注来源群。

        Returns:
            推送文本；无候选时返回 None。
        """
        candidates = self.candidates() if candidates is None else candidates
        if not candidates:
            return None
        prefix = f"📬 [群 {group_id}] AI 审核沉淀请求" if group_id else "📬 AI 审核沉淀请求"
        lines = [
            f"{prefix}：{len(candidates)} 条规则候选待确认",
            "（批准后进入观察期，命中仍走 AI 对比验证，不直接处罚）",
        ]
        for candidate in candidates[:10]:
            lines.append(
                f"#{candidate.candidate_id} [{candidate.note or candidate.pattern[:20]}]"
                f" L{candidate.level}"
            )
        if len(candidates) > 10:
            lines.append(f"…共 {len(candidates)} 条")
        lines.append(
            "用 /review rule approve <id> 批准，/review rule deny <id> 拒绝，"
            "/review rule pending 查看全部。"
        )
        return "\n".join(lines)
