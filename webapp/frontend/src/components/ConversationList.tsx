import { useState, useEffect, useCallback } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Plus, MessageSquare, Clock, AlertCircle, Loader2, CheckCircle2 } from 'lucide-react'
import type { Conversation, ConversationStatus } from '../types'

interface ConversationListProps {
  agentHome: {
    connected: boolean
    listConversations: () => Promise<Conversation[]>
    createConversation: (title?: string) => Promise<Conversation>
  }
}

const statusConfig: Record<ConversationStatus, { icon: typeof Clock; color: string; label: string }> = {
  idle: { icon: MessageSquare, color: 'text-gray-500', label: 'Idle' },
  waiting_approval: { icon: Clock, color: 'text-yellow-500', label: 'Waiting Approval' },
  running: { icon: Loader2, color: 'text-blue-500', label: 'Running' },
  error: { icon: AlertCircle, color: 'text-red-500', label: 'Error' },
}

export default function ConversationList({ agentHome }: ConversationListProps) {
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const navigate = useNavigate()

  const loadConversations = useCallback(async () => {
    if (!agentHome.connected) return

    try {
      setLoading(true)
      const data = await agentHome.listConversations()
      setConversations(data)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load conversations')
    } finally {
      setLoading(false)
    }
  }, [agentHome.connected, agentHome.listConversations])

  useEffect(() => {
    loadConversations()
  }, [loadConversations])

  const handleCreateConversation = async () => {
    if (!agentHome.connected) return

    try {
      setCreating(true)
      const conversation = await agentHome.createConversation()
      navigate(`/chat/${conversation.conversation_id}`)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create conversation')
    } finally {
      setCreating(false)
    }
  }

  if (!agentHome.connected) {
    return (
      <div className="text-center py-12">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600 mx-auto mb-4"></div>
        <p className="text-gray-600">Connecting to Agent Home...</p>
      </div>
    )
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-gray-900">Conversations</h1>
        <button
          onClick={handleCreateConversation}
          disabled={creating}
          className="flex items-center gap-2 bg-blue-600 text-white px-4 py-2 rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          {creating ? (
            <Loader2 className="w-5 h-5 animate-spin" />
          ) : (
            <Plus className="w-5 h-5" />
          )}
          New Conversation
        </button>
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-4 mb-6">
          <div className="flex items-center gap-2 text-red-700">
            <AlertCircle className="w-5 h-5" />
            {error}
          </div>
        </div>
      )}

      {loading ? (
        <div className="text-center py-12">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600 mx-auto"></div>
        </div>
      ) : conversations.length === 0 ? (
        <div className="text-center py-12 bg-white rounded-lg border border-gray-200">
          <MessageSquare className="w-12 h-12 text-gray-400 mx-auto mb-4" />
          <h3 className="text-lg font-medium text-gray-900 mb-2">No conversations yet</h3>
          <p className="text-gray-600 mb-4">
            Start a new conversation to begin working with your agent
          </p>
          <button
            onClick={handleCreateConversation}
            disabled={creating}
            className="inline-flex items-center gap-2 bg-blue-600 text-white px-4 py-2 rounded-lg hover:bg-blue-700 disabled:opacity-50"
          >
            <Plus className="w-5 h-5" />
            New Conversation
          </button>
        </div>
      ) : (
        <div className="space-y-3">
          {conversations.map((conversation) => {
            const config = statusConfig[conversation.status]
            const StatusIcon = config.icon

            return (
              <Link
                key={conversation.conversation_id}
                to={`/chat/${conversation.conversation_id}`}
                className="block bg-white rounded-lg border border-gray-200 p-4 hover:border-blue-300 hover:shadow-sm transition-all"
              >
                <div className="flex items-start justify-between">
                  <div className="flex-1 min-w-0">
                    <h3 className="font-medium text-gray-900 truncate">
                      {conversation.title}
                    </h3>
                    <p className="text-sm text-gray-500 mt-1">
                      {new Date(conversation.updated_at).toLocaleString()}
                    </p>
                  </div>
                  <div className={`flex items-center gap-1.5 text-sm ${config.color}`}>
                    <StatusIcon className={`w-4 h-4 ${conversation.status === 'running' ? 'animate-spin' : ''}`} />
                    {config.label}
                  </div>
                </div>
              </Link>
            )
          })}
        </div>
      )}
    </div>
  )
}
