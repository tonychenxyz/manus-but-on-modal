// API Types

export interface User {
  email: string
  name: string
  picture?: string
}

export interface ConnectResponse {
  agent_home_url: string
  token: string
  expires_in_seconds: number
}

// Conversation Types

export type ConversationStatus = 'idle' | 'waiting_approval' | 'running' | 'error'

export interface Conversation {
  conversation_id: string
  title: string
  status: ConversationStatus
  created_at: string
  updated_at: string
  active_run_id?: string
  metadata: Record<string, unknown>
}

// Run Types

export type RunState =
  | 'queued'
  | 'planning'
  | 'waiting_approval'
  | 'running'
  | 'completed'
  | 'failed'
  | 'denied'
  | 'cancelled'

export interface Run {
  run_id: string
  conversation_id: string
  state: RunState
  user_message: string
  plan?: string
  job_specs: JobSpec[]
  worker_jobs: string[]
  created_at: string
  approved_at?: string
  completed_at?: string
  error?: string
}

// Job Types

export type WorkerJobStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
export type WorkerJobKind = 'implement' | 'test' | 'investigate' | 'refactor' | 'build' | 'custom'

export interface JobSpec {
  job_id: string
  repo: {
    url: string
    ref: string
  }
  goal: string
  kind: WorkerJobKind
  constraints: {
    no_secrets_in_output: boolean
    produce_pr: boolean
    timeout_seconds: number
  }
}

export interface WorkerResult {
  job_id: string
  status: WorkerJobStatus
  pr_url?: string
  commit_hash?: string
  branch_name?: string
  summary: string
  error?: string
  duration_seconds?: number
}

// Event Types

export type EventType =
  | 'message.user'
  | 'message.assistant'
  | 'message.system'
  | 'run.created'
  | 'run.plan_ready'
  | 'run.approved'
  | 'run.denied'
  | 'run.started'
  | 'run.completed'
  | 'run.failed'
  | 'run.cancelled'
  | 'job.queued'
  | 'job.started'
  | 'job.progress'
  | 'job.completed'
  | 'job.failed'
  | 'assistant.token'
  | 'assistant.thinking'

export interface Event {
  cursor: number
  ts: string
  conversation_id: string
  type: EventType
  payload: Record<string, unknown>
  run_id?: string
  job_id?: string
}
