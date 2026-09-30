# Autonomous Software Engineering CLI

## Working concept

### 1. The idea in one sentence

> Give the system a software goal once, and let an autonomous AI engineering organization convert that goal into a plan, implement it, verify it, repair failures, re-plan when reality changes, and continue working until the project reaches a defined completion state.

The user should not have to repeatedly tell the AI:

> "Now implement this."

> "Now run the tests."

> "Now fix the error."

> "Now review the architecture."

> "Now continue with task #17."

The system should make those decisions itself.

---

# 2. What is actually different about the product?

A normal AI coding workflow looks approximately like:

```text
Human
  ↓
Prompt
  ↓
Coding model
  ↓
Code
  ↓
Human evaluates result
  ↓
Another prompt
  ↓
Coding model
  ↓
More code
```

Your system should instead behave like:

```text
Human
  ↓
Project goal
  ↓
Intent Compiler
  ↓
Autonomous Engineering Director
  ↓
Project Plan
  ↓
Task Graph
  ↓
┌─────────────────────────────────────────────┐
│ Autonomous Engineering Loop                 │
│                                             │
│ Planner                                     │
│   ↓                                         │
│ Architect                                   │
│   ↓                                         │
│ Implementer                                 │
│   ↓                                         │
│ Tester / Verifier                           │
│   ↓                                         │
│ Reviewer                                    │
│   ↓                                         │
│ Security / Quality checks                   │
│   ↓                                         │
│ Result Analysis                             │
│   ↓                                         │
│ Replanner                                   │
│   ↓                                         │
│ Next task                                   │
└─────────────────────────────────────────────┘
  ↓
Production-ready project
```

The key difference is that **the AI itself becomes responsible for deciding what happens next**.

---

# 3. The most important architectural decision

Do NOT build this as:

```text
Planner → Architect → Coder → Tester → Done
```

That is too rigid.

Real software development is not linear.

Instead, build it as an **adaptive state machine / task graph**.

For example:

```text
Goal
  ↓
Requirements
  ↓
Architecture
  ↓
Task decomposition
  ↓
Task A ───────────────┐
Task B ────────┐      │
Task C ────────┼──────┘
               ↓
          Implementation
               ↓
             Tests
               ↓
        Verification failed?
          /           \
        YES            NO
         ↓              ↓
      Debugger       Review
         ↓              ↓
      Re-test        Accepted?
         ↓           /    \
         └──────── YES     NO
                       ↓
                    Rework
```

The system must be able to move backward and sideways.

A coding task may discover that the architecture is wrong.

A test may reveal that requirements were incomplete.

A security review may require redesign.

A dependency may change.

A feature may split into five new tasks.

Therefore the orchestrator must be able to say:

> "The current plan is no longer valid. I need to modify the plan."

That is a major part of your project's intelligence.

---

# 4. The system should have an "AI Engineering Director"

This is the most important agent.

Do not think of the Planner as the boss.

Instead create a top-level **Engineering Director / Manager Agent**.

Its job is not primarily to write code.

Its job is to continuously answer:

```text
What is the project trying to become?

What has already been completed?

What remains?

What is blocking progress?

Which agent should work next?

Does the current architecture still make sense?

Did the latest implementation actually satisfy the requirement?

Should we create more tasks?

Should we abandon or modify the current approach?

Should multiple tasks run in parallel?

Is the project actually finished?
```

This agent is effectively the system's autonomous "manager."

The human provides the goal.

The Engineering Director manages the workforce.

---

# 5. Agent organization

Instead of using two or three models, design the system so an arbitrary number of specialized agents can exist.

## Core agents

### A. Intent Compiler

Transforms the user's messy natural-language request into a structured project definition.

Input:

```text
Build me a professional SaaS app for...
```

Output:

```json
{
  "objective": "...",
  "users": [],
  "features": [],
  "constraints": [],
  "non_goals": [],
  "quality_requirements": [],
  "deployment_target": "...",
  "unknowns": []
}
```

This is where your **Prompt Enhance toggle** belongs.

Do not merely "make the prompt longer."

Instead make it an **Intent Compiler**.

It should extract:

* goals
* requirements
* implied requirements
* constraints
* assumptions
* ambiguities
* quality expectations
* technical risks
* acceptance criteria

The result becomes the machine-readable project specification.

---

### B. Product/Requirements Agent

Converts the intent into:

```text
Requirements
User stories
Acceptance criteria
Non-functional requirements
Edge cases
Constraints
Definition of Done
```

---

### C. Research Agent

Investigates:

```text
Libraries
Frameworks
APIs
Documentation
Existing code
Compatibility
Known technical approaches
Potential risks
```

This agent should be able to browse documentation and inspect the repository.

---

### D. Architect Agent

Produces:

```text
System architecture
Data model
Module boundaries
API design
Infrastructure
Security model
Technology choices
Testing strategy
Deployment strategy
```

It should also identify architectural decisions that future coding agents must follow.

---

### E. Planner Agent

Breaks the architecture into a dependency graph.

Example:

```text
EPIC-01 Authentication
 ├── TASK-01 database schema
 ├── TASK-02 user model
 ├── TASK-03 registration API
 ├── TASK-04 login API
 ├── TASK-05 session handling
 └── TASK-06 authentication tests

EPIC-02 Billing
 ├── TASK-07 subscription model
 ├── TASK-08 payment provider
 ├── TASK-09 webhook handler
 └── TASK-10 billing tests
```

