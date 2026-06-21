"""python -m meridian entry point.

Routes through ``cli.entrypoint.main`` (the same target as the ``meridian``
console script) so ``python -m meridian`` gets the trivial fast paths and the
Windows UTF-8 stdout reconfigure — not ``cli.main.main`` directly, which skips
them and crashes on glyph output (e.g. ``mermaid check``'s ``✓``) under cp1252.
"""

from meridian.cli.entrypoint import main

if __name__ == "__main__":
    main()
