# Cloud Infra Service Desk (Job Portal) — Community Edition

A job and ticket portal for a cloud infrastructure team: assign, track and escalate IT jobs, with AI-assisted analysis, plus the team's cloud tooling in one place (cost, CMDB, security, DNS, health, monitoring, patching schedules, chat, calendars).

This is the **organisation-neutral** edition. Nothing about a particular company is built in: your name, domains, admin users, AWS accounts and third-party integrations are all configuration (see [Configuration](#configuration)). Features that depend on a specific third-party tool or AWS set-up are **off by default** and appear in the menu only when you enable them.

- **Stack:** React 18 + Vite frontend, FastAPI (Python) backend, AWS CDK (Python) infrastructure.
- **Local mode:** mock login, SQLite and a rule-based AI, so you can try it with no AWS account.
- **AWS mode:** CloudFront + S3, ECS Fargate behind an ALB with Microsoft Entra ID (Azure AD) SSO, DynamoDB, Amazon Bedrock, SES.

---

## Features

Always available (they only need AWS access to the accounts you register):

| Area | What it does |
|---|---|
| Tickets | Create, assign, comment, attach files, status flow, audit log, due dates, AI analysis of each job |
| Knowledge Base | Upload documents that ground the AI assistant |
| Cloud Accounts | Admins register AWS accounts and Azure subscriptions, verify access, set business units |
| Cost | Cost Analysis, Billing by Service, Savings Plan cost, manual cost adjustments |
| Resources | CMDB (EC2, RDS, S3, DynamoDB, load balancers, topology), AWS Health dashboard |
| Security | AWS Security Hub scores and findings, WAF rule monitoring, DNS records (Route 53) with audit log |
| Lifecycle | Patching schedules and Microsoft Teams meeting links |
| Monitoring | Website monitoring from several AWS regions (CloudWatch Synthetics canaries) |
| Collaboration | Chat, voice/video calls (Amazon Chime SDK), calendars, worldwide public holidays |
| Administration | Users, roles, user groups, per-tab access control, feedback and bug reports with AI triage |

Optional integrations (set in `ENABLED_FEATURES`, each needs its own settings):

`freshservice` · `aws_ams` · `s2s_vpn` · `vpn_lookup` · `whitelist_lms` · `devops_agent` · `security_portal` · `screensaver` · `licensing_dashboard` · `meraki` · `fortigate` · `sonicwall` · `azure_monitor` · `fs_calendar` · `sr_ticket` · `resource_inventory`

---

## Quick start (local, no AWS needed)

Prerequisites: Python 3.12+, Node.js 20+.

```powershell
# Backend
cd backend
python -m venv .venv ; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env          # then edit SEED_ADMIN_EMAILS etc.
python -m uvicorn app.main:app --reload --port 8101

# Frontend (new terminal)
cd frontend
npm install
npm run dev                          # http://localhost:5200
```

`frontend/vite.config.js` proxies `/api` to port 8101. Sign in by picking a user on the login page (local mock auth). API docs: http://localhost:8101/docs.

Run the backend tests (they use `moto`, no real AWS):

```powershell
cd backend
pip install -r requirements-dev.txt
python -m pytest tests -q
```

On the original code base three `test_auth_alb` tests and two `test_escalation` tests fail for reasons unrelated to configuration; expect the same five failures here.

---

## Configuration

All organisation-specific settings are environment variables read by `backend/app/org_config.py`. Locally put them in `backend/.env` (see `backend/.env.example`); in AWS they are set on the ECS task by the CDK from `infra-cdk/config.json`.

| Variable | Purpose |
|---|---|
| `SEED_ADMIN_EMAILS` | **Required.** Comma-separated founding admins; they can never be locked out |
| `ORG_NAME`, `PORTAL_DOMAIN`, `API_DOMAIN`, `ORG_EMAIL_DOMAIN` | Display name, public hosts, e-mail domain (also used for CORS) |
| `SES_SENDER_ADDRESS` | Verified Amazon SES sender for notifications |
| `PORTAL_HOST_ACCOUNT_ID` | AWS account that hosts the portal |
| `CORE_ACCOUNT_ID` | Hub account whose IAM identity the portal uses to reach the other accounts (cost, CMDB, Security Hub, DNS) |
| `KNOWN_BUSINESS_UNITS` | Business units offered on the Cloud Accounts page |
| `CLOUD_ACCOUNT_SEED_FILE` | Optional JSON file that pre-loads the account registry (otherwise start empty and add accounts in the UI) |
| `ENABLED_FEATURES` | Optional integrations to switch on |
| `FRESHSERVICE_DOMAIN` | Enables Freshservice ticket escalation |
| Integration settings | `S2S_VPN_TGW_CONFIG`, `SECURITY_PORTAL_API_BASE`, `MERAKI_ORG_ID`, `RESOURCE_INVENTORY_BUCKET`, `SCREENSAVER_*`, `LICENSING_DASHBOARD_URL`, ... (see `.env.example`) |

Secrets (API keys, cross-account credentials) are **never** environment variables: they live in AWS Secrets Manager under the `job-portal/...` names listed in `infra-cdk/infra_cdk/stacks/compute_stack.py`, and the ECS task role is granted read access to exactly those.

The frontend reads `GET /api/org-config` after sign-in, so it shows your organisation name and hides integrations that are switched off. Nothing is hard-coded in the web app.

### Cloud account access model

- The **core account** holds an IAM user whose access key is stored in Secrets Manager (`job-portal/core-admin-credentials`). The portal uses it to query the core account directly and to `sts:AssumeRole` into `PortalReadOnlyRole` in each legacy member account.
- Accounts you add on the **Cloud Accounts** page use a dedicated cross-account role created from the CloudFormation template in `backend/app/cloud_account_templates/infraops_role.yaml`, so no shared keys are needed for them.
- The AI features call Amazon Bedrock, optionally in a separate account through `infra-cdk/bedrock-account/bedrock-invoke-role.yaml`.

---

## Deploying to AWS

1. **Prepare** (once): a Route 53 / DNS name for the portal, an ACM certificate for it in the deployment region, a Microsoft Entra ID app registration (redirect URI `https://<portal domain>/oauth2/idpresponse`), and its client secret stored as the Secrets Manager secret `job-portal/azure-ad-client-secret`.
2. **Configure:** `Copy-Item infra-cdk\config.example.json infra-cdk\config.json` and fill it in (`account_id`, `custom_domain`, `acm_certificate_arn`, `azure_*`, `seed_admin_emails`, ...). Extra backend settings go under `extra_env`.
3. **Bootstrap and install:**
   ```powershell
   cd infra-cdk
   python -m venv .venv ; .\.venv\Scripts\Activate.ps1 ; pip install -r requirements.txt
   cdk bootstrap
   ```
4. **Deploy the base stacks, then the image, then the rest:**
   ```powershell
   .\deploy.ps1 -Action deploy -Stacks JobPortalNetworkStack,JobPortalDataStack,JobPortalSecurityStack
   .\build_and_push_backend.ps1 -AccountId <account id> -Region <region>      # creates the ECR repo and pushes :latest
   .\deploy.ps1 -Action deploy -Stacks JobPortalComputeStack
   cd ..\frontend ; npm ci ; npm run build ; cd ..\infra-cdk
   .\deploy.ps1 -Action deploy -Stacks JobPortalFrontendStack
   ```
5. **DNS:** point your portal domain (CNAME) at the CloudFront domain and your API domain at the ALB (`AlbDnsName` stack output).
6. Redeploy the backend later with `.\build_and_push_backend.ps1 ... -Redeploy`.

Review the IAM policies and Secrets Manager names in `compute_stack.py` before you deploy, and enable only the integrations you need.

Website monitoring needs its own stacks: `cd website-monitor ; .\deploy.ps1 -AlertEmail you@example.com -AwsProfile <profile>` (edit `website-monitor/sites.json` first).

---

## Mobile and desktop apps (optional)

- **Android / iOS:** a [Capacitor](https://capacitorjs.com) shell in `frontend/android` and `frontend/ios` that loads your portal URL. Before building set `appId` and `server.url` in `frontend/capacitor.config.json` and the matching package name in `frontend/android/app/build.gradle`, `strings.xml` and the Java package (`com.example.cloudinfraservicedesk`). Sign release builds with your own keystore (kept outside the repo; see `frontend/build-android.ps1`).
- **Windows desktop:** a [Tauri](https://tauri.app) shell in `frontend/src-tauri`. Set `app.windows[0].url` and, if you want in-app updates, your own updater key pair (`pubkey`) and `endpoints` in `tauri.conf.json`. The default logo is the Tauri logo; replace the icons in `frontend/src-tauri/icons` and `frontend/public`.

---

## Repository layout

```
backend/        FastAPI app (app/), tests (tests/), Dockerfile
frontend/       React app (src/), Capacitor (android/, ios/), Tauri (src-tauri/)
infra-cdk/      AWS CDK stacks, config.example.json, deploy.ps1, build_and_push_backend.ps1
website-monitor/ CloudFormation + canary code for multi-region website checks
```

## Security notes

- Auth in AWS is Microsoft Entra ID through the ALB OIDC action; the backend verifies the ALB-signed JWT on every request.
- Write operations (DNS changes, VPN creation, IP whitelisting) require a change/ticket reference and are written to an audit table.
- Do not commit `infra-cdk/config.json`, `backend/.env`, keystores or any credentials. `.gitignore` excludes the common ones.
- Report vulnerabilities privately to the maintainers rather than in a public issue.

## License

MIT. See `LICENSE`.
