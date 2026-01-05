# Agent Home Orchestrator

A persistent AI coding assistant that lives in the cloud, accessible from anywhere.

## Overview

Agent Home is a system that provides:

- **Persistent Agent Home**: A long-running orchestrator where "your Claude Code agent lives"
- **Multi-Conversation Support**: Handle multiple async conversations concurrently
- **Async Execution**: Runs continue even if your browser disconnects
- **Approval-Based Workflow**: All work requires explicit user approval before execution
- **Ephemeral Workers**: Code execution happens in isolated Modal sandboxes

## Architecture

```
┌──────────────┐     ┌─────────────────────────────────────────┐
│   Browser    │     │              Modal Cloud                │
│              │     │                                         │
│  ┌────────┐  │     │  ┌─────────────────────────────────┐   │
│  │ Webapp │──┼─────┼──│        Agent Home               │   │
│  │Frontend│  │     │  │  ┌─────────┐  ┌──────────────┐  │   │
│  └────────┘  │     │  │  │ Gateway │  │ Orchestrator │  │   │
│              │     │  │  │ (HTTP+WS│  │  (Claude)    │  │   │
│              │     │  │  └─────────┘  └──────────────┘  │   │
│              │     │  │  ┌─────────┐  ┌──────────────┐  │   │
│              │     │  │  │Persist. │  │   Curator    │  │   │
│              │     │  │  │(SQLite) │  │  (Memory)    │  │   │
│              │     │  │  └─────────┘  └──────────────┘  │   │
│              │     │  └──────────────────┬──────────────┘   │
│              │     │                     │                   │
│              │     │         ┌───────────┴───────────┐      │
│              │     │         ▼                       ▼      │
│              │     │  ┌────────────┐         ┌────────────┐ │
│              │     │  │  Worker    │         │  Worker    │ │
│              │     │  │  Sandbox   │  ...    │  Sandbox   │ │
│              │     │  │ (ephemeral)│         │ (ephemeral)│ │
│              │     │  └────────────┘         └────────────┘ │
└──────────────┘     └─────────────────────────────────────────┘
```

## Components

### Webapp

- Email/password authentication
- Conversations list, chat view, run approval UI
- Direct WebSocket connection to Agent Home for real-time updates

### Agent Home (Modal Sandbox)

- **Gateway API**: HTTP + WebSocket endpoints for webapp communication
- **Home Orchestrator**: Claude-powered agent that plans and coordinates work
- **Memory Curator**: Background loop that extracts facts and creates self-prompts
- **Persistence**: SQLite + Modal Volume for durable state

### Worker Sandboxes (Modal Sandboxes)

- Ephemeral containers that execute actual code work
- Clone repos, run tests/builds, make changes, create PRs
- Full Claude agent with code tools inside each worker

## Quick Start

### Prerequisites

- Python 3.11+
- Node.js 18+ (for frontend)
- Modal account with CLI configured
- Anthropic API key

### 1. Install Dependencies

```bash
# Install Python dependencies
pip install -e .

# Install frontend dependencies
cd webapp/frontend
npm install
```

### 2. Configure Modal Secrets

Create a Modal secret named `agent-home-secrets`:

```bash
modal secret create agent-home-secrets \
  ANTHROPIC_API_KEY=your_anthropic_key \
  GITHUB_TOKEN=your_github_token \
  AGENT_HOME_JWT_SECRET=$(openssl rand -hex 32)
```

### 3. Deploy to Modal

```bash
modal deploy modal_app/agent_home.py
```

### 4. Configure Webapp

Create `webapp/backend/.env`:

```bash
WEBAPP_ADMIN_EMAIL=admin@example.com
WEBAPP_ADMIN_PASSWORD=changeme
WEBAPP_AGENT_HOME_MODAL_APP=agent-home-orchestrator
```

### 5. Run the Webapp

```bash
# Backend
python3 -m venv .venv && source .venv/bin/activate
cd webapp/backend
uvicorn app:app --reload --port 8000

# Frontend (in another terminal)
cd webapp/frontend
npm run dev
```

