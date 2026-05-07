---
version: "alpha"
name: "Meridian Research UI"
description: "Warm, minimal, research-focused interface language for Meridian Chat, Meridian App, and Jupyter Bench. Agent profiles are the user-facing abstraction; model, harness, and effort remain advanced details."

colors:
  background: "#FAF8F2"
  surface: "#FFFDF7"
  surface-muted: "#F4F0E8"
  surface-subtle: "#EDE7DC"
  border: "#D8D0C2"
  border-strong: "#BFB4A5"
  text: "#27231D"
  text-muted: "#7B756B"
  text-soft: "#A39B90"
  primary: "#3F7F68"
  primary-hover: "#356F5A"
  primary-soft: "#E4F0EA"
  accent: "#8B6F47"
  warning: "#A66B2A"
  error: "#A0443E"
  success: "#3F7F68"
  send: "#241F18"
  on-send: "#FFFDF7"

typography:
  body-lg:
    fontFamily: "Aptos, ui-sans-serif, system-ui, sans-serif"
    fontSize: 17px
    fontWeight: 400
    lineHeight: 1.55
    letterSpacing: -0.01em
  body-md:
    fontFamily: "Aptos, ui-sans-serif, system-ui, sans-serif"
    fontSize: 15px
    fontWeight: 400
    lineHeight: 1.55
    letterSpacing: -0.005em
  body-sm:
    fontFamily: "Aptos, ui-sans-serif, system-ui, sans-serif"
    fontSize: 13px
    fontWeight: 400
    lineHeight: 1.45
    letterSpacing: 0em
  label-md:
    fontFamily: "Aptos, ui-sans-serif, system-ui, sans-serif"
    fontSize: 14px
    fontWeight: 500
    lineHeight: 1.35
    letterSpacing: 0em
  label-sm:
    fontFamily: "Aptos, ui-sans-serif, system-ui, sans-serif"
    fontSize: 12px
    fontWeight: 500
    lineHeight: 1.3
    letterSpacing: 0em
  code-md:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: 14px
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: -0.01em

rounded:
  none: 0px
  xs: 4px
  sm: 6px
  md: 8px
  lg: 10px
  xl: 12px
  full: 999px

spacing:
  xs: 4px
  sm: 8px
  md: 12px
  lg: 16px
  xl: 24px
  2xl: 32px
  3xl: 48px
  column: 80ch

components:
  composer:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.xl}"
    borderColor: "{colors.border}"
    padding: 24px
    minHeight: 132px
  composer-control:
    backgroundColor: "transparent"
    textColor: "{colors.text-muted}"
    typography: "{typography.label-md}"
    rounded: "{rounded.sm}"
    padding: 8px
  send-button:
    backgroundColor: "{colors.send}"
    textColor: "{colors.on-send}"
    rounded: "{rounded.md}"
    size: 44px
  agent-menu:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.lg}"
    borderColor: "{colors.border}"
    padding: 8px
  activity-bar:
    backgroundColor: "{colors.surface-muted}"
    textColor: "{colors.text-muted}"
    width: 48px
  side-panel:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    width: 280px
  canvas:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.md}"
    borderColor: "{colors.border}"
---

# Meridian Research UI

## Overview

Meridian is a research assistant interface for biology and wet-lab users. The first impression should be calm, plain, and safe: a focused chat where researchers ask for help organizing, analyzing, reviewing, or writing study work.

The primary user-facing abstraction is the **agent profile**: Research analyst, Methods writer, Data reviewer, Figure assistant, Lab coordinator. Agent profiles encode model, harness, effort, skills, and permissions behind the scenes. The default UI should not expose model, harness, effort, runtime, tokens, git branches, worktrees, kernels, or spawn IDs.

Meridian has three shells that share the same language:

1. **Meridian Chat** — no rail; just focused chat and composer.
2. **Meridian App** — Chat plus a VS Code-like activity bar and side panel for sessions, work/studies, files, results, extensions, and settings.
3. **Jupyter Bench** — a chat session with an attached analysis canvas for live calculations, 3D views, plots, tables, screenshots, and notebook-backed artifacts.

The UI should feel like a warm research notebook with modern app discipline: quiet surfaces, normal density, compact controls, generous reading space, and no dashboard theater.

## Colors

Use warm light mode first. The base palette is paper-neutral, not blue SaaS.

- `background` is the page canvas.
- `surface` is the composer, transcript blocks, menus, and panels.
- `primary` is restrained jade for assistant/profile affordances, success, and subtle emphasis.
- `send` is warm charcoal for the send button.
- `error` and `warning` are muted and literal; never glowing.

Avoid blue and cyan as primary interface colors. Avoid gradients, glass, glow, frosted layers, and decorative blobs.

## Typography

