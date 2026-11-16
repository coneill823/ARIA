# Roadmap — making the recursive agent stronger

Additions that most improve a long-running autonomous loop, roughly in
order of value-per-effort. (The loop today: plan → execute → verify →
advance → replan, with changelog, graceful stop, and a final report.)

## Memory & learning
1. ✅ **Per-project rolling memory** *(shipped)* — a `lessons.md` the
   loop distils from the changelog each iteration and feeds back into
   planning and every task. Stops the loop re-litigating settled
   questions; the single highest-value addition.
2. **Cross-project memory vault** — distilled lessons promoted to a
   global store the planner always sees ("markdown tables render badly in
   X", "user prefers Y"). ECC's memory design is the reference.

## Progress & safety
3. ✅ **Git auto-commit per task** *(shipped — `autopilot_git`, fires
   only in projects with their own `.git`)* — diffable history, true
   rollback, final report verifiable against real diffs.
4. ✅ **Stall detection** *(shipped — `autopilot_stall_limit`)* — N
   consecutive replans that add no new work → auto-pause as "stalled"
   + webhook notify. The main failure mode of unattended loops is busy
   non-progress.
5. ✅ **Budget meter** *(shipped — `autopilot_max_tasks` /
   `autopilot_max_hours`)* — pause and notify at the cap instead of
   burning a week of GPU.
6. **Task watchdog** — per-task wall-clock limit (giants excepted by
   config) so one hung generation can't stall the queue silently.
7. ✅ **Human-gate tasks** *(shipped)* — the planner emits
   `needs_human: true` tasks that wait until approved with one click on
   the Projects page. Autonomy with brakes at the decisions that
   genuinely need you.

## Capability
8. **Sandboxed code execution** — a runner (container/firejail) so the
   coder's output can be *executed and tested*, with results fed to the
   verifier. Turns "writes code" into "ships working code"; the single
   biggest capability jump, and the riskiest — allowlisted commands,
   no network, project-dir only.
9. **Web research lane** — a task type that routes through the Claude
   engine (or a self-hosted SearxNG with an allowlist) so research tasks
   produce sourced facts instead of recalled ones.
10. **Checkpointed generations** — persist partial tokens for giant-model
    tasks so a restart resumes mid-generation instead of restarting.

## Orchestration
11. **Parallel lanes** — independent tasks on different backends at once
    (coder on :8082 while deepseek reasons on :8080); dependency graph
    already supports it, the worker just needs N lanes.
12. ✅ **Duplicate-task guard** *(shipped)* — replanned titles are
    checked against everything done or seen and repeats dropped, so
    iterations don't re-add work that's already done.
13. **Nightly digest** — changelog summary through the `mission-done`
    webhook each morning: what happened overnight, what's blocked, what
    needs you. Closes the loop for multi-day runs.
