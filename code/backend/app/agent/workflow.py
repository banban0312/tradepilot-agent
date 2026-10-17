"""可恢复的订单工作流；业务写入由已授权的服务回调执行。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, TypedDict
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Interrupt, interrupt

GRAPH_VERSION = "v1"
TOOL_PLAN = (
    "query_customer",
    "match_material",
    "get_contract",
    "check_contract_balance",
    "save_order_draft",
)


class WorkflowState(TypedDict, total=False):
    """图检查点内的最小业务上下文；身份和权限不从模型输入构造。"""

    task_id: str
    graph_version: str
    command_id: str
    command_kind: str
    input_text: str
    input_at: str
    input_history: list[dict[str, str]]
    parsed_input: str
    tool_plan: list[str]
    tool_result: dict[str, Any]
    draft_id: str
    draft_version: int
    content_hash: str
    missing_fields: list[dict[str, str]]
    wait_context: dict[str, Any]
    resume_command: dict[str, Any]
    review_decision: str
    review_comment: str
    projection_state: str


class WorkflowServices(Protocol):
    """将有权限的模型/MCP/草稿服务接入纯图结构，便于统一验收。"""

    async def execute_tools(self, state: WorkflowState) -> Mapping[str, Any]: ...

    async def validate_draft(self, state: WorkflowState) -> Mapping[str, Any]: ...

    async def verify_persisted_draft(
        self, state: WorkflowState
    ) -> Mapping[str, Any]: ...

    async def load_resume_command(
        self, command_id: UUID, *, state: WorkflowState
    ) -> Mapping[str, Any]: ...

    async def load_review_command(
        self, command_id: UUID, *, state: WorkflowState
    ) -> Mapping[str, Any]: ...


class WorkflowResumeConflict(ValueError):
    """拒绝与当前 task/checkpoint/interrupt/图版本不匹配的恢复命令。"""

    code = "WORKFLOW_RESUME_CONFLICT"


def _thread_config(task_id: UUID | str) -> RunnableConfig:
    return {"configurable": {"thread_id": str(task_id)}}


def build_task_graph(
    services: WorkflowServices,
    *,
    checkpointer: BaseCheckpointSaver,
):
    """构建固定版本的 StateGraph；等待节点只中断，不执行业务副作用。"""

    async def ingest(state: WorkflowState) -> WorkflowState:
        if state.get("graph_version") != GRAPH_VERSION:
            raise WorkflowResumeConflict("任务图版本不匹配")
        resumed = state.get("resume_command")
        if resumed is not None:
            text = resumed.get("text")
            if not isinstance(text, str) or not text.strip():
                raise WorkflowResumeConflict("恢复命令缺少有效补充内容")
            history = list(
                state.get("input_history")
                or [{"role": "user", "text": state.get("input_text", "")}]
            )
            history.append({"role": "user", "text": text.strip()})
            return {
                "input_history": history,
                "input_at": str(resumed.get("input_at", state.get("input_at", ""))),
                "draft_version": resumed.get("expected_version")
                if isinstance(resumed.get("expected_version"), int)
                else state.get("draft_version"),
                "command_id": str(resumed.get("id", "")),
                "command_kind": "INPUT",
                "resume_command": {},
                "missing_fields": [],
                "projection_state": "RUNNING",
                "review_decision": "",
            }
        text = state.get("input_text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("任务输入不能为空")
        history = state.get("input_history") or [{"role": "user", "text": text.strip()}]
        return {
            "task_id": str(UUID(state["task_id"])),
            "input_text": text.strip(),
            "input_history": history,
            "command_kind": state.get("command_kind", "CREATE"),
            "projection_state": "RUNNING",
        }

    async def parse(state: WorkflowState) -> WorkflowState:
        # 原始文本仍由真实模型解析，图不在此处猜测或补造业务字段。
        history = state.get("input_history") or [
            {"role": "user", "text": state["input_text"]}
        ]
        original = history[0]["text"]
        supplements = history[1:]
        prompt = f"原始请求：\n{original}"
        if supplements:
            prompt += "\n\n用户补充：\n" + "\n".join(
                item["text"] for item in supplements
            )
        if state.get("draft_version") is not None:
            prompt += f"\n\n当前草稿版本：{state['draft_version']}"
        return {"parsed_input": prompt}

    async def plan_tools(state: WorkflowState) -> WorkflowState:
        return {"tool_plan": list(TOOL_PLAN)}

    async def execute_tools(state: WorkflowState) -> WorkflowState:
        result = await services.execute_tools(state)
        return {"tool_result": dict(result)}

    async def validate(state: WorkflowState) -> WorkflowState:
        result = await services.validate_draft(state)
        draft_id = result.get("draft_id")
        draft_version = result.get("draft_version")
        issues = result.get("missing_fields", [])
        if not isinstance(issues, list):
            raise TypeError("草稿校验服务返回的缺失字段无效")
        normalized: list[dict[str, str]] = []
        for item in issues:
            if not isinstance(item, Mapping):
                raise TypeError("草稿校验服务返回的字段问题无效")
            field, message = item.get("field"), item.get("message")
            if not isinstance(field, str) or not isinstance(message, str):
                raise TypeError("草稿校验服务返回的字段问题缺少说明")
            normalized.append({"field": field, "message": message})
        result_state: WorkflowState = {"missing_fields": normalized}
        if isinstance(draft_id, (str, UUID)):
            result_state["draft_id"] = str(draft_id)
        if isinstance(draft_version, int) and not isinstance(draft_version, bool):
            result_state["draft_version"] = draft_version
        return result_state

    async def prepare_input_wait(state: WorkflowState) -> WorkflowState:
        returning = state.get("review_decision") == "RETURN"
        if not state.get("missing_fields") and not returning:
            raise ValueError("不能为完整草稿创建输入等待点")
        return {
            "wait_context": {
                "kind": "input",
                "task_id": state["task_id"],
                "graph_version": GRAPH_VERSION,
                "draft_id": state.get("draft_id"),
                "draft_version": state.get("draft_version"),
                "content_hash": state.get("content_hash"),
                "missing_fields": state["missing_fields"],
                "reason": (
                    state.get("review_comment") or "审批人退回修改"
                    if returning
                    else "待补充必填信息"
                ),
            },
            "projection_state": "WAITING_INPUT",
        }

    async def await_input(state: WorkflowState) -> WorkflowState:
        # 节点恢复时 LangGraph 会从该节点开头重跑；除 interrupt 外不得写业务数据。
        command_ref = interrupt(state["wait_context"])
        if not isinstance(command_ref, Mapping):
            raise WorkflowResumeConflict("恢复载荷格式无效")
        command_id = command_ref.get("command_id")
        if not isinstance(command_id, str):
            raise WorkflowResumeConflict("恢复载荷缺少命令 ID")
        command = await services.load_resume_command(UUID(command_id), state=state)
        return {"resume_command": dict(command), "projection_state": "RUNNING"}

    async def persist_draft(state: WorkflowState) -> WorkflowState:
        # MCP 的草稿保存已在 execute_tools 中以 operation_id 幂等提交；此节点只核实事实。
        persisted = await services.verify_persisted_draft(state)
        if not persisted.get("exists"):
            raise RuntimeError("草稿工具执行后未找到对应持久化草稿")
        return {
            "draft_id": str(persisted["draft_id"]),
            "draft_version": int(persisted["draft_version"]),
            "content_hash": str(persisted.get("content_hash") or ""),
            "missing_fields": list(persisted.get("issues", [])),
            "projection_state": "WAITING_APPROVAL" if persisted.get("reviewable") else "WAITING_INPUT",
        }

    async def prepare_review(state: WorkflowState) -> WorkflowState:
        draft_id = state.get("draft_id")
        version = state.get("draft_version")
        digest = state.get("content_hash")
        if not isinstance(draft_id, str) or not isinstance(version, int) or not isinstance(digest, str) or len(digest) != 64:
            raise WorkflowResumeConflict("审批等待点缺少当前草稿版本或内容摘要")
        return {
            "wait_context": {
                "kind": "review",
                "task_id": state["task_id"],
                "graph_version": GRAPH_VERSION,
                "draft_id": draft_id,
                "draft_version": version,
                "content_hash": digest,
            },
            "projection_state": "WAITING_APPROVAL",
        }

    async def await_review(state: WorkflowState) -> WorkflowState:
        command_ref = interrupt(state["wait_context"])
        if not isinstance(command_ref, Mapping) or not isinstance(command_ref.get("command_id"), str):
            raise WorkflowResumeConflict("审批恢复载荷缺少命令 ID")
        command = await services.load_review_command(UUID(command_ref["command_id"]), state=state)
        decision = command.get("decision")
        if decision not in {"APPROVE", "RETURN", "REJECT", "EDIT"}:
            raise WorkflowResumeConflict("审批恢复命令决定无效")
        comment = command.get("comment")
        return {
            "resume_command": dict(command),
            "review_decision": str(decision),
            "review_comment": comment if isinstance(comment, str) else "",
            "projection_state": "RUNNING",
        }

    async def finish_approval(state: WorkflowState) -> WorkflowState:
        if state.get("review_decision") != "APPROVE":
            raise WorkflowResumeConflict("当前任务没有有效批准")
        # M3-01 leaves execution at the Spec's SUBMITTING boundary; M3-02 owns order commit.
        return {"projection_state": "SUBMITTING"}

    async def reject_task(state: WorkflowState) -> WorkflowState:
        if state.get("review_decision") != "REJECT":
            raise WorkflowResumeConflict("当前任务没有有效拒绝决定")
        return {"projection_state": "REJECTED"}

    builder = StateGraph(WorkflowState)
    for name, node in (
        ("ingest", ingest),
        ("parse", parse),
        ("plan_tools", plan_tools),
        ("execute_tools", execute_tools),
        ("validate", validate),
        ("prepare_input_wait", prepare_input_wait),
        ("await_input", await_input),
        ("persist_draft", persist_draft),
        ("prepare_review", prepare_review),
        ("await_review", await_review),
        ("finish_approval", finish_approval),
        ("reject_task", reject_task),
    ):
        builder.add_node(name, node)
    builder.add_edge(START, "ingest")
    builder.add_edge("ingest", "parse")
    builder.add_edge("parse", "plan_tools")
    builder.add_edge("plan_tools", "execute_tools")
    builder.add_edge("execute_tools", "validate")
    builder.add_conditional_edges(
        "validate",
        lambda state: (
            "prepare_input_wait" if state.get("missing_fields") else "persist_draft"
        ),
        {"prepare_input_wait": "prepare_input_wait", "persist_draft": "persist_draft"},
    )
    builder.add_edge("prepare_input_wait", "await_input")
    builder.add_edge("await_input", "ingest")
    builder.add_conditional_edges(
        "persist_draft",
        lambda state: "prepare_review" if state.get("projection_state") == "WAITING_APPROVAL" else "prepare_input_wait",
        {"prepare_review": "prepare_review", "prepare_input_wait": "prepare_input_wait"},
    )
    builder.add_edge("prepare_review", "await_review")
    builder.add_conditional_edges(
        "await_review",
        lambda state: {
            "APPROVE": "finish_approval",
            "RETURN": "prepare_input_wait",
            "REJECT": "reject_task",
            "EDIT": "ingest",
        }.get(state.get("review_decision"), "reject_task"),
        {"finish_approval": "finish_approval", "prepare_input_wait": "prepare_input_wait", "reject_task": "reject_task", "ingest": "ingest"},
    )
    builder.add_edge("finish_approval", END)
    builder.add_edge("reject_task", END)
    return builder.compile(checkpointer=checkpointer)


def wait_details(snapshot: Any) -> dict[str, Any] | None:
    """从官方 checkpoint 快照读取活动等待点，供业务投影保存和重启重建。"""
    config = getattr(snapshot, "config", None) or {}
    checkpoint_id = config.get("configurable", {}).get("checkpoint_id")
    values = getattr(snapshot, "values", None) or {}
    tasks = getattr(snapshot, "tasks", None) or ()
    interrupts: tuple[Interrupt, ...] = tuple(
        interruption
        for task in tasks
        for interruption in (getattr(task, "interrupts", None) or ())
        if isinstance(interruption, Interrupt)
    )
    if not interrupts:
        return None
    interruption = interrupts[0]
    payload = interruption.value
    if not isinstance(payload, Mapping) or not isinstance(checkpoint_id, str):
        raise WorkflowResumeConflict("LangGraph 等待检查点内容无效")
    return {
        "checkpoint_id": checkpoint_id,
        "interrupt_id": interruption.id,
        "kind": payload.get("kind"),
        "task_id": payload.get("task_id"),
        "graph_version": payload.get("graph_version"),
        "draft_id": payload.get("draft_id"),
        "draft_version": payload.get("draft_version"),
        "content_hash": payload.get("content_hash"),
        "missing_fields": payload.get("missing_fields", []),
        "reason": payload.get("reason"),
        "state_graph_version": values.get("graph_version"),
    }


async def start_or_continue_task_graph(
    graph: Any, *, input_state: WorkflowState
) -> Any:
    """相同 RUN_TASK 作业崩溃重试时从 checkpoint 继续，避免重新提交入口状态。"""
    task_id = input_state.get("task_id")
    command_id = input_state.get("command_id")
    graph_version = input_state.get("graph_version")
    if not isinstance(task_id, str) or not isinstance(command_id, str):
        raise TypeError("任务启动状态缺少 task_id 或 command_id")
    if graph_version != GRAPH_VERSION:
        raise WorkflowResumeConflict("当前部署不支持该任务图版本")

    config = task_graph_config(task_id)
    snapshot = await graph.aget_state(config)
    checkpoint_id = (
        (getattr(snapshot, "config", None) or {})
        .get("configurable", {})
        .get("checkpoint_id")
    )
    if not isinstance(checkpoint_id, str):
        return await graph.ainvoke(input_state, config=config)

    values = getattr(snapshot, "values", None) or {}
    if (
        values.get("task_id") != task_id
        or values.get("graph_version") != graph_version
        or values.get("command_id") != command_id
    ):
        raise WorkflowResumeConflict("已有 checkpoint 不属于当前任务启动命令")
    if wait_details(snapshot) is not None:
        # 可能是图已中断、但任务表等待投影尚未提交；调用方会从快照补建投影。
        return values
    if getattr(snapshot, "next", ()):
        return await graph.ainvoke(None, config=config)
    return values


async def resume_task_graph(
    graph: Any,
    *,
    task_id: UUID,
    command_id: UUID,
    checkpoint_id: str,
    interrupt_id: str,
    graph_version: str,
) -> Any:
    """只恢复当前官方检查点中的中断；旧任务、版本和等待命令均拒绝。"""
    if graph_version != GRAPH_VERSION:
        raise WorkflowResumeConflict("当前部署不支持该任务图版本")
    config = _thread_config(task_id)
    snapshot = await graph.aget_state(config)
    details = wait_details(snapshot)
    if details is None:
        raise WorkflowResumeConflict("任务当前没有可恢复的等待点")
    expected = {
        "task_id": str(task_id),
        "checkpoint_id": checkpoint_id,
        "interrupt_id": interrupt_id,
        "graph_version": graph_version,
        "state_graph_version": GRAPH_VERSION,
    }
    if any(details.get(field) != value for field, value in expected.items()):
        raise WorkflowResumeConflict("恢复命令与当前任务检查点或等待点不匹配")
    return await graph.ainvoke(
        Command(resume={"command_id": str(command_id)}), config=config
    )


async def resume_or_continue_task_graph(
    graph: Any,
    *,
    task_id: UUID,
    command_id: UUID,
    checkpoint_id: str,
    interrupt_id: str,
    graph_version: str,
) -> Any:
    """同一命令崩溃重试时续跑已恢复节点，不重复消费 interrupt 或重放入口。"""
    if graph_version != GRAPH_VERSION:
        raise WorkflowResumeConflict("当前部署不支持该任务图版本")
    config = _thread_config(task_id)
    snapshot = await graph.aget_state(config)
    values = getattr(snapshot, "values", None) or {}
    if (
        values.get("task_id") != str(task_id)
        or values.get("graph_version") != graph_version
    ):
        raise WorkflowResumeConflict("已有 checkpoint 不属于当前任务图版本")
    current_wait = wait_details(snapshot)
    resume_command = values.get("resume_command") or {}
    already_applied_command = values.get("command_id") == str(command_id) or (
        isinstance(resume_command, Mapping)
        and resume_command.get("id") == str(command_id)
    )
    if already_applied_command:
        if current_wait is not None:
            return values
        if getattr(snapshot, "next", ()):
            return await graph.ainvoke(None, config=config)
        return values
    if current_wait is None or (
        current_wait.get("checkpoint_id") != checkpoint_id
        or current_wait.get("interrupt_id") != interrupt_id
    ):
        raise WorkflowResumeConflict("恢复命令与当前任务的中断不匹配")
    return await resume_task_graph(
        graph,
        task_id=task_id,
        command_id=command_id,
        checkpoint_id=checkpoint_id,
        interrupt_id=interrupt_id,
        graph_version=graph_version,
    )


def task_graph_config(task_id: UUID | str) -> RunnableConfig:
    """所有初次执行及恢复都使用稳定任务 ID 作为 thread_id。"""
    return _thread_config(task_id)
