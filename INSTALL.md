# Meridian Install Guide for LLM Agents

Use this guide when the user asks you to install and set up `meridian-cli`.
Execute commands exactly, ask the user at decision points, and report command output succinctly.

## Step 0: Prerequisites

1. Check whether `uv` is installed:

```bash
which uv
```

2. If `uv` is missing, install it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

3. Check Python version:

```bash
python3 --version
```

4. Confirm Python is `3.12` or newer. If not, ask the user to upgrade Python before continuing.

## Step 1: Install Meridian

Run the recommended install:

```bash
uv tool install meridian-cli
```

From source (for development or pre-release):

```bash
git clone https://github.com/haowjy/meridian-cli.git
cd meridian-cli
uv tool install --force . --no-cache --reinstall
```

Verify the CLI is available:

```bash
meridian --help
```

## Step 2: Initialize Project

In the user's project root, run:

```bash
meridian init --link .claude
```

This creates `.meridian/` with baseline Meridian runtime config and delegates
package target wiring to Mars. Mars compiles package content into `.mars/` and
links harness-native content into `.claude/` so Claude Code can discover the
installed skills/tooling it needs.
Under the hood this is a convenience wrapper:
- if `mars.toml` is missing, it runs `meridian mars init --link .claude`
- if `mars.toml` already exists, it runs `meridian mars link .claude`

For other tools (Cursor, etc.), use `--link .cursor` instead or in addition.

If the user doesn't use Claude Code or any tool that reads from a dot-directory, omit `--link`:

```bash
meridian init
```

## Step 3: Install Agent & Skill Packages

Ask the user: `Would you like to install the dev workflow agents and skills? This gives you a full dev team — coder, reviewers, testers, researcher, documenter — with structured workflow skills.`

If yes, add the dev workflow package (includes core primitives as a dependency):

```bash
meridian mars add haowjy/meridian-dev-workflow
```

If they only want the core coordination primitives (without the dev team):

```bash
meridian mars add haowjy/meridian-base
```

## Step 4: Shell Completion (Optional)

Ask the user: `Would you like shell autocompletion for the meridian CLI?`

If yes, run:

```bash
meridian completion install
```

Tell the user to restart their shell (or source their shell profile) for completion to take effect.

## Step 5: Verify Setup

Run:

```bash
meridian doctor
meridian mars sync
meridian mars models list
meridian mars models list --live
```

Notes:
- `meridian mars models list` is a lightweight static catalog check.
- `meridian mars models list --live` checks runnable harness routes and may require provider credentials or installed harness CLIs.
- `meridian mars sync` should complete without package resolution errors.
- Report any failed checks and propose next concrete fix steps.
