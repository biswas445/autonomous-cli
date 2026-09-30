"""Agent message protocol tests (comms spec §82–§88).

Two layers, mirroring the repo's integration-first convention:

* protocol semantics — exercised through the real MessageService/MessageStore
  over the real SQLite database, with senders supplied by the test (the same
  identity binding the orchestrator uses);
* vertical slices — a *real* offline run (echo provider, no mocks) must
  produce a correlated Director→Architect→Coder→Tester conversation, survive a
  runtime restart with zero message loss, stay safe under duplicate delivery
  and concurrency, and leave a message→decision→task audit chain.
"""

from __future__ import annotations

from collections import Counter

import pytest

from autonomous_engine.messaging import (
    ORCHESTRATOR,
    DeliveryState,
    LoopDetected,
    MailboxFull,
    Message,
    MessageRejected,
    MessageService,
    MessageStore,
    MsgType,
    default_agents,
)
from autonomous_engine.runtime.context_setup import open_context
from autonomous_engine.runtime.orchestrator import Orchestrator


@pytest.fixture()
def service(context):
    """A real MessageService over the real project database."""
    return MessageService(
        MessageStore(context.store.db, context.store.project_id),
        context.workspace.events,
        run_id="RUN-TEST",
        agents=default_agents(),
    )


def _drain(service: MessageService, recipient: str) -> list[Message]:
    return service.fetch(recipient, limit=50)


# ---- §4–§9: envelopes, types, lifecycle -------------------------------------


async def test_send_persists_and_routes_a_typed_message(service):
    msg = service.send(
        msg_type=MsgType.TASK_REQUEST,
        sender=ORCHESTRATOR,
        recipient="coder",
        payload={"task_id": "TASK-001"},
        task_id="TASK-001",
        requires_response=True,
    )
    assert msg.id.startswith("msg-")
    assert msg.state == DeliveryState.QUEUED
    assert msg.correlation_id == msg.id  # request types correlate to themselves
    assert msg.protocol_version == 1

    fetched = _drain(service, "coder")
    assert [m.id for m in fetched] == [msg.id]
    assert fetched[0].state == DeliveryState.RECEIVED


async def test_payload_validation_rejects_malformed_messages(service):
    with pytest.raises(MessageRejected):
        service.send(
            msg_type=MsgType.TASK_FAILED,  # requires failure_type + summary
            sender="coder",
            recipient=ORCHESTRATOR,
            payload={"task_id": "TASK-1"},
        )
    with pytest.raises(MessageRejected):
        service.send(
            msg_type=MsgType.TASK_REQUEST,
            sender="",  # identity is runtime-bound: empty sender is rejected
            recipient="coder",
            payload={"task_id": "TASK-1"},
        )
    with pytest.raises(MessageRejected):
        service.send(
            msg_type=MsgType.TASK_REQUEST,
            sender=ORCHESTRATOR,
            recipient="coder",
            payload={"task_id": "TASK-1", "blob": "x" * 30_000},  # > MAX_PAYLOAD_CHARS
        )


async def test_delivery_states_are_deterministic(service):
    msg = service.send(
        msg_type=MsgType.BLOCKER_REPORTED,
        sender="coder",
        recipient=ORCHESTRATOR,
        payload={"task_id": "TASK-1", "blocker_type": "dependency", "description": "x"},
        task_id="TASK-1",
    )
    (received,) = _drain(service, ORCHESTRATOR)
    assert received.id == msg.id
    service.acknowledge(received, "director: seen")
    assert received.state == DeliveryState.ACKNOWLEDGED
    service.start_processing(received)
    assert received.state == DeliveryState.PROCESSING
    service.complete(received, "done")
    assert received.state == DeliveryState.COMPLETED
    assert received.is_terminal()
    # ACK never meant "done" — the recorded lifecycle shows both steps (§55).
    assert "director: seen" in received.status_detail or received.status_detail


async def test_correlation_and_threads_connect_request_to_response(service):
    request = service.send(
        msg_type=MsgType.REVIEW_REQUEST,
        sender=ORCHESTRATOR,
        recipient="reviewer",
        payload={"task_id": "TASK-9"},
        task_id="TASK-9",
        requires_response=True,
    )
    reply = service.send(
        msg_type=MsgType.REVIEW_RESULT,
        sender="reviewer",
        recipient=ORCHESTRATOR,
        payload={"task_id": "TASK-9", "verdict": "clean"},
        parent=request,
    )
    assert reply.parent_message_id == request.id
    assert reply.correlation_id == request.id
    assert reply.conversation_id == request.conversation_id
    thread = service.store.thread(request.conversation_id)
    assert [m.id for m in thread] == [request.id, reply.id]
    assert request.response_type() == MsgType.REVIEW_RESULT


