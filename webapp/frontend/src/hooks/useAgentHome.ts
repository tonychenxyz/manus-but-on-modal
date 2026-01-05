import { useState, useEffect, useCallback, useRef } from 'react'
import type { ConnectResponse, Conversation, Run, Event } from '../types'

interface AgentHomeState {
  connected: boolean
  connecting: boolean
  error: string | null
  agentHomeUrl: string | null
  token: string | null
}

interface UseAgentHomeResult extends AgentHomeState {
  connect: () => Promise<void>
  disconnect: () => void
  // API methods
  createConversation: (title?: string) => Promise<Conversation>
  listConversations: () => Promise<Conversation[]>
  getConversation: (id: string) => Promise<Conversation>
  sendMessage: (conversationId: string, content: string) => Promise<Run>
  approveRun: (runId: string) => Promise<void>
  denyRun: (runId: string, reason?: string) => Promise<void>
  cancelRun: (runId: string) => Promise<void>
  getRun: (runId: string) => Promise<Run>
  getEvents: (conversationId: string, after?: number) => Promise<Event[]>
  // WebSocket
  subscribeToEvents: (conversationId: string, onEvent: (event: Event) => void) => () => void
}

export function useAgentHome(): UseAgentHomeResult {
  const [state, setState] = useState<AgentHomeState>({
    connected: false,
    connecting: false,
    error: null,
    agentHomeUrl: null,
    token: null,
  })

  const wsRef = useRef<WebSocket | null>(null)
  const eventCallbacksRef = useRef<Map<string, (event: Event) => void>>(new Map())

  const connect = useCallback(async () => {
    setState(s => ({ ...s, connecting: true, error: null }))

    try {
      const response = await fetch('/api/connect', {
        method: 'POST',
        credentials: 'include',
      })

      if (!response.ok) {
        const error = await response.json()
        throw new Error(error.detail || 'Failed to connect')
      }

      const data: ConnectResponse = await response.json()

      setState({
        connected: true,
        connecting: false,
        error: null,
        agentHomeUrl: data.agent_home_url,
        token: data.token,
      })
    } catch (err) {
      setState(s => ({
        ...s,
        connecting: false,
        error: err instanceof Error ? err.message : 'Connection failed',
      }))
    }
  }, [])

  const disconnect = useCallback(() => {
    if (wsRef.current) {
      wsRef.current.close()
      wsRef.current = null
    }
    setState({
      connected: false,
      connecting: false,
      error: null,
      agentHomeUrl: null,
      token: null,
    })
  }, [])

  // API helper
  const apiCall = useCallback(async <T>(
    path: string,
    options: RequestInit = {}
  ): Promise<T> => {
    if (!state.agentHomeUrl || !state.token) {
      throw new Error('Not connected to Agent Home')
    }

    const url = `${state.agentHomeUrl}${path}`
    const response = await fetch(url, {
      ...options,
      headers: {
        'Authorization': `Bearer ${state.token}`,
        'Content-Type': 'application/json',
        ...options.headers,
      },
    })

    if (!response.ok) {
      const error = await response.json().catch(() => ({ detail: 'Request failed' }))
      throw new Error(error.detail || `Request failed: ${response.status}`)
    }

    return response.json()
  }, [state.agentHomeUrl, state.token])

  // API methods
  const createConversation = useCallback(async (title?: string): Promise<Conversation> => {
    const result = await apiCall<{ conversation: Conversation }>('/v1/conversations', {
      method: 'POST',
      body: JSON.stringify({ title }),
    })
    return result.conversation
  }, [apiCall])

  const listConversations = useCallback(async (): Promise<Conversation[]> => {
    const result = await apiCall<{ conversations: Conversation[] }>('/v1/conversations')
    return result.conversations
  }, [apiCall])

  const getConversation = useCallback(async (id: string): Promise<Conversation> => {
    return apiCall<Conversation>(`/v1/conversations/${id}`)
  }, [apiCall])

  const sendMessage = useCallback(async (conversationId: string, content: string): Promise<Run> => {
    const result = await apiCall<{ run: Run }>(`/v1/conversations/${conversationId}/messages`, {
      method: 'POST',
      body: JSON.stringify({ content }),
    })
    return result.run
  }, [apiCall])

  const approveRun = useCallback(async (runId: string): Promise<void> => {
    await apiCall(`/v1/runs/${runId}/approve`, {
      method: 'POST',
      body: JSON.stringify({}),
    })
  }, [apiCall])

  const denyRun = useCallback(async (runId: string, reason?: string): Promise<void> => {
    await apiCall(`/v1/runs/${runId}/deny`, {
      method: 'POST',
      body: JSON.stringify({ reason }),
    })
  }, [apiCall])

  const cancelRun = useCallback(async (runId: string): Promise<void> => {
    await apiCall(`/v1/runs/${runId}/cancel`, {
      method: 'POST',
      body: JSON.stringify({}),
    })
  }, [apiCall])

  const getRun = useCallback(async (runId: string): Promise<Run> => {
    return apiCall<Run>(`/v1/runs/${runId}`)
  }, [apiCall])

  const getEvents = useCallback(async (conversationId: string, after = 0): Promise<Event[]> => {
    const result = await apiCall<{ events: Event[] }>(
      `/v1/conversations/${conversationId}/events?after=${after}`
    )
    return result.events
  }, [apiCall])

  // WebSocket subscription
  const subscribeToEvents = useCallback((
    conversationId: string,
    onEvent: (event: Event) => void
  ): () => void => {
    if (!state.agentHomeUrl || !state.token) {
      console.error('Not connected to Agent Home')
      return () => {}
    }

    // Store callback
    eventCallbacksRef.current.set(conversationId, onEvent)

    // Create WebSocket if not exists
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      const wsUrl = state.agentHomeUrl.replace('https://', 'wss://').replace('http://', 'ws://')
      const ws = new WebSocket(
        `${wsUrl}/v1/stream?conversation_id=${conversationId}&token=${state.token}`
      )

      ws.onmessage = (event) => {
        try {
          const data: Event = JSON.parse(event.data)
          const callback = eventCallbacksRef.current.get(data.conversation_id)
          if (callback) {
            callback(data)
          }
        } catch (err) {
          console.error('Failed to parse event:', err)
        }
      }

      ws.onerror = (err) => {
        console.error('WebSocket error:', err)
      }

      ws.onclose = () => {
        wsRef.current = null
      }

      wsRef.current = ws
    }

    // Return cleanup function
    return () => {
      eventCallbacksRef.current.delete(conversationId)
      if (eventCallbacksRef.current.size === 0 && wsRef.current) {
        wsRef.current.close()
        wsRef.current = null
      }
    }
  }, [state.agentHomeUrl, state.token])

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (wsRef.current) {
        wsRef.current.close()
      }
    }
  }, [])

  return {
    ...state,
    connect,
    disconnect,
    createConversation,
    listConversations,
    getConversation,
    sendMessage,
    approveRun,
    denyRun,
    cancelRun,
    getRun,
    getEvents,
    subscribeToEvents,
  }
}
