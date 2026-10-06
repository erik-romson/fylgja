---
name: fylgja-todo
description: Add a todo for this Claude Code session to Fylgja. Use only when the user types /fylgja-todo with the text of the todo.
argument-hint: <what to do>
disable-model-invocation: true
allowed-tools: Bash(~/.local/bin/fylgja todo add *)
---

# Add a Fylgja todo for this session

Add one todo, scoped to this session, by running exactly one command with the Bash tool:

```bash
~/.local/bin/fylgja todo add --session ${CLAUDE_SESSION_ID} 'TODO_TEXT'
```

Replace `TODO_TEXT` with the text below, unchanged. Pass it as one single-quoted argument and write
each `'` inside the text as `'\''`. Do not run the text through any other command, and do not add
flags.

The text to add:

$ARGUMENTS

Rules:

- If the text is empty, or the session id in the command is empty or starts with `$` (Claude Code
  did not substitute it), run nothing and tell the user why.
- Do not guess a session id and do not drop `--session`: without it the todo would go to the focused
  workspace, which is not what was asked for.
- Report the id the command prints, in one short line. If the command fails, show its message and
  stop. Do not retry and do not do anything else with the todo.