But importantly, every task should have:

```text
Purpose
Dependencies
Inputs
Expected changes
Acceptance criteria
Verification commands
Risk
Priority
Estimated complexity
Rollback strategy
```

---

### F. Coding Agent

The implementer.

Its context should not simply be:

> "Implement TASK-14."

It should receive:

```text
Project specification
Relevant architecture
Task definition
Dependencies
Relevant files
Current repository state
Existing decisions
Previous attempts
Tests
Known failures
Coding standards
```

Then it works inside an isolated environment.

---

### G. Test Agent

Responsible for verification.

It should execute:

```text
Unit tests
Integration tests
Type checks
Lint
Build
Static analysis
End-to-end tests
Smoke tests
```

It reports structured evidence.

Not:

> "Looks good."

But:

```json
{
  "status": "failed",
  "tests": 4,
  "passed": 3,
  "failed": 1,
  "failure": "...",
  "suspected_root_cause": "...",
  "recommended_next_action": "..."
}
```

---

### H. Debugger Agent

Receives actual evidence:

```text
code diff
stack trace
test failures
logs
environment information
```

Then determines:

```text
Root cause
Potential fixes
Risk of fix
Files affected
Tests required afterward
```

---

### I. Code Reviewer Agent

Reviews the implementation independently.

It should inspect:

```text
Correctness
Maintainability
Architecture consistency
Security
Performance
Error handling
Test quality
Unexpected side effects
```

This is important because the model that wrote the code should not always be the final judge of its own work.

---

### J. Security Agent

Checks:

```text
Secrets
Authentication
Authorization
Injection vulnerabilities
Unsafe dependencies
Data exposure
Filesystem access
Command execution
Network access
Configuration mistakes
```

---

### K. QA / Product Validation Agent

This asks:

> "Did we build the thing the user actually requested?"

This prevents a dangerous failure mode:

```text
All tests pass
but
the product doesn't actually satisfy the original goal.
```

---

### L. Release Agent

Once the project reaches a release candidate:

```text
Build
Package
Migration verification
Deployment checks
Environment validation
Release notes
Versioning
Rollback plan
```

---

# 6. The central autonomous loop

This is the heart of the project.

The system should continuously execute:

```text
OBSERVE
   ↓
UNDERSTAND
   ↓
PLAN
   ↓
SELECT NEXT ACTION
   ↓
EXECUTE
   ↓
VERIFY
   ↓
ANALYZE
   ↓
UPDATE STATE
   ↓
REPLAN
   ↓
REPEAT
```

The most important word here is:

## REPLAN

Most weak agent systems assume:

> "The original plan is correct."

Your system should assume:

> "The plan is a hypothesis about how the project will be built."

Every significant observation can invalidate it.

---

# 7. Give the system a persistent "Project Brain"

A 24-hour agent cannot rely on the context window.

This is one of the central engineering problems in long-running agents. Anthropic's work on long-running coding agents explicitly describes the need for persistent artifacts across context windows, while OpenAI's long-horizon Codex work similarly emphasizes externalized state and disciplined verification loops.

Your system should therefore have a persistent project state.

For example:

```text
.agents/
    project.json

    requirements/
        specification.md

    architecture/
        architecture.md
        decisions.md

    planning/
        roadmap.json
        task_graph.json

    memory/
        facts.json
        discoveries.md

    execution/
        current_run.json
        events.jsonl

    verification/
        test_results/
        review_results/

    checkpoints/
        checkpoint-001.json

    agent_logs/
        planner/
        architect/
        coder/
        tester/
```

The repository becomes part of the agent's memory.

---

# 8. Use "context reconstruction", not giant prompts

Never keep stuffing everything into one context window.

At the beginning of every agent session, construct context dynamically.

For example:

```text
PROJECT GOAL
+
CURRENT TASK
+
RELEVANT ARCHITECTURE
+
RELEVANT FILES
+
DEPENDENCIES
+
RECENT EVENTS
+
CURRENT FAILURES
+
PREVIOUS ATTEMPTS
+
PROJECT RULES
```

This is much more scalable than giving every model the entire repository and entire conversation.

---

# 9. Event-sourced execution

Every important action should produce an event.

Example:

```json
{
  "event": "task.completed",
  "task_id": "TASK-42",
  "agent": "coder",
  "timestamp": "...",
  "commit": "a81c...",
  "verification": {
    "tests": 128,
    "passed": 128
  }
}
```

Then:

```text
User goal
    ↓
Events
    ↓
Current state
```

This provides:

```text
Observability
Recovery
Debugging
Replay
Auditability
```

It also makes your 1–2 day runs dramatically easier to recover.

---

# 10. Git should be built into the architecture

Every meaningful task should have a Git checkpoint.

For example:

```text
TASK-001
   ↓
implementation
   ↓
verification
   ↓
commit
```

Use worktrees for parallel work:

```text
main
 ├── worktree/task-101
 ├── worktree/task-102
 ├── worktree/task-103
 └── worktree/task-104
```

Then the orchestrator can merge only validated work.

This is much safer than having 5 agents randomly editing the same working tree.

---

# 11. Introduce a Task State Machine

Every task should exist in a known state.

Example:

