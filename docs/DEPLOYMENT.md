# Cloud deployment guide

This guide takes the repository from local development to a private daily cloud agent. Complete the steps in order because LinkedIn needs the final HTTPS callback URL from Render.

## 1. Create the Neon database

1. Sign in at <https://console.neon.tech>.
2. Create a free project named `frontier-brand-agent`.
3. Open **Connect** and copy the pooled PostgreSQL connection string.
4. Keep it private. It will be stored as `DATABASE_URL` in Render and GitHub Actions.

The application creates its tables automatically on the first connection. The cloud database starts fresh; the daily workflow will repopulate research. The ignored local SQLite file is not uploaded to GitHub.

## 2. Create the Resend reminder key

1. Sign in at <https://resend.com> with the email that should receive reminders.
2. Create an API key limited to sending access.
3. During personal testing, use the email associated with the Resend account as `REMINDER_EMAIL`.
4. If you later want a branded sender, verify a domain and set `RESEND_FROM_EMAIL` in Render or the worker.

## 3. Deploy the Render Blueprint

1. Sign in at <https://dashboard.render.com> and connect GitHub.
2. Create a **Blueprint** and select this repository.
3. Render reads `render.yaml` and prompts for every `sync: false` value.
4. Enter:

| Variable | Value |
|---|---|
| `DATABASE_URL` | Neon pooled connection string |
| `ADMIN_EMAIL` | Email returned by your LinkedIn account |
| `TOKEN_ENCRYPTION_KEY` | Fernet key generated below |

Generate the encryption key locally:

```powershell
.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Do not lose or rotate this key while an OAuth token is stored. If it is lost, delete the stored LinkedIn credential and reconnect.

The app automatically derives its public URL from Render. Set `BASE_URL` manually only if you later attach a custom domain.

The health endpoint is `/api/health`. A correct deployment returns `status: ok` and `database_backend: postgresql`.

## 4. Create the LinkedIn developer application

Do not provide the application with your LinkedIn password.

1. Go to <https://www.linkedin.com/developers/apps>.
2. Create an application associated with a LinkedIn Page you administer.
3. Add these products:
   - **Sign In with LinkedIn using OpenID Connect**
   - **Share on LinkedIn**
4. In the Auth settings, add this exact authorized redirect URL:

```text
https://your-actual-service.onrender.com/auth/linkedin/callback
```

5. Copy the Client ID into a new Render secret named `LINKEDIN_CLIENT_ID`.
6. Reveal the Client Secret and enter it directly in a new Render secret named `LINKEDIN_CLIENT_SECRET`.
7. Never paste the Client Secret into GitHub files, issues, commits, or chat.
8. Redeploy the Render service.
9. Visit the Render URL and complete LinkedIn authorization.

The OAuth request uses `openid profile email w_member_social`. The application rejects any LinkedIn account whose returned email does not exactly match `ADMIN_EMAIL`.

## 5. Configure GitHub Actions secrets

Open the repository on GitHub, then go to:

**Settings → Secrets and variables → Actions → New repository secret**

Create exactly these secrets:

| Secret | Value |
|---|---|
| `DATABASE_URL` | Same Neon pooled connection string |
| `BASE_URL` | Actual Render HTTPS URL |
| `RESEND_API_KEY` | Resend API key |
| `REMINDER_EMAIL` | Your reminder recipient email |

The daily workflow intentionally does not receive the LinkedIn Client Secret or access token. Only the authenticated dashboard can publish.

## 6. Test the daily worker

1. Open **Actions → Daily brand agent**.
2. Select **Run workflow**.
3. Confirm that the `Research, draft, and remind` step succeeds.
4. Confirm that new rows appear in the dashboard.
5. Confirm that the reminder email arrives.

The normal schedule is 08:17 every day in `Asia/Kolkata`. The non-round minute reduces the risk of GitHub schedule congestion.

## 7. Test approval without publishing

Render initially deploys with:

```text
PUBLISH_ENABLED=false
ALLOW_EXTERNAL_WRITES=false
```

Keep both disabled while testing:

1. Open a LinkedIn draft.
2. Edit the text.
3. Read the source, claims, and limitations.
4. Complete all three approval checks.
5. Approve it.
6. Verify that the status becomes `approved` and nothing appears on LinkedIn.

## 8. Enable real LinkedIn publishing

Only after the dry run succeeds, change these Render environment variables:

```text
PUBLISH_ENABLED=true
ALLOW_EXTERNAL_WRITES=true
```

Redeploy, create a fresh test draft, and approve it. For a LinkedIn draft, the button becomes **Approve & publish to LinkedIn**. A successful request stores the returned LinkedIn post reference and changes the status to `published`.

If the LinkedIn API fails, the draft changes to `publish_failed` and displays a retry button. It never silently treats a failed request as published.

## 9. Secret rotation and recovery

- **LinkedIn access revoked or expired:** sign out and reconnect through the dashboard.
- **LinkedIn Client Secret rotated:** update only Render and redeploy.
- **Resend key rotated:** update only the GitHub Actions secret.
- **Database credential rotated:** update both Render and GitHub Actions.
- **Token encryption key lost:** generate a new key, update Render, and reconnect LinkedIn.
- **Accidental secret commit:** revoke the credential first, then remove it from Git history.

## Free-tier behavior

- Render may sleep after inactivity; opening the emailed dashboard link wakes it.
- GitHub Actions runs the daily worker independently of Render sleeping.
- Neon may suspend compute while idle and automatically resumes on a connection.
- Free tiers can change and provide no uptime guarantee. Before supporting external users, move to paid infrastructure, add migrations/backups, and complete a security review.
