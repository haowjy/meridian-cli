"""Pytest subprocess bootstrap for ``--runner-history=off``."""


from patches import install_runner_history_blind, install_writer_import_hook

install_runner_history_blind(patch_writers=False)
install_writer_import_hook()