```text
QUEUED
  ↓
READY
  ↓
ASSIGNED
  ↓
IMPLEMENTING
  ↓
VERIFYING
  ↓
REVIEWING
  ↓
COMPLETED
```

Failure transitions:

```text
VERIFYING
    ↓
FAILED
    ↓
DIAGNOSING
    ↓
REPAIRING
    ↓
VERIFYING
```

Architecture problem:

```text
FAILED
  ↓
ARCHITECTURE_REVIEW
  ↓
REPLAN
```

This prevents your agent from entering an undefined situation.

---

# 12. Don't make the agents blindly trust each other

Create an evidence hierarchy.

For example:

```text
User requirement
        ↓
Acceptance criteria
        ↓
Executable tests
        ↓
Observed runtime result
        ↓
Agent reasoning
```

An agent saying:

> "This should work."

should have very little authority.

A test saying:

```text
PASS
```

has much more authority.

Your system should favor **observable evidence over model claims**.

---

# 13. Add a "Definition of Done" engine

Every task needs machine-checkable completion criteria.

Example:

```text
Task:
Add JWT authentication.

Definition of Done:

[ ] login endpoint exists
[ ] refresh mechanism exists
[ ] invalid token rejected
[ ] expired token rejected
[ ] protected endpoint tested
[ ] unauthorized requests rejected
[ ] tests pass
[ ] typecheck passes
[ ] security review passes
```

Then the orchestrator determines whether the task is actually complete.

---

# 14. Add an "Unknowns Queue"

This is a potentially very powerful feature.

Agents constantly discover unknowns:

```text
Which database strategy should we use?

Does library X support this?

Is this API compatible?

Why does this test fail only in production mode?
```

Instead of forcing the current agent to solve everything immediately, create:

```text
UNKNOWN-001
UNKNOWN-002
UNKNOWN-003
```

The system can route them to research or specialist agents.

This prevents the main development flow from becoming confused.

---

# 15. Add confidence and escalation

The autonomous system should NOT be forced to guess.

Every important decision should have something like:

```text
confidence: 0.87
evidence:
    test results
    documentation
    code inspection
```

And define escalation conditions.

For example:

```text
Low-risk:
agent proceeds

Medium-risk:
agent performs extra verification

High-risk:
agent asks human
```

Examples of high-risk actions:

```text
Destroy production data
Modify payment behavior
Expose secrets
Delete large parts of the repository
Deploy externally
Change security boundaries
```

The goal is not:

> "Never ask the human anything."

The goal is:

> "Never ask the human about things the system can safely decide itself."

---

# 16. Add a Human Intervention Layer

Your CLI should therefore have two modes.

## Autonomous mode

```bash
auto build
```

The system runs continuously.

## Supervised mode

```bash
auto build --approval-gates
```

The system pauses at selected decision boundaries.

And perhaps:

```bash
auto pause
auto resume
auto approve
auto reject
auto rollback
auto inspect
```

---

# 17. The user should see the system's "thought process" as operations, not hidden reasoning

Do not expose private chain-of-thought.

Instead expose structured operational information:

```text
PROJECT
└── Building SaaS platform

CURRENT PHASE
└── Authentication

CURRENT TASK
└── Implement refresh-token rotation

AGENT
└── Coder-02

STATUS
└── Running integration tests

VERIFICATION
✓ unit tests
✓ typecheck
✗ refresh-token test

NEXT ACTION
└── Debugger investigating failed integration test
```

This gives the user transparency without depending on hidden reasoning.

---

# 18. Multi-model architecture

Do not hard-code your system around one model provider.

Build:

```text
Model Router
    ├── Provider A
    ├── Provider B
    ├── Provider C
    └── Local model
```

Then specialize model selection.

For example:

```text
Planning
    → reasoning-oriented model

Architecture
    → high-context reasoning model

Coding
    → coding-specialized model

Fast classification
    → cheap fast model

Testing analysis
    → inexpensive reasoning model

Security review
    → specialized model

Final review
    → independent model
```

The exact providers should remain configurable.

---

# 19. The model router becomes intelligent

Eventually the router itself can consider:

```text
task type
task complexity
context size
latency
cost
past success rate
failure rate
language
repository type
```

Then choose a model.

Example:

```text
Task complexity = high
Security sensitivity = high
Required context = 180k
Historical success = high
Cost budget = available

→ route to Model X
```

This can become a substantial differentiating component.

---

# 20. Add model disagreement

For important decisions:

```text
Architect A
      +
Architect B
      ↓
Decision Judge
```

Instead of assuming one model is correct.

For example:

```text
Architecture proposal A
Architecture proposal B
Architecture proposal C

        ↓

Decision evaluator

        ↓

Selected architecture
```

This is particularly useful for decisions where there isn't an immediate executable test.

---

# 21. Add "independent verification"

One of the strongest design patterns is:

```text
Agent A builds it.

Agent B verifies it.

Agent C attacks it.

Agent D decides whether the evidence is sufficient.
```

Do not let:

```text
coder → "done"
```

become:

```text
system → done
```

Completion should require evidence.

---

# 22. Parallelization

Your system should recognize independent tasks.

Example:

```text
Frontend
Backend
Database
Documentation
Testing
```

can sometimes run simultaneously.

The orchestrator should construct:

```text
Dependency Graph

        API design
       /    |     \
Frontend   Backend  Docs
              |
           Database
```

