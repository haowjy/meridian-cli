# Harness projections

Claude's `--add-dir` is variadic: without `--`, it consumes a positional prompt. Pi interprets a leading `@` as a file reference even after `--`, so its projector prefixes a space.

[Projection contracts](.context/CONTEXT.md)
