# Frontier Brand Agent

A local-first automation pipeline for discovering and evaluating **Cloud × AI × real-world industry** research, turning the strongest evidence into briefs and platform-specific drafts, and placing every public action behind explicit human approval.

The application performs no LinkedIn, X, email, or cloud writes. Publishing is hard-disabled in [`config/agent.yaml`](config/agent.yaml), and approved drafts can only be exported to the local [`exports`](exports) directory.

## What works

- Read-only discovery from arXiv, OpenAlex, and configured official RSS feeds
- Offline curated seed set so the full pipeline remains testable if a source is unavailable
- Deduplication and Cloud × AI relevance scoring
- Classification into Cloud for AI, AI for Cloud, industry applications, and agent reliability
- Evidence briefs with explicit publication-status limitations
- Claim ledger that distinguishes source-reported claims from independent verification
- LinkedIn and X drafts in an evidence-first voice
- Private learning missions that require an explanation, critique, or experiment before claiming growth
- Optional local Ollama support; deterministic drafting works without a model
- Approval, rejection, editing, and local export dashboard
- Three-part approval checklist: understanding, claim review, and exact voice confirmation
- Queue protection: scheduled runs never overwrite pending edits and stop adding drafts at the configured review limit
- Quarantine rules for instruction injection, credential exfiltration, tool execution, and unauthorized external-action text found in sources
- SQLite audit trail and run history
- Local 12-hour scheduler and optional Windows Scheduled Task
- Hard guard preventing publishing when external writes are disabled

## Setup

From Windows Explorer, run:

```text
scripts\setup.cmd
```

Or from PowerShell:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1
```

## Use

Run one complete research cycle:

```text
scripts\run_once.cmd
```

Open the approval dashboard:

```text
scripts\start_dashboard.cmd
```

Then visit <http://127.0.0.1:8765>.

Run the foreground scheduler:

```text
scripts\start_scheduler.cmd
```

Register a local Windows task that runs every 12 hours:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\register_scheduled_task.ps1
```

Keep the local-only dashboard available after each Windows logon:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\register_dashboard_task.ps1
```

Remove it:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\unregister_scheduled_task.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\unregister_dashboard_task.ps1
```

## CLI

```powershell
.venv\Scripts\python.exe -m brand_agent doctor
.venv\Scripts\python.exe -m brand_agent run
.venv\Scripts\python.exe -m brand_agent status
.venv\Scripts\python.exe -m brand_agent serve
.venv\Scripts\python.exe -m brand_agent scheduler
```

Export requires an exact approved draft:

```powershell
.venv\Scripts\python.exe -m brand_agent export DRAFT_ID
```

There is intentionally no `publish` command in this MVP.

## Pipeline

```text
read-only sources
      ↓
normalize + deduplicate
      ↓
Cloud × AI relevance scoring
      ↓
evidence brief + claim ledger
      ↓
LinkedIn/X drafting
      ↓
pending approval
      ↓
edit / reject / approve
      ↓
local Markdown export only
```

## Configuration

- [`config/profile.yaml`](config/profile.yaml): identity, audience, voice, content pillars, and boundaries
- [`config/agent.yaml`](config/agent.yaml): sources, scoring, scheduler, local model, and safety policy

To use a local Ollama model later:

1. Install Ollama locally.
2. Pull the model named by `drafting.ollama_model`.
3. Leave `drafting.provider: auto`; the pipeline detects the local model.

If the model is absent, the pipeline uses the deterministic evidence-first template and remains fully operational.

## Safety model

Three independent conditions would be required before any future publishing adapter could operate:

1. `publish_enabled: true`
2. `allow_external_writes: true`
3. The exact draft status is `approved`

This repository ships with the first two set to `false` and contains no external publishing adapter or account credentials.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest
```

