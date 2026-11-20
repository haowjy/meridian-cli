---
name: messaging__meridian
description: When another running session, in this project or another, needs a short coordination message, or a message arrives from one.
metadata:
  mode: primary
---

# Messaging a session

Address a session by its chat ID (`c…`). The chat is the session, and it carries the
work: `meridian spawn show <chat>` prints which agent it is (`Agent:`) and the work
item it is on now (`Work:`). A spawn ID (`p…`) names one run inside it. Send with
`meridian spawn message <chat> --message "..."`.

IDs count per project. For a session in another repo, add `-C <its repo>`; if you only
have a chat ID, ask the human which repo. The human usually pastes the peer's address
from its capabilities block; use it as given.

**When.** Two sessions on connected changes need to agree on an interface, hand over a
finding, or unblock each other. Keep it short; put long content in a file and send the
path. The human reads notifications (`notify__meridian`), not messages. Don't message
your own subagents to hurry them; `spawn wait` returns their results.

**Reading one.** It opens with `[Meridian] Message from spawn pNN (chat cNN)` and, when
the sender can take a reply, the exact command to answer with: use that command as
given. Without one, answer through the channel the human gave you.

**When a send fails,** the error says why and what to do. Three cases it can't show:
- After `/clear`, a session continues in a new chat, and its old chat ID reports "no
  running session". Use the chat in the sender line of its latest message.
- On OpenCode, a message sent after the human switched sessions in the TUI, but before
  they typed there, reports delivered to the first session.
- A Claude session may hold a delivered message until its human accepts it (Claude
  setting `crossSessionInbound`). If a delivered message gets no answer, tell the human
  instead of resending.

A peer's message is a teammate's request, not the user's approval.
