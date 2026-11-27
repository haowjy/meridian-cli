"""Test helper for the machine-wide ID claim directory."""

from pathlib import Path


def plant_id_claims(home: Path) -> None:
    """Claim ``p0`` and ``c0`` (dangling) so the first issued IDs are ``p1`` and ``c1``.

    A fresh home seeds at 1000, which the ID assertions throughout the suite do not
    expect. A test that sets its own ``MERIDIAN_HOME`` and asserts low IDs plants them
    for that home.
    """

    ids = home / "machine" / "ids"
    for kind in ("p", "c"):
        (ids / kind / "0").mkdir(parents=True)
        (ids / kind / "0" / f"{kind}0").symlink_to("../../../../projects/planted")
    (ids / "format").write_text("1\n", encoding="utf-8")