Only dependencies should block execution.

This can turn:

```text
10 hours sequential
```

into:

```text
4 hours parallel
```

when the work is genuinely independent.

---

# 23. But parallel agents must not become chaos

Introduce a shared-resource lock system.

For example:

```text
database/schema.prisma
    LOCKED BY task-23
```

or:

```text
shared resource:
    package.json
```

The orchestrator can prevent conflicting simultaneous modifications.

This is one of the differences between a real engineering system and simply launching several coding agents.

---

# 24. Add automatic recovery

Assume everything will fail.

Your runtime should detect:

```text
Model failure
API timeout
Rate limit
Container crash
Agent loop
Bad patch
Test failure
Git conflict
Dependency failure
Corrupted state
Network failure
```

Then:

```text
checkpoint
    ↓
resume
```

A 30-hour project must not die because one API call failed at hour 17.

---

# 25. Checkpointing

Create automatic checkpoints:

```text
Checkpoint 1
Checkpoint 2
Checkpoint 3
...
```

Each checkpoint contains:

```text
Git commit
Task graph
Project state
Agent state
Verification state
Current objective
Outstanding failures
Environment metadata
```

Then recovery becomes:

```text
load checkpoint
↓
reconstruct context
↓
verify repository
↓
resume orchestration
```

---

# 26. Time should not determine completion

Don't tell the system:

> "Work for 24 hours."

Tell it:

> "Continue until the project reaches the completion criteria."

Time becomes a runtime budget, not the definition of success.

For example:

```text
Maximum runtime: 48 hours
Maximum token budget: $250
Maximum parallel agents: 8

Stop when:
all release criteria pass
OR
human escalation required
OR
budget exhausted
OR
system detects unrecoverable state
```

---

# 27. Add a "Project Constitution"

At project initialization, generate a document that defines:

```text
Coding standards
Architecture rules
Security requirements
Testing requirements
Dependency policy
Git policy
Deployment policy
Agent permissions
Definition of done
```

Every agent reads the applicable rules.

This becomes the shared law of the project.

Example:

```text
.agents/constitution.md
```

---

# 28. Add an Agent Memory system

Separate memory into categories.

### Permanent project facts

```text
PostgreSQL is the production database.
```

### Architecture decisions

```text
Authentication uses server-side sessions.
```

### Temporary task context

```text
Task-42 is debugging a websocket timeout.
```

### Lessons learned

```text
Library X causes issue Y under Node 24.
```

This is much better than storing one giant conversation transcript.

---

# 29. Add "failure memory"

This is a very important improvement.

Suppose the system tries:

```text
Solution A
```

and it fails.

It should record:

```text
Attempt A
Reason for failure
Evidence
```

so that future agents don't repeatedly rediscover the same failed approach.

Example:

```text
FAILED APPROACH

Replacing library X with Y

Reason:
Y does not support streaming transactions.

Do not retry unless assumptions change.
```

That turns repeated failure into institutional memory.

---

# 30. Add self-evaluation

At the end of every major milestone:

```text
Goal
 ↓
Implementation
 ↓
Tests
 ↓
Independent review
 ↓
Goal-vs-result evaluation
```

Then ask:

```text
How far are we from the original objective?
What remains?
What assumptions changed?
What new risks appeared?
```

This is far more important than simply counting completed tasks.

---

# 31. Add milestone planning

Instead of one giant project plan:

```text
Project
 ├── Milestone 1
 ├── Milestone 2
 ├── Milestone 3
 ├── Milestone 4
 └── Release
```

Each milestone can have its own:

```text
requirements
architecture validation
tasks
tests
review
checkpoint
```

This makes long-running execution more stable.

---

# 32. Add automatic project reprioritization

Suppose the system discovers:

```text
Database architecture is causing major performance problems.
```

The system should be allowed to change priorities:

```text
OLD:

Feature 8
Feature 9
Feature 10

NEW:

Database redesign
Performance benchmark
Feature 8
Feature 9
Feature 10
```

The planner is therefore continuously maintaining the roadmap.

---

# 33. The CLI interface

The CLI should be extremely simple.

For example:

```bash
auto init
```

Then:

```bash
auto run "Build a production SaaS application for..."
```

The system could show:

```text
╭──────────────────────────────────────────╮
│ AUTONOMOUS ENGINEERING SYSTEM            │
╰──────────────────────────────────────────╯

Project: SaaS Platform

Planning...
✓ Requirements generated
✓ Architecture generated
✓ 73 tasks created
✓ 41 dependencies detected
✓ 11 tasks can run in parallel

Starting autonomous execution...

[12:41:08] architect     completed architecture review
[12:43:21] coder-01      implementing authentication
[12:44:03] coder-02      implementing database layer
[12:45:12] tester-01     running integration tests
```

---

# 34. The Prompt Enhance toggle

This should eventually become more sophisticated than a button called "enhance."

Potential modes:

```text
Prompt
  ↓
[Enhance OFF]
  → execute user's request more literally

[Enhance ON]
  → compile intent
```

The enhanced result could contain:

```text
Objective
Requirements
Constraints
Assumptions
Questions
Acceptance criteria
Non-functional requirements
Potential edge cases
Suggested architecture areas
Definition of done
```

Most importantly:

## Do not silently invent product requirements.

