# Roadmap — making the recursive agent stronger

Additions that most improve a long-running autonomous loop, roughly in
order of value-per-effort. (The loop today: plan → execute → verify →
advance → replan, with changelog, graceful stop, and a final report.)

## Memory & learning
1. **Per-project rolling memory** — a `lessons.md` the planner updates
   each iteration (what worked, what failed, decisions made) and reads
   when replanning. Stops the loop re-litigating settled questions; the
   single highest-value addition.
2. **Cross-project memory vault** — distilled lessons promoted to a
   global store the planner always sees ("markdown tables render badly in
   X", "user prefers Y"). ECC's memory design is the reference.

## Progress & safety
3. **Git auto-commit per task** — every completed task commits inside the
   project repo. Gives diffable history, true rollback, and makes the
   final report verifiable against real diffs.
4. **Stall detection** — hash/compare each iteration's outputs; N
   consecutive low-novelty iterations → auto-pause + webhook notify.
   The main failure mode of unattended loops is busy non-progress.
5. **Budget meter** — track tokens/wall-clock per project with soft
   caps; pause and notify at the cap instead of burning a week of GPU.
6. **Task watchdog** — per-task wall-clock limit (giants excepted by
   config) so one hung generation can't stall the queue silently.
7. **Human-gate tasks** — let the planner emit `needs_human: true` tasks
   that pause the lane until approved on the Projects page. Autonomy
   with brakes at the decisions that genuinely need you.

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
12. **Duplicate-task guard** — title/content similarity check at replan
    time so iterations don't re-add work that's already done.
13. **Nightly digest** — changelog summary through the `mission-done`
    webhook each morning: what happened overnight, what's blocked, what
    needs you. Closes the loop for multi-day runs.
