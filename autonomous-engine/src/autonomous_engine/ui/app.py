"""The interactive TUI (Textual): one coherent window into the runtime.

Layout (adapts to terminal size, spec §4/§32):

    ┌──────────────────────────────────────────────────────────┐
    │ HEADER  project · run · status · runtime · budget        │
    ├──────────────────────────────┬───────────────────────────┤
    │ SIDE PANEL (tabbed)          │  LIVE ACTIVITY FEED       │
    │  tasks / agents / models     │  (runtime event stream)   │
    ├──────────────────────────────┴───────────────────────────┤
    │ DETAIL PANEL (selected task / agent / failure …)         │
    ├──────────────────────────────────────────────────────────┤
    │ INPUT BAR (prompt + slash commands + mode toggles)       │
    └──────────────────────────────────────────────────────────┘

Every value shown comes from RuntimeFacade reads; every control goes through
facade actions (control channel / config / memory). No fake state (§66).
"""

from __future__ import annotations

import time
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Input, RichLog, Static

from .facade import RuntimeFacade
from .runner import UIRunner
from .state import UIState

REFRESH_SECONDS = 1.0

STYLE = {
    "success": "green",
    "error": "red",
    "warning": "yellow",
    "decision": "magenta",
    "tool": "cyan",
    "verify": "blue",
    "info": "dim",
}

STATUS_GLYPH = {
    "COMPLETED": "[green]✓[/green]",
    "IMPLEMENTING": "[cyan]▶[/cyan]",
    "VERIFYING": "[blue]◌[/blue]",
    "REVIEWING": "[blue]◌[/blue]",
    "FAILED": "[red]✗[/red]",
    "DIAGNOSING": "[yellow]⚙[/yellow]",
    "REPAIRING": "[yellow]⚙[/yellow]",
    "ARCHITECTURE_REVIEW": "[yellow]⚠[/yellow]",
    "REPLAN": "[magenta]↻[/magenta]",
    "BLOCKED": "[yellow]▮[/yellow]",
    "QUEUED": "[dim]○[/dim]",
    "READY": "[dim]○[/dim]",
    "ASSIGNED": "[cyan]▶[/cyan]",
    "CANCELLED": "[dim]×[/dim]",
}


def _glyph(status: str) -> str:
    return STATUS_GLYPH.get(status, "[dim]?[/dim]")


