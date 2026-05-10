# Smoke: session

Session log, search, and repair — error paths and help.

## Setup

```bash
. tests/smoke/scripts/setup.sh
```

## Invalid ref — all subcommands fail cleanly

```bash
uv run meridian session log invalid-ref-xyz-123
```
- [ ] Exit non-zero
- [ ] No `Traceback` in stderr

```bash
uv run meridian session search pattern invalid-ref-xyz-456
```
- [ ] Exit non-zero
- [ ] No `Traceback` in stderr

```bash
uv run meridian session repair invalid-ref-xyz-789
```
- [ ] Exit non-zero
- [ ] No `Traceback` in stderr

## Help — all subcommands show usage

```bash
uv run meridian session log --help
```
- [ ] Exit 0
- [ ] `log` or `session` appears in output (case-insensitive)

```bash
uv run meridian session search --help
```
- [ ] Exit 0
- [ ] `search` appears in output (case-insensitive)

```bash
uv run meridian session repair --help
```
- [ ] Exit 0
- [ ] `repair` appears in output (case-insensitive)
