You are now responsible for the FINAL MAJOR LAYER of this project:

# BUILD THE FULLY FUNCTIONAL TERMINAL / CONSOLE UI

The autonomous engineering runtime already exists in this workspace.

The backend, orchestrator, agents, task system, model system, persistence, memory, verification, Git integration, recovery, planning, and other core systems have already been implemented.

Your job is NOT to redesign or rebuild those systems.

Your job is to build a professional, interactive, production-quality terminal interface that exposes and controls the existing runtime.

The final result should feel like a serious modern AI coding CLI/TUI inspired by the usability of tools such as OpenCode CLI, KiloCode CLI, and Gemini CLI, while being specifically designed around THIS project's autonomous multi-agent engineering architecture.

The terminal UI is now the main user-facing product.

======================================================================

1. START BY UNDERSTANDING THE EXISTING SYSTEM
   ======================================================================

Before changing anything:

1. Inspect the entire repository.
2. Read `plan.md` and the current implementation.
3. Identify the actual existing architecture.
4. Identify the actual CLI/backend entry points.
5. Identify:

   * orchestrator
   * Engineering Director
   * task engine
   * task graph
   * agents
   * model providers
   * model router
   * tools
   * verification
   * memory
   * event system
   * checkpoints
   * recovery
   * Git system
   * sandbox/security
   * budget/resource system
   * human approval system
   * project state
6. Determine which APIs/interfaces already exist.
7. Reuse those interfaces.

DO NOT create duplicate implementations of existing functionality.

DO NOT create fake data layers just for the UI.

DO NOT replace working backend systems because the UI is easier to build that way.

The UI must become a client/controller of the existing runtime.

======================================================================
2. THE CORE UX GOAL
===================

The terminal should make the autonomous engineering system feel alive.

The user should be able to open the CLI and immediately understand:

* what project is running
* what the system is currently doing
* which agents are active
* which models are active
* which tasks are being worked on
* what tools are being used
* what tests are running
* what succeeded
* what failed
* why the system changed direction
* what the Engineering Director decided to do next
* how much progress has been made
* whether the system is blocked
* whether human intervention is required
* how much time/resources have been consumed

The UI must communicate the autonomous loop clearly.

The user should be able to watch:

PLAN
→ DELEGATE
→ IMPLEMENT
→ VERIFY
→ ANALYZE
→ REPAIR
→ REPLAN
→ CONTINUE

in real time.

======================================================================
3. THIS MUST BE A REAL INTERACTIVE TUI
======================================

Do not implement a static log viewer.

Do not simply print text line-by-line forever.

Build an actual interactive terminal application with:

* keyboard navigation
* panels/views
* scrolling
* selectable tasks
* selectable agents
* selectable runs
* command input
* status indicators
* expandable details
* live updates
* shortcuts
* pause/resume
* approvals
* cancellation
* inspection
* filtering
* logs
* history

The user should feel like they are operating an application, not reading terminal output.

======================================================================
4. UI STRUCTURE
===============

Create a main application layout that can adapt to terminal size.

A strong default layout is:

┌──────────────────────────────────────────────────────────────┐
│ PROJECT / RUN STATUS / MODEL / RUNTIME / BUDGET              │
├───────────────────────┬──────────────────────────────────────┤
│                       │                                      │
│ TASK / AGENT / GRAPH  │      LIVE ACTIVITY / EVENTS          │
│                       │                                      │
│                       │                                      │
├───────────────────────┴──────────────────────────────────────┤
│ CURRENT ACTION / TOOL OUTPUT / VERIFICATION / DETAILS        │
├──────────────────────────────────────────────────────────────┤
│ COMMAND / PROMPT INPUT                                       │
└──────────────────────────────────────────────────────────────┘

The exact layout can change if the existing system or terminal dimensions suggest a better design.

The UI MUST degrade gracefully on small terminals.

Do not assume a huge terminal window.

======================================================================
5. MAIN HEADER
==============

The header should expose important project-level state.

Potential information:

PROJECT
CURRENT MILESTONE
CURRENT RUN
CURRENT TASK
ACTIVE AGENTS
ACTIVE MODELS
RUNTIME
BUDGET
TOKENS
STATUS

Example:

AUTONOMOUS ENGINEERING RUNTIME
Project: Autonomous Engineering Runtime
Milestone: Core Runtime
Run: #17
Agents: 5 active
Models: 3 active
Runtime: 04:31:18
Budget: 62% remaining
Status: AUTONOMOUS

Do not hard-code these values.

Everything must come from actual runtime state.

======================================================================
6. LIVE ACTIVITY FEED
=====================

Create a real-time activity stream.

Events may include:

PROJECT STARTED
PLAN CREATED
TASK CREATED
TASK ASSIGNED
AGENT STARTED
AGENT FINISHED
MODEL CALLED
TOOL STARTED
TOOL FINISHED
TEST STARTED
TEST PASSED
TEST FAILED
FAILURE DETECTED
DEBUGGER STARTED
REPLAN STARTED
REPLAN COMPLETED
CHECKPOINT CREATED
CHECKPOINT RESTORED
REVIEW STARTED
SECURITY CHECK
TASK COMPLETED
TASK BLOCKED
HUMAN APPROVAL REQUIRED
PROJECT COMPLETED

