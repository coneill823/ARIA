# active-projects

The default promotion target. Each subfolder is a fully independent
project workspace with its own PM (`CLAUDE.md`), subagent team
(`.claude/agents/`), docs, and `STATUS.md`.

Work on a project from *inside its folder*, not from the aria root:

```bash
cd active-projects/<slug>
claude
> begin execution
```

On your own machine you'll probably want projects outside this repo —
point `system/config.yml → promotion_target` at e.g. `~/Documents/Projects`.
