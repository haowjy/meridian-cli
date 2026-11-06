# Operations

Resolve caller policy before materializing launch state: an unauthorized nested
spawn must leave no identity, locks, telemetry or row. Keep harness mechanism in
`launch/` and `harness/`.

Work attachment updates the work and session stores sequentially, never under
both locks. [.context/CONTEXT.md](.context/CONTEXT.md) explains why.