Example display:

[14:02:18] DIRECTOR
Selected TASK-042

[14:02:20] CODER-02 / MODEL-X
Implementing refresh-token rotation

[14:03:11] TOOL
pytest tests/auth/test_refresh.py

[14:03:14] VERIFY
3 passed / 1 failed

[14:03:16] DEBUGGER
Investigating failure

[14:04:01] DIRECTOR
Architecture remains valid
Repair strategy selected

[14:05:28] VERIFY
4 passed

[14:05:31] TASK-042
COMPLETED

This should update live as the runtime produces events.

======================================================================
7. AGENT VIEW
=============

Create a dedicated agent view.

The user should be able to see every currently active agent.

For each agent expose relevant real information such as:

* agent ID
* role
* task
* model
* status
* start time
* elapsed time
* current action
* current tool
* last event
* verification state
* attempt count
* permission profile

Example:

ENGINEERING DIRECTOR
status: ACTIVE
model: Grok / configured model
action: selecting next task

CODER-01
status: RUNNING
task: TASK-041
model: DeepSeek
action: editing repository

TESTER-01
status: RUNNING
task: TASK-040
model: GLM
action: running integration tests

Never fabricate agent activity.

======================================================================
8. MODEL VIEW
=============

Create a model/provider view.

Expose actual currently configured/active models.

Show useful runtime information such as:

* provider
* model
* role
* current task
* request count
* input tokens
* output tokens
* latency
* failure count
* retry count
* estimated cost if available
* health/availability

Where the runtime does not have a metric, do not invent one.

Represent unknown values honestly.

======================================================================
9. TASK GRAPH VIEW
==================

Create a visual task/dependency view.

The user should be able to understand:

* completed tasks
* active tasks
* blocked tasks
* failed tasks
* queued tasks
* dependencies
* current critical path
* milestone

Example:

[✓] TASK-001
[✓] TASK-002
[▶] TASK-003
├── [✓] TASK-004
├── [▶] TASK-005
└── [ ] TASK-006
└── blocked by TASK-005

The exact representation can be textual if terminal constraints make graphical rendering impractical.

Important:
The graph must represent the ACTUAL task graph from the runtime.

======================================================================
10. TASK INSPECTOR
==================

Selecting a task should show detailed information.

Include:

* task ID
* title
* description
* parent
* dependencies
* dependents
* status
* priority
* risk
* assigned agent
* assigned model
* acceptance criteria
* definition of done
* verification history
* failures
* attempts
* related files
* Git commits
* artifacts
* recent events

Allow the user to inspect without stopping execution.

======================================================================
11. PROJECT OVERVIEW
====================

Create a project overview screen.

Show:

PROJECT
OBJECTIVE
CURRENT MILESTONE
OVERALL STATUS
REQUIREMENTS
TASK COUNTS
ACTIVE AGENTS
FAILED TASKS
BLOCKED TASKS
VERIFICATION HEALTH
RECENT EVENTS
CURRENT DIRECTOR ACTION
NEXT SCHEDULED ACTION

Make the project status understandable in a few seconds.

======================================================================
12. ENGINEERING DIRECTOR VIEW
=============================

This is one of the most important views.

The user should be able to see the operational decisions of the Engineering Director without exposing private chain-of-thought.

Do NOT display hidden reasoning.

Instead display structured decision summaries such as:

DIRECTOR DECISION

Current situation:
TASK-042 failed integration verification.

Evidence:
1 test failure
2 related stack traces
architecture dependency unchanged

Decision:
Assign debugger agent.

Reason summary:
Failure appears localized to token rotation implementation.

Next:
Run repair and verification.

This makes the system transparent without exposing private internal reasoning.

======================================================================
13. "WHY IS THE SYSTEM DOING THIS?" VIEW
========================================

Create an explainability/decision inspector based on structured metadata.

For any major action, the user should be able to inspect:

* triggering event
* affected task
* relevant evidence
* decision type
* selected action
* expected result
* resulting verification

Examples:

WHY DID YOU REPLAN?

WHY WAS THIS TASK BLOCKED?

WHY WAS THIS MODEL SELECTED?

WHY WAS A SECOND AGENT STARTED?

WHY DID THE SYSTEM STOP?

WHY IS HUMAN APPROVAL REQUIRED?

The answers should come from persisted structured runtime data.

Do not ask another LLM to invent an explanation for an already completed action unless necessary.

Prefer recorded evidence.

======================================================================
14. TOOL EXECUTION VIEW
=======================

Create a view for active tool execution.

Show:

* command/tool
* agent
* task
* start time
* elapsed time
* status
* exit code
* output

Support scrolling.

For long outputs:

* show the live tail
* allow expanding
* allow viewing full output
* keep UI responsive

Do not freeze the entire UI while a long-running process executes.

======================================================================
15. TERMINAL OUTPUT MUST BE NON-BLOCKING
========================================

This is critical.

The UI must remain responsive while:

* models are generating
* subprocesses are running
* tests execute
* multiple agents execute
* tool calls stream
* network operations occur