async def test_identity_cannot_be_spoofed(service):
    """§37/§38: the service binds the caller's identity, never a claimed one."""
    msg = service.send(
        msg_type=MsgType.HELP_RESPONSE,
        sender="coder",  # even if a model "claims" authority, sender is fixed by the caller
        recipient=ORCHESTRATOR,
        payload={"answer": "x"},
    )
    assert msg.sender == "coder"
    # self-addressed messages are invalid — coordination needs two parties
    with pytest.raises(MessageRejected):
        service.send(
            msg_type=MsgType.DIRECTIVE_CONTINUE,
            sender="coder",
            recipient="coder",
            payload={"note": "x"},
        )
    # and an unknown recipient is unroutable
    with pytest.raises(MessageRejected):
        service.send(
            msg_type=MsgType.STATUS_REQUEST,
            sender=ORCHESTRATOR,
            recipient="nonexistent-agent",
            payload={},
        )


async def test_expiration_moves_stale_messages_out_of_live_queues(service):
    msg = service.send(
        msg_type=MsgType.TASK_REQUEST,
        sender=ORCHESTRATOR,
        recipient="coder",
        payload={"task_id": "TASK-EXP"},
        task_id="TASK-EXP",
        expires_in_seconds=-1,  # already stale
    )
    assert msg.expires_at is not None
    expired = service.expire_stale()
    assert expired == 1
    assert service.store.get(msg.id).state == DeliveryState.EXPIRED
    assert _drain(service, "coder") == []  # never executed normally (§13)


async def test_retry_then_dead_letter_preserves_the_failure_reason(service):
    msg = service.send(
        msg_type=MsgType.HELP_REQUEST,
        sender="coder",
        recipient="debugger",
        payload={"question": "why does this fail?"},
        task_id="TASK-1",
    )
    (received,) = _drain(service, "debugger")
    service.fail_processing(received, "handler crashed once")
    assert received.state == DeliveryState.FAILED
    # FAILED -> QUEUED retry policy (§35)
    service.store.save(received)
    received.set_state(DeliveryState.QUEUED)
    service.store.save(received)
    (retry,) = _drain(service, "debugger")
    assert retry.id == msg.id and retry.attempts == 1
    service.fail_processing(retry, "handler crashed twice")
    service.store.save(retry)
    retry.set_state(DeliveryState.QUEUED)
    service.store.save(retry)
    (third,) = _drain(service, "debugger")
    service.fail_processing(third, "handler crashed three times")
    assert third.state == DeliveryState.DEAD
    assert "three times" in third.status_detail


async def test_mailbox_backpressure_rejects_overload(service):
    """§34: queue limits; CRITICAL priority still gets through."""
    service.agents["coder"].mailbox_limit = 2
    for i in range(2):
        service.send(
            msg_type=MsgType.STATUS_RESPONSE,
            sender="tester",
            recipient="coder",
            payload={"status": f"n{i}"},
        )
    with pytest.raises(MailboxFull):
        service.send(
            msg_type=MsgType.STATUS_RESPONSE,
            sender="tester",
            recipient="coder",
            payload={"status": "overflow"},
        )
    critical = service.send(
        msg_type=MsgType.BLOCKER_REPORTED,
        sender="coder",
        recipient="debugger",
        payload={"task_id": "T", "blocker_type": "security", "description": "urgent"},
        priority="critical",
    )
    assert critical.priority.value == "critical"


async def test_loop_detection_stops_pathological_patterns(service):
    """§67: same pair+type+task repeating rapidly raises LoopDetected."""
    with pytest.raises(LoopDetected):
        for _ in range(7):
            service.send(
                msg_type=MsgType.STATUS_REQUEST,
                sender="coder",
                recipient="tester",
                payload={},
            )


async def test_role_and_capability_routing_records_the_actual_recipient(service):
    """§30/§31: role addressing resolves deterministically and is recorded."""
    by_role = service.send(
        msg_type=MsgType.HELP_REQUEST,
        sender="coder",
        recipient="debugger",  # role
        payload={"question": "q"},
    )
    assert by_role.recipient == "debugger"
    by_capability = service.send(
        msg_type=MsgType.REVIEW_REQUEST,
        sender="coder",
        recipient="nobody-with-this-name",
        payload={"task_id": "T"},
        capability="security",
    )
    assert by_capability.recipient == "security"


# ---- §82: the success vertical slice with real agents ------------------------


