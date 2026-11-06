# lib/harness/ — Pi Integration

Create and fork mint IDs only after the store is known, checking every local
journal's first-line header for collisions. Resume/fork source selection requires
one exact filename suffix and a matching session header, and passes an absolute
path to Pi. Missing/empty files cannot be resumed: Pi would silently create a new
identity there. Post-exit verification checks only the assigned entry (including
the fork parent); an unmaterialized create stays bound and pending. It never
selects a newest/cwd-matching journal. Meridian writes no Pi native journal.
