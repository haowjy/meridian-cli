2026-10-15 pass 5 started: reading review/design and inventorying test interceptors.
2026-10-15 group 1: added alias-aware IOProbe, replaced scan/lock interceptors, and captured red proofs in /tmp/616-pass5-red-blind-{scan,locked-read}.txt.
2026-10-15 group 2: converted sequence/private-helper guards to alias-aware count bounds and removed the owner-session reread micro-guard; focused 84 tests passed.
2026-10-15 group 3: added chat-busy finalization/no-start, injected-clock lock timeout, live two-process continue race, and bidirectional run/session binding contracts. Red proofs: /tmp/616-pass5-red-{chat-busy-finalization,reopen-lock-timeout,continue-race,run-session-binding}.txt. Lock-order remains deferred to #418 B1.
