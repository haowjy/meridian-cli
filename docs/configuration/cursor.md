# Cursor harness configuration

## Model Selection

Pass a cursor model alias directly with `-m`:

```bash
meridian spawn --harness cursor -m composer -p "task"
meridian spawn --harness cursor -m gpt-5.5 -p "task"
```

Use the `cursor` shortcut when cursor is your default harness:

```bash
meridian cursor spawn -m composer -p "task"
```

## Effort and Slug Resolution

Mars resolves `model + effort` to `routing.harness_model` in the launch bundle.
Meridian passes that resolved harness model to Cursor as the effective `--model`.
Cursor model slugs encode effort as a suffix (e.g. `gpt-5.5-high`).

```bash
meridian spawn --harness cursor -m gpt-5.5 --effort high   # → effective --model gpt-5.5-high (bundle-resolved)
meridian spawn --harness cursor -m gpt-5.5 --effort low    # → effective --model gpt-5.5-low (bundle-resolved)
meridian spawn --harness cursor -m gpt-5.5                 # → effective --model gpt-5.5 (bundle-resolved)
```

**Claude models in Cursor:** if Mars resolves to a thinking variant for the selected effort, Meridian passes that effective resolved model through:

```bash
meridian spawn --harness cursor -m claude-opus-4-7 --effort high
# → effective --model claude-opus-4-7-thinking-high (bundle-resolved)
```

**Fast variants** (`-fast` suffix) are not yet available. Deferred to a future release.

## Approval

| Meridian approval mode | Cursor flag |
|---|---|
| `yolo` | `--yolo` |
| `auto` | `--force` |
| `default` / `confirm` | *(no flag)* |

## Limitations

The following features are not yet supported for the Cursor harness:

- Session resume and fork (`--continue`, `--fork`)
- Per-spawn MCP tools
- Interactive mode
