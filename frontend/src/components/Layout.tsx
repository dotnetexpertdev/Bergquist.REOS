import { ReactNode, useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import SearchBar from './SearchBar';

interface LayoutProps {
  children: ReactNode;
  username: string;
  onLogout: () => void;
}

export default function Layout({ children, username, onLogout }: LayoutProps) {
  const location = useLocation();
  const [showSearch, setShowSearch] = useState(false);

  return (
    <div style={{ minHeight: '100vh', display: 'flex', flexDirection: 'column' }}>
      {/* Header */}
      <header style={{
        background: 'white',
        borderBottom: '1px solid var(--gray-200)',
        padding: '0.75rem 1rem',
        position: 'sticky',
        top: 0,
        zIndex: 100,
      }}>
        <div className="container" style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: '1rem',
        }}>
          <Link to="/" style={{ textDecoration: 'none', color: 'inherit' }}>
            <h1 style={{ margin: 0, fontSize: '1.25rem', fontWeight: 600 }}>
              Morning Assistant
            </h1>
          </Link>

          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
            <button
              className="btn-secondary"
              onClick={() => setShowSearch(!showSearch)}
              style={{ padding: '0.5rem' }}
              title="Ask about a bill"
              aria-label="Ask about a bill"
            >
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <circle cx="11" cy="11" r="8" />
                <path d="m21 21-4.35-4.35" />
              </svg>
            </button>

            <Link
              to="/settings"
              className={location.pathname === '/settings' ? 'btn-primary' : 'btn-secondary'}
              style={{ padding: '0.5rem', display: 'inline-flex' }}
              title="Settings"
              aria-label="Settings"
            >
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z" />
                  <circle cx="12" cy="12" r="3" />
                </svg>
            </Link>

            <span className="hide-mobile" style={{ color: 'var(--gray-500)', fontSize: '0.875rem' }}>
              {username}
            </span>

            <button className="btn-secondary" onClick={onLogout}>
              Sign out
            </button>
          </div>
        </div>
      </header>

      {/* Search overlay */}
      {showSearch && (
        <SearchBar onClose={() => setShowSearch(false)} />
      )}

      {/* Main content */}
      <main style={{ flex: 1, padding: '1rem 0' }}>
        <div className="container">
          {children}
        </div>
      </main>
    </div>
  );
}