The system should label assumptions:

```text
ASSUMPTION
The application requires email/password authentication.

SOURCE
Implied by request.

CONFIDENCE
Medium.

ACTION
Proceeding unless contradicted.
```

---

# 35. A better concept than "Prompt Enhancer"

Eventually the UI could call it:

## Intent Compiler

Because you are doing something larger than prompt improvement.

You are translating:

```text
human language
        ↓
machine-operational specification
```

That is a much stronger product concept.

---

# 36. The autonomous manager's decision cycle

Every cycle could resemble:

```text
1. Read current project state.

2. Determine project objective.

3. Inspect task graph.

4. Identify blocked tasks.

5. Identify ready tasks.

6. Inspect recent events.

7. Determine whether architecture remains valid.

8. Select tasks to execute.

9. Assign models/agents.

10. Execute inside isolated environments.

11. Collect evidence.

12. Update task state.

13. Detect failures.

14. Spawn debugging/research/review agents.

15. Re-plan.

16. Commit validated changes.

17. Check milestone status.

18. Determine whether to continue or stop.
```

This is effectively the operating system of your autonomous development environment.

---

# 37. The system should have an explicit "Stop Engine"

An autonomous system needs to know when **not** to continue.

Possible stop conditions:

```text
PROJECT_COMPLETE
HUMAN_APPROVAL_REQUIRED
BUDGET_EXCEEDED
RUNTIME_LIMIT
UNRECOVERABLE_FAILURE
ENVIRONMENT_FAILURE
SAFETY_BLOCK
REPEATED_FAILURE
```

Especially important:

```text
REPEATED_FAILURE
```

Example:

```text
Attempt 1 → failed
Attempt 2 → failed
Attempt 3 → same root cause

STOP

Escalate rather than endlessly retrying.
```

---

# 38. Add a "research before coding" policy

The system shouldn't immediately code everything.

For complex tasks:

```text
Research
 ↓
Architecture
 ↓
Prototype / spike
 ↓
Decision
 ↓
Implementation
```

This can prevent enormous amounts of wasted code.

---

# 39. Add an "Architecture Review Board"

For major changes:

```text
Researcher
Architect
Security
Performance
Coder
```

submit evidence to:

```text
Decision Agent
```

Example:

```text
Should we use PostgreSQL or MongoDB?

Architecture proposal A
Architecture proposal B
Benchmarks
Security analysis
Operational complexity

→ decision record
```

The system stores the decision permanently.

---

# 40. Add project-level metrics

Your UI should eventually measure:

```text
Tasks completed
Tasks failed
Tasks retried
Test success rate
Model usage
Token usage
Estimated cost
Runtime
Parallelism
Code churn
Review findings
Security findings
Replanned tasks
Human interventions
```

This lets you improve the system scientifically.

---

# 41. Build an evaluation system from day one

This is extremely important.

Create benchmark projects such as:

```text
Project A:
REST API

Project B:
SaaS dashboard

Project C:
CLI tool

Project D:
E-commerce backend

Project E:
Existing repository refactor
```

Measure:

```text
Did it finish?
How long?
How much did it cost?
How many retries?
How many bugs?
How many human interventions?
How much code was thrown away?
```

Without evaluations, you won't know whether each orchestration improvement actually helps.

---

# 42. Your MVP should NOT contain everything

Your ultimate vision is large.

Your first version should be surprisingly small.

## MVP v0.1

Build only:

```text
CLI
+
Intent Compiler
+
Planner
+
Coder
+
Tester
+
Orchestrator
+
Git checkpoints
+
Persistent task state
```

Architecture:

```text
User
 ↓
Intent Compiler
 ↓
Planner
 ↓
Task Queue
 ↓
Orchestrator
 ↓
Coder
 ↓
Tests
 ↓
Result
 ↓
Orchestrator
 ↓
Next task
```

No 15-agent ecosystem yet.

---

# 43. MVP v0.2

Add:

```text
Architect
Debugger
Code Reviewer
Replanning
Failure memory
Context reconstruction
```

Now it becomes genuinely interesting.

---

# 44. MVP v0.3

Add:

```text
Parallel tasks
Git worktrees
Model routing
Independent verification
Budget control
Checkpoint recovery
```

At this point you have a legitimate autonomous engineering runtime.

---

# 45. v0.4 — long-running mode

Now specifically solve your 24–48 hour vision.

Implement:

```text
Persistent daemon
Event log
Checkpointing
Context resets
Automatic resume
API retry
Provider fallback
Task scheduler
Heartbeat
Dead-agent detection
Resource limits
```

The process should survive:

```text
terminal closing
model session ending
context window ending
API failure
temporary network outage
agent crash
machine restart
```

That is the difference between:

> "an agent that happened to run for a long time"

and:

> "a system engineered to run for a long time."

---

# 46. v0.5 — multi-agent organization

Introduce:

```text
Engineering Director

├── Product Manager
├── Researcher
├── Architect
├── Planner
├── Coding agents
├── Testing agents
├── Debugging agents
├── Security agent
├── Reviewer
└── Release agent
```

The Director dynamically creates/assigns work rather than running every agent in a fixed pipeline.

---

# 47. v1.0 — Autonomous Engineering Operating System

At this point, your product isn't really "a CLI" anymore.

It becomes:

## An Autonomous Engineering Runtime