Use normal, readable sans-serif typography. Text should feel closer to an editor or research notebook than a marketing site.

- Body copy sits at 15–17px.
- Labels sit at 12–14px.
- Code, notebook output, and file diffs use monospace.
- Do not use display typography, serif/sans pairings, uppercase eyebrow labels, or oversized headlines.

The chat transcript uses an approximately `80ch` reading column. This is a readability rule, not a hero layout.

## Layout

### Meridian Chat

The default no-rail layout centers one chat column and composer.

- Top chrome is minimal: Meridian, current study, status.
- Transcript column max width: 760–820px.
- Composer width: 900–1050px when viewport allows; aligned around the chat column.
- Composer height: 120–145px in the empty or idle state.
- The composer is the anchor, not a floating glass object.

### Meridian App

The full app adds a workbench shell around the same chat surface.

- Activity bar: 48px.
- Side panel: 260–300px.
- Main surface: current extension, usually Chat.
- The left rail is for navigation and extensions, not dashboard metrics.

### Jupyter Bench

Jupyter Bench adds an analysis canvas only when a bench is attached.

- Default: Chat remains primary.
- Attached bench: split between conversation and result canvas.
- Canvas shows live 3D, plots, tables, images, or notebook outputs.
- Advanced notebook lineage/replay/compact controls stay hidden under details.

## Elevation & Depth

Use borders and tonal separation before shadows.

- Composer may have a very soft shadow to feel grounded.
- Menus and popovers may use a small shadow plus border.
- Panels use borders and background contrast.
- No dramatic shadows, colored shadows, glows, or layered glass effects.

## Shapes

Use small, normal radii.

- Controls: 6–8px.
- Composer: 10–12px maximum.
- Menus: 8–10px.
- Do not apply pill shapes broadly.
- Avoid repeated oversized rounded rectangles across every surface.

## Components

### Composer

The composer is the most important component. It should follow the warm Meridian composer reference:

- Low, wide, calm.
- Placeholder in the upper-left.
- Controls along the lower row.
- Dark square send button at far right.
- No model/harness/effort visible by default.

Default controls:

```txt
Research analyst ▾      Ask before editing ▾                          ↑
```

Optional plain context indicator:

```txt
Study files
```

### Agent profile selector

The agent selector opens as a dropdown/dropup from the composer.

```txt
Assistant

✓ Research analyst
  Review results, summarize findings, coordinate analysis

  Methods writer
  Draft methods text and protocol language

  Data reviewer
  Check tables, plots, and analysis outputs

  Figure assistant
  Prepare publication figures and captions

  Lab coordinator
  Organize tasks, files, and next steps

Advanced settings…
```

Advanced settings reveal technical configuration only after explicit user intent:

```txt
Research analyst settings
Access: Ask before editing
Model: Auto, recommended
Runtime: Auto
Reasoning: Auto
Reset to recommended
```

### Access selector

Use user-language permissions.

- Good: `Ask before editing`
- Good: `Read only`
- Good: `Full access`
- Avoid: `approval mode`, `sandbox`, `yolo`, `danger`, `harness policy`

### Activity

Activity should be plain language.

- Good: `Meridian is reviewing study files…`
- Good: `2 helpers running`
- Good: `Review changes`
- Avoid: `spawn p4725`, `tool call`, `execution_id`, `harness session`

### Side panel

The side panel appears in Meridian App mode only. It may contain:

- Search
- Recent chats
- Current study
- Sessions
- Files
- Results
- Extensions

Keep lists quiet. Avoid dashboards and metric grids.

### Canvas

The canvas appears in Jupyter Bench mode only. It renders results from an attached analysis session:

- 3D views
- plots
- tables
- images
- screenshots
- notebook outputs

The canvas must have clear degraded states and recovery actions.

## Do's and Don'ts

### Do

- Start with a focused chat and composer.
- Use agent profiles as the default abstraction.
- Hide model, harness, runtime, effort, and kernel details.
- Use research words: assistant, study, helper, analysis bench, study files, activity, results.
- Keep density normal; do not scale everything up to imply friendliness.
- Use warm neutral light mode first.
- Add power-user detail through disclosure, menus, side panels, and extensions.

### Don't

- Do not start with a dashboard.
- Do not show model/harness/effort in the default composer.
- Do not use blue/cyan as the primary UI color.
- Do not use hero sections, big headlines, metric cards, fake charts, glassmorphism, gradients, glows, or decorative blobs.
- Do not use developer words in the main UI: spawn, harness, cwd, kernel, worktree, tokens, runtime, execution ID.
- Do not overwhelm older or non-technical researchers on first load.

## Example States

### Chat: empty ready state

