"""Pytest subprocess bootstrap for ``--runner-history=off``."""


from patches import install_runner_history_blind

install_runner_history_blind()
