"""The orchestrator: the deterministic engine of the whole system.

One cycle is:

    OBSERVE -> UNDERSTAND -> PLAN -> SELECT -> EXECUTE -> VERIFY
            -> ANALYSE -> UPDATE STATE -> REPLAN -> (repeat or STOP)

Every arrow above is code in this file, not model reasoning. The model is
consulted inside EXECUTE (and optionally to override SELECT) but it never
decides: it proposes, this module validates and disposes. Concretely:

    model-owned    what the change is, what the architecture looks like,
                   what a failure's root cause probably is, what to do next

    this module    which task runs next, whether it is allowed to run, which
                   sandbox it runs in, when a state transition is legal,
                   whether the evidence justifies COMPLETED, when the run
                   stops, what gets committed, and what is persisted

That is the whole safety story: model output is data until this module has
validated it against a state machine, a permission class, and real command
output.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..agents.architect import ArchitectAgent, ArchitectureProposal
from ..agents.coder import CoderAgent
from ..agents.debugger import DebuggerAgent
from ..agents.director import DirectorAgent, DirectorProposal
from ..agents.intent_compiler import IntentCompiler, ProjectIntent, literal_intent
from ..agents.planner import PlannerAgent, fallback_plan
from ..agents.product import ProductAgent, fallback_requirements
from ..agents.qa import QAAgent, gap_task
from ..agents.release import ReleaseAgent
from ..agents.researcher import ResearchAgent
from ..agents.reviewer import ReviewerAgent
from ..agents.security import SecurityAgent
from ..agents.shared import make_tasks_from_spec
from ..agents.tester import TesterAgent
from ..core.events import EventTypes
from ..core.state_machine import TaskState
from ..core.store import (
    CheckpointRecord,
    DecisionRecord,
    ProjectRecord,
    RunRecord,
    UnknownRecord,
)
from ..core.task import AttemptRecord, Task, TaskGraph, TaskRole, new_id, now_iso
from ..git.manager import GitError, GitManager
from ..messaging import (
    DIRECTOR,
    ORCHESTRATOR,
    DeliveryState,
    Message,
    MsgPriority,
    MsgType,
)
from ..models.router import ModelRouter
from ..verification.engine import CheckStatus, VerificationEngine, VerificationReport
from .base import AgentDeps, AgentResult, AgentRunner
from .budget import BudgetManager
from .constitution import ensure_constitution
from .context import ContextBuilder
from .control import ControlChannel
from .hooks import HookRunner
from .lessons import lessons_context_section, record_decision_lessons, record_project_lessons
from .locks import LockManager, resource_keys
from .memory import memory_store
from .permissions import ToolBox
from .review_board import ReviewBoard
from .risk import high_risk_commands
from .roadmap import milestone_self_evaluation, refresh_roadmap
from .stop import StopEngine, StopReason, StopSignal

EventHook = Callable[[str, dict[str, Any]], None]


class NoObjective(RuntimeError):
    """Raised when a run starts without a recorded objective."""


@dataclass
class CycleReport:
    """What one cycle did. Used by the CLI, the tests, and the logs."""

    index: int = 0
    task_id: str = ""
    agent: str = ""
    action: str = "idle"
    ok: bool = True
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    duration_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "task_id": self.task_id,
            "agent": self.agent,
            "action": self.action,
            "ok": self.ok,
            "detail": self.detail,
            "duration_s": self.duration_s,
            "evidence": self.evidence,
        }


@dataclass
class RunResult:
    status: str
    stop: StopSignal | None
    cycles: int = 0
    completed: int = 0
    failed: int = 0
    cost_usd: float = 0.0
    started_at: str = ""
    finished_at: str = ""
    summary: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "stop": self.stop.as_dict() if self.stop else None,
            "cycles": self.cycles,
            "completed": self.completed,
            "failed": self.failed,
            "cost_usd": round(self.cost_usd, 4),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "summary": self.summary,
        }


class Orchestrator:
    """Owns the loop, the state machine, and every side effect."""

    def __init__(
        self,
        context: Any,  # RuntimeContext (typed loosely to avoid an import cycle)
        *,
        router: ModelRouter | None = None,
        on_event: EventHook | None = None,
        max_cycles: int = 200,
        use_model_director: bool = True,
    ):
        self.ctx = context
        self.workspace = context.workspace
        self.store = context.store
        self.config = context.config
        self.repo_root: Path = context.repo_root
        self.git = GitManager(self.repo_root)
        self.router = router or ModelRouter(self.config)
        self.on_event = on_event
        self.max_cycles = max_cycles
        self.use_model_director = use_model_director

        self.budget = BudgetManager(config=self.config.budget)
        self.locks = LockManager()
        self.stop_engine = StopEngine(max_task_attempts=self.config.budget.max_task_attempts)
        self.control = ControlChannel(self.workspace.paths.execution)
        self.context_builder = ContextBuilder(self.workspace)
        self.hooks = HookRunner(self.workspace, enabled=self.config.hooks_enabled)

        self.graph = TaskGraph()
        self.run = RunRecord(id=new_id("RUN"), project_id=self.store.project_id)

        # Agent-to-agent message transport (comms spec §46, §94): the
        # orchestrator publishes lifecycle messages and consumes the
        # Director inbox; agents speak through AgentMessenger only.
        from ..messaging import (
            AgentMessenger,
            DirectorInbox,
            MessageService,
            MessageStore,
            default_agents,
        )

        self.messaging = MessageService(
            MessageStore(self.store.db, self.store.project_id),
            self.workspace.events,
            run_id=self.run.id,
            agents=default_agents(),
        )
        self.director_inbox = DirectorInbox(self.messaging)
        self.messenger = {
            name: AgentMessenger(self.messaging, name) for name in self.messaging.agents
        }
        self.history: list[CycleReport] = []

        self._consecutive_no_progress = 0
        self._pause_requested = False
        self._stop_requested = False
        self._pending_approvals: list[str] = []
        self._pending_rejections: list[str] = []
        self._cycle_index = 0
        self._checkpoint_counter = 0
        # Supervised mode: task ids the operator has explicitly approved.
        self._approved_tasks: set[str] = set(self._load_approved_tasks())
        # The QA gate runs before PROJECT_COMPLETE is accepted; a bounded
        # number of rounds stops gap-finding and gate-failing from ping-ponging.
        self._qa_rounds = 0
        self.MAX_QA_ROUNDS = 3
        # Architecture Review Board guard: once per question per process (§39).
        self._board_convened_for: set[str] = set()
        # Observability: heartbeat cadence and goal-drift tracking (§55).
        self._last_heartbeat = time.perf_counter()
        self.HEARTBEAT_SECONDS = 60.0
        self._last_goal_progress: float | None = None

        # The orchestrator's own sandbox is privileged on purpose: it plans,
        # verifies, and commits. Agents never receive it — each agent gets a
        # sandbox built from its own permission class (see `tools_for`).
        self.orchestrator_tools = ToolBox(
            work_root=self.repo_root,
            permissions=self.config.permission_for("director"),
            sandbox_backend=self.config.sandbox_backend,
            sandbox_image=self.config.sandbox_image,
        )
        self.deps = AgentDeps(
            router=self.router,
            tools=self.orchestrator_tools,
            workspace=self.workspace,
            store=self.store,
            git=self.git,
        )
        self.runner = AgentRunner(self.deps, event_log=self.workspace.events, on_event=self._hook)

        self.agents: dict[str, Any] = {
            "intent": IntentCompiler(self.deps),
            "product": ProductAgent(self.deps),
            "architect": ArchitectAgent(self.deps),
            "planner": PlannerAgent(self.deps),
            "coder": CoderAgent(self.deps),
            "tester": TesterAgent(self.deps),
            "debugger": DebuggerAgent(self.deps),
            "reviewer": ReviewerAgent(self.deps),
            "security": SecurityAgent(self.deps),
            "director": DirectorAgent(self.deps),
            "researcher": ResearchAgent(self.deps),
            "qa": QAAgent(self.deps),
            "release": ReleaseAgent(self.deps),
        }
        self.director: DirectorAgent = self.agents["director"]
        self.planner: PlannerAgent = self.agents["planner"]
        self.coder: CoderAgent = self.agents["coder"]
        self.debugger: DebuggerAgent = self.agents["debugger"]
        self.reviewer: ReviewerAgent = self.agents["reviewer"]

    # ==================================================================
    # plumbing
    # ==================================================================

    def tools_for(
        self, agent_class: str, work_root: Path | None = None, *, approved: bool = False
    ) -> ToolBox:
        """Build the sandbox for one agent's permission class.

        `approved=True` is passed only for tasks a human explicitly approved in
        supervised mode — it lifts the HIGH-risk command refusal for that task.
        The run budget and workspace are attached so agents get the
        budget_status and save/recall memory tools (gemini/kilo/codex pattern).
        """
        return ToolBox(
            work_root=work_root or self.repo_root,
            permissions=self.config.permission_for(agent_class),
            sandbox_backend=self.config.sandbox_backend,
            sandbox_image=self.config.sandbox_image,
            allow_high_risk=approved,
            budget=self.budget,
            workspace=self.workspace,
        )

    def _heartbeat(self) -> None:
        """Emit a periodic liveness signal for external monitors (v0.4)."""
        elapsed = time.perf_counter() - self._last_heartbeat
        if elapsed >= self.HEARTBEAT_SECONDS:
            self._last_heartbeat = time.perf_counter()
            self.emit(
                EventTypes.RUN_HEARTBEAT,
                cycle=self._cycle_index,
                progress=self.graph.progress(),
                budget=self.budget.snapshot(),
            )

    def _track_goal_drift(self, proposal: Any) -> None:
        """Record the director's goal-progress self-assessment; warn on regressions.

        The director re-assesses how far the project is from the ORIGINAL
        objective every cycle; a significant drop means recent work moved away
        from the goal and is worth surfacing (§55).
        """
        progress = proposal.goal_progress
        if not 0.0 < progress <= 1.0:
            return
        previous = self._last_goal_progress
        self._last_goal_progress = progress
        if previous is not None and previous - progress >= 0.2:
            self.emit(
                "goal.drift_detected",
                previous=round(previous, 2),
                current=round(progress, 2),
                rationale=proposal.rationale[:200],
                new_risks=proposal.new_risks[:5],
            )

    def _hook(self, event: str, payload: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        with contextlib.suppress(Exception):
            self.on_event(event, payload)  # a broken listener must never break the run

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        """Append one event to the JSONL log and its SQLite mirror."""
        payload = self.workspace.events.append(event, **fields)
        with contextlib.suppress(Exception):
            self.store.append_event(event, payload)
        self._hook(event, payload)
        with contextlib.suppress(Exception):
            self.hooks.dispatch(event, payload)  # user hooks never break the run
        return payload

    def _persist_graph(self) -> None:
        existing = self.workspace.load_graph()
        if {tid: task.model_dump(mode="json") for tid, task in self.graph.tasks.items()} == {
            tid: task.model_dump(mode="json") for tid, task in existing.tasks.items()
        }:
            return  # identical graph: a no-op write would dirty the repository
        self.graph.updated_at = now_iso()
        self.workspace.save_graph(self.graph)
        try:
            self.store.save_graph(self.graph)
        except Exception as exc:  # persistence failure is an environment failure
            self.emit("persistence.error", detail=str(exc))

    def _load_state(self) -> None:
        graph = self.workspace.load_graph()
        if not graph.tasks:
            graph = self.store.load_graph()
        self.graph = graph

    def _save_run_state(self, status: str = "running") -> None:
        self.workspace.save_run(
            {
                "run_id": self.run.id,
                "project_id": self.store.project_id,
                "status": status,
                "started_at": self.run.started_at,
                "finished_at": self.run.finished_at,
                "stop_reason": self.run.stop_reason,
                "cycle": self._cycle_index,
                "graph": self.graph.progress(),
                "budget": self.budget.snapshot(),
                "locks": self.locks.snapshot(),
                "approved_tasks": sorted(self._approved_tasks),
            }
        )

    def _load_approved_tasks(self) -> list[str]:
        run_doc = self.workspace.load_run()
        approved = run_doc.get("approved_tasks", [])
        return [str(t) for t in approved] if isinstance(approved, list) else []

    def _persist_approved_tasks(self) -> None:
        self._save_run_state(status=self.run.status or "running")

    def _record_budget(self, result: AgentResult, agent_name: str) -> None:
        if not (result.cost_usd or result.tokens_in or result.tokens_out):
            return
        self.budget.record(
            cost_usd=result.cost_usd, tokens_in=result.tokens_in, tokens_out=result.tokens_out
        )
        with contextlib.suppress(Exception):
            self.store.record_usage(
                self.run.id,
                agent_name,
                self.config.route_for(agent_name).model,
                result.tokens_in,
                result.tokens_out,
                result.cost_usd,
            )

    def _mark_ready(self, task: Task) -> None:
        """A freshly created task is QUEUED; make it schedulable exactly once."""
        if task.status == TaskState.QUEUED:
            self._transition(
                task, TaskState.READY, agent="orchestrator", note="dependencies satisfied"
            )

    def _transition(self, task: Task, target: TaskState, *, agent: str, note: str = "") -> None:
        previous = task.status
        try:
            task.set_state(target, agent=agent, note=note)
        except Exception as exc:
            self.emit(
                "task.transition_rejected", task_id=task.id, to_state=target.value, detail=str(exc)
            )
            raise
        if previous != target:
            self.emit(
                EventTypes.TASK_STATE_CHANGED,
                task_id=task.id,
                agent=agent,
                from_state=previous.value,
                to_state=target.value,
                note=note[:200],
            )

    # ==================================================================
    # recovery
    # ==================================================================

    def recover_stale_tasks(self) -> list[str]:
        """Dead-agent detection: nothing is running, so any active task is stale.

        A task left mid-cycle by a process that no longer exists is returned
        to a schedulable state instead of blocking the run forever. Work in
        VERIFYING/REVIEWING is re-verified rather than trusted.
        """
        recovered: list[str] = []
        for task in self.graph.active_tasks():
            if task.status == TaskState.ARCHITECTURE_REVIEW:
                continue
            try:
                if task.status in (TaskState.VERIFYING, TaskState.REVIEWING):
                    task.set_state(
                        TaskState.FAILED, agent="recovery", note="interrupted by a crashed run"
                    )
                    task.set_state(
                        TaskState.READY, agent="recovery", note="re-queued for re-verification"
                    )
                elif task.status in (TaskState.DIAGNOSING, TaskState.REPAIRING):
                    task.set_state(
                        TaskState.READY, agent="recovery", note="re-queued after interruption"
                    )
                else:  # ASSIGNED / IMPLEMENTING
                    task.set_state(
                        TaskState.FAILED, agent="recovery", note="interrupted by a crashed run"
                    )
                    task.set_state(
                        TaskState.READY, agent="recovery", note="re-queued after interruption"
                    )
                recovered.append(task.id)
            except Exception as exc:
                self.emit("recovery.error", task_id=task.id, detail=str(exc))
        if recovered:
            self._persist_graph()
            self.emit("run.recovered", task_ids=recovered)
        return recovered

    def _preflight_dirty_repo(self) -> None:
        """Aider's discipline: never build on unknown dirty state.

        Uncommitted leftovers (a crash mid-task, an operator's edit) would be
        silently mixed into the next task's checkpoint commit. Commit them
        explicitly as a pre-run snapshot so every task checkpoint contains
        exactly its own work.
        """
        if not self.config.git_checkpoints:
            return
        try:
            if not self.git.is_repo() or not self.git.is_dirty():
                return
            commit = self.git.create_checkpoint_commit(
                "pre-run", f"pre-run: uncommitted work at {now_iso()}"
            )
            self.emit("git.pre_run_commit", commit=commit[:12])
        except GitError as exc:
            self.emit("git.preflight_failed", detail=str(exc)[:200])

    # ==================================================================
    # the loop
    # ==================================================================

    async def run_loop(self, objective: str = "") -> RunResult:
        """Run the autonomous loop until a stop condition fires."""
        self.run.started_at = now_iso()
        objective = (
            objective
            or self.ctx.objective
            or str(self.workspace.load_project().get("objective", ""))
        )
        if not objective.strip():
            # Planning without an objective would produce a meaningless graph;
            # failing fast is the honest behaviour (plan.md §27: the objective
            # is the only goal).
            raise NoObjective(
                'no objective recorded; run `auto init --objective "..."` '
                'or `auto run "the goal"` before executing'
            )
        self._set_objective(objective)

        ensure_constitution(self.workspace, self.config, objective)
        self._load_state()
        self.recover_stale_tasks()
        self._preflight_dirty_repo()

        self.store.start_run(self.run)
        self.emit(
            EventTypes.RUN_STARTED,
            run_id=self.run.id,
            project_id=self.store.project_id,
            objective=objective[:500],
            budget=self.config.budget.model_dump(),
        )
        self._save_run_state()

        self._consume_control()
        if not self.graph.tasks:
            await self._bootstrap(objective)

        stop: StopSignal | None = None
        while self._cycle_index < self.max_cycles:
            self._cycle_index += 1
            self._consume_control()
            self._heartbeat()
            # Agent communication: expired messages leave the queues, then the
            # Director reads its inbox before proposing (comms spec §13, §76).
            self.messaging.expire_stale()
            await self._process_director_inbox()

            exhausted = self.budget.exhausted()
            if exhausted:
                self.emit(
                    EventTypes.BUDGET_EXCEEDED, reason=exhausted, budget=self.budget.snapshot()
                )

            stop = self.stop_engine.evaluate(
                self.graph,
                budget_exhausted=exhausted,
                pause_requested=self._pause_requested,
                stop_requested=self._stop_requested,
                escalations=len(self.workspace.pending_escalations()),
                consecutive_no_progress=self._consecutive_no_progress,
            )
            if stop is not None:
                # Completion is not accepted on the graph's word alone: the QA
                # gate compares the delivered work against the original intent
                # first (§5K, §30). Unmet requirements reopen the graph.
                if (
                    stop.reason == StopReason.PROJECT_COMPLETE
                    and self._qa_rounds < self.MAX_QA_ROUNDS
                ):
                    reopened = await self._run_qa_gate()
                    if reopened:
                        stop = None
                if stop is not None:
                    break

            started = time.perf_counter()
            try:
                report = await self._run_cycle()
            except Exception as exc:  # noqa: BLE001 - one bad cycle must not kill the run
                # The cycle unwound mid-task: every in-process lock is stale
                # now (nothing is executing), so release them all before the
                # next selection or the run livelocks on lock_wait.
                for owner in set(self.locks.snapshot().values()):
                    self.locks.release(owner)
                self.emit("cycle.error", cycle=self._cycle_index, error=str(exc)[:400])
                report = CycleReport(
                    action="cycle_error",
                    ok=False,
                    detail=f"unexpected cycle error: {exc}"[:400],
                )
            report.index = self._cycle_index
            report.duration_s = round(time.perf_counter() - started, 2)
            self.history.append(report)

            self._consecutive_no_progress = 0 if report.ok else self._consecutive_no_progress + 1
            self.emit(
                "cycle.completed",
                cycle=report.index,
                task_id=report.task_id,
                agent=report.agent,
                action=report.action,
                ok=report.ok,
                detail=report.detail[:400],
                duration_s=report.duration_s,
            )
            self._save_run_state()

        # Drain the Director inbox one last time so every report is
        # interpreted and settled before the run summary (§94).
        await self._process_director_inbox()

        if stop is None:
            stop = StopSignal(
                StopReason.NO_PROGRESS,
                detail=f"reached the maximum of {self.max_cycles} cycles without a stop condition",
                remediation="raise max_cycles, or inspect the task graph and replan.",
            )
        if stop.is_success:
            await self._release()
        return self._finish(stop)

    def _set_objective(self, objective: str) -> None:
        self.ctx.objective = objective
        self.ctx.project.objective = objective
        self.workspace.update_project(objective=objective)
        self.store.upsert_project(
            ProjectRecord(
                id=self.store.project_id,
                name=self.ctx.project.name,
                objective=objective,
                intent=self.workspace.load_project().get("intent", {}) or {},
            )
        )

    def _finish(self, stop: StopSignal) -> RunResult:
        status = "completed" if stop.is_success else stop.reason.value.lower()
        self._memory_run_digest(stop)
        self._commit_memory_snapshot(stop)
        self.run.finished_at = now_iso()
        self.run.status = status
        self.run.stop_reason = stop.reason.value
        stats = {
            "cycles": self._cycle_index,
            "progress": self.graph.progress(),
            "budget": self.budget.snapshot(),
        }
        with contextlib.suppress(Exception):
            self.store.finish_run(self.run.id, status, stop.reason.value, stats)
        self.emit(EventTypes.RUN_STOPPED, run_id=self.run.id, stop=stop.as_dict(), stats=stats)
        self._save_run_state(status=status)
        self._persist_graph()
        return RunResult(
            status=status,
            stop=stop,
            cycles=self._cycle_index,
            completed=len(self.graph.completed_tasks()),
            failed=len(self.graph.failed_tasks()),
            cost_usd=self.budget.cost_usd,
            started_at=self.run.started_at,
            finished_at=self.run.finished_at,
            summary=stop.describe(),
        )

    def _consume_control(self) -> None:
        """Read and consume the operator's control request, exactly once."""
        signal = self.control.read()
        if not signal.any_request():
            return
        # Consume the file FIRST: an operator pause/stop written during the
        # (multi-write) processing below was previously deleted unseen by the
        # trailing clear().
        self.control.clear()
        if signal.pause and not self._pause_requested:
            self._pause_requested = True
            self.emit(EventTypes.RUN_PAUSED, reason=signal.reason)
        if signal.stop and not self._stop_requested:
            self._stop_requested = True
            self.emit("run.stop_requested", reason=signal.reason)
        if signal.resume and self._pause_requested:
            self._pause_requested = False
            self.emit(EventTypes.RUN_RESUMED)
        for escalation_id in signal.approvals:
            item = self.workspace.resolve_escalation(
                escalation_id, "approved", "approved by the operator"
            )
            self._pending_approvals.append(escalation_id)
            if item and item.get("task_id"):
                task_id = str(item["task_id"])
                self._approved_tasks.add(task_id)
                self._persist_approved_tasks()
                task = self.graph.tasks.get(task_id)
                if task is not None and task.status == TaskState.ARCHITECTURE_REVIEW:
                    # Mirror the daemon: an approved escalation requeues the
                    # task, otherwise the next run stops with REPEATED_FAILURE
                    # before doing any work.
                    self._transition(
                        task,
                        TaskState.READY,
                        agent="orchestrator",
                        note="human approved another attempt",
                    )
                    self._persist_graph()
            self.emit("escalation.approved", escalation=escalation_id)
        for escalation_id in signal.rejections:
            item = self.workspace.resolve_escalation(
                escalation_id, "rejected", "rejected by the operator"
            )
            self._pending_rejections.append(escalation_id)
            if item and item.get("task_id"):
                task = self.graph.tasks.get(str(item["task_id"]))
                if task is not None and task.status == TaskState.ARCHITECTURE_REVIEW:
                    # Mirror the daemon: rejection cancels the task and its
                    # dependents, who could never run otherwise.
                    for cancelled_id in self.graph.cascade_cancel(
                        task.id, agent="orchestrator", note="human rejected the work"
                    ):
                        self.emit(EventTypes.TASK_CANCELLED, task_id=cancelled_id)
                    self._persist_graph()
            self.emit("escalation.rejected", escalation=escalation_id)

    # ==================================================================
    # bootstrap: intent -> requirements -> architecture -> plan
    # ==================================================================

    async def _bootstrap(self, objective: str) -> None:
        self.emit("bootstrap.started", objective=objective[:300])
        context = self.context_builder.build(task=None, role="director", repo_root=self.repo_root)

        intent = await self._compile_intent(objective, context)
        self.workspace.write_artifact("requirements/specification.md", intent.to_markdown())
        self.workspace.update_project(intent=intent.model_dump(mode="json"))
        self._set_objective(intent.objective or objective)
        self.emit(
            EventTypes.INTENT_COMPILED,
            enhanced=intent.enhanced,
            assumptions=len(intent.assumptions),
            requirements=len(intent.requirements),
        )
        for question in intent.unknowns:
            self._queue_unknown(question, raised_by="intent-compiler")
        # §14: unknowns are routed to research instead of stalling the main flow.
        for question in intent.unknowns[:3]:
            matches = [u for u in self.store.open_unknowns() if u.question == question]
            if matches:
                await self._research_unknown(question, matches[0].id)

        requirements = await self._derive_requirements(intent, context)
        self.workspace.write_json_artifact("requirements/requirements.json", requirements)

        await self._design_architecture(intent, requirements, context)
        await self._plan(intent, requirements)
        self.refresh_roadmap()

        self.emit(
            "bootstrap.completed", tasks=len(self.graph.tasks), ready=len(self.graph.ready_tasks())
        )

    async def _compile_intent(self, objective: str, context: Any) -> ProjectIntent:
        if not self.config.enhance_prompt:
            return literal_intent(objective)
        agent = self.agents["intent"]
        agent.bind_tools(self.tools_for(agent.agent_class))
        result = await self.runner.run(agent, None, context)
        self._record_budget(result, agent.name)
        if result.ok and result.output:
            try:
                return ProjectIntent.model_validate(result.output)
            except Exception as exc:
                self.emit("intent.invalid", detail=str(exc))
        self.emit("intent.fallback", reason=result.error or "model returned no intent")
        return literal_intent(objective)

    async def _derive_requirements(self, intent: ProjectIntent, context: Any) -> dict[str, Any]:
        agent = self.agents["product"]
        agent.bind_tools(self.tools_for(agent.agent_class))
        result = await self.runner.run(agent, None, context)
        self._record_budget(result, agent.name)
        if result.ok and result.output:
            return result.output
        self.emit("requirements.fallback", reason=result.error or "model returned no requirements")
        return fallback_requirements(intent.features, intent.objective).model_dump()

    async def _design_architecture(
        self, intent: ProjectIntent, requirements: dict[str, Any], context: Any
    ) -> None:
        agent = self.agents["architect"]
        agent.bind_tools(self.tools_for(agent.agent_class))
        lessons = lessons_context_section()
        extra = {
            "intent": f"# COMPILED INTENT\n\n{intent.to_markdown()}",
            "requirements": f"# REQUIREMENTS\n\n{requirements}",
        }
        if lessons:
            extra["lessons"] = lessons  # §60: architecture memory across projects
        arch_context = self.context_builder.build(
            task=None, role="architect", repo_root=self.repo_root, extra_sections=extra
        )
        result = await self.runner.run(agent, None, arch_context)
        self._record_budget(result, agent.name)
        # The Director's delegation is a real message exchange: the request is
        # correlated with the architect's result (comms spec §82).
        arch_request = self.messaging.send(
            msg_type=MsgType.ARCHITECTURE_REQUEST,
            sender=DIRECTOR,
            recipient="architect",
            payload={"question": f"Design the architecture for: {intent.objective[:300]}"},
            requires_response=True,
        )
        # The architect receives the delegation before any model work: ACK is
        # "received", never "done" (comms spec §55).
        with contextlib.suppress(Exception):
            arch_request.set_state(DeliveryState.DELIVERED)
            self.messaging.store.save(arch_request)
            arch_request.set_state(DeliveryState.RECEIVED)
            self.messaging.store.save(arch_request)
            self.messaging.acknowledge(arch_request, "architect: accepted")
        if result.ok and result.output:
            try:
                proposal = ArchitectureProposal.model_validate(result.output)
            except Exception as exc:
                self.emit("architecture.invalid", detail=str(exc))
                with contextlib.suppress(Exception):
                    self.messaging.start_processing(arch_request)
                    self.messaging.complete(arch_request, "proposal invalid; fallback architecture recorded")
                return
            agent.persist(proposal)
            # The architect processes the delegation, then reports the result as
            # a correlated reply (request -> response, §7).
            with contextlib.suppress(Exception):
                self.messaging.start_processing(arch_request)
                self.messaging.complete(arch_request, "architecture delivered")
                self.messaging.send(
                    msg_type=MsgType.ARCHITECTURE_RESULT,
                    sender="architect",
                    recipient=DIRECTOR,
                    parent=arch_request,
                    payload={
                        "question": intent.objective[:200],
                        "findings": f"{len(proposal.modules)} modules, {len(proposal.decisions)} decisions",
                    },
                )
            self.emit(
                "architecture.recorded",
                modules=len(proposal.modules),
                decisions=len(proposal.decisions),
            )
            return
        with contextlib.suppress(Exception):
            self.messaging.start_processing(arch_request)
            self.messaging.complete(arch_request, "fallback architecture recorded")
        self.emit("architecture.fallback", reason=result.error or "model returned no architecture")
        self.workspace.write_artifact(
            "architecture/architecture.md",
            "# Architecture\n\nNo architecture was produced; tasks are executed against the "
            "project constitution and the compiled requirements.\n",
        )

    async def _plan(self, intent: ProjectIntent, requirements: dict[str, Any]) -> None:
        planner = self.planner
        planner.bind_tools(self.tools_for(planner.agent_class))
        context = self.context_builder.build(
            task=None,
            role="planner",
            repo_root=self.repo_root,
            extra_sections={
                "intent": f"# COMPILED INTENT\n\n{intent.to_markdown()}",
                "requirements": f"# REQUIREMENTS\n\n{requirements}",
            },
        )
        result = await self.runner.run(planner, None, context)
        self._record_budget(result, planner.name)

        tasks: list[Task] = []
        source = "deterministic-fallback"
        if result.ok and result.output.get("tasks"):
            try:
                tasks = [Task.model_validate(t) for t in result.output["tasks"]]
                source = "model"
            except Exception as exc:
                self.emit("plan.invalid", detail=str(exc))
        if not tasks:
            tasks = fallback_plan(
                intent.objective, intent.features or requirements.get("functional") or []
            )

        added = 0
        for task in tasks:
            if task.id in self.graph.tasks:
                continue
            self.graph.add_task(task)
            added += 1
        cycles = self.graph.detect_cycles()
        if cycles:
            planner._break_cycles(self.graph, cycles)
        self._persist_graph()
        self.emit(
            EventTypes.TASK_CREATED,
            count=added,
            total=len(self.graph.tasks),
            source=source,
            ready=len(self.graph.ready_tasks()),
        )

    # ==================================================================
    # one cycle
    # ==================================================================

    async def _run_cycle(self) -> CycleReport:
        # ---- OBSERVE / UNDERSTAND: deterministic baseline, model override ----
        proposal = self.director.deterministic_proposal(self.graph, "cycle baseline")
        if self.use_model_director and self.budget.remaining_usd() > 0:
            override = await self._ask_director()
            if override is not None:
                valid, reason = override.validate_against(self.graph)
                if valid:
                    proposal = override
                else:
                    self.emit("director.proposal_rejected", action=override.action, reason=reason)
                self._track_goal_drift(override)
        self.emit(
            "director.proposed",
            action=proposal.action,
            rationale=proposal.rationale[:300],
            task_ids=proposal.task_ids,
        )

        # ---- PLAN: apply validated management actions ----
        if not await self._apply_management_actions(proposal):
            return CycleReport(
                action="managed",
                ok=proposal.action
                in ("create_tasks", "replan", "reprioritise", "request_research"),
                detail=f"director action '{proposal.action}' applied; nothing to execute this cycle",
                evidence={"action": proposal.action},
            )

        # ---- SELECT: deterministic, priority-ordered, lock-aware ----
        task_ids = self._select_runnable(proposal.task_ids)
        if not task_ids:
            return self._idle_report()

        # ---- EXECUTE ----
        if self.config.worktree_parallelism and len(task_ids) > 1:
            capacity = max(1, self.config.budget.max_parallel_agents)
            reports = await self._run_parallel(task_ids[:capacity])
            return self._merge_reports(reports)

        # Sequential mode executes only the first selection; any further
        # selections must not keep holding resource locks.
        for extra_id in task_ids[1:]:
            self.locks.release(extra_id)
        task = self.graph.get(task_ids[0])

        # ---- SUPERVISED GATE (§16): high-risk work needs explicit approval ----
        if self._needs_approval(task):
            self._escalate(
                task.id,
                f"supervised mode: task '{task.title}' is high-risk and needs "
                "explicit approval before execution",
                kind="approval_gate",
            )
            self.locks.release(task.id)
            return CycleReport(
                task_id=task.id,
                action="awaiting_approval",
                ok=True,
                detail="supervised mode: escalated for approval",
            )
        return await self._execute_task(task)

    def _needs_approval(self, task: Task) -> bool:
        if self.config.run_mode != "supervised" or task.id in self._approved_tasks:
            return False
        if task.risk == "high":
            return True
        # A task whose verification commands include HIGH-risk operations
        # needs a human to see them before they run (OpenHands ConfirmRisky).
        return bool(high_risk_commands(list(task.verification_commands)))

    def _idle_report(self) -> CycleReport:
        """Nothing runnable. Waiting is progress-neutral, not a failure."""
        if self.graph.active_tasks():
            in_flight = ", ".join(f"{t.id}({t.status.value})" for t in self.graph.active_tasks())
            return CycleReport(action="waiting", ok=True, detail=f"in flight: {in_flight}")
        if self.graph.blocked_tasks():
            blocked = ", ".join(t.id for t in self.graph.blocked_tasks()[:5])
            return CycleReport(
                action="blocked", ok=True, detail=f"blocked by dependencies: {blocked}"
            )
        return CycleReport(
            action="idle",
            ok=False,
            detail="no runnable task and nothing in flight",
            evidence=self.director.status_report(self.graph),
        )

    def _select_runnable(self, preferred: list[str]) -> list[str]:
        """Deterministic selection: director's preference first, then ready order.

        Tasks whose declared artifacts collide with a running task's locks are
        skipped rather than raced.
        """
        ready_ids = [t.id for t in self.graph.ready_tasks()]
        ordered: list[str] = []
        for task_id in preferred:
            if task_id in ready_ids and task_id not in ordered:
                ordered.append(task_id)
        ordered.extend(tid for tid in ready_ids if tid not in ordered)

        selected: list[str] = []
        for task_id in ordered:
            task = self.graph.get(task_id)
            # Keys derive from declared artifacts only: locked_paths stores
            # already-computed keys ("dir:src"), and re-feeding them through
            # resource_keys minted junk like "dir:dir:src" on every restart.
            keys = resource_keys(list(task.artifacts))
            acquired, blocking = self.locks.acquire(task_id, keys)
            if not acquired:
                self.emit("task.lock_wait", task_id=task_id, blocked_by=blocking)
                continue
            task.locked_paths = sorted(set(keys))
            selected.append(task_id)
            if len(selected) >= max(1, self.config.budget.max_parallel_agents):
                break
        return selected

    async def _ask_director(self) -> DirectorProposal | None:
        director = self.director
        director.bind_tools(self.tools_for(director.agent_class))
        context = self.context_builder.build(
            task=None,
            role="director",
            repo_root=self.repo_root,
            extra_sections={"director_brief": self._director_brief()},
        )
        result = await self.runner.run(director, None, context)
        self._record_budget(result, director.name)
        if not result.ok or not result.output:
            return None
        try:
            return DirectorProposal.model_validate(result.output)
        except Exception:
            return None

    def _director_brief(self) -> str:
        progress = self.graph.progress()
        return (
            "# CURRENT SITUATION\n"
            f"objective: {self.ctx.objective}\n"
            f"progress: {progress}\n"
            f"budget: {self.budget.snapshot()}\n"
            f"ready: {[t.id for t in self.graph.ready_tasks()]}\n"
            f"active: {[(t.id, t.status.value) for t in self.graph.active_tasks()]}\n"
            f"failed: {[t.id for t in self.graph.failed_tasks()]}\n"
            f"locks: {self.locks.snapshot()}\n"
        )

    async def _apply_management_actions(self, proposal: DirectorProposal) -> bool:
        """Apply a validated Director proposal. False => run no task this cycle."""
        planner = self.planner
        planner.bind_tools(self.tools_for(planner.agent_class))

        if proposal.action == "create_tasks" and proposal.new_tasks:
            new_tasks = make_tasks_from_spec({"tasks": proposal.new_tasks})
            summary = planner.replan(self.graph, add_tasks=new_tasks, reason=proposal.rationale)
            self._persist_graph()
            self.emit(
                "director.created_tasks", tasks=summary["added"], rationale=proposal.rationale[:200]
            )
            return False

        if proposal.action == "reprioritise" and proposal.reprioritise:
            summary = planner.replan(
                self.graph, reprioritise=proposal.reprioritise, reason=proposal.rationale
            )
            self._persist_graph()
            self.emit("director.reprioritised", tasks=summary["reprioritised"])
            return False

        if proposal.action == "replan":
            await self._replan(proposal.rationale or "the director found the plan invalid")
            return False

        if proposal.action == "architecture_review":
            task_ids = [
                task_id
                for task_id in proposal.task_ids or [t.id for t in self.graph.failed_tasks()]
                if task_id in self.graph.tasks
            ]
            decision = await self._convene_board(
                f"director requested an architecture review: {proposal.rationale[:200]}",
                task_ids,
            )
            if decision is not None and decision.verdict == "revise_plan":
                await self._replan(f"review board: {decision.rationale[:200]}")
                return False
            if decision is None:
                # board unavailable: fall back to a human escalation
                for task_id in task_ids:
                    self._escalate(
                        task_id,
                        f"Architecture review requested by the director: {proposal.rationale}",
                        kind="architecture",
                    )
            return False

        if proposal.action == "request_research":
            questions = proposal.research_questions
            if not questions:
                questions = [u.question for u in self.store.open_unknowns()[:3]]
            for question in questions:
                unknown_id = self._queue_unknown(question, raised_by="director")
                await self._research_unknown(question, unknown_id)
            return False

        if proposal.action == "escalate":
            self._escalate(
                proposal.task_ids[0] if proposal.task_ids else "",
                proposal.escalation or proposal.rationale,
                kind="director",
            )
            return False

        if proposal.action == "stop":
            self._stop_requested = True
            self.emit("run.stop_proposed", reason=proposal.stop_reason or proposal.rationale)
            return False

        if proposal.cancel_task_ids:
            summary = planner.replan(
                self.graph, cancel_task_ids=proposal.cancel_task_ids, reason=proposal.rationale
            )
            self._persist_graph()
            self.emit("director.cancelled_tasks", tasks=summary["cancelled"])
            return False

        return True

    async def _replan(self, reason: str) -> None:
        """Rewrite the remaining work using current evidence."""
        for task in self.graph.all():
            if task.status in (TaskState.QUEUED, TaskState.READY, TaskState.FAILED):
                try:
                    task.set_state(TaskState.REPLAN, agent="orchestrator", note=reason[:200])
                    task.set_state(TaskState.QUEUED, agent="orchestrator", note="replanned")
                except Exception:
                    continue
        await self._plan(self._current_intent(), self._current_requirements())
        self._persist_graph()
        self.emit(EventTypes.TASK_REPLANNED, reason=reason[:300], tasks=len(self.graph.tasks))

    # ==================================================================
    # agent-to-agent messaging (comms spec §10, §94)
    #
    # The orchestrator is the only runtime component that publishes real
    # protocol messages: it speaks FOR the agents (whose model output it has
    # already validated) and it consumes the Director inbox, interpreting
    # incoming structured reports into the management actions this module
    # already owns. Agents never mutate each other's state directly (§93);
    # every coordination act is a persisted Message on the runtime-owned
    # identity of its sender.
    # ==================================================================

    def _publish_lifecycle(
        self, msg_type: MsgType, task: Task, sender: str, payload: dict[str, Any], **kwargs: Any
    ) -> None:
        """Best-effort lifecycle publication: comms must never break the loop."""
        try:
            body = dict(payload)
            body.setdefault("task_id", task.id)  # payload contract for task-bearing types
            self.messenger[sender].send(
                msg_type,
                payload=body,
                task_id=task.id,
                **kwargs,
            )
        except Exception as exc:  # flood/loop/validation must not kill execution
            self.emit("message.publish_error", task_id=task.id, msg_type=msg_type.value, error=str(exc)[:200])

    def _msg_task_assigned(self, task: Task) -> None:
        self._publish_lifecycle(
            MsgType.TASK_REQUEST,
            task,
            ORCHESTRATOR,
            {"task_id": task.id, "title": task.title[:200], "description": task.description[:1000]},
            recipient=task.role.value if task.role.value in self.messaging.agents else "coder",
            requires_response=True,
            context_refs=[f"task:{task.id}"],
            artifact_refs=list(task.artifacts[:10]),
        )

    def _msg_task_accepted(self, task: Task) -> None:
        self._publish_lifecycle(
            MsgType.TASK_ACCEPTED,
            task,
            task.role.value if task.role.value in self.messaging.agents else "coder",
            {"task_id": task.id, "note": "delegation acknowledged"},
            recipient=ORCHESTRATOR,
        )

    def _msg_task_completed(self, task: Task, summary: str) -> None:
        self._publish_lifecycle(
            MsgType.TASK_COMPLETED,
            task,
            task.role.value if task.role.value in self.messaging.agents else "coder",
            {
                "task_id": task.id,
                "summary": summary[:1000],
                "idempotency_key": f"completed:{self.run.id}:{task.id}",
            },
            recipient=ORCHESTRATOR,
            artifact_refs=list(task.artifacts[:10]),
        )

    def _msg_task_failed(
        self, task: Task, failure_type: str, summary: str, agent: str
    ) -> None:
        self._publish_lifecycle(
            MsgType.TASK_FAILED,
            task,
            agent if agent in self.messaging.agents else "coder",
            {
                "task_id": task.id,
                "failure_type": failure_type,
                "summary": summary[:1000],
                "attempt": task.attempts,
                "recommended_action": "debug",
            },
            recipient=ORCHESTRATOR,
            priority="high" if failure_type in ("SECURITY", "MODEL") else "normal",
        )

    def _msg_verification(self, task: Task, passed: bool, summary: str) -> None:
        self._publish_lifecycle(
            MsgType.VERIFICATION_RESULT,
            task,
            "tester",
            {"task_id": task.id, "passed": passed, "summary": summary[:1000]},
            recipient=ORCHESTRATOR,
            priority="high" if not passed else "normal",
        )

    def _msg_research_result(self, task: Task, answer: str, sources: list[str]) -> None:
        self._publish_lifecycle(
            MsgType.RESEARCH_RESULT,
            task,
            "researcher",
            {"task_id": task.id, "question": task.title[:200], "findings": answer[:1500]},
            recipient=ORCHESTRATOR,
            artifact_refs=[f"research/{task.id}.md"],
            **({"sources": sources[:5]} if sources else {}),
        )

    def _msg_review_result(self, task: Task, findings: list[dict[str, Any]], blocking: int) -> None:
        self._publish_lifecycle(
            MsgType.REVIEW_RESULT,
            task,
            "reviewer",
            {
                "task_id": task.id,
                "findings": [str(f.get("description", ""))[:120] for f in findings[:5]],
                "blocking_count": blocking,
            },
            recipient=ORCHESTRATOR,
        )

    def _msg_diagnosis(self, task: Task, diagnosis: dict[str, Any]) -> None:
        self._publish_lifecycle(
            MsgType.DISCOVERY_REPORTED,
            task,
            "debugger",
            {
                "discovery": f"root cause: {str(diagnosis.get('root_cause', ''))[:400]}",
                "confidence": 0.7,
                "idempotency_key": f"diagnosis:{self.run.id}:{task.id}:{task.attempts}",
            },
            recipient=ORCHESTRATOR,
            context_refs=[f"task:{task.id}"],
        )

    def _settle_delegation(self, task: Task, summary: str) -> None:
        """The delegation request's lifecycle follows the task's (§15, §79)."""
        for state in (DeliveryState.QUEUED, DeliveryState.DELIVERED, DeliveryState.RECEIVED):
            for msg in self.messaging.store.list_messages(
                msg_type=MsgType.TASK_REQUEST.value, task_id=task.id, state=state.value, limit=10
            ):
                try:
                    # Advance deterministically to RECEIVED before ACK/complete.
                    if msg.state == DeliveryState.QUEUED:
                        msg.set_state(DeliveryState.DELIVERED)
                        self.messaging.store.save(msg)
                        msg.set_state(DeliveryState.RECEIVED)
                    elif msg.state == DeliveryState.DELIVERED:
                        msg.set_state(DeliveryState.RECEIVED)
                    self.messaging.store.save(msg)
                    self.messaging.acknowledge(msg, "delegation settled")
                    self.messaging.complete(msg, summary[:300])
                except Exception:
                    continue

    def _msg_milestone(self, milestone: dict[str, Any]) -> None:
        """A genuine system observation — the one case a broadcast is honest (§32)."""
        try:
            self.messaging.send(
                msg_type=MsgType.STATUS_RESPONSE,
                sender=ORCHESTRATOR,
                recipient=DIRECTOR,
                payload={
                    "status": f"milestone {milestone['id']} complete: {milestone['name']}",
                    "tasks_completed": milestone["tasks_completed"],
                    "idempotency_key": f"milestone:{self.run.id}:{milestone['id']}",
                },
                priority=MsgPriority.HIGH,
            )
        except Exception as exc:
            self.emit("message.publish_error", milestone=milestone["id"], error=str(exc)[:200])

    def _record_message_traceability(self, message: Message, action: str, detail: str) -> None:
        """message → decision → action chain, persisted for audit (§41, §42, §61)."""
        with contextlib.suppress(Exception):
            self.store.save_decision(
                DecisionRecord(
                    id=new_id("DEC"),
                    project_id=self.store.project_id,
                    title=f"{message.type.value} -> {action} (from {message.id})",
                    body=f"message {message.id} from {message.sender} -> {action}: {detail[:800]}",
                    status="accepted",
                    confidence=0.6,
                    evidence=[message.id, message.correlation_id or ""],
                    decided_by=DIRECTOR,
                )
            )
        with contextlib.suppress(Exception):
            self.messaging.store.set_state(
                message.id,
                message.state,
                status_detail=f"directive:{action}:{detail[:200]}",
            )

    async def _process_director_inbox(self) -> int:
        """Consume the Director inbox once per cycle (§76, §94).

        Structured reports become the management actions this module already
        owns — continue, reassign, create task, replan, escalate — never raw
        state mutation by the sending agent (§28, §78). Every interpretation
        is traced back to the originating message.
        """
        pending = self.director_inbox.pending()
        if not pending:
            return 0
        handled = 0
        for message in pending:
            # Step through DELIVERED so ACK/PROCESSING/COMPLETED are legal.
            with contextlib.suppress(Exception):
                if message.state == DeliveryState.QUEUED:
                    message.set_state(DeliveryState.DELIVERED)
                    self.messaging.store.save(message)
                    message.set_state(DeliveryState.RECEIVED)
                    self.messaging.store.save(message)
            try:
                await self._interpret_director_message(message)
            except Exception as exc:
                self.emit("message.interpret_error", message_id=message.id, error=str(exc)[:200])
                with contextlib.suppress(Exception):
                    self.messaging.fail_processing(message, str(exc))
                continue
            handled += 1
        return handled

    async def _interpret_director_message(self, message: Message) -> str:
        """One Director decision per structured report (§41–§43, §76)."""
        payload = message.payload
        summary = str(payload.get("summary") or payload.get("discovery") or payload.get("reason") or "")

        if message.type == MsgType.TASK_FAILED:
            self.messaging.acknowledge(message, "director: failure reviewed")
            self.messaging.start_processing(message)
            # The deterministic loop already retries via _fail_task; here the
            # Director records the structured report and, on repeated failure,
            # recommends escalation rather than another blind retry (§21).
            if payload.get("attempt", 1) >= self.config.budget.max_task_attempts:
                self.messaging.complete(message, "attempt budget exhausted; escalated to a human")
                self._escalate(
                    message.task_id,
                    f"agent-reported failure exhausted the attempt budget: {summary[:200]}",
                    kind="repeated_failure",
                    evidence=f"message:{message.id}",
                )
                self._record_message_traceability(message, "escalate", summary)
                return "escalate"
            self.messaging.complete(message, "retry scheduled by the failure policy")
            self._record_message_traceability(message, "continue", summary)
            return "continue"

        if message.type == MsgType.DISCOVERY_REPORTED:
            self.messaging.acknowledge(message, "director: discovery accepted")
            self.messaging.start_processing(message)
            discovery = summary
            confidence = float(payload.get("confidence", 0.6) or 0.6)
            # §43: only durable, confident knowledge becomes durable memory.
            with contextlib.suppress(Exception):
                self.workspace.append_discovery(f"DISCOVERY [{message.sender}]: {discovery[:280]}")
            # High-confidence discoveries recommend new work (§41); the task
            # traces back to the originating task, or to the message itself.
            if confidence >= 0.75:
                self.messaging.complete(message, "follow-up task created")
                task = Task(
                    title=f"Follow-up: {discovery[:80]}",
                    description=f"From {message.sender} via message {message.id}: {discovery[:500]}",
                    role=TaskRole.CODE,
                    created_by=message.task_id or message.id,
                )
                self.graph.add_task(task)
                self._persist_graph()
                self.emit(
                    EventTypes.TASK_CREATED,
                    task_id=task.id,
                    title=task.title,
                    source="director:discovery",
                    message_id=message.id,
                )
                self._record_message_traceability(message, "create_task", task.id)
                return "create_task"
            self.messaging.complete(message, "recorded as discovery; no graph change")
            self._record_message_traceability(message, "record", discovery[:200])
            return "record"

        if message.type == MsgType.REPLAN_REQUEST:
            self.messaging.acknowledge(message, "director: replan accepted")
            self.messaging.start_processing(message)
            # §42: request -> Director evaluation -> real task graph update.
            await self._replan(f"agent request: {summary[:200]}")
            self.messaging.complete(message, "replan executed")
            self._record_message_traceability(message, "replan", summary)
            self.director_inbox.reply(
                message,
                msg_type=MsgType.REPLAN_RESULT,
                payload={"accepted": True, "reason": summary[:300]},
            )
            self.emit("director.replan_requested", message_id=message.id, reason=summary[:200])
            return "replan"

        if message.type == MsgType.VERIFICATION_RESULT and not payload.get("passed", True):
            # The verification engine already failed the task deterministically;
            # the Director only confirms the report and keeps the loop moving.
            self.messaging.acknowledge(message, "director: failure confirmed")
            self.messaging.complete(message, "handled by the verification policy")
            self._record_message_traceability(message, "continue", summary)
            return "continue"

        if message.type in (
            MsgType.TASK_COMPLETED,
            MsgType.VERIFICATION_RESULT,
            MsgType.TASK_ACCEPTED,
            MsgType.REVIEW_RESULT,
            MsgType.RESEARCH_RESULT,
            MsgType.ARCHITECTURE_RESULT,
            MsgType.STATUS_RESPONSE,
            MsgType.HELP_RESPONSE,
        ):
            self.messaging.acknowledge(message, "director: acknowledged")
            self.messaging.complete(message, "no action required")
            return "acknowledge"

        # Unknown or unsupported types are dead-lettered, never dropped silently.
        self.messaging.fail_processing(message, f"no director policy for {message.type.value}")
        return "dead_letter"

    def _current_intent(self) -> ProjectIntent:
        data = self.workspace.load_project().get("intent") or {}
        if data:
            try:
                return ProjectIntent.model_validate(data)
            except Exception:
                pass
        return literal_intent(self.ctx.objective or "")

    def _current_requirements(self) -> dict[str, Any]:
        path = self.workspace.paths.requirements / "requirements.json"
        if path.is_file():
            import json

            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _merge_reports(self, reports: list[CycleReport]) -> CycleReport:
        if not reports:
            return CycleReport(action="idle", ok=False, detail="no task ran")
        return CycleReport(
            action="parallel",
            ok=all(r.ok for r in reports),
            task_id=",".join(r.task_id for r in reports),
            detail=" | ".join(f"{r.task_id}:{r.detail}" for r in reports)[:600],
            evidence={"reports": [r.as_dict() for r in reports]},
        )

    # ==================================================================
    # EXECUTE + VERIFY: one task, end to end
    # ==================================================================

    async def _execute_task(self, task: Task) -> CycleReport:
        self._mark_ready(task)
        self._transition(task, TaskState.ASSIGNED, agent="orchestrator", note="scheduled")
        self._transition(
            task, TaskState.IMPLEMENTING, agent="orchestrator", note="implementation started"
        )
        attempt = AttemptRecord(
            attempt_number=task.attempts + 1, agent="coder", started_at=now_iso()
        )
        task.attempts += 1
        self._persist_graph()

        # Agent communication: the delegation is a real protocol exchange —
        # TASK_REQUEST on assignment, TASK_ACCEPTED when work starts (§15, §82).
        self._msg_task_assigned(task)
        self._msg_task_accepted(task)

        # ---- RESEARCH BEFORE CODING (§38): complex tasks investigate first ----
        await self._research_before_coding(task)

        coder = self.coder
        coder.bind_tools(
            self.tools_for(coder.agent_class, approved=task.id in self._approved_tasks)
        )
        context = self.context_builder.build(task=task, role="coder", repo_root=self.repo_root)
        result = await self.runner.run(coder, task, context)
        self._record_budget(result, coder.name)
        attempt.finished_at = now_iso()

        applied = list(result.output.get("applied", []) if result.output else [])
        # Tool-loop writes (write_file/edit_file mid-reasoning) go through the
        # same sandbox; merge them so nothing the agent did escapes bookkeeping.
        applied = sorted(set(applied) | set(coder.tools.written_paths))
        if not result.ok and not applied:
            return await self._fail_task(
                task, attempt, result.error or "implementation produced no changes", agent="coder"
            )
        if applied:
            task.artifacts = sorted(set(task.artifacts) | set(applied))
        rejected = result.output.get("rejected", []) if result.output else []
        if rejected:
            self.emit("task.edits_rejected", task_id=task.id, rejected=rejected[:10])

        # ---- VERIFY ----
        self._transition(
            task, TaskState.VERIFYING, agent="orchestrator", note="verification started"
        )
        await self._ensure_machine_checkable(task)
        report = await self._verify(task, work_root=self.repo_root)
        task.verification = report.as_task_verification()
        attempt.evidence = {"verification": report.as_task_verification()}
        # The tester reports its verdict as a structured message (§26).
        self._msg_verification(task, report.passed, report.summary())

        if (
            not report.passed
            and self._only_manual_unknowns(report)
            and await self._adjudicate_manual(task, report)
        ):
            # Manual criteria are adjudicated by an independent reviewer, per
            # the evidence hierarchy — never auto-passed.
            report.passed = True
            task.verification = report.as_task_verification()
        if not report.passed:
            self._persist_graph()
            return await self._fail_task(
                task,
                attempt,
                "; ".join(report.failures)[:1200] or "the definition of done did not pass",
                agent="verifier",
                report=report,
            )

        # ---- ANALYSE: independent review, weighted below executable evidence ----
        blocked = await self._review_and_security(task, attempt, report)
        if blocked is not None:
            return blocked
        return self._complete_task(task, attempt, result, report)

    async def _review_and_security(
        self,
        task: Task,
        attempt: AttemptRecord,
        report: VerificationReport,
        *,
        work_root: Path | None = None,
    ) -> CycleReport | None:
        """Independent review + red-team pass before completion.

        Shared by inline and worktree execution so a parallel task meets the
        same completion contract as a sequential one. Returns a failed
        CycleReport when a gate blocks the task, None to proceed.
        """
        repo = work_root if work_root is not None else self.repo_root
        if self._needs_review(task, report):
            self._transition(
                task, TaskState.REVIEWING, agent="orchestrator", note="independent review"
            )
            reviewer = self.reviewer
            reviewer.bind_tools(self.tools_for(reviewer.agent_class, work_root=repo))
            review_context = self.context_builder.build(
                task=task,
                role="reviewer",
                repo_root=repo,
                extra_sections={"change_diff": self._diff_section(None if repo is self.repo_root else repo)},
            )
            review_result = await self.runner.run(reviewer, task, review_context)
            self._record_budget(review_result, reviewer.name)
            review_findings = (review_result.output or {}).get("findings", [])
            review_blocking = [
                f for f in review_findings if f.get("severity") in ("critical", "high")
            ]
            self._msg_review_result(task, review_findings, len(review_blocking))
            self.workspace.write_json_artifact(
                f"verification/review_results/{task.id}.json", review_result.output or {}
            )
            findings = (review_result.output or {}).get("findings", [])
            blocking = [f for f in findings if f.get("severity") in ("critical", "high")]
            if (review_result.output or {}).get("blocking") and not blocking:
                blocking = [
                    {"severity": "high", "description": "reviewer marked the change blocking"}
                ]
            if blocking:
                return await self._fail_task(
                    task,
                    attempt,
                    f"review blocked by {len(blocking)} high-severity finding(s)",
                    agent="reviewer",
                    report=report,
                )
            if not review_result.ok:
                # A review without blocking findings is recorded evidence, not
                # a verdict: executable evidence decides completion.
                self.emit(
                    "review.non_blocking",
                    task_id=task.id,
                    findings=len(findings),
                    error=review_result.error[:200],
                )

        # ---- SECURITY: high-risk changes get a red-team pass before completion ----
        if task.risk == "high":
            security_result = await self._run_security(task, work_root=work_root)
            if security_result is not None and not security_result.ok:
                return await self._fail_task(
                    task,
                    attempt,
                    "security review found blocking findings; see the security report",
                    agent="security",
                    report=report,
                )
        return None

    def _needs_review(self, task: Task, report: VerificationReport) -> bool:
        """Deterministic review policy: risk and unverified criteria decide."""
        if task.risk == "high":
            return True
        if task.estimated_complexity >= 7:
            return True
        return any(c.status == CheckStatus.UNKNOWN for c in report.checks)

    # ---- verification support agents ----

    async def _research_before_coding(self, task: Task) -> None:
        """Research-before-coding policy (plan.md §38).

        Complex tasks get one investigation pass before implementation; the
        findings are persisted so later attempts and other agents reuse them
        instead of re-deriving (or skipping) the research.
        """
        if not self.config.research_before_coding:
            return
        if task.estimated_complexity < self.config.research_complexity_threshold:
            return
        research_path = self.workspace.paths.state / "research" / f"{task.id}.md"
        if research_path.is_file():
            return  # already researched; never pay for the same answer twice
        researcher = self.agents["researcher"]
        researcher.bind_tools(self.tools_for(researcher.agent_class))
        probe = Task(title=task.title, description=task.description, role="research")
        context = self.context_builder.build(task=task, role="researcher", repo_root=self.repo_root)
        result = await self.runner.run(researcher, probe, context)
        self._record_budget(result, researcher.name)
        answer = str((result.output or {}).get("answer", "")) if result.ok else ""
        sources = list((result.output or {}).get("sources", []) or []) if result.ok else []
        body = (
            f"# Research: {task.title}\n\n"
            f"task: {task.id} | complexity: {task.estimated_complexity}\n\n"
            f"{answer or '_no research findings recorded_'}\n\n"
            + "\n".join(f"- source: {s}" for s in sources)
        )
        self.workspace.write_artifact(f"research/{task.id}.md", body)
        if answer:
            self.workspace.append_discovery(f"RESEARCH [{task.id}]: {answer[:300]}")
            # Research findings travel as a structured message, not just text (§22).
            self._msg_research_result(task, answer, sources)
        self.emit(
            "research.completed",
            task_id=task.id,
            resolved=bool(result.ok and (result.output or {}).get("resolved")),
            chars=len(answer),
        )

    async def _ensure_machine_checkable(self, task: Task, *, work_root: Path | None = None) -> None:
        """A task with no executable criteria gets them generated (plan.md §56).

        The tester writes the smallest check that makes the task's acceptance
        criteria executable; the orchestrator adopts the commands it used so
        the Definition-of-Done engine — not the agent — owns the verdict.
        """
        if task.verification_commands or task.definition_of_done or task.acceptance_criteria:
            return
        root = work_root or self.repo_root
        tester = self.agents["tester"]
        tester.bind_tools(self.tools_for(tester.agent_class, work_root=root))
        context = self.context_builder.build(task=task, role="tester", repo_root=root)
        result = await self.runner.run(tester, task, context)
        self._record_budget(result, tester.name)
        if not result.ok:
            self.emit("verification.generation_failed", task_id=task.id, error=result.error[:200])
            return
        generated = list((result.output or {}).get("generated_tests", []))
        if generated:
            task.artifacts = sorted(set(task.artifacts) | set(generated))
        for command in self.agents["tester"].commands_from_report(result.output or {}):
            if command not in task.verification_commands:
                task.verification_commands.append(command)
        self.emit("verification.criteria_generated", task_id=task.id, tests=generated)

    def _only_manual_unknowns(self, report: VerificationReport) -> bool:
        """True when verification failed *only* because of manual criteria."""
        if report.failures:
            return False
        unresolved = [c for c in report.checks if c.status != CheckStatus.PASS]
        return bool(unresolved) and all(c.status == CheckStatus.UNKNOWN for c in unresolved)

    async def _adjudicate_manual(
        self, task: Task, report: VerificationReport, *, work_root: Path | None = None
    ) -> bool:
        """Reviewer verdict on manual criteria; UNKNOWN is never auto-passed."""
        reviewer = self.reviewer
        reviewer.bind_tools(self.tools_for(reviewer.agent_class, work_root=work_root))
        manual = [c.criterion for c in report.checks if c.status == CheckStatus.UNKNOWN]
        context = self.context_builder.build(
            task=task,
            role="reviewer",
            repo_root=work_root or self.repo_root,
            extra_sections={
                "manual_criteria": (
                    "# MANUAL CRITERIA TO ADJUDICATE\n"
                    "Inspect the repository and decide whether each criterion holds.\n"
                    + "\n".join(f"- {c}" for c in manual)
                )
            },
        )
        result = await self.runner.run(reviewer, task, context)
        self._record_budget(result, reviewer.name)
        self.workspace.write_json_artifact(
            f"verification/review_results/{task.id}-manual.json", result.output or {}
        )
        if result.ok and (result.output or {}).get("approved"):
            for check in report.checks:
                if check.status == CheckStatus.UNKNOWN:
                    check.status = CheckStatus.PASS
                    check.detail = "approved by independent review (manual criterion)"
            self.emit("verification.manual_approved", task_id=task.id, criteria=len(manual))
            return True
        for check in report.checks:
            if check.status == CheckStatus.UNKNOWN:
                check.status = CheckStatus.FAIL
                check.detail = "reviewer could not approve this manual criterion"
            if check.status == CheckStatus.FAIL and not any(
                check.criterion in f for f in report.failures
            ):
                report.failures.append(f"{check.criterion} (not approved by review)")
        return False

    async def _run_security(self, task: Task, *, work_root: Path | None = None) -> AgentResult | None:
        """Red-team pass for high-risk work; static evidence plus model review."""
        repo = work_root if work_root is not None else self.repo_root
        security = self.agents["security"]
        security.bind_tools(self.tools_for(security.agent_class, work_root=repo))
        context = self.context_builder.build(task=task, role="security", repo_root=repo)
        result = await self.runner.run(security, task, context)
        self._record_budget(result, security.name)
        self.workspace.write_json_artifact(
            f"verification/review_results/{task.id}-security.json", result.output or {}
        )
        self.emit(
            "security.scan",
            task_id=task.id,
            ok=result.ok,
            findings=len((result.output or {}).get("findings", [])),
        )
        return result

    # ---- QA gate and release (plan.md §5K, §5L, §30) ----

    async def _convene_board(self, reason: str, task_ids: list[str]) -> Any:
        """Convene the Architecture Review Board at most once per question."""
        key = ",".join(sorted(task_ids))
        if key in self._board_convened_for:
            return None
        self._board_convened_for.add(key)
        board = ReviewBoard(self)
        try:
            return await board.convene(reason, task_ids, self.graph)
        except Exception as exc:
            self.emit("review_board.failed", detail=str(exc)[:300])
            return None

    async def _run_qa_gate(self) -> bool:
        """Validate the finished graph against the original intent.

        Returns True when the gate found unmet requirements and reopened the
        graph with catch-up tasks; False when the objective is satisfied (or
        the QA verdict is not actionable).
        """
        self._qa_rounds += 1
        qa = self.agents["qa"]
        qa.bind_tools(self.tools_for(qa.agent_class))
        intent = self._current_intent()
        context = self.context_builder.build(
            task=None,
            role="qa",
            repo_root=self.repo_root,
            extra_sections={"intent": f"# ORIGINAL INTENT\n\n{intent.to_markdown()}"},
        )
        result = await self.runner.run(qa, None, context)
        self._record_budget(result, qa.name)
        if result.ok and result.output:
            gaps = [str(g) for g in result.output.get("gaps", []) or []]
            aligned = bool(result.output.get("aligned", True)) and not gaps
            summary = str(result.output.get("summary", ""))
        else:
            # No QA verdict available: fall back to the deterministic
            # coverage check so the gate never silently passes.
            check = qa.deterministic_check(intent, self.graph)
            gaps = check.gaps
            aligned = check.aligned
            summary = check.summary
        self.emit("qa.gate", round=self._qa_rounds, aligned=aligned, gaps=gaps[:10])
        self.workspace.write_json_artifact(
            "verification/qa_gate.json", {"aligned": aligned, "gaps": gaps, "summary": summary}
        )
        if aligned and not gaps:
            return False
        new_tasks = [gap_task(gap, self.graph) for gap in gaps[:5]]
        if not new_tasks:
            return False
        self.planner.replan(
            self.graph, add_tasks=new_tasks, reason=f"QA gate: unmet requirements ({summary[:120]})"
        )
        self._persist_graph()
        self.emit(EventTypes.TASK_CREATED, count=len(new_tasks), source="qa-gate")
        return True

    async def _release(self) -> None:
        """Release step on a successful completion: notes, tag, checkpoint."""
        release = self.agents["release"]
        release.bind_tools(self.tools_for(release.agent_class))
        context = self.context_builder.build(task=None, role="release", repo_root=self.repo_root)
        result = await self.runner.run(release, None, context)
        self._record_budget(result, release.name)
        report = result.output or {}
        notes = str(report.get("notes", "") or "")
        version = str(report.get("version", "") or "")
        if not notes or notes.startswith("echo:"):
            # The model contributed nothing substantive; derive the release
            # from the recorded evidence instead of publishing a stub.
            offline = release.deterministic_report(self.graph, self.ctx.objective)
            report = offline.model_dump(mode="json")
            notes, version = offline.notes, offline.version
        from ..core.workspace import atomic_write

        atomic_write(
            self.repo_root / "CHANGELOG.md", notes if notes.endswith("\n") else notes + "\n"
        )
        self.workspace.write_json_artifact("release/release.json", report)
        self.emit("release.prepared", version=version)
        # Accepted architecture decisions join project memory (kind=decision).
        with contextlib.suppress(Exception):
            store = memory_store(self.workspace)
            for decision in self.store.list_decisions()[-10:]:
                if decision.status == "accepted" and decision.body.strip():
                    store.add(
                        "decision",
                        f"{decision.title}: {decision.body}",
                        source=decision.decided_by or "architecture",
                        confidence=decision.confidence,
                        tags=["architecture"],
                    )
        # §60: a released project's verified lessons become global knowledge.
        lessons = record_project_lessons(
            self.workspace, self.ctx.project.name or self.store.project_id
        )
        decisions_lessons = record_decision_lessons(
            self.store.list_decisions(), self.ctx.project.name or self.store.project_id
        )
        if lessons or decisions_lessons:
            self.emit("lessons.recorded", failures=lessons, decisions=decisions_lessons)
        if self.config.git_checkpoints:
            try:
                self.git.stage_all()
                commit = self.git.commit(f"release: {version or 'release candidate'}")
                tag = f"release-{version or now_iso()[:10]}"
                self.git.tag(tag, f"release {version or 'candidate'}")
                self.emit(EventTypes.COMMIT_CREATED, commit=commit[:12], tag=tag)
            except GitError as exc:
                self.emit("release.git_failed", detail=str(exc))

    # ---- unknowns queue ----

    def _queue_unknown(self, question: str, *, raised_by: str) -> str:
        unknown_id = new_id("UNK")
        self.store.save_unknown(
            UnknownRecord(
                id=unknown_id,
                project_id=self.store.project_id,
                question=question,
                raised_by=raised_by,
            )
        )
        self.emit(EventTypes.UNKNOWN_QUEUED, question=question, unknown_id=unknown_id)
        return unknown_id

    async def _research_unknown(self, question: str, unknown_id: str) -> None:
        """Route one unknown to the research agent; unresolved stays open."""
        researcher = self.agents["researcher"]
        researcher.bind_tools(self.tools_for(researcher.agent_class))
        probe = Task(title=question, role="research")
        context = self.context_builder.build(task=None, role="researcher", repo_root=self.repo_root)
        result = await self.runner.run(researcher, probe, context)
        self._record_budget(result, researcher.name)
        if result.ok and result.output:
            answer = str(result.output.get("answer", ""))[:2000]
            resolved = bool(result.output.get("resolved", False))
            self.store.save_unknown(
                UnknownRecord(
                    id=unknown_id,
                    project_id=self.store.project_id,
                    question=question,
                    status="resolved" if resolved else "open",
                    answer=answer,
                    raised_by="researcher",
                    resolved_at=now_iso() if resolved else None,
                )
            )
            self.workspace.append_discovery(f"RESEARCH [{question[:120]}]: {answer[:400]}")
            self.emit("unknown.researched", unknown_id=unknown_id, resolved=resolved)
        else:
            self.emit("unknown.research_failed", unknown_id=unknown_id, error=result.error[:200])

    async def _verify(self, task: Task, *, work_root: Path) -> VerificationReport:
        risky = high_risk_commands(list(task.verification_commands))
        if risky:
            # Observability regardless of mode: the human (and the log) sees
            # exactly which high-risk operations verification will attempt.
            self.emit(
                "security.high_risk_command",
                task_id=task.id,
                commands=[c for c, _ in risky],
                reasons=[r for _, r in risky],
                approved=task.id in self._approved_tasks,
            )
        engine = VerificationEngine(
            self.tools_for("tester", work_root=work_root, approved=task.id in self._approved_tasks)
        )
        report = await asyncio.to_thread(engine.verify_task, task)
        for check in report.checks:
            if check.status == CheckStatus.UNKNOWN:
                self.emit(
                    "verification.unknown",
                    task_id=task.id,
                    criterion=check.criterion,
                    detail="a manual criterion is never counted as a pass",
                )
        if report.passed:
            self.emit(
                EventTypes.VERIFICATION_PASSED,
                task_id=task.id,
                summary=report.summary(),
                commands=[e.command for e in report.evidence],
            )
        else:
            self.emit(
                EventTypes.VERIFICATION_FAILED,
                task_id=task.id,
                summary=report.summary(),
                failures=report.failures[:5],
            )
        return report

    def _diff_section(self, repo_root: Path | None = None) -> str:
        try:
            # None = the main repository; a worktree path diffs that worktree
            # (the changes live there until the merge).
            diff = (
                self.git.diff("HEAD")
                if repo_root is None
                else GitManager(repo_root).diff("HEAD")
            )
        except GitError:
            return ""
        return f"# CHANGE DIFF\n```diff\n{diff[:20000]}\n```" if diff else ""

    def _complete_task(
        self, task: Task, attempt: AttemptRecord, result: AgentResult, report: VerificationReport
    ) -> CycleReport:
        attempt.outcome = "success"
        attempt.finished_at = now_iso()
        task.attempts_history.append(attempt)
        self._transition(
            task, TaskState.COMPLETED, agent="orchestrator", note="executable evidence passed"
        )
        task.verification = report.as_task_verification()
        self._persist_graph()
        self.emit(EventTypes.TASK_COMPLETED, task_id=task.id, summary=report.summary())
        self._msg_task_completed(task, report.summary())
        self._settle_delegation(task, f"completed: {report.summary()[:200]}")
        self.locks.release(task.id)
        self._memory_episode(
            task,
            outcome="completed",
            summary=report.summary(),
            lesson="",
        )
        self._checkpoint(task, "completed")
        milestone_report = self.refresh_roadmap()
        for milestone in milestone_report["newly_completed"]:
            self.emit(
                "milestone.completed",
                milestone=milestone["id"],
                name=milestone["name"],
                tasks=milestone["tasks_completed"],
            )
            self._msg_milestone(milestone)
            evaluation = milestone_self_evaluation(self.workspace, milestone, self.graph)
            self.store.save_decision(
                DecisionRecord(
                    id=new_id("DEC"),
                    project_id=self.store.project_id,
                    title=f"Milestone {milestone['id']} complete: {milestone['name']}",
                    body=str(evaluation.get("assessments", {}))[:1000],
                    status="accepted",
                    confidence=0.6,
                    evidence=[f"{milestone['tasks_completed']} tasks verified"],
                    decided_by="orchestrator",
                )
            )
        return CycleReport(
            task_id=task.id,
            agent="coder",
            action="completed",
            ok=True,
            detail=report.summary(),
            evidence={"verification": report.as_task_verification(), "artifacts": result.artifacts},
        )

    def _memory_episode(
        self, task: Task, *, outcome: str, summary: str, lesson: str = ""
    ) -> None:
        """Record what actually happened, for the next session's recall."""
        with contextlib.suppress(Exception):
            memory_store(self.workspace).record_episode(
                {
                    "kind": "task",
                    "task_id": task.id,
                    "title": task.title[:120],
                    "outcome": outcome,
                    "summary": summary[:300],
                    "lesson": lesson[:300],
                    "attempts": task.attempts,
                    "commit": (task.verification.get("commands") or [{}])[0].get("command", "")
                    if outcome == "completed"
                    else "",
                }
            )

    def _commit_memory_snapshot(self, stop: StopSignal) -> None:
        """Leave the tree clean at run end: commit the memory this run wrote."""
        if not self.config.git_checkpoints:
            return
        try:
            commit = self.git.create_checkpoint_commit(
                "memory", f"memory: run digest ({stop.reason.value})"
            )
            if commit:
                self.emit("git.memory_commit", commit=commit[:12])
        except GitError as exc:
            self.emit("git.memory_commit_failed", detail=str(exc)[:200])

    def _memory_run_digest(self, stop: StopSignal) -> None:
        with contextlib.suppress(Exception):
            store = memory_store(self.workspace)
            progress = self.graph.progress()
            store.record_episode(
                {
                    "kind": "run",
                    "run_id": self.run.id,
                    "stop_reason": stop.reason.value,
                    "cycles": self._cycle_index,
                    "completed": progress["completed"],
                    "failed": progress["failed"],
                    "cost_usd": round(self.budget.cost_usd, 4),
                }
            )
            store.consolidate()

    def refresh_roadmap(self) -> dict[str, Any]:
        """Rebuild the milestone roadmap from the graph (plan.md §31)."""
        report = refresh_roadmap(self.workspace, self.graph)
        return report

    # ==================================================================
    # failure path: FAILED -> DIAGNOSING -> REPAIRING -> READY
    # ==================================================================

    async def _fail_task(
        self,
        task: Task,
        attempt: AttemptRecord,
        reason: str,
        *,
        agent: str,
        report: VerificationReport | None = None,
    ) -> CycleReport:
        attempt.outcome = "failed"
        attempt.failure_summary = reason[:2000]
        task.attempts_history.append(attempt)
        self._transition(task, TaskState.FAILED, agent=agent, note=reason[:200])
        self._persist_graph()
        self.emit(
            EventTypes.TASK_FAILED, task_id=task.id, reason=reason[:400], attempts=task.attempts
        )
        failure_type = "TEST" if agent == "verifier" else ("SECURITY" if agent == "security" else "CODE")
        self._msg_task_failed(task, failure_type, reason, agent)
        self._settle_delegation(task, f"failed: {reason[:200]}")
        self._memory_episode(
            task,
            outcome="failed",
            summary=reason[:300],
            lesson=(task.attempts_history[-1].lesson if task.attempts_history else ""),
        )
        self.locks.release(task.id)

        if task.attempts >= self.config.budget.max_task_attempts:
            self._transition(
                task, TaskState.ARCHITECTURE_REVIEW, agent="orchestrator", note="attempts exhausted"
            )
            # §39: convene the review board before escalating, so the human
            # sees a recorded recommendation rather than a bare failure.
            board_decision = await self._convene_board(
                f"task {task.id} failed {task.attempts} times", [task.id]
            )
            if board_decision is not None and board_decision.verdict == "revise_plan":
                self._transition(
                    task,
                    TaskState.REPLAN,
                    agent="review-board",
                    note="board revised the plan",
                )
                self._transition(task, TaskState.QUEUED, agent="review-board", note="replanned")
                await self._replan(f"review board: {board_decision.rationale[:200]}")
                self._persist_graph()
                return CycleReport(
                    task_id=task.id,
                    agent="review-board",
                    action="board_replanned",
                    ok=False,
                    detail=f"board revised the plan: {board_decision.rationale[:200]}",
                )
            self._escalate(
                task.id,
                f"task failed {task.attempts} times; the plan or architecture is probably wrong"
                + (
                    f" (board: {board_decision.verdict}: {board_decision.rationale[:150]})"
                    if board_decision is not None
                    else ""
                ),
                kind="repeated_failure",
                evidence=reason[:1000],
            )
            self._persist_graph()
            return CycleReport(
                task_id=task.id,
                agent=agent,
                action="escalated",
                ok=False,
                detail="attempts exhausted; escalated to a human",
            )

        diagnosis = await self._diagnose(task)
        self._transition(
            task, TaskState.DIAGNOSING, agent="orchestrator", note="diagnosing the failure"
        )
        if diagnosis is not None:
            # The debugger reports its root cause as a DISCOVERY_REPORTED (§22).
            self._msg_diagnosis(task, diagnosis)
        if diagnosis is None:
            self._transition(
                task, TaskState.READY, agent="orchestrator", note="retry after transient failure"
            )
            self._persist_graph()
            return CycleReport(
                task_id=task.id,
                agent=agent,
                action="failed",
                ok=False,
                detail=reason[:400] + " (no diagnosis available; requeued once)",
            )
        self._transition(task, TaskState.REPAIRING, agent="orchestrator", note="diagnosis applied")
        self._apply_diagnosis(task, diagnosis)
        self._transition(task, TaskState.READY, agent="orchestrator", note="repair scheduled")
        self._persist_graph()
        return CycleReport(
            task_id=task.id,
            agent="debugger",
            action="diagnosed",
            ok=False,
            detail=f"{reason[:240]} | root cause: {diagnosis.get('root_cause', 'unknown')[:200]}",
            evidence={"diagnosis": diagnosis},
        )

    async def _diagnose(self, task: Task) -> dict[str, Any] | None:
        debugger = self.debugger
        debugger.bind_tools(self.tools_for(debugger.agent_class))
        context = self.context_builder.build(task=task, role="debugger", repo_root=self.repo_root)
        result = await self.runner.run(debugger, task, context)
        self._record_budget(result, debugger.name)
        if not result.ok or not result.output:
            return None
        diagnosis = result.output
        self.workspace.agent_log("debugger", f"{task.id}.json", diagnosis)
        if diagnosis.get("architecture_issue"):
            self._escalate(
                task.id,
                f"diagnosis reports an architecture problem: {diagnosis.get('root_cause', '')}",
                kind="architecture",
                evidence=str(diagnosis.get("candidate_fixes", []))[:800],
            )
        return diagnosis

    def _apply_diagnosis(self, task: Task, diagnosis: dict[str, Any]) -> None:
        """Turn a diagnosis into concrete, deterministic task updates."""
        files = [str(f) for f in diagnosis.get("files_affected") or [] if str(f).strip()]
        for path in files:
            # An edit outside the coder's write policy would be silently dropped
            # later; surface it now instead.
            probe = self.tools_for("coder")
            try:
                resolved = (self.repo_root / path).resolve()
                resolved.relative_to(self.repo_root)
            except ValueError:
                self.emit("diagnosis.path_rejected", task_id=task.id, path=path)
                continue
            if not probe._matches_write_policy(resolved):
                self.emit(
                    "diagnosis.path_outside_policy",
                    task_id=task.id,
                    path=path,
                    detail="the coder may not write there; the plan needs a permission change",
                )
                continue
            if path not in task.artifacts:
                task.artifacts.append(path)

        required = [str(t) for t in diagnosis.get("tests_required") or [] if str(t).strip()]
        for command in required[:5]:
            if command not in task.verification_commands:
                task.verification_commands.append(command)

        lesson = str(diagnosis.get("lesson", "")).strip()
        if lesson and task.attempts_history:
            task.attempts_history[-1].lesson = lesson

    # ==================================================================
    # parallel execution with worktrees
    # ==================================================================

    async def _run_parallel(self, task_ids: list[str]) -> list[CycleReport]:
        """Run independent tasks concurrently, each isolated in a worktree.

        Tasks whose lock keys collide are split into separate waves, because a
        shared surface cannot be written twice at the same time.
        """
        if not self.config.worktree_parallelism:
            reports: list[CycleReport] = []
            for task_id in task_ids:
                reports.append(await self._execute_task(self.graph.get(task_id)))
            return reports

        waves: list[list[str]] = [[]]
        used_keys: set[str] = set()
        for task_id in task_ids:
            task = self.graph.get(task_id)
            keys = resource_keys(list(task.artifacts))
            if waves[-1] and (used_keys.intersection(keys) or len(waves[-1]) >= 2):
                waves.append([task_id])
                used_keys = set(keys)
            else:
                waves[-1].append(task_id)
                used_keys.update(keys)

        reports: list[CycleReport] = []
        for wave in waves:
            if not wave:
                continue
            if len(wave) == 1:
                reports.append(await self._execute_task(self.graph.get(wave[0])))
                continue
            outcomes = await asyncio.gather(
                *(self._execute_in_worktree(self.graph.get(tid)) for tid in wave),
                return_exceptions=True,
            )
            for tid, outcome in zip(wave, outcomes, strict=True):
                if isinstance(outcome, CycleReport):
                    reports.append(outcome)
                else:
                    # The gather exception skipped every cleanup path; release
                    # the task's locks or later waves lock_wait forever.
                    self.locks.release(tid)
                    task = self.graph.tasks.get(tid)
                    if task is not None:
                        task.locked_paths = []
                    reports.append(
                        CycleReport(
                            task_id=tid,
                            action="failed",
                            ok=False,
                            detail=f"parallel execution error: {outcome}",
                        )
                    )
        return reports

    async def _execute_in_worktree(self, task: Task) -> CycleReport:
        """Execute in an isolated worktree, then merge only if it verifies."""
        worktree = self.git.create_worktree(task.id)
        if worktree is None:
            self.emit("worktree.unavailable", task_id=task.id, detail="running inline instead")
            return await self._execute_task(task)
        task.worktree = str(worktree)
        try:
            report = await self._execute_isolated(task, worktree)
            if report.ok:
                try:
                    commit = self.git.merge_validated_worktree(
                        task.id, f"merge task {task.id}: {task.title}"
                    )
                    self.emit(EventTypes.COMMIT_CREATED, task_id=task.id, commit=commit[:12])
                    self._checkpoint(task, "completed")
                except GitError as exc:
                    report.ok = False
                    report.action = "merge_conflict"
                    report.detail = f"worktree merge failed: {exc}"
                    self.emit("worktree.merge_failed", task_id=task.id, detail=str(exc))
            return report
        finally:
            self.git.remove_worktree(task.id)
            task.worktree = ""

    async def _execute_isolated(self, task: Task, work_root: Path) -> CycleReport:
        self._mark_ready(task)
        self._transition(
            task, TaskState.ASSIGNED, agent="orchestrator", note="scheduled in a worktree"
        )
        self._transition(
            task, TaskState.IMPLEMENTING, agent="orchestrator", note="implementation started"
        )
        attempt = AttemptRecord(
            attempt_number=task.attempts + 1, agent="coder", started_at=now_iso()
        )
        task.attempts += 1
        self._persist_graph()

        coder = self.coder
        coder.bind_tools(
            self.tools_for(
                coder.agent_class,
                work_root=work_root,
                approved=task.id in self._approved_tasks,
            )
        )
        context = self.context_builder.build(task=task, role="coder", repo_root=work_root)
        result = await self.runner.run(coder, task, context)
        self._record_budget(result, coder.name)
        attempt.finished_at = now_iso()

        applied = list(result.output.get("applied", []) if result.output else [])
        applied = sorted(set(applied) | set(coder.tools.written_paths))
        if not result.ok and not applied:
            return await self._fail_task(
                task, attempt, result.error or "no changes produced", agent="coder"
            )
        if applied:
            task.artifacts = sorted(set(task.artifacts) | set(applied))

        self._transition(
            task, TaskState.VERIFYING, agent="orchestrator", note="verification started"
        )
        await self._ensure_machine_checkable(task, work_root=work_root)
        report = await self._verify(task, work_root=work_root)
        task.verification = report.as_task_verification()
        if (
            not report.passed
            and self._only_manual_unknowns(report)
            and await self._adjudicate_manual(task, report, work_root=work_root)
        ):
            report.passed = True
            task.verification = report.as_task_verification()
        if not report.passed:
            return await self._fail_task(
                task,
                attempt,
                "; ".join(report.failures)[:1200] or "verification failed",
                agent="verifier",
                report=report,
            )

        # Same completion contract as inline execution: independent review and
        # the red-team pass run here too, not only on the sequential path.
        blocked = await self._review_and_security(task, attempt, report, work_root=work_root)
        if blocked is not None:
            return blocked

        attempt.outcome = "success"
        task.attempts_history.append(attempt)
        self._transition(
            task, TaskState.COMPLETED, agent="orchestrator", note="executable evidence passed"
        )
        self._persist_graph()
        self.locks.release(task.id)
        return CycleReport(
            task_id=task.id,
            agent="coder",
            action="completed",
            ok=True,
            detail=report.summary(),
            evidence={"worktree": str(work_root)},
        )

    # ==================================================================
    # escalation, checkpoints, and the public inspection surface
    # ==================================================================

    def _escalate(
        self, task_id: str, reason: str, *, kind: str, evidence: str = ""
    ) -> dict[str, Any]:
        escalation = {
            "id": new_id("ESC"),
            "task_id": task_id,
            "kind": kind,
            "reason": reason,
            "evidence": evidence,
            "status": "pending",
            "created_at": now_iso(),
        }
        self.workspace.add_escalation(escalation)
        self.emit(EventTypes.ESCALATION_RAISED, task_id=task_id, kind=kind, reason=reason[:300])
        return escalation

    def _checkpoint(self, task: Task, label: str) -> None:
        """Commit verified work and record a restorable checkpoint."""
        commit = ""
        if self.config.git_checkpoints:
            try:
                commit = self.git.create_checkpoint_commit(
                    task.id, f"checkpoint({label}): {task.title} [{task.id}]"
                )
                if commit:
                    self.emit(EventTypes.COMMIT_CREATED, task_id=task.id, commit=commit[:12])
            except GitError as exc:
                self.emit("git.checkpoint_failed", task_id=task.id, detail=str(exc))

        self._checkpoint_counter = (
            max(
                self._checkpoint_counter,
                len(self.workspace.list_checkpoints()),
            )
            + 1
        )
        checkpoint_id = f"checkpoint-{self._checkpoint_counter:04d}"
        record = CheckpointRecord(
            id=checkpoint_id,
            project_id=self.store.project_id,
            run_id=self.run.id,
            git_commit=commit,
            objective=self.ctx.objective[:500],
            outstanding_failures=[f.id for f in self.store.list_failures()][-5:],
            environment={"python": self._python_version(), "cwd": str(self.repo_root)},
            task_graph=self.graph.model_dump(mode="json"),
            project_state={"progress": self.graph.progress(), "budget": self.budget.snapshot()},
        )
        try:
            self.store.save_checkpoint(record)
            self.workspace.save_checkpoint(
                checkpoint_id,
                {
                    "id": checkpoint_id,
                    "git_commit": commit,
                    "label": label,
                    "task": task.id,
                    "created_at": record.created_at,
                },
            )
            self.emit(EventTypes.CHECKPOINT_CREATED, checkpoint=checkpoint_id, commit=commit[:12])
        except Exception as exc:
            self.emit("checkpoint.error", checkpoint=checkpoint_id, detail=str(exc))

    @staticmethod
    def _python_version() -> str:
        import sys

        return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

    # ---- inspection ----

    def status(self) -> dict[str, Any]:
        return {
            "run_id": self.run.id,
            "status": self.run.status,
            "cycle": self._cycle_index,
            "progress": self.graph.progress(),
            "budget": self.budget.snapshot(),
            "locks": self.locks.snapshot(),
            "ready": [t.id for t in self.graph.ready_tasks()],
            "active": [
                {"id": t.id, "state": t.status.value, "agent": t.assigned_agent}
                for t in self.graph.active_tasks()
            ],
            "failed": [t.id for t in self.graph.failed_tasks()],
            "escalations": self.workspace.pending_escalations(),
        }

    def inspect_task(self, task_id: str) -> dict[str, Any]:
        task = self.graph.get(task_id)
        return {
            "task": task.model_dump(mode="json"),
            "verification": task.verification,
            "attempts": [a.model_dump(mode="json") for a in task.attempts_history],
        }

    def restore_checkpoint(self, checkpoint_id: str) -> dict[str, Any]:
        """Restore a checkpoint's task graph (git is restored separately)."""
        record = self.store.get_checkpoint(checkpoint_id)
        if record is None:
            raise KeyError(f"unknown checkpoint: {checkpoint_id}")
        self.graph = TaskGraph.model_validate(record.task_graph)
        self.workspace.save_graph(self.graph)
        self.store.save_graph(self.graph)
        self.emit(
            EventTypes.CHECKPOINT_RESTORED, checkpoint=checkpoint_id, commit=record.git_commit[:12]
        )
        return {
            "checkpoint": checkpoint_id,
            "git_commit": record.git_commit,
            "progress": self.graph.progress(),
        }

    # Agents referenced only through `self.agents`; keep the names importable.
    __all__ = ["Orchestrator", "CycleReport", "RunResult"]
