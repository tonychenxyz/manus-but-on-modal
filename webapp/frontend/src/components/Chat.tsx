import { useState, useEffect, useCallback, useRef } from 'react'
import { useParams, Link } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import {
  ArrowLeft,
  Send,
  Check,
  X,
  Loader2,
  AlertCircle,
  User,
  Bot,
  GitBranch,
  ExternalLink,
} from 'lucide-react'
import type { Conversation, Run, Event, RunState } from '../types'

interface ChatProps {
  agentHome: {
    connected: boolean
    getConversation: (id: string) => Promise<Conversation>
    getEvents: (conversationId: string, after?: number) => Promise<Event[]>
    sendMessage: (conversationId: string, content: string) => Promise<Run>
    approveRun: (runId: string) => Promise<void>
    denyRun: (runId: string, reason?: string) => Promise<void>
    cancelRun: (runId: string) => Promise<void>
    getRun: (runId: string) => Promise<Run>
    subscribeToEvents: (conversationId: string, onEvent: (event: Event) => void) => () => void
  }
}

interface Message {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  timestamp: string
  runId?: string
}

export default function Chat({ agentHome }: ChatProps) {
  const { conversationId } = useParams<{ conversationId: string }>()
  const [conversation, setConversation] = useState<Conversation | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [activeRun, setActiveRun] = useState<Run | null>(null)
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const messagesEndRef = useRef<HTMLDivElement>(null)

  // Load conversation and events
  const loadData = useCallback(async () => {
    if (!agentHome.connected || !conversationId) return

    try {
      setLoading(true)
      const [conv, events] = await Promise.all([
        agentHome.getConversation(conversationId),
        agentHome.getEvents(conversationId),
      ])

      setConversation(conv)

      // Convert events to messages
      const msgs: Message[] = []
      for (const event of events) {
        if (event.type === 'message.user') {
          msgs.push({
            id: `${event.cursor}`,
            role: 'user',
            content: event.payload.content as string,
            timestamp: event.ts,
            runId: event.run_id,
          })
        } else if (event.type === 'message.assistant') {
          msgs.push({
            id: `${event.cursor}`,
            role: 'assistant',
            content: event.payload.content as string,
            timestamp: event.ts,
            runId: event.run_id,
          })
        }
      }
      setMessages(msgs)

      // Load active run if any
      if (conv.active_run_id) {
        const run = await agentHome.getRun(conv.active_run_id)
        setActiveRun(run)
      }

      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load conversation')
    } finally {
      setLoading(false)
    }
  }, [agentHome.connected, conversationId])

  useEffect(() => {
    loadData()
  }, [loadData])

  // Subscribe to events
  useEffect(() => {
    if (!agentHome.connected || !conversationId) return

    const unsubscribe = agentHome.subscribeToEvents(conversationId, async (event) => {
      if (event.type === 'message.user') {
        setMessages(msgs => [...msgs, {
          id: `${event.cursor}`,
          role: 'user',
          content: event.payload.content as string,
          timestamp: event.ts,
          runId: event.run_id,
        }])
      } else if (event.type === 'message.assistant') {
        setMessages(msgs => [...msgs, {
          id: `${event.cursor}`,
          role: 'assistant',
          content: event.payload.content as string,
          timestamp: event.ts,
          runId: event.run_id,
        }])
      } else if (event.type === 'run.plan_ready' && event.run_id) {
        const run = await agentHome.getRun(event.run_id)
        setActiveRun(run)
      } else if (
        event.type === 'run.completed' ||
        event.type === 'run.failed' ||
        event.type === 'run.cancelled' ||
        event.type === 'run.denied'
      ) {
        setActiveRun(null)
        // Refresh conversation status
        const conv = await agentHome.getConversation(conversationId)
        setConversation(conv)
      }
    })

    return unsubscribe
  }, [agentHome.connected, conversationId])

  // Scroll to bottom on new messages
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, activeRun])

  const handleSend = async () => {
    if (!input.trim() || !conversationId || sending) return

    const content = input.trim()
    setInput('')
    setSending(true)

    try {
      const run = await agentHome.sendMessage(conversationId, content)
      setActiveRun(run)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to send message')
    } finally {
      setSending(false)
    }
  }

  const handleApprove = async () => {
    if (!activeRun) return

    try {
      await agentHome.approveRun(activeRun.run_id)
      setActiveRun({ ...activeRun, state: 'running' })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to approve run')
    }
  }

  const handleDeny = async () => {
    if (!activeRun) return

    try {
      await agentHome.denyRun(activeRun.run_id)
      setActiveRun(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to deny run')
    }
  }

  const handleCancel = async () => {
    if (!activeRun) return

    try {
      await agentHome.cancelRun(activeRun.run_id)
      setActiveRun(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to cancel run')
    }
  }

  if (loading) {
    return (
      <div className="text-center py-12">
        <Loader2 className="w-8 h-8 animate-spin text-blue-600 mx-auto" />
      </div>
    )
  }

  if (!conversation) {
    return (
      <div className="text-center py-12">
        <AlertCircle className="w-12 h-12 text-red-500 mx-auto mb-4" />
        <p className="text-gray-600">Conversation not found</p>
        <Link to="/" className="text-blue-600 hover:underline mt-2 inline-block">
          Back to conversations
        </Link>
      </div>
    )
  }

  return (
    <div className="flex flex-col h-[calc(100vh-8rem)]">
      {/* Header */}
      <div className="flex items-center gap-4 pb-4 border-b border-gray-200">
        <Link to="/" className="p-2 hover:bg-gray-100 rounded-lg">
          <ArrowLeft className="w-5 h-5 text-gray-600" />
        </Link>
        <div>
          <h1 className="font-semibold text-gray-900">{conversation.title}</h1>
          <p className="text-sm text-gray-500">
            {conversation.status === 'idle' ? 'Ready' : conversation.status}
          </p>
        </div>
      </div>

      {/* Error banner */}
      {error && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-3 mt-4 flex items-center gap-2 text-red-700">
          <AlertCircle className="w-5 h-5 flex-shrink-0" />
          <span className="text-sm">{error}</span>
          <button onClick={() => setError(null)} className="ml-auto">
            <X className="w-4 h-4" />
          </button>
        </div>
      )}

      {/* Messages */}
      <div className="flex-1 overflow-y-auto py-4 space-y-4">
        {messages.length === 0 && !activeRun && (
          <div className="text-center py-12 text-gray-500">
            <Bot className="w-12 h-12 mx-auto mb-4 text-gray-400" />
            <p>Start a conversation with your agent</p>
          </div>
        )}

        {messages.map((message) => (
          <div
            key={message.id}
            className={`flex gap-3 ${message.role === 'user' ? 'justify-end' : ''}`}
          >
            {message.role === 'assistant' && (
              <div className="w-8 h-8 bg-blue-100 rounded-full flex items-center justify-center flex-shrink-0">
                <Bot className="w-5 h-5 text-blue-600" />
              </div>
            )}
            <div
              className={`max-w-[80%] rounded-lg p-4 ${
                message.role === 'user'
                  ? 'bg-blue-600 text-white'
                  : 'bg-white border border-gray-200'
              }`}
            >
              <div className={`markdown-content ${message.role === 'user' ? 'text-white' : ''}`}>
                <ReactMarkdown>{message.content}</ReactMarkdown>
              </div>
            </div>
            {message.role === 'user' && (
              <div className="w-8 h-8 bg-gray-200 rounded-full flex items-center justify-center flex-shrink-0">
                <User className="w-5 h-5 text-gray-600" />
              </div>
            )}
          </div>
        ))}

        {/* Active run display */}
        {activeRun && <RunDisplay run={activeRun} onApprove={handleApprove} onDeny={handleDeny} onCancel={handleCancel} />}

        <div ref={messagesEndRef} />
      </div>

      {/* Input */}
      <div className="border-t border-gray-200 pt-4">
        <div className="flex gap-3">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && !e.shiftKey && handleSend()}
            placeholder="Type your message..."
            disabled={sending || !!activeRun}
            className="flex-1 border border-gray-300 rounded-lg px-4 py-3 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent disabled:bg-gray-100"
          />
          <button
            onClick={handleSend}
            disabled={!input.trim() || sending || !!activeRun}
            className="bg-blue-600 text-white px-6 py-3 rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {sending ? (
              <Loader2 className="w-5 h-5 animate-spin" />
            ) : (
              <Send className="w-5 h-5" />
            )}
          </button>
        </div>
      </div>
    </div>
  )
}

