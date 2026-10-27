# extensions/commands/ — First-Party Handler Implementations

Handlers for first-party extension commands that are not plain ops (today
`mermaid.py`; `biomed.py` is an empty placeholder). Specs register in
`../first_party.py`, not here. Handler signature, pipeline, and surface rules:
`../.context/CONTEXT.md`.

- Every spec here sets `first_party=True`.
- Return `ExtensionJSONResult` or `ExtensionErrorResult`; don't raise.
- Importing `meridian.lib.*` is fine; these are internals, not plugin-boundary code.