### 6. Login

Open `http://localhost:3000` and login with:
- Email: `admin@example.com`
- Password: `changeme`

See [SETUP.md](SETUP.md) for detailed setup instructions.

## Usage

### Workflow

1. **Start a Conversation**: Create a new conversation from the webapp
2. **Send a Message**: Describe what you want done (e.g., "Fix the failing test in auth.py")
3. **Review the Plan**: Agent Home generates a plan showing what workers will be spawned
4. **Approve or Deny**: Review the plan and approve to proceed
5. **Monitor Progress**: Watch real-time updates as workers execute
6. **Get Results**: See PRs created, tests run, changes made

### Run States

- `planning`: Agent is analyzing the request and creating a plan
- `waiting_approval`: Plan ready, awaiting user approval
- `running`: Approved and executing worker jobs
- `completed`: All workers finished successfully
- `failed`: One or more workers encountered errors
- `denied`: User rejected the plan
- `cancelled`: User cancelled during execution

## Project Structure

```
agent-home-orchestrator/
├── shared/                 # Shared models and config
│   ├── models.py          # Pydantic models (Conversation, Run, Event, etc.)
│   └── config.py          # Settings management
├── agent_home/            # Agent Home implementation
│   ├── gateway/           # HTTP + WebSocket API
│   ├── orchestrator/      # Home Orchestrator agent
│   ├── curator/           # Memory curation loop
│   └── persistence/       # SQLite event store + state manager
├── workers/               # Worker sandbox implementation
│   ├── runner.py          # Job execution runner
│   └── agent.py           # Worker Claude agent
├── modal_app/             # Modal app definitions
│   ├── app.py             # App, images, volume config
│   ├── agent_home.py      # Agent Home sandbox class
│   └── worker.py          # Worker spawn function
├── webapp/                # Web application
│   ├── backend/           # FastAPI backend
│   └── frontend/          # React frontend
└── pyproject.toml         # Python project config
```

## API Reference

### HTTP Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/v1/conversations` | Create a new conversation |
| GET | `/v1/conversations` | List all conversations |
| GET | `/v1/conversations/{id}` | Get a conversation |
| POST | `/v1/conversations/{id}/messages` | Send a message (creates a run) |
| GET | `/v1/runs/{id}` | Get a run |
| POST | `/v1/runs/{id}/approve` | Approve a waiting run |
| POST | `/v1/runs/{id}/deny` | Deny a waiting run |
| POST | `/v1/runs/{id}/cancel` | Cancel a running run |
| GET | `/v1/conversations/{id}/events` | Get events for catch-up |

### WebSocket

Connect to `/v1/stream?conversation_id={id}&token={token}` to receive real-time events.

## Configuration

### Environment Variables

#### Modal Secrets (for Agent Home)

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `GITHUB_TOKEN` | GitHub token for repo access |
| `AGENT_HOME_JWT_SECRET` | Secret for JWT tokens |

#### Webapp Backend (.env)

| Variable | Description |
|----------|-------------|
| `WEBAPP_ADMIN_EMAIL` | Admin login email (default: admin@example.com) |
| `WEBAPP_ADMIN_PASSWORD` | Admin login password (default: changeme) |
| `WEBAPP_JWT_SECRET` | Secret for session tokens (auto-generated if not set) |
| `WEBAPP_AGENT_HOME_MODAL_APP` | Modal app name (default: agent-home-orchestrator) |
| `WEBAPP_COOKIE_SECURE` | Set to true for HTTPS (default: false) |

## Memory and Persistence

Agent Home stores:

- **Conversations & Runs**: In SQLite database
- **Events**: Append-only event log for audit and catch-up
- **Memory**: Facts, preferences, and self-prompts in markdown files
- **Skills**: Reusable knowledge in `.claude/skills/`

All data persists in a Modal Volume across restarts.

## License

MIT
