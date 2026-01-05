# Detailed Setup Guide

This guide walks through setting up Agent Home Orchestrator for local development and testing.

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Step 1: Google OAuth Setup](#step-1-google-oauth-setup)
3. [Step 2: Modal Account Setup](#step-2-modal-account-setup)
4. [Step 3: GitHub Token](#step-3-github-token)
5. [Step 4: Environment Configuration](#step-4-environment-configuration)
6. [Step 5: Install Dependencies](#step-5-install-dependencies)
7. [Step 6: Deploy Agent Home to Modal](#step-6-deploy-agent-home-to-modal)
8. [Step 7: Run the Webapp Locally](#step-7-run-the-webapp-locally)
9. [Step 8: Test the Full Flow](#step-8-test-the-full-flow)
10. [Troubleshooting](#troubleshooting)

---

## Prerequisites

Before starting, ensure you have:

- **Python 3.11+** installed
- **Node.js 18+** installed (for frontend)
- **Git** installed
- A **Google account** (for OAuth)
- An **Anthropic API key** (get one at [console.anthropic.com](https://console.anthropic.com))
- A **GitHub account** and personal access token

---

## Step 1: Google OAuth Setup

You need Google OAuth credentials for user authentication.

### 1.1 Create a Google Cloud Project

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Click "Select a project" → "New Project"
3. Name it (e.g., `agent-home-dev`) and create

### 1.2 Enable the APIs

1. Go to "APIs & Services" → "Library"
2. Search for and enable:
   - **Google+ API** (or "Google People API")
   - **Google OAuth2 API**

### 1.3 Configure OAuth Consent Screen

1. Go to "APIs & Services" → "OAuth consent screen"
2. Select "External" and click "Create"
3. Fill in:
   - App name: `Agent Home` (or anything)
   - User support email: your email
   - Developer contact: your email
4. Click "Save and Continue"
5. On Scopes page, click "Add or Remove Scopes"
   - Add: `openid`, `email`, `profile`
6. Continue through and save

### 1.4 Create OAuth Credentials

1. Go to "APIs & Services" → "Credentials"
2. Click "Create Credentials" → "OAuth client ID"
3. Application type: **Web application**
4. Name: `Agent Home Webapp`
5. Add Authorized JavaScript origins:
   ```
   http://localhost:3000
   http://localhost:8000
   ```
6. Add Authorized redirect URIs:
   ```
   http://localhost:8000/auth/callback
   ```
7. Click "Create"
8. **Save the Client ID and Client Secret** - you'll need these!

---

## Step 2: Modal Account Setup

### 2.1 Create Modal Account

1. Go to [modal.com](https://modal.com) and sign up
2. Install the Modal CLI:
   ```bash
   pip install modal
   ```
3. Authenticate:
   ```bash
   modal token new
   ```

### 2.2 Create Modal Secret

Create a secret with all required environment variables:

```bash
modal secret create agent-home-secrets \
  ANTHROPIC_API_KEY="sk-ant-..." \
  GITHUB_TOKEN="ghp_..." \
  AGENT_HOME_JWT_SECRET="$(openssl rand -hex 32)" \
  AGENT_HOME_GOOGLE_CLIENT_ID="your-client-id.apps.googleusercontent.com" \
  AGENT_HOME_GOOGLE_CLIENT_SECRET="GOCSPX-..." \
  AGENT_HOME_ALLOWED_EMAILS="your@email.com"
```

**Note:** Replace each value with your actual credentials.

To update an existing secret:
```bash
modal secret create agent-home-secrets --force \
  ANTHROPIC_API_KEY="..." \
  # ... rest of values
```

---

## Step 3: GitHub Token

Create a GitHub Personal Access Token for worker sandboxes to clone repos and create PRs.

1. Go to [GitHub Settings → Developer settings → Personal access tokens → Tokens (classic)](https://github.com/settings/tokens)
2. Click "Generate new token (classic)"
3. Give it a name (e.g., `agent-home-worker`)
4. Select scopes:
   - `repo` (full control of private repositories)
   - `workflow` (if you need to trigger GitHub Actions)
5. Generate and **save the token**

---

## Step 4: Environment Configuration

### 4.1 Create `.env` file for Webapp

Create `webapp/backend/.env`:

```bash
# Google OAuth (from Step 1)
WEBAPP_GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
WEBAPP_GOOGLE_CLIENT_SECRET=GOCSPX-your-secret

# JWT Secret (generate with: openssl rand -hex 32)
WEBAPP_JWT_SECRET=your-jwt-secret-here

# Allowed emails (comma-separated)
WEBAPP_ALLOWED_EMAILS=your@email.com,another@email.com

# Frontend URL for redirects
WEBAPP_FRONTEND_URL=http://localhost:3000

# OAuth callback URL
WEBAPP_GOOGLE_REDIRECT_URI=http://localhost:8000/auth/callback

# Modal app name
WEBAPP_AGENT_HOME_MODAL_APP=agent-home-orchestrator

# Cookie security (set to true in production with HTTPS)
WEBAPP_COOKIE_SECURE=false
```

### 4.2 Create `.env` file for Agent Home (optional, for local testing)

Create a root `.env` file if you want to test components locally:

```bash
# Anthropic
AGENT_HOME_ANTHROPIC_API_KEY=sk-ant-...

# GitHub
AGENT_HOME_GITHUB_TOKEN=ghp_...

# Auth
AGENT_HOME_JWT_SECRET=your-jwt-secret-here
AGENT_HOME_GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
AGENT_HOME_GOOGLE_CLIENT_SECRET=GOCSPX-...
AGENT_HOME_ALLOWED_EMAILS=your@email.com
```

---

## Step 5: Install Dependencies

### 5.1 Python Dependencies

From the project root:

```bash
# Create virtual environment (recommended)
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install the package in development mode
pip install -e ".[dev]"
```

### 5.2 Frontend Dependencies

```bash
cd webapp/frontend
npm install
```

---

## Step 6: Deploy Agent Home to Modal

This deploys the Agent Home orchestrator to Modal's cloud.

```bash
# From project root
modal deploy modal_app/agent_home.py
```

You should see output like:
```
✓ Created mount /home/user/...
✓ Created AgentHomeSandbox
✓ Created ensure_agent_home_running
✓ Created mint_access_token
✓ App agent-home-orchestrator deployed!

Web: https://your-username--agent-home-orchestrator-agenthomesandbox-web.modal.run
```

**Note the web URL** - this is your Agent Home endpoint.

### Verify Deployment

Check that the deployment works:

```bash
modal app logs agent-home-orchestrator
```

You can also check the Modal dashboard at [modal.com/apps](https://modal.com/apps).

---

## Step 7: Run the Webapp Locally

You need to run both the backend and frontend.

### Terminal 1: Backend

```bash
cd webapp/backend

# Make sure you have the .env file set up (Step 4.1)
# Activate your virtual environment if not already active
source ../../venv/bin/activate

# Run the backend
uvicorn app:app --reload --port 8000
```

The backend will be available at `http://localhost:8000`

### Terminal 2: Frontend

```bash
cd webapp/frontend

# Run the frontend dev server
npm run dev
```

The frontend will be available at `http://localhost:3000`

---

## Step 8: Test the Full Flow

### 8.1 Login

1. Open `http://localhost:3000` in your browser
2. Click "Sign in with Google"
3. Complete the Google OAuth flow
4. You should be redirected back and see your email in the header

### 8.2 Create a Conversation

1. Click "New Conversation"
2. Enter a title (e.g., "Test Task")
3. Click "Create"

### 8.3 Send a Message

1. In the chat view, type a message like:
   ```
   Please analyze the repository https://github.com/your-user/your-repo
   and tell me what it does.
   ```
2. Press Enter or click Send

### 8.4 Review and Approve the Plan

1. Wait for the orchestrator to create a plan (you'll see "Planning...")
2. Once the plan appears, review it
3. Click "Approve" to execute

### 8.5 Watch Execution

1. The orchestrator will spawn worker sandboxes
2. You'll see real-time updates as workers execute
3. Once complete, you'll see the results

---

## Troubleshooting

### "Agent Home Modal app not deployed"

The webapp can't find the Agent Home on Modal.

**Fix:**
```bash
modal deploy modal_app/agent_home.py
```

### "OAuth callback error"

The redirect URI doesn't match what's configured in Google Cloud.

**Fix:**
1. Go to Google Cloud Console → APIs & Services → Credentials
2. Edit your OAuth client
3. Ensure "Authorized redirect URIs" includes: `http://localhost:8000/auth/callback`

### "Email not authorized"

Your email isn't in the allowed list.

**Fix:** Add your email to:
1. `webapp/backend/.env` → `WEBAPP_ALLOWED_EMAILS`
2. Modal secret → `AGENT_HOME_ALLOWED_EMAILS`

Then update the Modal secret:
```bash
modal secret create agent-home-secrets --force ...
```

### "Failed to connect to Agent Home"

The Agent Home container might not be running.

**Fix:**
1. Check Modal logs:
   ```bash
   modal app logs agent-home-orchestrator
   ```
2. Redeploy:
   ```bash
   modal deploy modal_app/agent_home.py
   ```

### Frontend shows "Disconnected"

WebSocket connection to Agent Home failed.

**Fix:**
1. Check if Agent Home is running (see above)
2. Check browser console for errors
3. Ensure your JWT hasn't expired (try logging out and back in)

### "CORS error" in browser console

Backend CORS configuration doesn't allow the frontend origin.

**Fix:** Ensure `webapp/backend/.env` has:
```
WEBAPP_FRONTEND_URL=http://localhost:3000
```

### Workers fail with "git clone failed"

GitHub token is missing or invalid.

**Fix:**
1. Generate a new GitHub token (Step 3)
2. Update Modal secret with new token:
   ```bash
   modal secret create agent-home-secrets --force \
     GITHUB_TOKEN="ghp_new_token" \
     # ... other values
   ```
3. Redeploy:
   ```bash
   modal deploy modal_app/agent_home.py
   ```

---

## Development Tips

### View Modal Logs

```bash
# Stream logs in real-time
modal app logs agent-home-orchestrator --follow

# View specific function logs
modal function logs agent-home-orchestrator/spawn_worker
```

### Redeploy After Code Changes

```bash
modal deploy modal_app/agent_home.py
```

### Check Modal Volume Contents

```bash
# List files in the volume
modal volume ls agent-home-volume

# Download a file
modal volume get agent-home-volume /db/state.sqlite ./local-copy.sqlite
```

### Reset Agent Home State

To start fresh, delete and recreate the volume:

```bash
modal volume delete agent-home-volume
modal deploy modal_app/agent_home.py  # Will recreate volume
```

### Local Testing Without Modal

For faster iteration on orchestrator logic, you can test components locally:

```python
# test_orchestrator.py
import asyncio
from pathlib import Path
from agent_home.persistence import EventStore, StateManager
from agent_home.orchestrator import HomeOrchestrator

async def main():
    db_path = Path("./test.sqlite")
    memory_path = Path("./test_memory")
    memory_path.mkdir(exist_ok=True)

    event_store = EventStore(db_path)
    await event_store.initialize()

    state_manager = StateManager(db_path)
    await state_manager.initialize()

    orchestrator = HomeOrchestrator(
        state_manager=state_manager,
        event_store=event_store,
        memory_path=memory_path,
    )

    # Test orchestrator methods...

asyncio.run(main())
```

---

## Production Deployment

For production, you'll want to:

1. **Use HTTPS**: Set `secure=True` in cookie settings
2. **Deploy webapp to a server**: e.g., Railway, Render, or your own VM
3. **Update OAuth redirect URIs**: Change from localhost to your production domain
4. **Set up monitoring**: Use Modal's built-in metrics or add your own

### Example Production `.env`

```bash
WEBAPP_GOOGLE_REDIRECT_URI=https://your-app.com/auth/callback
WEBAPP_FRONTEND_URL=https://your-app.com
WEBAPP_COOKIE_SECURE=true
```

And update your Google OAuth credentials to include the production URLs.