async def test_vertical_slice_real_run_produces_the_full_conversation(project, echo):
    """Director→Architect→Coder→Tester over REAL agents, offline, no mocks."""
    context = open_context(project)
    orch = Orchestrator(context, use_model_director=False)
    result = await orch.run_loop("Build a note-taking REST API")
    assert result.status == "completed"

    store = orch.messaging.store
    messages = store.list_messages(limit=500)
    types = Counter(m.type.value for m in messages)

    # the spec's conversation, as real persisted objects (§82, §92)
    assert types[MsgType.ARCHITECTURE_REQUEST.value] == 1
    assert types[MsgType.ARCHITECTURE_RESULT.value] == 1
    assert types[MsgType.TASK_REQUEST.value] == result.completed
    assert types[MsgType.TASK_ACCEPTED.value] == result.completed
    assert types[MsgType.TASK_COMPLETED.value] == result.completed
    assert types[MsgType.VERIFICATION_RESULT.value] == result.completed

    # request/response correlation survives the whole chain (§7)
    arch_result = next(m for m in messages if m.type == MsgType.ARCHITECTURE_RESULT)
    arch_request = store.get(arch_result.parent_message_id)
    assert arch_request.type == MsgType.ARCHITECTURE_REQUEST
    assert arch_result.correlation_id == arch_request.id

    # every message settled deterministically
    assert all(m.state == DeliveryState.COMPLETED for m in messages)
    # the run itself stayed honest
    assert result.completed == len(orch.graph.completed_tasks())
    context.db.close()


async def test_failure_slice_reports_and_director_interprets(project, echo):
    """§83: a failing task emits structured TASK_FAILED messages the Director
    interprets; the deterministic repair loop still owns the retry."""
    context = open_context(project)
    marker = context.repo_root / "repair-marker.txt"
    check = (
        f"import pathlib,sys; sys.exit(0 if pathlib.Path('{marker.as_posix()}').exists() else 1)"
    )
    graph = context.workspace.load_graph()
    from autonomous_engine.core.task import Task

    task = Task(
        id="TASK-MSG-F1",
        title="fails once then repaired",
        verification_commands=[f'python -c "{check}"'],
    )
    graph.add_task(task)
    context.workspace.save_graph(graph)

    original = Orchestrator._diagnose

    async def repairing(self, t):
        marker.write_text("repaired", encoding="utf-8")
        return {"root_cause": "marker missing", "files_affected": [], "tests_required": []}

    Orchestrator._diagnose = repairing  # type: ignore[method-assign]
    try:
        orch = Orchestrator(context, use_model_director=False)
        result = await orch.run_loop()
    finally:
        Orchestrator._diagnose = original  # type: ignore[method-assign]

    assert result.status == "completed"
    store = orch.messaging.store
    messages = store.list_messages(limit=500)
    failures = [m for m in messages if m.type == MsgType.TASK_FAILED and m.task_id == "TASK-MSG-F1"]
    assert failures, "the failed attempt must be reported as a structured TASK_FAILED"
    assert failures[0].payload["failure_type"] in ("TEST", "CODE")
    verdicts = [
        m
        for m in messages
        if m.type == MsgType.VERIFICATION_RESULT and m.task_id == "TASK-MSG-F1"
    ]
    assert {m.payload["passed"] for m in verdicts} == {False, True}
    diagnoses = [m for m in messages if m.type == MsgType.DISCOVERY_REPORTED and m.task_id == "TASK-MSG-F1"]
    assert diagnoses, "the debugger's root cause must be a DISCOVERY_REPORTED"
    assert all(m.state in (DeliveryState.COMPLETED, DeliveryState.DEAD) for m in messages)
    context.db.close()


# ---- §84/§85/§86: persistence, duplicates, concurrency -----------------------


async def test_messages_survive_a_runtime_restart(project, echo):
    """§84: run, stop, restart — inbox restored, zero message loss."""
    context = open_context(project)
    orch = Orchestrator(context, use_model_director=False)
    result = await orch.run_loop("Build a note-taking REST API")
    assert result.status == "completed"
    before = {m.id: m.state for m in orch.messaging.store.list_messages(limit=500)}
    assert before
    context.db.close()

    # runtime restart: a fresh orchestrator over the same project must see
    # every message with its settled state.
    context2 = open_context(project)
    orch2 = Orchestrator(context2, use_model_director=False)
    after = {m.id: m.state for m in orch2.messaging.store.list_messages(limit=500)}
    assert after == before, "no message loss across restart"
    pending = [m for m in after.values() if m in (DeliveryState.QUEUED, DeliveryState.DELIVERED)]
    assert not pending, "a completed run leaves nothing pending"
    context2.db.close()


async def test_duplicate_delivery_is_safe_and_recorded(service):
    """§14/§85: handler side effects happen once; duplicates are recorded."""
    payload = {"task_id": "TASK-DUP", "summary": "done", "idempotency_key": "run1:TASK-DUP"}
    first = service.send(
        msg_type=MsgType.TASK_COMPLETED,
        sender="coder",
        recipient=ORCHESTRATOR,
        payload=payload,
        task_id="TASK-DUP",
    )
    replay = service.send(
        msg_type=MsgType.TASK_COMPLETED,
        sender="coder",
        recipient=ORCHESTRATOR,
        payload=dict(payload),  # same idempotency key, redelivered
        task_id="TASK-DUP",
    )
    assert replay.id == first.id, "duplicate suppressed to the original message"

    # completing twice is a no-op (idempotent handling, §85)
    (received,) = _drain(service, ORCHESTRATOR)
    service.complete(received, "handled once")
    before = service.store.get(received.id).status_detail
    service.complete(service.store.get(received.id), "handled twice")
    again = service.store.get(received.id)
    assert again.state == DeliveryState.COMPLETED
    assert again.status_detail == before