Use the appropriate async/background architecture supported by the existing project.

Never let a single blocking model call freeze the entire UI.

======================================================================
16. MULTI-AGENT LIVE ACTIVITY
=============================

The user should be able to watch multiple agents operating at the same time.

Example:

┌ ACTIVE AGENTS ──────────────────────────────┐
│ DIRECTOR   → planning TASK-031             │
│ CODER-01   → editing backend/api.py        │
│ CODER-02   → editing frontend/Auth.tsx     │
│ TESTER-01  → running integration tests     │
│ REVIEWER   → reviewing TASK-029            │
└────────────────────────────────────────────┘

The interface should make parallel work obvious.

======================================================================
17. PROMPT INPUT
================

The user must have a proper prompt/input area.

The user should be able to:

* submit a new project objective
* send additional instructions
* add constraints
* request changes
* ask for status
* request a specific action
* provide feedback
* respond to approval requests

The input system should not accidentally interfere with active agent output.

======================================================================
18. PROMPT ENHANCE / INTENT COMPILER TOGGLE
===========================================

Expose the existing Intent Compiler through the UI.

Provide a visible mode/toggle.

For example:

MODE: [ENHANCE ON]

When enabled:

user prompt
→ Intent Compiler
→ structured intent
→ requirements
→ autonomous execution

When disabled:

user prompt
→ normal execution path

The user should be able to inspect the compiled intent before execution when appropriate.

Do not remove the existing backend functionality.

Wire the actual Intent Compiler into the UI.

======================================================================
19. CHAT + AUTONOMOUS EXECUTION
===============================

The interface should support BOTH:

NORMAL INTERACTION

and

AUTONOMOUS EXECUTION.

The user can talk to the system while the runtime is active.

For example:

User:
"Show me what you're working on."

System:
Current milestone: Authentication
Active tasks: 4
Blocked tasks: 1
Current issue: refresh-token integration failure

Or:

User:
"Pause autonomous execution."

The command should invoke the real pause functionality.

Do not build a fake conversational layer disconnected from the runtime.

======================================================================
20. CONTROL COMMANDS
====================

Provide intuitive keyboard shortcuts and/or slash commands.

Potential commands:

/help
/status
/tasks
/agents
/models
/project
/logs
/events
/failures
/memory
/checkpoints
/verify
/pause
/resume
/cancel
/approve
/reject
/rollback
/inspect
/clear

Use whichever command format best fits the existing CLI architecture.

Support both keyboard shortcuts and textual commands where useful.

======================================================================
21. KEYBOARD SHORTCUTS
======================

Implement a coherent keyboard system.

Potential examples:

q          quit
p          pause/resume
t          tasks
a          agents
m          models
e          events
l          logs
f          failures
c          checkpoints
r          refresh
i          inspect
?          help
Ctrl+C     safe interrupt/cancel behavior

Do not blindly use keys that conflict with text input.

Input mode and navigation mode must be clearly distinguishable.

======================================================================
22. APPROVAL REQUESTS
=====================

Human approval must be handled directly in the UI.

When a critical approval is required:

╭──────────────────────────────────────────────╮
│ HUMAN APPROVAL REQUIRED                     │
├──────────────────────────────────────────────┤
│ Action: Production deployment               │
│ Risk: CRITICAL                               │
│                                              │
│ Reason: release gate requires approval      │
│                                              │
│ [A] Approve   [R] Reject   [I] Inspect      │
╰──────────────────────────────────────────────╯

Use the actual approval backend.

Do not create a fake UI-only approval state.

======================================================================
23. PAUSE / RESUME
==================

The user should be able to pause autonomous execution without corrupting state.

Display:

PAUSING...
PAUSED
RESUMING...
RUNNING

The UI must reflect real runtime state.

======================================================================
24. CANCEL
==========

Provide a safe cancellation flow.

If the user requests cancellation:

* stop new tasks
* signal running operations
* terminate processes according to backend policy
* persist state
* show cancellation progress
* return to a stable state

Do not simply kill the terminal process unless the existing runtime explicitly defines that behavior.

======================================================================
25. CHECKPOINT VIEW
===================

Expose checkpoints.

Show:

* checkpoint ID
* timestamp
* Git commit/state
* milestone
* task state
* verification state
* reason created

Allow inspection.

If rollback/restore is supported by the existing backend, expose it safely.

======================================================================
26. FAILURE CENTER
==================

Create a dedicated failure view.

Show:

* failure ID
* task
* agent
* type
* timestamp
* root cause if known
* attempt count
* current status
* repair action
* verification result

Group failures by:

MODEL
TOOL
CODE
TEST
ENVIRONMENT
DEPENDENCY
ARCHITECTURE
SECURITY
CONCURRENCY
RESOURCE

Make it easy to understand:

WHAT FAILED?
WHY?
WHAT IS THE SYSTEM DOING ABOUT IT?

======================================================================
27. VERIFICATION CENTER
=======================

Create a verification view.

Show:

* tests
* builds
* lint
* type checks
* integration
* E2E
* security
* performance
* acceptance criteria

Display real results.

Example:

VERIFICATION

