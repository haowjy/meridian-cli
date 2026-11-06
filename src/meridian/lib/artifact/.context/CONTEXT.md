# Artifact serving

Tailscale port 443 does not route to local serve handlers on some nodes, and
well-known ports can clobber unrelated serves. Funnel uses public port 10000;
its local backend uses a separate high port. See KB
`research/tailscale-serve-semantics.md`.

Tailscale forwards the full path, so `_serve_dir.py` strips the slug in
`translate_path`; directory redirects retain the prefix.
