import { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { Home, LogOut, Wifi, WifiOff } from 'lucide-react'
import type { User } from '../types'

interface LayoutProps {
  user: User
  onLogout: () => void
  agentHome: {
    connected: boolean
    connecting: boolean
    error: string | null
  }
  children: ReactNode
}

export default function Layout({ user, onLogout, agentHome, children }: LayoutProps) {
  return (
    <div className="min-h-screen flex flex-col">
      {/* Header */}
      <header className="bg-white border-b border-gray-200 px-4 py-3">
        <div className="max-w-6xl mx-auto flex items-center justify-between">
          <Link to="/" className="flex items-center gap-2 text-xl font-semibold text-gray-900">
            <Home className="w-6 h-6" />
            Agent Home
          </Link>

          <div className="flex items-center gap-4">
            {/* Connection status */}
            <div className="flex items-center gap-2 text-sm">
              {agentHome.connecting ? (
                <>
                  <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-blue-600"></div>
                  <span className="text-gray-600">Connecting...</span>
                </>
              ) : agentHome.connected ? (
                <>
                  <Wifi className="w-4 h-4 text-green-600" />
                  <span className="text-green-600">Connected</span>
                </>
              ) : (
                <>
                  <WifiOff className="w-4 h-4 text-red-600" />
                  <span className="text-red-600">Disconnected</span>
                </>
              )}
            </div>

            {/* User info */}
            <div className="flex items-center gap-3">
              {user.picture && (
                <img
                  src={user.picture}
                  alt={user.name}
                  className="w-8 h-8 rounded-full"
                />
              )}
              <span className="text-sm text-gray-700">{user.name}</span>
              <button
                onClick={onLogout}
                className="p-2 text-gray-500 hover:text-gray-700 rounded-lg hover:bg-gray-100"
                title="Log out"
              >
                <LogOut className="w-5 h-5" />
              </button>
            </div>
          </div>
        </div>
      </header>

      {/* Error banner */}
      {agentHome.error && (
        <div className="bg-red-50 border-b border-red-200 px-4 py-2">
          <div className="max-w-6xl mx-auto text-red-700 text-sm">
            {agentHome.error}
          </div>
        </div>
      )}

      {/* Main content */}
      <main className="flex-1 bg-gray-50">
        <div className="max-w-6xl mx-auto py-6 px-4">
          {children}
        </div>
      </main>
    </div>
  )
}