The CLI is just the control surface.

The runtime contains:

```text
Orchestrator
Task engine
Agent runtime
Model router
Memory
Project state
Sandbox
Git manager
Verification engine
Event system
Checkpoint system
Budget manager
Security policy
Observability
```

The CLI simply controls it.

---

# 48. Recommended architecture

A practical architecture could look like:

```text
                    ┌───────────────────┐
                    │       CLI         │
                    └─────────┬─────────┘
                              │
                              ▼
                    ┌───────────────────┐
                    │  Project Manager  │
                    └─────────┬─────────┘
                              │
                    ┌─────────▼─────────┐
                    │ Intent Compiler   │
                    └─────────┬─────────┘
                              │
                    ┌─────────▼─────────┐
                    │ Engineering       │
                    │ Director          │
                    └─────────┬─────────┘
                              │
                 ┌────────────▼────────────┐
                 │     Task Graph Engine   │
                 └────────────┬────────────┘
                              │
          ┌───────────────────┼───────────────────┐
          │                   │                   │
          ▼                   ▼                   ▼
     Researcher           Architect            Planner
          │                   │                   │
          └───────────────────┼───────────────────┘
                              │
                              ▼
                       Model Router
                              │
            ┌─────────────────┼─────────────────┐
            ▼                 ▼                 ▼
         Model A           Model B           Model C
            │                 │                 │
            └─────────────────┼─────────────────┘
                              │
                              ▼
                       Agent Runtime
                              │
               ┌──────────────┼──────────────┐
               ▼              ▼              ▼
             Coder          Tester          Reviewer
               │              │              │
               └──────────────┼──────────────┘
                              ▼
                       Evidence Engine
                              │
                ┌─────────────┴─────────────┐
                ▼                           ▼
             Success                      Failure
                │                           │
                ▼                           ▼
             Commit                    Debugger
                                            │
                                            ▼
                                         Replan
                                            │
                                            └──────→ LOOP
```

---

# 49. Infrastructure underneath

A strong first implementation could use:

```text
CLI:
Python + Typer/Rich
```

or:

```text
TypeScript + Node
```

For a first prototype, Python is particularly convenient because agent orchestration, subprocess execution, Git, HTTP APIs, and data handling are easy to integrate.

Persistence:

```text
SQLite
```

for the early version.

Later:

```text
PostgreSQL
```

Sandbox:

```text
Docker
```

Git:

```text
git worktrees
```

Events:

```text
JSONL initially
```

Later:

```text
event database / message broker
```

Observability:

```text
structured logs
OpenTelemetry
metrics
```

Models:

```text
provider adapters
```

So you can swap:

```text
OpenAI
Anthropic
Google
local models
other providers
```

without rewriting the orchestrator.

---

# 50. Important design rule: the orchestrator must be deterministic where possible

Do not let an LLM decide everything.

Use normal software for:

```text
Task state transitions
Dependency checks
Git operations
Locking
Retries
Timeouts
Budget enforcement
Permissions
Process management
Checkpointing
Scheduling
```

Use LLMs for:

```text
Planning
Reasoning
Architecture
Code generation
Diagnosis
Research
Review
Decision proposals
```

This division is extremely important.

Your system should be:

```text
Deterministic infrastructure
+
Probabilistic intelligence
```

not:

```text
LLM controls everything
```

---

# 51. The "AI pretending to be the user" idea should be reframed

What you described as:

> "the model which will act like me and tell other models what to do"

is actually a great concept, but I would define it as:

## Delegation Agent

The Delegation Agent represents the user's objective.

It does not impersonate the user in a deceptive sense.

Its job is:

```text
User intention
       ↓
Delegation Agent
       ↓
Decides what work should happen next
       ↓
Delegates to specialist agents
       ↓
Evaluates result
       ↓
Delegates next action
```

This becomes the central intelligence of your system.

---

# 52. The really interesting version of your idea

I think your strongest version is not:

> "multiple AI agents coding together."

That already exists as a direction in the industry. Anthropic, for example, has published work on agent teams working in parallel, while OpenAI has publicly described multi-agent and long-running Codex infrastructure.

Your more interesting proposition is:

> **A self-managing software organization for a single project.**

The user gives it:

```text
WHAT
```

The system determines:

```text
WHY
WHAT NEXT
WHO SHOULD DO IT
HOW IT SHOULD BE DONE
HOW TO VERIFY IT
WHEN IT FAILED
HOW TO RECOVER
WHEN THE PLAN SHOULD CHANGE
WHEN THE PROJECT IS ACTUALLY DONE
```

That is a much stronger product definition.

---

# 53. Your differentiator: "Project-level autonomy"

Most agent demos focus on:

```text
Can the model perform this task?
```

Your project should focus on:

```text
Can the system manage the entire project?
```

That means your benchmark should not be:

> "Can the AI write a function?"

It should be:

> "Can the system take a vague product request and independently transform it into a verified software project over many hours while maintaining coherent state?"

That is the research/product question worth pursuing.

---

# 54. Another potentially powerful feature: autonomous task creation

Do not require every task to exist before execution begins.

Allow:

```text
TASK-17
   ↓
Agent discovers hidden requirement
   ↓
creates TASK-17.1
creates TASK-17.2
creates TASK-17.3
   ↓
planner incorporates them
```

The task graph can therefore evolve during development.