def _fmt_elapsed(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


class SidePanel(Static):
    """Tabbed tasks / agents / models view (spec §7, §8, §9)."""

    def __init__(self) -> None:
        super().__init__(id="side-panel")
        self.tab = "tasks"

    def render_tasks(self, facade: RuntimeFacade) -> list[str]:
        lines = []
        for task in facade.tasks()[:40]:
            mark = _glyph(task["status"])
            retried = (
                " [yellow]![/yellow]"
                if task["attempts"] > 1 and task["status"] != "COMPLETED"
                else ""
            )
            lines.append(f"{mark} {task['id']} {task['title'][:32]}{retried}")
        if not lines:
            lines.append("[dim]no tasks yet — submit an objective below[/dim]")
        return lines

    def render_agents(self, facade: RuntimeFacade) -> list[str]:
        agents = facade.active_agents()
        if not agents:
            return ["[dim]no agents active[/dim]"]
        return [
            f"[cyan]▶[/cyan] {a['agent'][:18]:18} {a['task_id']} {a['status'][:12]}\n"
            f"   {a['task_title'][:46]}"
            for a in agents
        ]

    def render_models(self, facade: RuntimeFacade) -> list[str]:
        lines = []
        for model in facade.models():
            health = model["health"]
            health_style = (
                "green" if health == "healthy" else "yellow" if health != "unavailable" else "red"
            )
            lines.append(
                f"{model['provider']}/{model['model'][:22]} [{health_style}]{health}[/]\n"
                f"   role: {model['role']}  calls: {model['calls']}  "
                f"tok: {model['tokens_in']}/{model['tokens_out']}  ${model['cost_usd']:.4f}"
            )
        if not lines:
            lines.append("[dim]no model routes configured[/dim]")
        return lines

    def render_messages(self, facade: RuntimeFacade) -> list[str]:
        """AGENT COMMUNICATION: real persisted messages (comms spec §57)."""
        messages = facade.messages(limit=30)
        if not messages:
            return ["[dim]no agent messages yet[/dim]"]
        lines: list[str] = []
        for m in messages:
            state = m["state"]
            state_style = (
                "green"
                if state == "COMPLETED"
                else "yellow"
                if state in ("QUEUED", "DELIVERED", "RECEIVED", "PROCESSING")
                else "red"
            )
            priority = " [red]!][/red]" if m["priority"] in ("high", "critical") else ""
            task = f" [magenta]{m['task_id']}[/magenta]" if m["task_id"] else ""
            sender = DetailPanel._safe(m["sender"], 18)
            recipient = DetailPanel._safe(m["recipient"], 18)
            lines.append(
                f"{sender} → {recipient}{task}\n"
                f"   [bold]{m['type']}[/bold] [dim]{state_style}]{state}[/]{priority}"
            )
            if m["summary"]:
                lines.append(f"   {DetailPanel._safe(m['summary'], 64)}")
        return lines

    def update_panel(self, state: UIState) -> None:
        facade = state.facade
        if self.tab == "tasks":
            title, body = "TASKS  [1]", self.render_tasks(facade)
        elif self.tab == "agents":
            title, body = "ACTIVE AGENTS  [2]", self.render_agents(facade)
        elif self.tab == "models":
            title, body = "MODELS  [3]", self.render_models(facade)
        else:
            title, body = "AGENT COMMUNICATION  [4]", self.render_messages(facade)
        text = [f"[bold reverse] {title} [/bold reverse]", ""]
        text.extend(body)
        self.update("\n".join(text))


class DetailPanel(Static):
    """Detail area: task detail, failures, verification, memory, help (§10)."""

    def set_text(self, text: str) -> None:
        self.update(text)

    def render_overview(self, state: UIState) -> None:
        progress = state.progress()
        health = state.facade.verification_health()
        lines = [
            "[bold reverse] OVERVIEW [/bold reverse]",
            "",
            f"objective: {state.facade.objective()[:92] or '(none)'}",
            f"tasks: {progress['completed']}/{progress['total']} complete · "
            f"{progress['active']} active · {progress['failed']} failed · "
            f"{progress['pending']} pending",
            f"verification: [green]{health['passed']}[/green] passed · "
            f"[red]{health['failed']}[/red] failed · {health['unknown']} unknown",
        ]
        escalations = state.pending_escalations()
        if escalations:
            reason = str(escalations[0].get("reason", ""))[:90]
            lines.append(
                f"[yellow bold]⚠ APPROVAL REQUIRED[/yellow bold] {reason}\n"
                "[yellow]press A to approve · R to reject[/yellow]"
            )
        if state.stop_reason():
            lines.append(f"[magenta]last stop: {state.stop_reason()}[/magenta]")
        lines += [
            "",
            "[dim]? help · 1/2/3 tasks/agents/models · f failures · v verification "
            "· y memory · c checkpoints · / filter · type a goal or /command below[/dim]",
        ]
        self.set_text("\n".join(lines))

    def render_task(self, facade: RuntimeFacade, task_id: str) -> None:
        detail = facade.task_detail(task_id)
        if detail is None:
            self.set_text(f"[red]unknown task: {task_id}[/red]")
            return
        lines = [
            f"[bold reverse] TASK {detail['id']} — {detail['status']} [/bold reverse]",
            f"[bold]{detail['title']}[/bold]",
            "",
        ]
        if detail["description"]:
            lines.append(detail["description"][:400])
        meta = (
            f"priority {detail['priority']} · risk {detail['risk']} · complexity "
            f"{detail['complexity']} · attempts {detail['attempts']}"
        )
        if detail["epic"]:
            meta += f" · epic {detail['epic']}"
        lines += ["", meta]
        if detail["dependencies"]:
            lines.append(f"depends on: {', '.join(detail['dependencies'])}")
        for heading, items in (
            ("acceptance criteria", detail["acceptance_criteria"]),
            ("definition of done", detail["definition_of_done"]),
            ("verification commands", detail["verification_commands"]),
        ):
            if items:
                lines += ["", f"[bold]{heading}[/bold]"]
                lines += [f"  □ {str(c)[:104]}" for c in items[:8]]
        verification = detail.get("verification") or {}
        if verification:
            passed = verification.get("passed")
            mark = "[green]✓ PASS[/green]" if passed else "[red]✗ FAIL[/red]"
            lines += ["", "[bold]verification[/bold]", f"  {mark} {verification.get('summary', '')[:110]}"]
        if detail["attempts_history"]:
            lines += ["", "[bold]attempts[/bold]"]
            for attempt in detail["attempts_history"][-3:]:
                outcome = (
                    "[green]success[/green]"
                    if attempt["outcome"] == "success"
                    else "[red]failed[/red]"
                )
                lines.append(
                    f"  {attempt['attempt_number']}. {outcome} — {attempt['failure_summary'][:100]}"
                )
        if detail["artifacts"]:
            lines += ["", "[bold]artifacts[/bold]"]
            lines += [f"  {a}" for a in detail["artifacts"][:8]]
        self.set_text("\n".join(lines))

    def render_failures(self, facade: RuntimeFacade) -> None:
        failures = facade.failures()
        if not failures:
            self.set_text(
                "[bold reverse] FAILURES [/bold reverse]\n\n[dim]no failures recorded[/dim]"
            )
            return
        lines = ["[bold reverse] FAILURES [/bold reverse]", ""]
        for failure in failures[-12:]:
            lines.append(
                f"[red]✗[/red] {failure['created_at'][11:19]} {failure['task_id'] or '—'} "
                f"({failure['agent']})"
            )
            lines.append(f"   {failure['summary'][:112]}")
            if failure["root_cause"] and failure["root_cause"] != failure["summary"]:
                lines.append(f"   [dim]cause: {failure['root_cause'][:110]}[/dim]")
            if failure["lesson"]:
                lines.append(f"   [yellow]lesson: {failure['lesson'][:110]}[/yellow]")
        self.set_text("\n".join(lines))

    def render_verification(self, facade: RuntimeFacade) -> None:
        health = facade.verification_health()
        lines = [
            "[bold reverse] VERIFICATION [/bold reverse]",
            "",
            f"[green]✓[/green] tasks with passing evidence: {health['passed']}",
            f"[red]✗[/red] tasks with failing evidence: {health['failed']}",
            f"[dim]· manual/unknown evidence: {health['unknown']}[/dim]",
            "",
        ]
        verified = 0
        for task in facade.tasks():
            if task["verification_passed"]:
                verified += 1
                if verified <= 10:
                    lines.append(f"  [green]✓[/green] {task['id']} {task['title'][:48]}")
        failed = [t for t in facade.tasks() if t["verification_passed"] is False]
        if failed:
            lines += ["", "[bold red]failing[/bold red]"]
            lines += [f"  [red]✗[/red] {t['id']} {t['title'][:48]}" for t in failed[:8]]
        self.set_text("\n".join(lines))

    def render_memory(self, facade: RuntimeFacade) -> None:
        items = facade.memory()
        if not items:
            self.set_text(
                "[bold reverse] MEMORY [/bold reverse]\n\n[dim]empty — add with: /remember <fact>[/dim]"
            )
            return
        lines = ["[bold reverse] PROJECT MEMORY [/bold reverse]", ""]
        for item in items[-15:]:
            pin = " [yellow]*[/yellow]" if item["pinned"] else ""
            lines.append(
                f"[magenta]{self._safe(item['kind'], 30)}[/magenta]{pin} "
                f"conf {item['confidence']:.2f} · {item['updated_at'][:10]}"
            )
            lines.append(f"  {self._safe(item['text'], 112)}")
        unknowns = facade.unknowns()
        if unknowns:
            lines += ["", "[bold]open unknowns[/bold]"]
            lines += [f"  [yellow]?[/yellow] {self._safe(u['question'], 100)}" for u in unknowns[:6]]
        self.set_text("\n".join(lines))

    def render_checkpoints(self, facade: RuntimeFacade) -> None:
        checkpoints = facade.checkpoints()
        if not checkpoints:
            self.set_text("[bold reverse] CHECKPOINTS [/bold reverse]\n\n[dim]none yet[/dim]")
            return
        lines = ["[bold reverse] CHECKPOINTS [/bold reverse]", ""]
        for cp in checkpoints[-12:]:
            commit = cp["git_commit"][:10] if cp["git_commit"] else "—"
            lines.append(f"  {cp['id']}  {cp['created_at'][11:19]}  git:{commit}")
        lines += ["", "[dim]restore with the CLI: auto rollback <id> --yes[/dim]"]
        self.set_text("\n".join(lines))

    @staticmethod
    def _safe(text: object, limit: int = 160) -> str:
        """Neutralize markup so runtime text can never inject console tags.

        Textual's parser treats any bracketed span as a tag — even across
        newlines — so every '[' is escaped unconditionally (prompt-injection
        defense for the display layer, comms spec §71).
        """
        return str(text).replace("\n", " ")[:limit].replace("[", "\\[")

    def render_message(self, facade: RuntimeFacade, message_id: str) -> None:
        """Full envelope detail for one real message (comms spec §58)."""
        detail = facade.message_detail(message_id)
        if detail is None:
            self.set_text(f"[red]unknown message: {message_id}[/red]")
            return
        lines = [
            f"[bold reverse] MESSAGE {detail['id']} — {detail['type']} [/bold reverse]",
            f"[bold]{detail['sender']} → {detail['recipient']}[/bold]  "
            f"[{('green' if detail['state'] == 'COMPLETED' else 'yellow' if detail['state'] in ('QUEUED', 'DELIVERED', 'RECEIVED', 'PROCESSING') else 'red')}]{detail['state']}[/]",
            "",
            f"project: {detail['project_id']} · run: {detail['run_id'][:16]} · task: {detail['task_id'] or '—'}",
            f"conversation: {detail['conversation_id']} · parent: {detail['parent_message_id'] or '—'}",
            f"correlation: {detail['correlation_id'] or '—'} · priority: {detail['priority']} "
            f"· requires response: {detail['requires_response']} · attempts: {detail['attempts']}",
            f"created: {detail['created_at']}" + (f" · expires: {detail['expires_at']}" if detail['expires_at'] else ""),
        ]
        if detail["summary"]:
            lines += ["", "[bold]payload[/bold]", f"  {self._safe(detail['summary'])}"]
        extra_payload = {
            k: v
            for k, v in detail["payload"].items()
            if k not in ("summary", "discovery", "question", "reason", "findings", "description", "title", "note", "status")
        }
        if extra_payload:
            lines += [
                f"  [dim]{self._safe(k, 40)}: {self._safe(v, 80)}[/dim]"
                for k, v in list(extra_payload.items())[:6]
            ]
        if detail["context_refs"]:
            lines += ["", "[bold]context refs[/bold]"]
            lines += [f"  {self._safe(ref, 80)}" for ref in detail["context_refs"][:8]]
        if detail["artifact_refs"]:
            lines += ["", "[bold]artifact refs[/bold]"]
            lines += [f"  {self._safe(ref, 80)}" for ref in detail["artifact_refs"][:8]]
        if detail["status_detail"]:
            lines += ["", f"[dim]status detail: {self._safe(detail['status_detail'], 120)}[/dim]"]
        self.set_text("\n".join(lines))

    def render_agent_comm(self, facade: RuntimeFacade) -> None:
        """Per-agent communication stats (comms spec §59)."""
        stats = facade.agent_comm_stats()
        if not stats:
            self.set_text("[bold reverse] AGENT TRAFFIC [/bold reverse]\n\n[dim]no messages yet[/dim]")
            return
        lines = ["[bold reverse] AGENT TRAFFIC [/bold reverse]", ""]
        for s in stats[:14]:
            lines.append(
                f"{s['agent'][:18]:18} sent {s['sent']:3} · recv {s['received']:3} · "
                f"[yellow]pending {s['pending']}[/yellow] · "
                + (f"[red]failed {s['failed']}[/red]" if s["failed"] else "failed 0")
            )
        self.set_text("\n".join(lines))


class ActivityFeed(RichLog):
    """The live event stream (spec §6): append-only, bounded by RichLog."""

    def __init__(self) -> None:
        super().__init__(highlight=False, markup=True, id="activity", wrap=True)

    def emit_item(self, item) -> None:
        color = STYLE.get(item.kind, "dim")
        task = f" [magenta]{item.task_id}[/magenta]" if item.task_id else ""
        actor = f"[bold]{item.actor[:24]}[/bold]"
        # The message is raw runtime/model text: an unbalanced '[' would raise
        # MarkupError inside write() and kill the app — escape it like
        # DetailPanel._safe does (comms spec §71).
        message = str(item.message).replace("[", "\\[")
        self.write(
            f"[dim]{item.time()}[/dim] [{color}]{item.event}[/] {actor}{task}\n  {message}"
        )


HELP_TEXT = """[bold reverse] AUTONOMOUS ENGINEERING RUNTIME — HELP [/bold reverse]

[bold]keys[/bold]
  1/2/3/4      side panel: tasks / agents / models / comm   tab   focus cycle
  f            failures          v  verification        y  memory
  m            agent traffic (per-agent message stats)
  c            checkpoints       ?  this help           r  force refresh
  A            approve pending escalation            R  reject pending
  p            pause / resume runtime                x  cancel (stop) run
  e            toggle Intent Compiler (enhance) on/off
  Ctrl+L       clear activity feed                   q  quit (runtime keeps state)

[bold]input[/bold]
  <objective>          submit a new objective and start autonomous execution
  /status | /tasks     one-shot status / task list into the feed
  /filter <text>       filter the activity feed (agent/task/event text)
  /filter clear        clear the filter
  /inspect TASK-003    open full task detail (or /inspect msg-... for a message)
  /remember <fact>     persist a durable fact into project memory
  /pause /resume /stop control the run through the real control channel
  /approve [id|all]    approve pending escalation(s) (plain /a approves the first)
  /reject [id|all]     reject pending escalation(s)
  /budget              spend, token usage and pending-approval count
  /metrics             project metrics (tasks, retries, verification, events)
  /roadmap             milestone list and their status
  /config              current project configuration
  /mode                show the Intent Compiler toggle state

Everything displayed is real runtime state; nothing is simulated."""


class EngineTUI(App):
    """The main application (spec §3, §4)."""

    TITLE = "AUTONOMOUS ENGINEERING RUNTIME"
    CSS = """
    #main { height: 1fr; }
    #side-panel { width: 44; border: round $accent; padding: 0 1; }
    #feed-container { border: round $panel; padding: 0 1; }
    #detail { height: 12; border: round $secondary; padding: 0 1; }
    #input-bar { dock: bottom; height: 3; }
    #activity { height: 1fr; }
    """
    BINDINGS = [
        Binding("1", "tab_tasks", "tasks", show=False),
        Binding("2", "tab_agents", "agents", show=False),
        Binding("3", "tab_models", "models", show=False),
        Binding("4", "tab_messages", "comm", show=False),
        Binding("m", "view_agent_comm", "agent traffic", show=False),
        Binding("f", "view_failures", "failures", show=False),
        Binding("v", "view_verification", "verification", show=False),
        Binding("y", "view_memory", "memory", show=False),
        Binding("c", "view_checkpoints", "checkpoints", show=False),
        Binding("?", "view_help", "help", show=False),
        Binding("r", "force_refresh", "refresh", show=False),
        Binding("p", "toggle_pause", "pause/resume", show=False),
        Binding("x", "cancel_run", "cancel", show=False),
        Binding("a", "approve", "approve", show=False),
        Binding("j", "reject", "reject", show=False),
        Binding("e", "toggle_enhance", "enhance", show=False),
        Binding("slash", "focus_input", "input", show=False, key_display="/"),
        Binding("ctrl+l", "clear_feed", "clear", show=False),
        Binding("escape", "back_to_overview", "overview", show=False),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self, root: Path, *, attached: bool = True, objective: str = ""):
        super().__init__()
        self.facade = RuntimeFacade(root)
        self.state = UIState(facade=self.facade)
        self.runner = UIRunner(self.facade, self.state, attached=attached)
        self._initial_objective = objective
        self._detail_mode = "overview"
        self._detail_task_id = ""
        self._detail_message_id = ""
        self._was_running = False

    # ---- layout ----

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            yield SidePanel()
            with Vertical(id="feed-container"):
                yield ActivityFeed()
        yield DetailPanel(id="detail")
        yield Input(placeholder="objective, /command, or ? for help…", id="input-bar")
        yield Footer()

    # ---- lifecycle ----

    async def on_mount(self) -> None:
        self.state.load_recent()
        feed = self.query_one(ActivityFeed)
        for item in self.state.visible_activity()[-80:]:
            feed.emit_item(item)
        self.refresh_all()
        if self._initial_objective:
            self._submit_objective(self._initial_objective)
        elif self.state.run_status() in ("running", "paused") and not self.runner.attached:
            self._push("runtime is owned by another process; observing (spec §47)")
        # Start in NAVIGATION mode (spec §21): character keys drive views;
        # '/' enters input mode, escape leaves it. The input is not focused
        # on launch so 'p' pauses instead of typing the letter p.
        self.set_focus(None)
        self.set_interval(REFRESH_SECONDS, self.on_tick)

    async def on_tick(self) -> None:
        fresh = self.state.poll_events()
        if fresh:
            feed = self.query_one(ActivityFeed)
            # Emit exactly the new items, applying the filter per item —
            # re-slicing visible_activity() replayed old lines whenever a
            # non-matching event arrived while a filter was active.
            for item in fresh:
                if item.matches(self.state.filter_text):
                    feed.emit_item(item)
        self.refresh_all()

    # ---- rendering ----

    def refresh_all(self) -> None:
        self.refresh_header()
        self.query_one(SidePanel).update_panel(self.state)
        detail = self.query_one(DetailPanel)
        if self._detail_mode == "overview":
            detail.render_overview(self.state)
        elif self._detail_mode == "task":
            detail.render_task(self.facade, self._detail_task_id)
        elif self._detail_mode == "message":
            detail.render_message(self.facade, self._detail_message_id)
        elif self._detail_mode == "agents_comm":
            detail.render_agent_comm(self.facade)
        elif self._detail_mode == "failures":
            detail.render_failures(self.facade)
        elif self._detail_mode == "verification":
            detail.render_verification(self.facade)
        elif self._detail_mode == "memory":
            detail.render_memory(self.facade)
        elif self._detail_mode == "checkpoints":
            detail.render_checkpoints(self.facade)
        elif self._detail_mode == "help":
            detail.set_text(HELP_TEXT)

    def refresh_header(self) -> None:
        progress = self.state.progress()
        run = self.facade.current_run()
        status = self.state.run_status().upper() or "IDLE"
        if self.runner.running:
            status = "AUTONOMOUS"
        elif self._was_running and not self.runner.running:
            status = (run.get("stop_reason") or status or "STOPPED").upper()
        budget = self.state.budget_remaining_pct()
        budget_text = f"{budget:.0f}% left" if budget is not None else "n/a"
        agents = len(self.facade.active_agents())
        mode = "ENHANCE ON" if self.facade.enhance_prompt_enabled() else "ENHANCE OFF"
        self.sub_title = (
            f"{self.facade.project().get('name', 'project')} · "
            f"tasks {progress['completed']}/{progress['total']} · "
            f"agents {agents} · {status} · "
            f"runtime {_fmt_elapsed(self.state.elapsed_seconds())} · "
            f"budget {budget_text} · {mode}"
        )

    def _push(self, message: str, kind: str = "info") -> None:
        from .state import ActivityItem

        item = ActivityItem(
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            event="ui",
            actor="ui",
            task_id="",
            message=message,
            kind=kind,
        )
        self.state.activity.append(item)
        self.query_one(ActivityFeed).emit_item(item)

    # ---- input handling (spec §17, §20) ----

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        # Return to navigation mode after submit (spec §21): character keys
        # drive views again until '/' focuses the input.
        self.set_focus(None)
        if not text:
            return
        if text.startswith("/"):
            await self._command(text)
        else:
            self._submit_objective(text)

    async def action_focus_input(self) -> None:
        self.query_one("#input-bar").focus()

    def _submit_objective(self, text: str) -> None:
        self._push(f"objective accepted: {text[:100]}", "decision")
        if not self.runner.attached:
            self._push(
                "runtime is owned by another process; write the objective via `auto run` instead",
                "warning",
            )
            return
        self._was_running = True
        self.runner.start(text)
        self._push("autonomous execution started (attached daemon)", "success")

    async def _command(self, text: str) -> None:
        parts = text[1:].split(maxsplit=1)
        command = parts[0].lower()
        argument = parts[1].strip() if len(parts) > 1 else ""
        if command in ("status", "s"):
            progress = self.state.progress()
            self._push(
                f"tasks {progress['completed']}/{progress['total']} · "
                f"active {progress['active']} · failed {progress['failed']} · "
                f"status {self.state.run_status()} · "
                f"escalations {len(self.state.pending_escalations())}",
                "decision",
            )
        elif command == "tasks":
            for task in self.facade.tasks()[:15]:
                mark = "✓" if task["status"] == "COMPLETED" else "▶" if task["active"] else "○"
                self._push(f"{mark} {task['id']} [{task['status']}] {task['title'][:60]}")
        elif command == "filter":
            self.state.filter_text = "" if argument.lower() in ("", "clear") else argument
            self._push(
                f"filter: {self.state.filter_text or 'cleared'}"
                f" ({len(self.state.visible_activity())} events)"
            )
        elif command == "inspect":
            if argument:
                if argument.startswith("msg-"):
                    self._detail_mode = "message"
                    self._detail_message_id = argument
                else:
                    self._detail_mode = "task"
                    self._detail_task_id = argument
                self.refresh_all()
        elif command == "remember":
            if argument and self.facade.remember(argument):
                self._push(f"remembered: {argument[:80]}", "success")
            else:
                self._push("usage: /remember <fact>", "warning")
        elif command == "pause":
            self.facade.pause()
            self._push("pause requested (control channel)", "warning")
        elif command == "resume":
            self.facade.resume()
            self._push("resume requested (control channel)", "success")
        elif command == "stop":
            await self.action_cancel_run()
        elif command in ("approve", "a"):
            pending = self.state.pending_escalations()
            if argument.lower() == "all":
                targets = [str(i.get("id", "")) for i in pending]
            elif argument:
                targets = [argument]
            else:
                targets = [str(pending[0].get("id", ""))] if pending else []
            if not targets:
                self._push("no pending approvals", "info")
            for escalation_id in targets:
                if self.facade.approve(escalation_id):
                    self._push(f"approved escalation {escalation_id}", "success")
                else:
                    self._push(f"could not approve {escalation_id}", "warning")
        elif command in ("reject", "j"):
            pending = self.state.pending_escalations()
            if argument.lower() == "all":
                targets = [str(i.get("id", "")) for i in pending]
            elif argument:
                targets = [argument]
            else:
                targets = [str(pending[0].get("id", ""))] if pending else []
            if not targets:
                self._push("no pending approvals", "info")
            for escalation_id in targets:
                if self.facade.reject(escalation_id):
                    self._push(f"rejected escalation {escalation_id}", "warning")
                else:
                    self._push(f"could not reject {escalation_id}", "warning")
        elif command == "budget":
            snapshot = self.facade.budget_snapshot()
            spend = ", ".join(f"{k}={v}" for k, v in snapshot.items()) or "no spend recorded yet"
            self._push(
                f"budget: {spend} · pending approvals "
                f"{len(self.state.pending_escalations())}",
                "decision",
            )
        elif command == "metrics":
            from ..runtime.metrics import collect_metrics

            data = collect_metrics(self.facade.workspace, self.facade.store)
            for key, value in data.items():
                if isinstance(value, (int, float, str)):
                    self._push(f"metrics.{key}: {value}", "decision")
        elif command == "roadmap":
            milestones = self.facade.milestones()
            if not milestones:
                self._push("roadmap: no milestones recorded yet", "info")
            for milestone in milestones:
                self._push(
                    f"{milestone.get('id', '?')} [{milestone.get('status', '?')}] "
                    f"{str(milestone.get('title', ''))[:60]}"
                )
        elif command == "config":
            for key, value in sorted(self.facade.config.model_dump(mode="json").items()):
                self._push(f"config.{key}: {value}", "decision")
        elif command == "mode":
            enabled = self.facade.enhance_prompt_enabled()
            self._push(f"Intent Compiler: {'ON' if enabled else 'OFF'} (press e to toggle)")
        elif command == "help":
            self._detail_mode = "help"
            self.refresh_all()
        else:
            self._push(f"unknown command: {command} (try /help… or press ?)", "warning")

    # ---- key actions ----

    async def action_tab_tasks(self) -> None:
        self.query_one(SidePanel).tab = "tasks"
        self.refresh_all()

    async def action_tab_agents(self) -> None:
        self.query_one(SidePanel).tab = "agents"
        self.refresh_all()

    async def action_tab_models(self) -> None:
        self.query_one(SidePanel).tab = "models"
        self.refresh_all()

    async def action_tab_messages(self) -> None:
        self.query_one(SidePanel).tab = "messages"
        self.refresh_all()

    async def action_view_agent_comm(self) -> None:
        self._detail_mode = "agents_comm"
        self.refresh_all()

    async def action_view_failures(self) -> None:
        self._detail_mode = "failures"
        self.refresh_all()

    async def action_view_verification(self) -> None:
        self._detail_mode = "verification"
        self.refresh_all()

    async def action_view_memory(self) -> None:
        self._detail_mode = "memory"
        self.refresh_all()

    async def action_view_checkpoints(self) -> None:
        self._detail_mode = "checkpoints"
        self.refresh_all()

    async def action_view_help(self) -> None:
        self._detail_mode = "help"
        self.refresh_all()

    async def action_back_to_overview(self) -> None:
        self._detail_mode = "overview"
        self.refresh_all()

    async def action_force_refresh(self) -> None:
        self.state.load_recent()
        self._push("state reloaded from runtime", "info")
        self.refresh_all()

    async def action_clear_feed(self) -> None:
        self.query_one(ActivityFeed).clear()

    async def action_toggle_pause(self) -> None:
        # Track the toggle locally: the persisted run status can be stale
        # (the loop consumes the control file only at a cycle boundary).
        self._pause_toggled = not getattr(self, "_pause_toggled", False)
        if self._pause_toggled:
            self.facade.pause()
            self._push("pause requested", "warning")
        else:
            self.facade.resume()
            self._push("resume requested", "success")

    async def action_cancel_run(self) -> None:
        self.facade.cancel()
        self._push("cancellation requested — the loop stops at the next cycle boundary", "warning")
        if self.runner.attached:
            await self.runner.stop_and_wait()
            self._push("runtime stopped cleanly; state persisted", "info")

    async def action_approve(self) -> None:
        pending = self.state.pending_escalations()
        if not pending:
            self._push("no pending approvals", "info")
            return
        item = pending[0]
        if self.facade.approve(str(item.get("id", ""))):
            self._push(
                f"approved escalation {item.get('id')} ({str(item.get('reason', ''))[:60]})",
                "success",
            )

    async def action_reject(self) -> None:
        pending = self.state.pending_escalations()
        if not pending:
            self._push("no pending approvals", "info")
            return
        item = pending[0]
        if self.facade.reject(str(item.get("id", ""))):
            self._push(
                f"rejected escalation {item.get('id')} ({str(item.get('reason', ''))[:60]})",
                "warning",
            )

    async def action_toggle_enhance(self) -> None:
        enabled = not self.facade.enhance_prompt_enabled()
        self.facade.set_enhance_prompt(enabled)
        state_text = "ON — new objectives go through the Intent Compiler" if enabled else "OFF — objectives execute literally"
        self._push(f"Intent Compiler {state_text}", "decision")

    async def action_quit(self) -> None:
        # The runtime owns its state; a UI exit never kills it (spec §46/§47).
        if self.runner.running:
            self._push("UI closing — the background run will be detached", "warning")
            self.runner._task.cancel()
        self.facade.close()
        self.exit()


def launch(root: Path, *, attached: bool = True, objective: str = "") -> None:
    """Entry point used by the CLI. Falls back to plain mode without a TTY."""
    import sys

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        from .plain import run_plain

        run_plain(root, objective=objective)
        return
    EngineTUI(root, attached=attached, objective=objective).run()
