# Core

The bottom layer: `core` imports only `core`, `platform` and `utils`, at any scope
(`dev/lints.py`). A type it needs from above moves down here; code that needs a
higher package belongs in that package. A spawn's lifecycle lives in
[../spawn/](../spawn/AGENTS.md).

`RuntimeOverrides` uses `None` for an unset layer. An empty string or zero is a
value and wins over lower-precedence layers.

Permission policy and tool rules (`permission_policy.py`, `tools.py`):
[.context/permission-policy.md](.context/permission-policy.md).
