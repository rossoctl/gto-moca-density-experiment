# Agent trajectories: Claude Code on SWE-bench, light, medium and heavy tasks, two kinds of hands

18 trajectories of a stock coding agent, Claude Code, solving real SWE-bench tasks: three tasks (a light, a medium
and a heavy one) × 3 runs each (reps 1–3) × two kinds of "hands", meaning where the agent's commands ran:

- **container hands** (`runs/container/`, batch `am5-l30-a`): the agent ran its own tools in its own container, on
  the repository there;
- **microVM hands** (`runs/microvm/`, batch `am5-l30-b`): every command the agent ran was sent to a fresh MOCA
  microVM sandbox.

Each trajectory is the sequence of the agent's model calls and tool calls, timed, so it can be read as a profile of
an agent session or replayed as a realistic workload. **Every failed tool call is removed** (see "What was
removed"), so every tool step left succeeded in the run.

## What is here

- `runs/container/` and `runs/microvm/`, three files per run, named `<batch>.<instance>.r<rep>`:
  - `.trajectory.json`: the machine-readable trajectory (the format below);
  - `.timeline.txt`: the same, one line per step, for reading;
  - `.patch`: the run's change to the repository, exactly as collected from the run and graded.
- `repos/<instance>/source/`: the code every run of the task started from.
- `images/`: a container image per task, with the repository and Python environment the commands ran in, and how
  to load it and replay a trajectory (its own `README.md`).

## The tasks

| Weight | SWE-bench instance | Project | Base commit | Project version |
|---|---|---|---|---|
| Light | `django__django-11133` | [django/django](https://github.com/django/django) | `879cc3da6249e920b8d54518a0ae06de835d7373` | 3.0 |
| Medium | `django__django-10914` | [django/django](https://github.com/django/django) | `e7fd69d051eaa67cb17f172a39b57253e9cb831a` | 3.0 |
| Heavy | `pydata__xarray-4094` | [pydata/xarray](https://github.com/pydata/xarray) | `a64cf2d5476e7bbda099b34c40b7be1880dbd39a` | 0.12 |

They are the three task weights of our sandbox-density (concurrency) experiments, chosen by how much sandbox time
real agents spent on them per run (light 2.5 s, medium 9.4 s, heavy 57.4 s per run, medians across agents): from a
few short commands, to many short ones, to long CPU-bound test runs.

## The 18 runs

| Weight | Hands | Rep | Turn | Model calls | Tool calls left | Removed | Resolved |
|---|---|---|---|---|---|---|---|
| Light | container | 1 | 49 s | 11 | 8 | 2 | yes |
| Light | container | 2 | 43 s | 10 | 8 | 1 | yes |
| Light | container | 3 | 30 s | 9 | 7 | 1 | yes |
| Medium | container | 1 | 116 s | 26 | 25 | 0 | yes |
| Medium | container | 2 | 141 s | 27 | 25 | 1 | yes |
| Medium | container | 3 | 110 s | 23 | 22 | 0 | yes |
| Heavy | container | 1 | 538 s | 48 | 44 | 3 | yes |
| Heavy | container | 2 | 312 s | 24 | 21 | 2 | yes |
| Heavy | container | 3 | 213 s | 20 | 18 | 1 | yes |
| Light | microVM | 1 | 65 s | 12 | 10 | 1 | yes |
| Light | microVM | 2 | 26 s | 6 | 5 | 0 | yes |
| Light | microVM | 3 | 44 s | 10 | 7 | 2 | yes |
| Medium | microVM | 1 | 367 s | 39 | 38 | 0 | yes |
| Medium | microVM | 2 | 62 s | 11 | 9 | 1 | yes |
| Medium | microVM | 3 | 88 s | 15 | 11 | 3 | yes |
| Heavy | microVM | 1 | 267 s | 20 | 17 | 2 | yes |
| Heavy | microVM | 2 | 199 s | 14 | 11 | 2 | yes |
| Heavy | microVM | 3 | 175 s | 14 | 13 | 0 | yes |

- "Turn" is the whole recorded run, removed calls included (`turn_ms` in a file).
- "Model calls" is every model call of the run; none is removed.
- All 18 resolved: official grades from the SWE-bench evaluation harness (swebench 5.0.2), run on 2026-10-19 by
  our grading pipeline. "Resolved" means the harness applied the run's patch in the instance's image, and the
  issue's hidden tests (FAIL_TO_PASS) all passed while the existing ones (PASS_TO_PASS) still passed.

## What was removed

22 failed tool calls in all, each listed in its file's `removed` field: its start (`at_ms`), duration, tool and
reason. None of them is random: each fails every time in the environment it ran in.

| Reason (in `removed`) | What happened in the run | Calls |
|---|---|---|
| the agent's own command failed | a script the agent wrote to inspect the code failed: a reproduction run before the fix, a wrong attribute, a malformed `sed` range, a test run with failures | 15 |
| ran in the agent's own container, not the sandbox | microVM hands: the agent ran `cd /workspace && sbx run …`; the `cd` ran locally, where `/workspace` does not exist, so the command never reached the sandbox | 3 |
| never ran | Claude Code rejected the agent's tool input (a `Read` with a text `offset`), so nothing ran | 2 |
| environment: Django's parallel test runner needs `/dev/shm` | microVM hands: `runtests.py` without `--parallel 1` failed in the microVM, which had no `/dev/shm`; the agent's next command added `--parallel 1` | 2 |

None of the removed calls changed a file: each only read code or ran it. So the steps left still lead from the
image's `/testbed` to the run's patch.

- The `totals` count only the steps left.
- Where a call was removed, two model calls now sit next to each other; that is expected.

## How they were produced

- **Agent:** Claude Code 2.1.286, as shipped, non-interactive (`claude -p`).
- **Model:** `aws/claude-sonnet-5`, through a LiteLLM gateway, prompt caching as shipped, the gateway's response
  cache bypassed (each rep is an independent sample).
- **Container hands (`am5-l30-a`):** the agent ran in a fresh container per run, from the task's image with Claude
  Code added: 2 vCPU, 4 GiB, as uid 1000, the repository at `/testbed`, the task's Python first on the PATH, and a
  network that reached only the model gateway. The agent used its own tools there: `Bash`, `Read` and `Edit`.
  Started with `--permission-mode bypassPermissions`, web tools disabled.
- **MicroVM hands (`am5-l30-b`):** the same container, with the repository hidden and the agent's shell restricted
  to `sbx run` / `sbx put`, so every command went to MOCA's microVM tier and none ran locally. Each command ran in a
  fresh Firecracker microVM (2 vCPU, 4 GiB, no network) restored from the task's snapshot; the repository lived in a
  per-run `/workspace` that persisted across commands.
- **Prompt:** the SWE-bench issue text plus a short fixed instruction (make the minimal fix to non-test files; how
  to run the tests), one prompt per run, no follow-up turns.
- **Runs:** batches `am5-l30-a` and `am5-l30-b`, part of the AM5 experiment on 29 SWE-bench Lite instances × 3 reps,
  started 2026-10-18T01:58Z on one AWS c5.metal host, the container and microVM runs of each task and rep at the
  same time. Limits: 2,400 s per agent run, 1,800 s per sandbox command.

## Format (`trajectory.json`)

```json
{
  "schema": "autobench-moca/trajectory/1",
  "batch": "am5-l30-b", "instance_id": "django__django-11133", "rep": 3, "attempt": 1,
  "agent": "Claude Code", "hands": "sbx", "model": "aws/claude-sonnet-5",
  "turn_ms": 44281, "resolved": true,
  "usage":  {"input": 20, "output": 1840, "cacheRead": 331441, "cacheWrite": 4293, "total": 337594},
  "totals": {"model_calls": 10, "tool_calls": 7, "model_ms": 35333, "tool_ms": 1841, "sandbox_ms": 1269},
  "steps": [
    {"kind": "model", "at_ms": 528,  "dur_ms": 1481},
    {"kind": "tool",  "at_ms": 2497, "dur_ms": 159, "tool": "Bash", "is_error": false,
     "command": "sbx run <<'EOF'\ngrep -n ... django/http/response.py | head -50\nEOF",
     "sandbox": [{"at_ms": 2580, "dur_ms": 62, "exit": 0, "cpu_ms": 38.5}]}
  ],
  "removed": [
    {"at_ms": 28145, "dur_ms": 592, "tool": "Bash", "reason": "the agent's own command failed (...)"},
    {"at_ms": 33743, "dur_ms": 725, "tool": "Bash", "reason": "environment: Django's parallel test runner needs /dev/shm, ..."}
  ]
}
```

(The light microVM rep 3's first two steps and its removed list, shortened; the files carry them in full.)

- **`hands`** is `local` (container hands) or `sbx` (microVM hands).
- **Times** are milliseconds from the start of the agent run (`at_ms`), and the step's duration (`dur_ms`).
- **`kind: model`** is one model call: the wait from the agent's previous event to the model's reply, as the
  agent's own output stream shows it. It includes the gateway and network, not just the model.
- **`kind: tool`** is one tool call: from the agent issuing it to its result.
  - `Bash` steps carry `command`, the full shell command the agent asked for (the longest is 1,903 characters).
  - `Read` and `Edit` steps (container hands only) carry `input`: the file, a Read's line range (`offset`,
    `limit`), and an Edit's exact `old_string` and `new_string`, so a replay can perform the same read or edit.
- **`sandbox`** (microVM hands only) lists the microVM command that ran inside that tool call, as MOCA's worker
  timed it: start, duration, exit code, and the VM's CPU time. The gap between a tool call's `dur_ms` and its
  sandbox command's time is mostly the `sbx` client and MOCA's relay.
- **`usage`** is the run's token totals from Claude Code's own final report, removed calls included. Per-call token
  counts are not given: Claude Code's stream reports placeholder values per call, so only the totals are reliable.

### Reading notes

- **Container hands** mix tools: 125 `Bash`, 30 `Read` and 23 `Edit` calls over the nine runs. A `Read` or `Edit`
  takes 8–128 ms; the `Bash` calls run the searches and tests.
- **MicroVM hands** use only `Bash` (121 calls), each one `sbx run` with one sandbox command, since the agent could
  not read or edit files locally.
- Model time dominates every run: 61–85% of the turn, against 3–22% for the tool calls left.
- `cacheRead` is most of the tokens: each model call re-sends the conversation so far, served from the prompt cache.
- **Runs vary.** The same agent on the same task, with the same prompt, takes a different path each run: the medium
  task's microVM reps took 9, 11 and 38 tool calls. A workload built from these should draw on all three reps.

## The code

- **`repos/<instance>/source/`** is the public upstream repository exported at the instance's base commit
  (`git archive`, so plain files, no git history). It is the tree every run started from: the image's `/testbed` is
  this commit's content, which was checked file by file for xarray-4094.
- **Each run's `.patch`** (in `runs/`, beside its trajectory) is the change that run made, as a `git diff` against
  the base commit. It is exactly what was collected from the run and what the official grader applied and scored
  as resolved.
  - To see the code after a run, apply it to a copy of `source/`, or in the image's `/testbed` (`git apply`).
  - To check a replay, compare `git diff` in `/testbed` after the replay with the run's patch.
- **What the runs changed:**
  - In the light and heavy tasks every run changed one source file: `django/http/response.py` and
    `xarray/core/dataarray.py`.
  - In the medium task every run changed `django/conf/global_settings.py`. Four runs also updated the docs: all
    three container runs and microVM rep 1, under `docs/ref/settings.txt`, `docs/releases/` and, in three of them,
    `docs/howto/deployment/checklist.txt`.
  - Each fix resolves the issue.
- Each tree keeps its project's license (Django: BSD-3-Clause; xarray: Apache 2.0), and is the whole project (48 MB
  for Django, 5 MB for xarray); its dependencies are not included (the images carry them).

## What is not included, on purpose

- **No model text:** the model's reasoning and replies are left out.
- **No tool output:** what the commands printed, and what a `Read` returned, are left out (the commands and the
  edits themselves are included).
- **No prompts, keys, host names or addresses.**

The commands and edits include short code the agent wrote to inspect, edit or test the repository; they act only on
the public projects above.

Produced 2026-10-28 in rossoctl/autobench-moca from the runs' recorded event streams and timing spans. The tool
output was read only to tell the failures apart; none is written to these files. The files are complete as they
are: the `removed` list in each records exactly what was taken out and why.