Unit tests       ✓ 184/184
Integration      ✓ 42/42
Typecheck        ✓
Lint             ✓
E2E              ✗ 1 failure
Security         ✓

Current action:
Debugger repairing E2E failure.

======================================================================
28. REQUIREMENTS VIEW
=====================

The UI should expose requirement progress.

Show:

* requirement ID
* description
* status
* acceptance criteria
* linked tasks
* verification state

Allow the user to see:

USER GOAL
→ REQUIREMENT
→ TASK
→ CODE
→ VERIFICATION

This makes project-level autonomy understandable.

======================================================================
29. MEMORY VIEW
===============

Expose project memory where appropriate.

Show:

* durable facts
* decisions
* discoveries
* lessons
* failure memory
* unknowns

Allow inspection.

Do not make memory editable in ways that bypass the existing persistence/business rules.

======================================================================
30. EVENT TIMELINE
==================

Provide a timeline of the autonomous run.

The user should be able to scroll backward and see:

* when a task started
* what agent handled it
* what tool was used
* what verification occurred
* what failed
* what decision followed
* when replanning happened

This becomes the "flight recorder" for the autonomous system.

======================================================================
31. LIVE RUN VIEW
=================

Create a dedicated run/session view.

Display:

RUN #17
Started: 14:02
Runtime: 04:31
Status: RUNNING

Progress:
Requirements: 34/41
Tasks: 72/98
Completed: 61
Active: 5
Blocked: 2
Failed: 4
Retried: 9

Models:
3 active

Recent:
task → agent → tool → result

This should update continuously.

======================================================================
32. SMALL TERMINAL SUPPORT
==========================

The CLI must remain usable in smaller terminals.

Implement responsive behavior.

For narrow terminals:

* collapse panels
* switch to tabs/views
* reduce secondary metadata
* preserve the main activity feed
* preserve input
* preserve status

Never create a UI that becomes unreadable simply because the terminal is 80 columns wide.

======================================================================
33. COLORS / VISUAL LANGUAGE
============================

Use a consistent visual language.

Suggested semantic states:

SUCCESS
ACTIVE
WARNING
ERROR
BLOCKED
INFO
UNKNOWN

Do not overuse color.

The system must remain understandable without color alone.

Use symbols and text where useful:

✓ success
▶ active
⚠ warning
✗ failure
⏸ paused
○ queued
! approval

Make sure symbols render safely across common terminals.

======================================================================
34. LOG LEVELS
==============

Support different levels of terminal verbosity.

For example:

QUIET
NORMAL
VERBOSE
DEBUG

Normal mode should show important operational activity without overwhelming the user.

Debug mode can expose much more detail.

Do not force users to watch huge shell output continuously.

======================================================================
35. FILTERING
=============

Allow filtering event/activity streams by:

* agent
* task
* model
* tool
* event type
* severity
* current run

Example:

SHOW:
only CODER-02

or:

SHOW:
only failures

The filtering should operate on actual events.

======================================================================
36. SEARCH
==========

Support searching through:

* tasks
* agents
* events
* failures
* decisions
* requirements

The exact implementation can depend on the existing persistence layer.

======================================================================
37. RESPONSIVE EVENT STREAM
===========================

Incoming runtime events must be converted into UI updates through a clear event adapter.

Do not let UI components directly poll random database tables or internal objects.

Prefer a clean flow:

Runtime Event
→ UI Event Adapter
→ UI State Store
→ View

````

This keeps the UI maintainable.

======================================================================
38. UI STATE ARCHITECTURE
======================================================================

Do not put business logic into rendering components.

Separate:

RUNTIME STATE
from
UI STATE
from
RENDERING

Conceptually:

Runtime
→ Event/State Adapter
→ UI State
→ Views

The UI should not become another orchestrator.

======================================================================
39. SINGLE SOURCE OF TRUTH
======================================================================

The backend/runtime remains the source of truth.

The UI is a representation/control layer.

Never maintain a competing fake copy of:

- task status
- agent status
- project status
- budget
- verification
- approval
- checkpoints

Temporary UI state is fine.

Business state must come from the runtime.

======================================================================
40. REAL-TIME UPDATE ARCHITECTURE
======================================================================

Use the best mechanism supported by the existing implementation:

- event stream
- async queue
- pub/sub
- callbacks
- websocket/local event bus
- event polling as fallback

Prefer push/event-driven updates.

Do not create aggressive polling that wastes resources.

If polling is necessary, use sane intervals and change detection.

======================================================================
41. OUTPUT MANAGEMENT
======================================================================

AI output can be huge.

Do not dump every generated token into the terminal.

Instead distinguish:

SUMMARY
DETAILS
RAW OUTPUT

Example:

CODER-02
Implementing authentication middleware

Status:
Running

Summary:
2 files modified

Tool:
pytest

Use a detail view to inspect raw output.

This keeps the main interface readable.

======================================================================
42. MODEL STREAMING
======================================================================

Where supported by the existing provider abstraction, stream model activity.

But do not make raw token streaming the main UI.

Show useful progressive information:

- agent active
- current operation
- tool call
- summary
- result

The user should feel the system working without being buried in token noise.

