# Debug tracing

Trace writes are best-effort and can stop after a failure, so they cannot carry
correctness-critical state. `report_failures=False` keeps failures off a terminal
owned by a harness TUI.
