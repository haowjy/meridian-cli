---
name: handoff
description: When the next phase belongs to a different interactive lead.
---

# Handoff

Write a transition brief and give the human what they need to start a new
interactive session with a different agent lead. Do not start it for them.

## Write the Transition Brief

Separate what the human said from what they want to be true when the work is
done. Then compact the conversation into a file the next agent reads as its
opening prompt:

- Human intent: the end goal in their terms, not implementation steps
- Current state: what was accomplished and where things stand
- Key decisions and why
- Open questions or blockers
- Exact artifact paths to read first
- Suggested skills to invoke

Keep artifact content in its files and put the brief in the work directory.
Without a work item, use a temporary file.

Before completing the handoff, run any installed `pre-*` checkpoint that
applies to the next phase. Choose the next agent from the human's request or
the available agents; ask when the choice is ambiguous. Show the work item's
primary-session trail with `meridian work sessions`.

## Prepare the Human's Launch

Print the root `meridian` launch command, but never run it. It runs in a new
terminal where your environment is gone, so every reference must be a concrete
value rather than a variable: the work item, source checkout, brief path, and
current chat ID as the parent. Use `meridian -h` for the launch flags.