// Run display component
interface RunDisplayProps {
  run: Run
  onApprove: () => void
  onDeny: () => void
  onCancel: () => void
}

function RunDisplay({ run, onApprove, onDeny, onCancel }: RunDisplayProps) {
  const stateConfig: Record<RunState, { label: string; color: string }> = {
    queued: { label: 'Queued', color: 'text-gray-500' },
    planning: { label: 'Planning...', color: 'text-blue-500' },
    waiting_approval: { label: 'Waiting for Approval', color: 'text-yellow-600' },
    running: { label: 'Running...', color: 'text-blue-500' },
    completed: { label: 'Completed', color: 'text-green-600' },
    failed: { label: 'Failed', color: 'text-red-600' },
    denied: { label: 'Denied', color: 'text-gray-600' },
    cancelled: { label: 'Cancelled', color: 'text-gray-600' },
  }

  const config = stateConfig[run.state]

  return (
    <div className="bg-white border border-gray-200 rounded-lg p-4">
      <div className="flex items-center justify-between mb-3">
        <span className={`font-medium ${config.color}`}>
          {run.state === 'planning' || run.state === 'running' ? (
            <span className="flex items-center gap-2">
              <Loader2 className="w-4 h-4 animate-spin" />
              {config.label}
            </span>
          ) : (
            config.label
          )}
        </span>
      </div>

      {/* Plan display */}
      {run.plan && (
        <div className="bg-gray-50 rounded-lg p-4 mb-4">
          <h4 className="font-medium text-gray-900 mb-2">Plan</h4>
          <div className="markdown-content text-sm">
            <ReactMarkdown>{run.plan}</ReactMarkdown>
          </div>
        </div>
      )}

      {/* Job specs */}
      {run.job_specs.length > 0 && (
        <div className="mb-4">
          <h4 className="font-medium text-gray-900 mb-2">Worker Jobs</h4>
          <div className="space-y-2">
            {run.job_specs.map((job) => (
              <div key={job.job_id} className="bg-gray-50 rounded p-3 text-sm">
                <div className="flex items-center gap-2">
                  <GitBranch className="w-4 h-4 text-gray-500" />
                  <span className="font-mono text-xs">{job.repo.url.split('/').slice(-2).join('/')}</span>
                  <span className="text-gray-400">@</span>
                  <span className="font-mono text-xs">{job.repo.ref}</span>
                </div>
                <p className="mt-1 text-gray-700">{job.goal}</p>
                {job.constraints.produce_pr && (
                  <span className="inline-flex items-center gap-1 text-xs text-blue-600 mt-1">
                    <ExternalLink className="w-3 h-3" />
                    Will create PR
                  </span>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Actions */}
      {run.state === 'waiting_approval' && (
        <div className="flex gap-3">
          <button
            onClick={onApprove}
            className="flex items-center gap-2 bg-green-600 text-white px-4 py-2 rounded-lg hover:bg-green-700 transition-colors"
          >
            <Check className="w-4 h-4" />
            Approve
          </button>
          <button
            onClick={onDeny}
            className="flex items-center gap-2 bg-gray-200 text-gray-700 px-4 py-2 rounded-lg hover:bg-gray-300 transition-colors"
          >
            <X className="w-4 h-4" />
            Deny
          </button>
        </div>
      )}

      {run.state === 'running' && (
        <button
          onClick={onCancel}
          className="flex items-center gap-2 bg-red-100 text-red-700 px-4 py-2 rounded-lg hover:bg-red-200 transition-colors"
        >
          <X className="w-4 h-4" />
          Cancel
        </button>
      )}

      {/* Error display */}
      {run.error && (
        <div className="mt-3 bg-red-50 border border-red-200 rounded p-3 text-sm text-red-700">
          {run.error}
        </div>
      )}
    </div>
  )
}
