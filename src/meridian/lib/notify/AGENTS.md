# Notification delivery

Call `emit.emit`, not `service.send`: direct delivery bypasses hook routing and
extensions. Route and transport reports are separate; callers decide how to
surface their failures.

Prefer the SMTP password file: the plain environment fallback reaches spawned
agents and may appear in transcripts.
