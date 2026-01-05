import { Routes, Route, Navigate } from 'react-router-dom'
import { useAuth } from './hooks/useAuth'
import { useAgentHome } from './hooks/useAgentHome'
import { useEffect } from 'react'
import Layout from './components/Layout'
import ConversationList from './components/ConversationList'
import Chat from './components/Chat'
import Login from './components/Login'

function App() {
  const { user, loading: authLoading, login, logout } = useAuth()
  const agentHome = useAgentHome()

  // Auto-connect when user is authenticated
  useEffect(() => {
    if (user && !agentHome.connected && !agentHome.connecting) {
      agentHome.connect()
    }
  }, [user, agentHome.connected, agentHome.connecting])

  if (authLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600"></div>
      </div>
    )
  }

  if (!user) {
    return <Login onLogin={login} />
  }

  return (
    <Layout user={user} onLogout={logout} agentHome={agentHome}>
      <Routes>
        <Route path="/" element={<ConversationList agentHome={agentHome} />} />
        <Route path="/chat/:conversationId" element={<Chat agentHome={agentHome} />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Layout>
  )
}

export default App
