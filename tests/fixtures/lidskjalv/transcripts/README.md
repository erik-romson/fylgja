# Transcript fixtures

One Claude Code session transcript, in the shape found under
`~/.claude/projects/<mangled-cwd>/<session-id>.jsonl`. Static rather than
generated: nothing in `lidskjalv summary` compares a transcript timestamp against
now, so unlike `cc_sessions/mkrec.sh` records these cannot age out.

`test-1.jsonl` is named for the session id `create_lane` writes, and deliberately
carries the three records that must NOT count as user turns — a tool result
(array content), an injected `isMeta` line, and a subagent's `isSidechain`
prompt — alongside the ones that must.