======================================================================
43. TOOL STREAMING
======================================================================

For long-running tools, show live output safely.

Examples:

pytest
npm build
docker build
cargo test
git operations
browser operations

Allow:

- live tail
- pause scrolling
- resume following
- expand full output

======================================================================
44. DO NOT LEAK PRIVATE MODEL REASONING
======================================================================

The UI must NOT attempt to expose private chain-of-thought.

Instead show:

- structured decisions
- action summaries
- evidence
- tool calls
- test results
- state changes
- failure causes
- next action

This is both safer and more useful for users.

======================================================================
45. ERROR HANDLING IN THE UI
======================================================================

Never let a UI exception terminate the autonomous runtime.

If a rendering component fails:

- capture/log the error
- preserve runtime state
- degrade gracefully where possible
- show a useful error
- allow recovery

The UI should not be able to corrupt orchestration state.

======================================================================
46. UI CRASH VS RUNTIME CRASH
======================================================================

Treat them as separate concerns.

The autonomous runtime should ideally continue safely even if:

- terminal rendering fails
- terminal disconnects
- the UI is restarted

The UI should reconnect to the persisted runtime state.

This is especially important for your long-running execution design.

======================================================================
47. DETACHED / REATTACHED UI
======================================================================

Support the concept of:

runtime continues
↓
terminal closes
↓
runtime keeps running
↓
CLI reopened
↓
UI reconnects
↓
current state restored

Do not assume the terminal process and autonomous runtime must have identical lifetimes.

Use the existing daemon/checkpoint architecture where available.

======================================================================
48. MULTIPLE PROJECT / RUN SUPPORT
======================================================================

If the backend supports multiple projects or runs, the UI should allow selection.

For example:

PROJECTS

1. autonomous-runtime
2. ecommerce-api
3. saas-platform

RUNS

#17 RUNNING
#16 COMPLETED
#15 FAILED

Do not implement this if the backend architecture explicitly supports only one project and there is no clean path yet.

Do not fabricate support.

======================================================================
49. STARTUP EXPERIENCE
======================================================================

When launching the CLI:

1. detect project
2. load runtime state
3. reconcile actual state
4. connect to active run if one exists
5. display current status
6. show recent important activity
7. enter interactive mode

Example:

Connecting to project...

✓ Project loaded
✓ Runtime state loaded
✓ Task graph loaded
✓ 4 agents active
✓ 2 verification jobs active
✓ Run #17 restored

AUTONOMOUS MODE ACTIVE

======================================================================
50. FIRST-RUN EXPERIENCE
======================================================================

For a new project:

START
→ detect project
→ initialize if needed
→ ask for objective through the UI
→ show Intent Compiler mode
→ compile intent
→ show resulting summary
→ start autonomous execution

Keep the process simple.

======================================================================
51. PROJECT PROMPT UX
======================================================================

The central interaction should feel natural.

Example:

┌ INPUT ──────────────────────────────────────┐
│ What do you want to build?                  │
│                                             │
│ > Build a production SaaS application...   │
│                                             │
│ Intent Compiler: ON                         │
│ Mode: AUTONOMOUS                            │
└─────────────────────────────────────────────┘

The user submits once.

The autonomous runtime takes over.

======================================================================
52. USER MESSAGES DURING AUTONOMOUS WORK
======================================================================

The user should be able to send additional context while the runtime continues.

Examples:

"Use PostgreSQL instead."

"Prioritize security before performance."

"Do not implement social login."

"Pause after the current milestone."

The runtime must correctly route these instructions through the existing control architecture.

Do not let arbitrary text silently mutate persisted requirements.

Important changes should become explicit events/decisions.

======================================================================
53. COMMAND PALETTE
======================================================================

A command palette would be highly useful.

For example:

Ctrl+P

Then:

> Tasks
> Agents
> Models
> Failures
> Checkpoints
> Requirements
> Memory
> Events
> Settings
> Pause
> Resume

Make command discovery easy for new users.

======================================================================
54. HELP SCREEN
======================================================================

Implement a clear help screen containing:

- keyboard shortcuts
- commands
- views
- navigation
- autonomous controls
- approval behavior

Do not require users to memorize everything.

======================================================================
55. ACCESSIBILITY / TERMINAL COMPATIBILITY
======================================================================

Avoid relying on visual effects that only work in one terminal.

Support common terminal environments.

Ensure:

- text remains legible
- symbols degrade reasonably
- no broken layout on standard ANSI terminals
- input remains usable
- logs remain usable in plain mode

Provide a non-interactive/plain-output mode if the architecture already supports CLI automation.

======================================================================
56. NON-INTERACTIVE MODE
======================================================================

Do not destroy scriptability.

The project should ideally still support:

```bash
auto run ...
auto status
auto verify
````

without launching the full TUI.

The interactive TUI and traditional CLI commands should coexist.

======================================================================
57. SCRIPT / CI SAFETY
======================

The TUI must never accidentally launch when the process is clearly running in a non-interactive environment.

Detect appropriate terminal capabilities.

Provide a plain-output fallback when necessary.

======================================================================
58. CONFIGURATION
=================

Expose UI configuration where appropriate:

* theme
* compact mode
* verbosity
* refresh behavior
* default view
* keybindings
* logging
* timestamps
* token/cost display

Do not create a giant settings subsystem unless necessary.

======================================================================
59. PERFORMANCE
===============

The UI must remain fast with large projects.

It must handle:

* thousands of events
* many tasks
* multiple agents
* long logs
* long-running sessions

Do not render the entire event history from scratch every update.

Use:

* bounded buffers
* pagination
* virtualization where supported
* incremental updates
* derived state
* lazy loading

======================================================================
60. MEMORY MANAGEMENT
=====================

Do not keep unlimited raw model/tool output in RAM.

Use persistent storage plus bounded UI buffers.

The UI should display recent output and load older output on demand.

======================================================================
61. EVENT ORDERING
==================

Multiple agents will produce events concurrently.

The UI must handle:

* out-of-order arrival
* duplicate events
* late events
* reconnection
* task completion arriving before UI receives intermediate events

Use event IDs/timestamps/sequence information from the backend where available.

Do not assume terminal arrival order always equals causal order.

======================================================================
62. RECONNECTION
================

If the UI loses connection to the runtime:

DISPLAY:

CONNECTION LOST
Reconnecting...

When reconnected:

* reload current state
* reconcile events
* restore active views
* continue observing

The runtime should not be restarted just because the UI reconnects.

======================================================================
63. THEMING
===========

Provide one polished default theme.

Do not waste time building dozens of themes.

Focus on:

* readability
* hierarchy
* status clarity
* minimal visual noise
* professional appearance

======================================================================
64. TESTING THE UI
==================

Do not rely only on manually launching the CLI.

Create automated tests for:

* startup
* rendering
* navigation
* task selection
* agent selection
* event handling
* filtering
* command execution
* pause/resume
* approval flow
* cancellation
* reconnection
* small terminal behavior
* non-interactive fallback

Use the appropriate TUI/CLI testing strategy for the actual framework chosen.

Where useful, use pseudo-terminal/PTY integration tests.

======================================================================
65. TEST REAL BACKEND INTEGRATION
=================================

The UI must be tested against the real runtime interfaces.

Do not mock everything.

Mocks can be used for isolated UI unit tests, but create integration tests that prove:

runtime event
→ UI event adapter
→ UI state
→ rendered state

and:

user action
→ UI command
→ runtime API
→ actual state change
→ UI update

======================================================================
66. NO FAKE EVENTS
==================

Do not build a demo mode into normal operation.

Never fabricate:

* agents
* tasks
* model status
* test results
* token counts
* progress
* verification results
* completion state

Every displayed operational value must originate from real runtime data or be clearly marked as unavailable.

======================================================================
67. NO DUPLICATE BUSINESS LOGIC
===============================

The UI must not independently decide:

* which task should execute
* which agent should execute
* whether a task is complete
* whether tests passed
* whether the project is complete
* whether a model should be routed

Those decisions belong to the runtime.

The UI displays and controls them.

======================================================================
68. DO NOT REBUILD THE BACKEND
==============================

This is one of the most important instructions.

The repository already contains the autonomous engineering functionality.

Only modify backend/runtime code when necessary to expose a clean interface required by the UI.

If an adapter/API is missing:

1. identify the smallest missing interface
2. implement it cleanly
3. keep existing behavior intact
4. add tests
5. wire the UI to it

Do not rewrite working subsystems.

======================================================================
69. UI API / FACADE
===================

Where appropriate, create a clear application-level facade for the UI.

Conceptually:

UI
↓
Runtime Facade
↓
Orchestrator / Services

The facade can expose operations such as:

* get project state
* subscribe to events
* list tasks
* inspect task
* list agents
* inspect agent
* list models
* pause
* resume
* cancel
* approve
* reject
* checkpoint
* rollback
* submit user instruction
* compile intent
* inspect verification

Use the project's existing abstractions when possible.

======================================================================
70. UI NAVIGATION MODEL
=======================

Use a coherent navigation hierarchy.

For example:

HOME
TASKS
AGENTS
MODELS
EVENTS
FAILURES
VERIFICATION
REQUIREMENTS
MEMORY
CHECKPOINTS
PROJECT
HELP

The main screen should always allow quick return to the overview.

======================================================================
71. ACTIVE AGENT DETAIL
=======================

When an agent is selected, show:

AGENT
ROLE
MODEL
TASK
STATUS
TOOLS
PERMISSIONS
CURRENT ACTION
RECENT EVENTS
VERIFICATION
ATTEMPTS
ERRORS
ELAPSED TIME

This is especially important for the multi-model/multi-agent vision.

======================================================================
72. ACTIVE MODEL DETAIL
=======================

When a model is selected, show:

PROVIDER
MODEL
TASK
AGENT
CONTEXT USAGE IF AVAILABLE
REQUEST COUNT
TOKEN USAGE IF AVAILABLE
LATENCY
FAILURES
CURRENT STATUS

Do not show inaccurate token/context information if the provider does not expose it.

======================================================================
73. DIRECTOR TIMELINE
=====================

Provide a focused timeline of high-level system actions.

Example:

14:01 Director initialized project
14:02 requirements created
14:03 architecture created
14:05 task graph generated
14:08 parallel work started
14:24 test failure discovered
14:25 debugger assigned
14:33 architecture revalidated
14:41 task completed
14:43 next milestone started

This allows the user to understand the autonomous process without reading every tool call.

======================================================================
74. MILESTONE VIEW
==================

Expose:

CURRENT MILESTONE
PROGRESS
TASKS
BLOCKERS
VERIFICATION
EXIT CRITERIA

Allow drill-down into individual tasks.

======================================================================
75. RESOURCE VIEW
=================

Expose, where available:

runtime
CPU
memory
disk
active processes
agents
model calls
budget
tokens
cost
concurrency

Keep this secondary to engineering state.

======================================================================
76. RUN COMPARISON
==================

If historical runs are available, allow the user to compare:

* duration
* tasks completed
* failures
* retries
* human interventions
* model usage
* verification
* final result

This can remain a later view if the backend already supports historical execution data.

======================================================================
77. COMPLETION SCREEN
=====================

When the project actually completes:

show a clear final summary.

Example:

╭──────────────────────────────────────────────╮
│ PROJECT COMPLETE                            │
├──────────────────────────────────────────────┤
│ Requirements verified: 41/41               │
│ Tasks completed: 98/98                      │
│ Tests: 412 passed                           │
│ Security: PASS                              │
│ Review: PASS                                │
│ Runtime: 19h 04m                            │
│ Human interventions: 1                      │
│ Checkpoint: release-042                     │
╰──────────────────────────────────────────────╯

Only show numbers supported by actual data.

======================================================================
78. FAILURE STOP SCREEN
=======================

If the project reaches an unrecoverable state:

show:

PROJECT STOPPED

Reason:
Unrecoverable failure

Last task:
TASK-114

Attempts:
3

Evidence:
...

Available actions:

[I] Inspect
[R] Resume/retry
[P] Replan
[C] Checkpoint
[Q] Exit

The exact actions must match the actual runtime capabilities.

======================================================================
79. HUMAN ESCALATION SCREEN
===========================

When the runtime needs the human:

do not simply print:

"Need input."

Instead display:

WHY
CONTEXT
RISK
OPTIONS
RECOMMENDATION IF PROVIDED BY RUNTIME
WHAT HAPPENS NEXT

The user should be able to make the decision quickly.

======================================================================
80. USER EXPERIENCE PRINCIPLE
=============================

The terminal should make the project feel like:

"an AI engineering team working in front of me"

not:

"a chatbot with colored logs."

The user should see:

Planner working
Architect thinking about structure
Coder changing files
Tester running tests
Reviewer examining changes
Debugger repairing failure
Director changing priorities
Multiple models cooperating
System continuing without micromanagement

without exposing private chain-of-thought.

======================================================================
81. REAL AUTONOMOUS STORY IN THE UI
===================================

The UI should naturally communicate this loop:

USER GOAL
↓
INTENT
↓
PLAN
↓
TASK GRAPH
↓
DIRECTOR
↓
AGENT
↓
MODEL
↓
TOOL
↓
CODE
↓
VERIFICATION
↓
EVIDENCE
↓
DECISION
↓
REPLAN
↓
NEXT TASK

This is the defining experience of this product.

======================================================================
82. PLAIN MODE
==============

Maintain a plain terminal output mode for:

* CI
* logging
* pipes
* debugging
* environments without TTY

Example:

[14:02:18] task.created TASK-042
[14:02:20] agent.started coder-02
[14:03:14] verification.failed TASK-042
[14:03:16] debugger.started TASK-042

The full TUI is an enhanced interface, not the only interface.

======================================================================
83. KEEP ARCHITECTURE CLEAN
===========================

Use clear separation:

Runtime
↓
Application Facade
↓
Event Adapter / UI State
↓
Views
↓
Input / Commands

Do not allow UI components to directly manipulate internal persistence structures.

======================================================================
84. DEPENDENCY DISCIPLINE
=========================

Inspect the existing technology stack before choosing a TUI library.

If the project is Python:
consider an appropriate mature terminal UI framework or an extension of the project's existing CLI tooling.

If the project is TypeScript/Node:
consider an appropriate mature TUI framework compatible with the existing architecture.

Do not add a large dependency stack unnecessarily.

Prefer one coherent UI framework over many small UI packages.

======================================================================
85. IMPLEMENTATION ORDER
========================

Build in this order:

PHASE 1
Functional TUI shell

PHASE 2
Runtime connection

PHASE 3
Live event stream

PHASE 4
Task + agent views

PHASE 5
Interactive prompt/input

PHASE 6
Runtime controls

PHASE 7
Verification/failure/checkpoint views

PHASE 8
Intent Compiler integration

PHASE 9
Responsive/small-terminal behavior

PHASE 10
Reconnection + detached runtime behavior

PHASE 11
Testing + performance

PHASE 12
Final UX polish

Do not attempt visual polish before the runtime integration works.

======================================================================
86. FIRST VERTICAL SLICE
========================

The first working slice should prove:

launch CLI
→ connect runtime
→ display project
→ display active task
→ display active agent
→ receive live event
→ update UI
→ user submits command
→ runtime receives command
→ runtime changes state
→ UI updates

Do not move on until this works reliably.

======================================================================
87. SECOND VERTICAL SLICE
=========================

Then prove:

multiple agents
→ multiple events
→ task graph
→ tool execution
→ verification
→ failure
→ debugger
→ replan
→ UI reflects entire sequence

This should become the killer demonstration of the product.

======================================================================
88. THIRD VERTICAL SLICE
========================

Prove:

pause
→ persist
→ terminal exit
→ runtime remains alive if supported
→ relaunch CLI
→ reconnect
→ restore state
→ resume

This proves the TUI is actually integrated with the long-running runtime.

======================================================================
89. PERFORMANCE REQUIREMENT
===========================

The terminal UI must NOT become the bottleneck.

Large event streams, model streaming, tool output, or dozens of agents should not make navigation unusable.

Profile and optimize where needed.

Do not optimize prematurely, but do not ship an obviously laggy interface.

======================================================================
90. FINAL INTEGRATION TEST
==========================

At the end, perform a genuine end-to-end demonstration using the actual runtime.

Start from a project.

Launch the TUI.

Submit an objective.

Enable Intent Compiler.

Start autonomous execution.

Observe:

requirements
architecture
tasks
agents
models
tools
implementation
verification
failure
repair
replanning
next task

Interact with the system while it is running.

Pause it.

Resume it.

Inspect a task.

Inspect an agent.

Inspect a model.

Inspect a failure.

Inspect verification.

Inspect checkpoint.

Reconnect if supported.

Continue execution.

Finally reach the actual project completion state.

The UI must represent the real backend behavior throughout the entire run.

======================================================================
91. DEFINITION OF DONE FOR THE TUI
==================================

The terminal interface is NOT complete merely because it launches.

It is complete when:

[ ] It launches reliably.
[ ] It connects to the actual runtime.
[ ] It shows real project state.
[ ] It shows real tasks.
[ ] It shows real agents.
[ ] It shows real models.
[ ] It shows real events.
[ ] It shows real tool execution.
[ ] It shows real verification.
[ ] It shows real failures.
[ ] It shows real replanning.
[ ] It supports interactive user input.
[ ] Intent Compiler is integrated.
[ ] Pause/resume is integrated.
[ ] Cancellation is integrated where supported.
[ ] Approval flow is integrated.
[ ] Checkpoints are visible.
[ ] Failures are inspectable.
[ ] Task details are inspectable.
[ ] Agent details are inspectable.
[ ] Model details are inspectable.
[ ] The UI can survive/recover from reconnects where the runtime supports it.
[ ] Non-interactive mode still works.
[ ] Small terminals remain usable.
[ ] Automated UI tests exist.
[ ] Backend/UI integration tests exist.
[ ] No fake state is used.
[ ] No critical backend functionality was unnecessarily rewritten.
[ ] The final CLI feels like one coherent application rather than disconnected screens.

======================================================================
92. FINAL QUALITY PASS
======================

After functionality is complete, perform a UX and engineering review.

Look for:

* confusing navigation
* clutter
* excessive output
* poor hierarchy
* broken keyboard behavior
* race conditions
* blocking operations
* stale UI state
* incorrect status indicators
* event-order bugs
* terminal resize bugs
* small-terminal failures
* reconnect failures
* memory leaks
* unnecessary rendering
* duplicated business logic
* missing error handling

Fix the highest-impact issues you find.

======================================================================
93. IMPORTANT — DO NOT STOP AT A BEAUTIFUL MOCK
===============================================

A beautiful fake dashboard is useless.

The requirement is:

REAL RUNTIME
→ REAL EVENTS
→ REAL UI
→ REAL CONTROLS
→ REAL STATE CHANGES

Every important button, key, command, status, task, agent, model, verification result, and progress indicator must connect to actual functionality.

======================================================================
94. FINAL DIRECTIVE
===================

Start by inspecting the current repository.

Understand what is already implemented.

Do not rebuild the autonomous runtime.

Build the terminal UI around it.

Wire every important existing capability into one coherent interactive CLI.

Make the terminal the primary window into the autonomous engineering system.

The final experience should allow a user to:

1. start or open a project
2. submit a goal
3. use Intent Compiler
4. start autonomous execution
5. watch the Engineering Director
6. watch multiple agents
7. watch different models
8. watch tools execute
9. watch code being changed
10. watch tests run
11. watch failures occur
12. watch debugging happen
13. watch replanning happen
14. inspect the task graph
15. inspect requirements
16. inspect decisions
17. inspect failures
18. inspect verification
19. pause
20. resume
21. approve/reject when required
22. inspect checkpoints
23. reconnect after interruption where supported
24. continue execution
25. see verified completion

The interface should make the core idea immediately understandable:

THE HUMAN GIVES THE OBJECTIVE.

THE SYSTEM MANAGES THE ENGINEERING.

Build it to that standard.

Do not merely describe what you built.

Actually implement it, integrate it, test it, run it, and verify it against the real runtime.
