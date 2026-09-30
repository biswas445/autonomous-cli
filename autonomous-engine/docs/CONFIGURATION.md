# Configuration

Configuration lives in the project brain at `.agents/config.json`. `auto init`
writes defaults; every field is overridable.

## Model routes (§18)

Each agent role maps to a provider/model pair with optional fallbacks:

```json
{
  "model_routes": [
    {"role": "coder", "provider": "openai", "model": "gpt-4.1",
     "fallbacks": ["anthropic/claude-sonnet-4", "echo/default"]},
    {"role": "director", "provider": "anthropic", "model": "claude-sonnet-4"}
  ]
}
```

- **Providers built in**: `echo` (offline, deterministic), `openai` (works with
  OpenAI and any OpenAI-compatible endpoint: Ollama, vLLM, LM Studio,
  OpenRouter, ...), `anthropic`.
- Credentials come from the environment only: `OPENAI_API_KEY`,
  `ANTHROPIC_API_KEY`, optionally `OPENAI_BASE_URL` / `ANTHROPIC_BASE_URL`.
  Nothing is ever hardcoded or written to the repo.
- The router retries retriable failures (timeouts, 429/5xx) with backoff, then
  walks the fallback chain.
- The `echo` provider needs no credentials and exercises the entire loop
  offline: `auto run --provider echo --no-director`.
- A locally hosted model endpoint (e.g. Ollama) is refused by the network
  policy unless `AUTO_ALLOW_PRIVATE_ENDPOINTS=true` is set explicitly.

Custom providers implement `ProviderAdapter` (`complete(request, model) ->
ModelResponse`) and register via `register_provider` — no orchestrator changes.

### Capability profiles (v2 §19–22, §115)

Models can advertise their abilities in the capability registry:
`models/capabilities.py` ranks eligible profiles per role (a planner needs
`planning` + `structured_output`; a coder needs `coding` + `tool_use`),
honours context limits, and tracks runtime provider health from real
outcomes. A provider that keeps failing degrades and yields to a healthy
fallback until it recovers. Register a profile to opt a configured model in:

```python
from autonomous_engine.models.capabilities import Capability, ModelProfile, REGISTRY

REGISTRY.register(ModelProfile(
    provider="openai", model="gpt-4.1",
    capabilities={Capability.CODING, Capability.PLANNING, Capability.TOOL_USE,
                  Capability.LARGE_CONTEXT, Capability.STRUCTURED_OUTPUT},
    context_limit=128_000, cost_class="premium",
))
```

## Budgets (§26, §58)

```json
"budget": {
  "max_runtime_seconds": 172800,
  "max_token_budget": 250.0,
  "max_parallel_agents": 4,
  "max_task_attempts": 3
}
```

Time and money are runtime budgets, not definitions of success: exhausting one
produces a stop with a reason (`BUDGET_EXCEEDED` / `RUNTIME_LIMIT`) and a
remediation, never a claim of completion. `max_task_attempts` drives the
repeated-failure escalation (`REPEATED_FAILURE` / `ARCHITECTURE_REVIEW`).

## Permission classes (§62)

Every agent class has explicit least-privilege permissions:

```json
"permission_classes": {
  "coder": {
    "read_repo": true,
    "write_paths": ["src/**", "tests/**"],
    "run_commands": true,
    "allowed_command_globs": ["pytest*", "python*", "npm*"],
    "network": false,
    "git_write": false
  }
}
```

- `write_paths` are globs relative to the project root; writes outside them
  are rejected and reported (`task.edits_rejected`).
- `allowed_command_globs` match the first token or the whole command; commands
  run as argv lists (`shell=False`), so model output cannot inject shell
  metacharacters.
- `network` gates any outbound access; model endpoints additionally pass the
  endpoint policy (http/https only, no loopback/private/link-local/reserved
  hosts unless explicitly opted in).
- `git_write` is reserved for the director and release classes; agents' git
  usage is read-only.

## Run modes (§16)

- `autonomous` (default): the system runs until a stop condition.
- `supervised`: tasks with `risk == "high"` require explicit approval before
  execution. The run stops with `HUMAN_APPROVAL_REQUIRED`; approve with
  `auto approve <id>` (or `auto approve all`), then `auto run` again.
  Approvals persist in `current_run.json`, so approval survives restarts.
  The mode is persisted to config when set via `auto run --supervised`.

## Agent tool surface

Agents call tools mid-reasoning through the agentic tool loop. Every call goes
through the agent's `ToolBox` (the same permission checks as everything else);
inspect what a class may call with `auto tools -c <class>`:

| group | tools | policy gate |
| --- | --- | --- |
| read | `read_file`, `read_files` (batch), `list_dir`, `search`, `glob`, `git_diff` | `read_repo` |
| write | `write_file`, `edit_file` (aider-style SEARCH/REPLACE) | `write_paths` globs |
| exec | `run_command` | `run_commands` + `allowed_command_globs` + risk analyzer |
| memory | `save_memory`, `recall_memory` | always on; secrets refused |
| environment | `current_time`, `budget_status` | always on |
| network | `web_fetch`, `web_search` | `network` + endpoint policy (no loopback/private hosts) |

Notes:

- `save_memory` (gemini's save-memory / kilo's memory) writes typed items into
  the project's memory store with secret redaction; `recall_memory` (kilo's
  recall) searches them. Saved memories are recalled into later agents'
  contexts, so knowledge persists across tasks and sessions.
- Tool-driven writes (`write_file`/`edit_file` during the tool loop) are
  tracked and merged into the task's artifacts, exactly like EditPlan edits.
- `web_fetch` / `web_search` are available to classes with `network: true`
  (the researcher by default); URLs pass the SSRF policy before any request.
  Builders (`BUILDER_TOOLS`) get write + memory + time tools but no network.

## Behaviour flags

| key | default | meaning |
| --- | --- | --- |
| `enhance_prompt` | `true` | Intent Compiler on (§34). `false` executes the request literally. |
| `verify_definitions_of_done` | `true` | DoD engine gates completion. |
| `git_checkpoints` | `true` | commit + checkpoint after each completed task. |
| `worktree_parallelism` | `false` | run independent task waves in git worktrees, merge only verified work. |
| `stop_on_repeated_failure` | `true` | escalate instead of endlessly retrying. |

Change persistent settings with
`auto config --set budget.max_token_budget=500` (or edit `.agents/config.json`).

## Environment variables

| variable | purpose |
| --- | --- |
| `OPENAI_API_KEY` | OpenAI-compatible provider auth |
| `OPENAI_BASE_URL` | alternate OpenAI-compatible endpoint |
| `ANTHROPIC_API_KEY` | Anthropic provider auth |
| `ANTHROPIC_BASE_URL` | alternate Anthropic endpoint |
| `AUTO_ALLOW_PRIVATE_ENDPOINTS` | explicit opt-in for local model endpoints |
| `GIT_TERMINAL_PROMPT` | set to `0` by the git manager (no interactive auth) |

## CLI quick reference

```text
auto init [path] --objective "..."     create the project brain
auto run ["objective"] [--provider echo] [--supervised] [--parallel]
auto status / tasks / events / checkpoints / constitution / config
auto inspect <TASK-id | graph | run>
auto tools -c <agent-class>            show an agent class's tool surface
auto memory [--query TEXT] [--add TEXT] [--consolidate] [--prune]
auto remember <fact> [--kind fact] [--pinned] [--tags a,b]
auto pause / resume / stop             cross-process control
auto approve <id|all> / reject <id|all>
auto rollback <checkpoint-id> --yes
auto reset [--hard --yes]
```
