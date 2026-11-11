# Frontier Brand Agent

A private Cloud × AI research and personal-brand pipeline. It discovers credible research, ranks real-world industry relevance, creates evidence briefs and LinkedIn/X drafts, reminds the owner to review them, and publishes a LinkedIn draft only after explicit approval of the exact text.

## Operating modes

- **Local:** SQLite, Windows Task Scheduler, no external writes by default.
- **Cloud:** Render dashboard, Neon PostgreSQL, GitHub Actions daily worker, Resend reminders, and LinkedIn OAuth publishing.

The same codebase supports both modes. Real credentials belong only in environment-variable secret stores and must never be committed.

## Cloud flow

```text
GitHub Actions (daily, 08:17 Asia/Kolkata)
        ↓
research → safety scan → keyword scoring → AI judge (top 20)
        → full-paper AI brief (quote-verified claims) → AI drafts (number-checked)
        ↓
Neon PostgreSQL
        ↓
Resend reminder email → authenticated Render dashboard
        ↓
edit → verify claims → approve exact text
        ↓
LinkedIn Posts API through member OAuth
```

## Safety properties

- Internet sources are treated as untrusted and scanned before drafting.
- The dashboard is private in production and restricted to `ADMIN_EMAIL` through LinkedIn OAuth.
- Every state-changing form is protected by a session-bound CSRF token.
- LinkedIn tokens are encrypted before database storage.
- Publishing requires `PUBLISH_ENABLED=true`, `ALLOW_EXTERNAL_WRITES=true`, and an approved exact draft.
- Publishing uses an atomic status transition and records the LinkedIn post identifier to prevent duplicates.
- Email reminders use idempotency keys and run at most once in a 20-hour window.
- AI (free Gemini API) only returns text. Claims must quote the source verbatim, every number in a
  draft must appear in the source, and drafts may not claim personal experience or name the employer.
- When Gemini is unavailable, the run falls back to rule-based briefs and template drafts.
- X publishing and automated outreach are not implemented.

## Local setup

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1
.venv\Scripts\python.exe -m brand_agent run
.venv\Scripts\python.exe -m brand_agent serve
```

Open <http://127.0.0.1:8765>.

Useful commands:

```powershell
.venv\Scripts\python.exe -m brand_agent doctor
.venv\Scripts\python.exe -m brand_agent status
.venv\Scripts\python.exe -m brand_agent daily
.venv\Scripts\python.exe -m brand_agent remind
.venv\Scripts\python.exe -m brand_agent ai-check     # Gemini model availability
.venv\Scripts\python.exe -m brand_agent ai-preview   # AI run on one item, no database writes
.venv\Scripts\python.exe -m pytest
```

## Configuration

- [`config/profile.yaml`](config/profile.yaml): identity, goals, audience, voice, and content pillars.
- [`config/agent.yaml`](config/agent.yaml): research sources, scoring, drafting, and safe defaults.
- [`.env.example`](.env.example): environment variable reference with no real secrets.
- [`render.yaml`](render.yaml): Render Blueprint.
- [`.github/workflows/daily-agent.yml`](.github/workflows/daily-agent.yml): daily cloud worker.

The complete account setup and deployment procedure is in [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

## Current limitations

- Free Render instances sleep when idle, so opening the dashboard can have a cold start.
- Free-tier Gemini models are sometimes overloaded; affected runs fall back to templates. Each draft's
  rationale names the model or template that wrote it.
- Paywalled papers are analysed from their abstract only.
- LinkedIn self-service access tokens expire, so periodic reconnection may be required.
- Free tiers are suitable for this personal pilot but do not provide production SLAs.