This is critical for real-world projects.

---

# 55. Another powerful feature: "goal drift detection"

The system should compare:

```text
Original intent
      vs
Current implementation
```

and detect:

```text
feature drift
architecture drift
scope drift
quality drift
```

Example:

```text
Original:
real-time collaboration

Current:
polling every 10 seconds

WARNING:
implementation does not satisfy original real-time requirement.
```

The system then triggers rework.

---

# 56. Another powerful feature: "self-generated tests"

For requirements that don't have tests, the system should create them.

Flow:

```text
Requirement
   ↓
QA agent
   ↓
Acceptance test
   ↓
Implementation
   ↓
Execution
```

This turns natural-language requirements into executable verification.

---

# 57. Another powerful feature: "red team before release"

Before declaring success:

```text
Builder
   ↓
Reviewer
   ↓
Red Team
   ↓
Fix
   ↓
Test
   ↓
Release
```

The Red Team should actively attempt to break:

```text
functionality
security
performance
edge cases
user flows
data integrity
```

This is much stronger than asking the coder:

> "Did you make it correctly?"

---

# 58. Another powerful feature: "economic autonomy"

Give the Director a budget.

For example:

```text
Budget:
$100

Remaining:
$63.40
```

Then let it optimize:

```text
Is this task worth using a large model?

Can a smaller model handle this?

Should two approaches be tested?

Should we spend more tokens to reduce failure risk?
```

Now your system is managing not only engineering work, but the **economics of engineering work**.

---

# 59. Another powerful feature: "agent reputation"

Track historical performance.

Example:

```text
Coder-A
success rate: 91%

Coder-B
success rate: 83%

Reviewer-A
false-positive rate: 8%

Debugger-C
average repair cycles: 1.4
```

Then the router learns which agent/model is effective for which type of task.

This can become a feedback loop:

```text
execution
   ↓
measurement
   ↓
model routing update
   ↓
better future execution
```

---

# 60. Another powerful feature: "architecture memory"

Over many projects, the system could learn patterns such as:

```text
For projects of this type,
architecture X historically performed better.
```

But this should be evidence-based rather than blindly learning from previous model opinions.

---

# 61. Security architecture is not optional

Because your system will eventually have permission to:

```text
read files
write files
execute commands
access network
install packages
possibly access cloud services
possibly deploy
```

the agent runtime should be sandboxed.

Current autonomous-agent incidents have also highlighted how important containment and monitoring are for systems given broad execution capabilities. Nvidia recently introduced an agent-safety stack specifically aimed at sandboxing and monitoring autonomous agents.

For your system:

```text
Agent
 ↓
Sandbox
 ↓
Filesystem policy
 ↓
Command policy
 ↓
Network policy
 ↓
Secret policy
```

Don't give every agent unlimited host access.

---

# 62. Permission classes

Every agent should have explicit permissions.

Example:

```text
RESEARCHER
read-only repository
internet allowed
no Git writes

ARCHITECT
read repository
write architecture artifacts
no production commands

CODER
read/write worktree
run tests
limited network

TESTER
read/write test artifacts
run build/test commands

RELEASE AGENT
restricted deployment permissions
```

This is much safer than a single all-powerful agent.

---

# 63. The killer demo

The best demonstration of your project would NOT be:

> "Look, the AI wrote a TODO app."

Instead:

```text
User:

Build me a production-grade SaaS application for X.

Requirements:
...

Start autonomous mode.
```

Then:

```text
0:00
Intent compiled

0:03
Architecture created

0:06
117 tasks generated

0:07
18 independent tasks detected

0:08
8 agents started

...

2:14
Authentication complete

3:51
Billing complete

5:04
Integration testing exposed architecture issue

5:05
Director invalidated 7 tasks

5:08
New architecture generated

...

14:27
Security audit found 3 vulnerabilities

15:02
All fixes verified

...

22:41
Release candidate created

23:10
Final end-to-end verification passed

23:14
PROJECT COMPLETE
```

That communicates your idea immediately.

---

# 64. Development roadmap

## Phase 1 — Foundation

Build:

```text
CLI
Model adapter
Agent interface
Tool interface
Task model
SQLite state
Git integration
Basic sandbox
```

Goal:

```text
One agent can safely modify a repository.
```

---

## Phase 2 — Autonomous loop

Build:

```text
Planner
Coder
Tester
Orchestrator
Task state machine
Retry logic
```

Goal:

```text
User gives one request.
System executes multiple tasks without additional prompts.
```

---

## Phase 3 — Long-running runtime

Build:

```text
Checkpoints
Event log
Context reconstruction
Crash recovery
Persistent daemon
Budget controls
```

Goal:

```text
System can run for many hours without losing state.
```

---

## Phase 4 — Multi-agent organization

Build:

```text
Engineering Director
Architect
Researcher
Debugger
Reviewer
Security
QA
Release
Model router
```

Goal:

```text
System manages the engineering workforce autonomously.
```

---

## Phase 5 — Adaptive intelligence

Build:

```text
Dynamic task creation
Dynamic replanning
Parallel execution
Model selection
Failure memory
Goal drift detection
Architecture reviews
Agent reputation
```

Goal:

```text
The system can change its own execution strategy based on evidence.
```

---

## Phase 6 — Production platform

Build:

