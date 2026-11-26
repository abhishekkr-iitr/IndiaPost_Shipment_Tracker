# India Post AI Shipment Assistant — Production Operations & Deployment Runbook
**Milestone 3 — Stage 15 Operational Guide**

---

## 1. System Architecture & Component Mapping

The India Post AI Shipment Assistant is composed of three cooperating service subsystems orchestrated under a single unified process tree:

| Service Subsystem | Entry Point | Default Port / Interval | Responsibility |
| :--- | :--- | :--- | :--- |
| **Web Server & UI** | `app.py` via Uvicorn | `http://127.0.0.1:8000` | FastAPI server for Browser UI, AI Agent endpoint, Passwordless Auth API, Guest Notifications API, and Telegram Webhook. |
| **Monitoring Scheduler** | `run_monitoring_scheduler.py` | Polls every 60s (configurable) | Claims due shipment monitoring jobs, polls tracking updates, records checkpoints, and triggers change detection. |
| **Notification Scheduler** | `run_notification_scheduler.py` | Polls every 10s (configurable) | Claims queued outbox records, rechecks fail-closed delivery authorization, and dispatches via registered adapters. |

All three subsystems can be started, supervised, and cleanly terminated together via `orchestrator.py`.

---

## 2. Standard Operating Procedures

### 2.1 Starting the Services

#### Local / Windows Development:
```powershell
# In PowerShell:
.\scripts\start_assistant.ps1
```
Or directly using Python in the virtual environment:
```powershell
& ".\venv\Scripts\python.exe" orchestrator.py --host 127.0.0.1 --port 8000
```

#### Linux Production (Systemd):
```bash
sudo systemctl daemon-reload
sudo systemctl enable indiapost-assistant
sudo systemctl start indiapost-assistant
sudo systemctl status indiapost-assistant
```

### 2.2 Graceful Termination
The orchestrator traps `SIGINT` (Ctrl+C) and `SIGTERM`. It stops each worker gracefully, waiting up to 5 seconds for in-flight requests or database writes to finish before closing connections.

### 2.3 Running Health & Diagnostic Inspections
To inspect database consistency, table record counts, active worker queues, and channel configurations:
```powershell
& ".\venv\Scripts\python.exe" scripts/health_inspector.py
```
Exit code `0` indicates a fully healthy system. Any failure returns non-zero with explicit diagnostic guidance.

---

## 3. Database Maintenance, Backup & Disaster Recovery

### 3.1 Online Database Backup
The system database (`database/shipments.db`) supports transactional online backups without stopping the service:
```powershell
& ".\venv\Scripts\python.exe" scripts/backup_database.py --keep 10
```
- Performs a zero-lock SQLite online backup to `database/backup/shipments_backup_<YYYYMMDD_HHMMSS>/shipments.db`.
- Automatically executes `PRAGMA integrity_check` and `PRAGMA foreign_key_check` on the backup.
- Automatically retains the latest 10 snapshots and prunes older ones.

### 3.2 Backup Verification
To verify the integrity of an existing backup snapshot:
```powershell
& ".\venv\Scripts\python.exe" scripts/backup_database.py --verify database/backup/shipments_backup_20261120_163619/shipments.db
```

### 3.3 Database Disaster Recovery (Restore Procedure)
In the event of database corruption or hardware recovery:
1. Stop the assistant services:
   ```powershell
   # Stop orchestrator or systemctl stop indiapost-assistant
   ```
2. Identify the desired verified backup directory in `database/backup/`.
3. Preserve the existing damaged database:
   ```powershell
   Move-Item database/shipments.db database/shipments_corrupted_before_restore.db
   ```
4. Copy the backup database into place:
   ```powershell
   Copy-Item database/backup/<backup_dir>/shipments.db database/shipments.db
   ```
5. Run the health inspector to verify consistency:
   ```powershell
   & ".\venv\Scripts\python.exe" scripts/health_inspector.py
   ```
6. Restart the assistant service.

---

## 4. Live Provider Onboarding & Credential Rotation

All external provider keys remain in `.env` and must never be committed to source code or shared in plaintext.

### 4.1 OpenAI API Key Configuration & HTTP 401 Reconciliation
- **Symptoms**: Browser chat returns "Error: 401 Unauthorized" when sending queries to the AI agent.
- **Root Cause**: The API key is either invalid, revoked, not permitted for model `gpt-4o`, or scoped to an organization/project different from the key's billing profile.
- **Reconciliation Steps**:
  1. Verify the key at [platform.openai.com/api-keys](https://platform.openai.com/api-keys).
  2. Confirm the key has permissions for `gpt-4o` under the correct Project.
  3. Update `.env`:
     ```env
     OPENAI_API_KEY=sk-proj-...
     ```
  4. Run health check to verify configuration:
     ```powershell
     & ".\venv\Scripts\python.exe" scripts/health_inspector.py
     ```

### 4.2 AfterShip API Subscription Renewal
- **Symptoms**: Polling monitoring jobs fail or return subscription expired errors.
- **Root Cause**: The developer trial or paid AfterShip plan requires renewal.
- **Reconciliation Steps**:
  1. Renew subscription at [aftership.com](https://www.aftership.com).
  2. Generate a fresh API key.
  3. Update `.env`:
     ```env
     AFTERSHIP_API_KEY=asat_...
     AFTERSHIP_BASE_URL=https://api.aftership.com/tracking/2024-04
     ```
  4. Execute a single synthetic tracking check to verify adapter readiness before enabling live polling.

### 4.3 Live Telegram Bot Onboarding
To enable live outbound Telegram delivery and inbound deep linking:
1. Contact `@BotFather` on Telegram to create a bot:
   - Note the bot username (e.g. `IndiaPostShipmentBot`) and Bot Token (e.g. `123456789:ABCdef...`).
2. Generate a high-entropy secret token for webhook verification:
   ```powershell
   & ".\venv\Scripts\python.exe" -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
3. Update `.env`:
   ```env
   TELEGRAM_BOT_TOKEN=123456789:ABCdef...
   TELEGRAM_BOT_USERNAME=IndiaPostShipmentBot
   TELEGRAM_WEBHOOK_SECRET=<generated_secret>
   ```
4. Register the webhook with Telegram:
   ```bash
   curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
        -H "Content-Type: application/json" \
        -d '{"url": "https://<your-public-domain>/api/webhooks/telegram", "secret_token": "<TELEGRAM_WEBHOOK_SECRET>"}'
   ```
5. Confirm registration status:
   ```bash
   curl -s "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getWebhookInfo"
   ```

---

## 5. Troubleshooting & Operational Failure Modes

| Issue | Detection | Resolution |
| :--- | :--- | :--- |
| **Database Locked / Busy** | `sqlite3.OperationalError: database is locked` in logs | All database transactions use `BEGIN IMMEDIATE` or the online backup API. Ensure no external SQLite browser has an uncommitted write lock. |
| **Stale Worker Claims** | Monitoring jobs or notifications stay in `processing` | The scheduler automatically reclaims expired leases when `lease_expires_at <= current_time`. Check system clock synchronization. |
| **OTP Rate Limit Exceeded** | HTTP 429 on `/api/auth/challenge` | The user has requested more than 3 active challenges within the 10-minute TTL window. Wait for expiry or consume the code. |
| **Notification Dropped / Unauthorized** | Outbox record marked `unauthorized` | The subscriber revoked channel consent, disabled tracking, or revoked their guest identity before the message dispatched. This is expected fail-closed behaviour. |