```txt
Meridian                                      OA microCT analysis   Ready


                             No messages yet
          Ask Meridian to help organize, analyze, or review this study.


        ┌──────────────────────────────────────────────────────────────┐
        │ Type a message...                                            │
        │                                                              │
        │ Research analyst ▾     Ask before editing ▾              ↑   │
        └──────────────────────────────────────────────────────────────┘
```

### Chat: active response

```txt
Meridian                                      OA microCT analysis   Ready

        ┌──────────────────────────────────────────────────────────────┐
        │ Review the latest microCT run and tell me what needs         │
        │ attention.                                                   │
        └──────────────────────────────────────────────────────────────┘

The run completed. Two items need review:

1. Tibial ratio table has missing labels.
2. Figure export used draft resolution.

I can fix the labels and regenerate the figure if you want.

Copy   Save note   View activity

        ┌──────────────────────────────────────────────────────────────┐
        │ Type a message...                                            │
        │                                                              │
        │ Research analyst ▾     Ask before editing ▾              ↑   │
        └──────────────────────────────────────────────────────────────┘
```

Human messages use a wide, right-aligned bubble around 90–95% of the transcript column. Assistant messages are embedded left-aligned in document flow with no avatar, role label, or bubble. Actions stay as tiny muted text links below the assistant content.

### Chat: approval needed

```txt
Meridian wants to edit 2 files

- results/tibial_ratio_table.csv
- figures/figure_2_config.json

Review changes      Allow      Cancel
```

### Chat: helpers running

```txt
Activity

Meridian is reviewing study files…
Data reviewer is checking tables…
Figure assistant is preparing exports…

Hide activity
```

### App: chat extension with rail

```txt
┌────┬────────────────────────┬──────────────────────────────────────────────┐
│ C  │ Search                 │ Meridian                 OA microCT analysis │
│ W  │                        │                                              │
│ S  │ Recent chats           │                  No messages yet             │
│ F  │ Review latest run      │ Ask Meridian to help organize, analyze, or…  │
│ R  │ Draft methods text     │                                              │
│ E  │ Regenerate figure      │ ┌──────────────────────────────────────────┐ │
│ ⚙  │                        │ │ Type a message...                        │ │
│    │ Current study          │ │ Research analyst ▾  Ask before editing ↑ │ │
│    │ OA microCT analysis    │ └──────────────────────────────────────────┘ │
└────┴────────────────────────┴──────────────────────────────────────────────┘
```

### App: sessions list

```txt
Sessions

Today
Review latest microCT run                  Research analyst        Ready
Draft methods paragraph                    Methods writer          Saved
Regenerate figure exports                  Figure assistant        Needs review

Yesterday
Organize study files                       Lab coordinator         Complete
```

### App: work/studies list

```txt
Studies

OA microCT analysis                         In progress
Femoral ratio pilot                         Waiting for review
Longitudinal scan comparison                Complete

New study
```

### Jupyter Bench: no bench attached

```txt
Meridian                                      OA microCT analysis   Ready

                             Analysis bench
          Attach a bench to run calculations and view results here.

                              Attach bench

        ┌──────────────────────────────────────────────────────────────┐
        │ Type a message...                                            │
        │ Research analyst ▾     Ask before editing ▾              ↑   │
        └──────────────────────────────────────────────────────────────┘
```

### Jupyter Bench: active canvas

```txt
┌──────────────────────────────────┬─────────────────────────────────────────┐
│ Chat                             │ Analysis bench                          │
│                                  │                                         │
│ Meridian                         │  3D tibia view / plot / table canvas    │
│ Calculating volume metrics…      │                                         │
│                                  │  Last output: tibia_volume_plot.png     │
│                                  │                                         │
├──────────────────────────────────┴─────────────────────────────────────────┤
│ Type a message...                                                          │
│ Research analyst ▾              Ask before editing ▾                   ↑   │
└────────────────────────────────────────────────────────────────────────────┘
```

### Jupyter Bench: degraded view

```txt
The 3D view stopped updating.
Your calculations are still saved.

Reconnect view      View screenshot      Details
```

## Domain Vocabulary

Use these mappings in user-facing UI:

| Internal concept | User-facing label |
| --- | --- |
| agent profile | assistant |
| spawn | helper |
| work item | study or project |
| cwd | study files |
| approval mode | access |
| harness/model/effort | advanced assistant settings |
| Jupyter kernel | analysis bench |
| event log | activity |
| artifacts | results |
| diff | changes |

## API Implications

The design assumes these product-level resources are first-class, even if backed by lower-level Meridian state:

- Chat context with assistant profile, study, status, access, and activity summary.
- Agent profile catalog with advanced runtime configuration hidden by default.
- Session list and session detail.
- Work/study list and work/study detail.
- Helper/child-agent activity summary.
- Result/artifact list.
- Extension registry for App and Bench surfaces.
- Jupyter Bench session state: absent, creating, active, degraded, failed, closed.