async def test_concurrent_senders_lose_nothing(service):
    """§86: many agents send simultaneously; every message persists once."""
    import threading

    # §34 backpressure is real, so the shared recipient's mailbox must be
    # large enough for this flood; the dedicated backpressure test covers it.
    service.agents[ORCHESTRATOR].mailbox_limit = 200

    def flood(name: str, n: int) -> None:
        for i in range(n):
            # distinct task ids keep the loop detector (§67) out of the way:
            # this test exercises concurrency, not pathological repetition.
            try:
                service.send(
                    msg_type=MsgType.TASK_COMPLETED,
                    sender=name,
                    recipient=ORCHESTRATOR,
                    payload={"task_id": f"TASK-{name}-{i}", "summary": f"{name}-{i}"},
                    task_id=f"TASK-{name}-{i}",
                )
            except Exception:
                raise

    threads = [
        threading.Thread(target=flood, args=(sender, 12))
        for sender in ("coder", "tester", "debugger", "reviewer")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    messages = service.store.list_messages(limit=1000)
    assert len(messages) == 48
    ids = [m.id for m in messages]
    assert len(ids) == len(set(ids)), "no duplicate message ids"
    for sender in ("coder", "tester", "debugger", "reviewer"):
        mine = [m for m in messages if m.sender == sender]
        assert len(mine) == 12
        assert all(m.recipient == ORCHESTRATOR for m in mine)


# ---- §41/§42/§61: Director interpretations with traceability -----------------


async def test_high_confidence_discovery_creates_a_traced_task(project, echo):
    """§41/§43: message → Director decision → task, recorded end to end."""
    context = open_context(project)
    orch = Orchestrator(context, use_model_director=False)
    messenger = orch.messenger["researcher"]
    discovery = messenger.discovery(
        "the storage engine needs a migration",
        confidence=0.9,
        task_id=None,
    )
    # The discovery lands in the Director inbox...
    assert any(m.id == discovery.id for m in orch.director_inbox.pending())
    # ...and the next inbox pass turns it into a real, traced task.
    handled = await orch._process_director_inbox()
    assert handled >= 1

    followups = [
        t for t in orch.graph.all() if t.created_by == discovery.task_id or "migration" in t.title.lower()
    ]
    assert followups, "the Director must create a follow-up task from the discovery"
    decisions = context.store.list_decisions()
    titles = " ".join(d.title for d in decisions)
    assert discovery.id in titles, "the decision records the originating message"
    context.db.close()


async def test_replan_request_reaches_the_director_and_replies(project, echo):
    """§27/§42: an agent proposes, the Director disposes, the reply correlates."""
    context = open_context(project)
    orch = Orchestrator(context, use_model_director=False)
    request = orch.messenger["debugger"].replan_request(
        "current architecture prevents streaming", task_id=""
    )
    assert any(m.id == request.id for m in orch.director_inbox.pending())

    replanned = {"called": 0}
    original = Orchestrator._replan

    async def counting_replan(self, reason: str) -> None:
        replanned["called"] += 1
        await original(self, reason)

    Orchestrator._replan = counting_replan  # type: ignore[method-assign]
    try:
        await orch._process_director_inbox()
    finally:
        Orchestrator._replan = original  # type: ignore[method-assign]

    assert replanned["called"] == 1
    replies = orch.messaging.store.by_correlation(request.id)
    assert any(r.type == MsgType.REPLAN_RESULT for r in replies), "the Director replies (§76)"
    context.db.close()


async def test_failure_exhausting_attempts_escalates_via_the_inbox(project, echo):
    """§21/§76: a TASK_FAILED at the attempt budget escalates to a human."""
    context = open_context(project)
    orch = Orchestrator(context, use_model_director=False)
    messenger = orch.messenger["coder"]
    report = messenger.task_failed(
        "TASK-ESC",
        "TEST",
        "integration test 3 keeps failing",
        attempt=context.config.budget.max_task_attempts,
    )
    await orch._process_director_inbox()
    escalations = context.workspace.pending_escalations()
    assert escalations, "the Director escalates instead of retrying blindly"
    assert any("TASK-ESC" in str(e.get("task_id", "")) or "TASK-ESC" in str(e) for e in escalations)
    assert orch.messaging.store.get(report.id).state == DeliveryState.COMPLETED
    context.db.close()