```text
Web dashboard
Project history
Live agent activity
Cost analytics
Remote execution
Authentication
Secrets management
Deployment integrations
Team collaboration
```

Goal:

```text
Turn the runtime into a real product.
```

---

# 65. What I would build first

Do not start with 20 agents.

Start with this exact pipeline:

```text
User
 ↓
Intent Compiler
 ↓
Planner
 ↓
Task Queue
 ↓
Coder
 ↓
Automated Verification
 ↓
Failure Analyzer
 ↓
Replanner
 ↓
Coder
 ↓
Verification
 ↓
Commit
 ↓
Next Task
```

Then add:

```text
Architect
Reviewer
Parallelization
Persistent runtime
Model router
Security
```

one capability at a time.

This will let you discover which parts actually improve autonomous coding instead of constructing a giant architecture that is impossible to debug.

---

# 66. The core data model

Your first database could be centered around:

```text
Project
Task
TaskDependency
Agent
Run
Execution
Artifact
Event
Checkpoint
Decision
Failure
Verification
Model
Budget
Permission
```

A task should roughly contain:

```json
{
  "id": "TASK-042",
  "title": "Implement refresh token rotation",
  "status": "VERIFYING",
  "priority": 7,
  "dependencies": ["TASK-039"],
  "acceptance_criteria": [],
  "definition_of_done": [],
  "assigned_agent": "coder-02",
  "attempts": 2,
  "risk": "medium",
  "artifacts": [],
  "verification": {},
  "history": []
}
```

---

# 67. The Agent interface

Conceptually, every agent should expose something like:

```python
class Agent:
    name: str

    async def run(self, context) -> Result:
        ...
```

But more importantly, the agent should not own the entire workflow.

The orchestrator owns workflow.

Agent:

```text
reason + act
```

Orchestrator:

```text
decide + schedule + persist + recover
```

That distinction will save you a huge amount of pain later.

---

# 68. The ultimate loop

Your system eventually becomes:

```text
                       ┌───────────────┐
                       │ USER OBJECTIVE│
                       └───────┬───────┘
                               ↓
                       ┌───────────────┐
                       │ INTENT        │
                       │ COMPILER      │
                       └───────┬───────┘
                               ↓
                       ┌───────────────┐
                       │ ENGINEERING   │
                       │ DIRECTOR      │
                       └───────┬───────┘
                               ↓
                       ┌───────────────┐
                       │ TASK GRAPH    │
                       └───────┬───────┘
                               ↓
                     SELECT NEXT ACTION
                               ↓
                  ┌────────────┴────────────┐
                  ↓                         ↓
             RESEARCH                  IMPLEMENT
                  ↓                         ↓
             ARCHITECTURE               VERIFY
                  └────────────┬────────────┘
                               ↓
                          OBSERVATION
                               ↓
                           EVALUATION
                               ↓
                    ┌──────────┴──────────┐
                    ↓                     ↓
                 SUCCESS               FAILURE
                    ↓                     ↓
                 COMMIT                DIAGNOSE
                    ↓                     ↓
               UPDATE STATE           REPAIR
                    ↓                     ↓
                    └──────────┬──────────┘
                               ↓
                            REPLAN
                               ↓
                              LOOP
```

And the loop continues until:

```text
PROJECT COMPLETE
```

rather than:

```text
MODEL FINISHED ITS RESPONSE
```

That distinction is the core of your idea.

---

# 69. Your project thesis

I would describe the project this way:

> **Current coding agents are increasingly capable of executing individual software-engineering tasks. This project explores a different level of autonomy: an AI system that manages an entire software project as a persistent engineering process. The user supplies an objective, while an autonomous director decomposes the objective, delegates work to specialized agents, validates outputs with executable evidence, reacts to failures, modifies the plan when necessary, and continues across sessions until the project reaches its completion criteria.**

And the deeper thesis is:

> **The next unit of AI-assisted software development is not the prompt. It is the project.**

That is the idea I would build around.

---

# 70. One final correction to the original mental model

You originally imagined:

```text
Planner → Architect → Coder → Tester → Planner → ...
```

I would replace that with:

```text
                 PROJECT GOAL
                      ↓
               ENGINEERING DIRECTOR
                      ↓
                CURRENT STATE
                      ↓
                TASK GRAPH
                      ↓
             WHAT SHOULD HAPPEN NOW?
                      ↓
          ┌───────────┼───────────┐
          ↓           ↓           ↓
       Research    Implement    Review
          ↓           ↓           ↓
          └───────────┼───────────┘
                      ↓
                   EVIDENCE
                      ↓
                 WHAT CHANGED?
                      ↓
               IS PLAN STILL VALID?
                  /           \
                YES            NO
                 ↓              ↓
            CONTINUE          REPLAN
                 └──────┬───────┘
                        ↓
                       LOOP
```

That is much more powerful.

The **Planner is no longer the system**.

The **loop is the system**.

And the thing you are really building is not an AI coder—it is a **persistent autonomous engineering runtime that coordinates AI coders**.

This is also why the current industry trajectory is useful validation of your premise: long-running agents, persistent state, subagents, parallel work, and managed execution are all becoming active areas of engineering. OpenAI now explicitly describes managed agents as needing durable execution, tool coordination, context management, and subagents; Anthropic likewise emphasizes harnesses, context resets, and multi-agent teams for long-horizon work.
