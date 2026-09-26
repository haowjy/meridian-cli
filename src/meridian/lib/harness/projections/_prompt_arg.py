"""Shared validation for prompts passed as individual harness arguments."""

_MAX_ARG_BYTES = 128 * 1024


def check_prompt_argument(prompt: str) -> str:
    """Return prompt after rejecting strings Linux cannot pass in one argv slot."""
    size = len(prompt.encode("utf-8"))
    if size >= _MAX_ARG_BYTES:
        kib = (size + 1023) // 1024
        raise ValueError(
            f"starting prompt is {kib} KiB; the harness CLI accepts at most 128 KiB as an argument"
        )
    return prompt
