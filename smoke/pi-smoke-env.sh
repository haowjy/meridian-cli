#!/usr/bin/env bash
# Source before pi-smoke tmux panes and meridian spawn --harness pi runs.
export PATH="/home/jimyao/.nvm/versions/node/v24.13.0/bin:${PATH}"
export SMOKE_STATE="${SMOKE_STATE:-$HOME/meridian-pi/smoke-pi-bg-tasks-20260522}"
export MERIDIAN_PI_STATE_DIR="$SMOKE_STATE"
export EXT_SRC="/home/jimyao/gitrepos/meridian-cli.worktrees/pi-generic-background-tasks/src/meridian/pi_runtime/dist/extensions"
export BT="$EXT_SRC/background-tasks/index.js"
export SW="$EXT_SRC/meridian-spawn-watch/index.js"
export PI_CODING_AGENT_SESSION_DIR="${PI_CODING_AGENT_SESSION_DIR:-$HOME/meridian-pi/sessions}"
export MERIDIAN_REPO="/home/jimyao/gitrepos/meridian-cli.worktrees/pi-generic-background-tasks"
mkdir -p "$SMOKE_STATE"
