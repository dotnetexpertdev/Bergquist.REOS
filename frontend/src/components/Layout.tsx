import { ReactNode, useEffect, useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { BrandMark, PRODUCT_TAGLINE, WORKSPACE_NAME } from '../brand';
import ResetDemo from './ResetDemo';
import SearchBar from './SearchBar';
import VoiceButton from './VoiceButton';

interface LayoutProps {
  children: ReactNode;
  username: string;
  onLogout: () => void;
}

export default function Layout({ children, username, onLogout }: LayoutProps) {
  const location = useLocation();
  const [showSearch, setShowSearch] = useState(false);
  const [showReset, setShowReset] = useState(false);
  // A question spoken with the header Talk button; the assistant opens and answers it.
  const [spoken, setSpoken] = useState('');

  // Ctrl+K / Cmd+K opens the assistant from anywhere.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setShowSearch(true);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="container app-header-inner">
          <Link to="/" className="brand">
            <BrandMark />
            <div>
              <h1>Morning Assistant</h1>
              <div className="brand-sub">{WORKSPACE_NAME} · {PRODUCT_TAGLINE}</div>
            </div>
          </Link>

          <div className="header-actions">
            <button
              className="search-pill"
              onClick={() => setShowSearch(!showSearch)}
              title="Ask about a bill"
              aria-label="Ask about a bill"
            >
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
                <circle cx="11" cy="11" r="8" />
                <path d="m21 21-4.35-4.35" />
              </svg>
              <span>Ask anything about your documents</span>
              <kbd>Ctrl K</kbd>
            </button>

            <VoiceButton onQuestion={(transcript) => { setSpoken(transcript); setShowSearch(true); }} />

            <Link
              to="/settings"
              className={`icon-btn${location.pathname === '/settings' ? ' active' : ''}`}
              title="Settings"
              aria-label="Settings"
            >
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
                <path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z" />
                <circle cx="12" cy="12" r="3" />
              </svg>
            </Link>

            <button
              className="icon-btn danger"
              onClick={() => setShowReset(true)}
              title="Reset demo data"
              aria-label="Reset demo data"
            >
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
                <path d="M3 6h18" />
                <path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2" />
                <path d="M19 6l-1 14a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1L5 6" />
                <path d="M10 11v6M14 11v6" />
              </svg>
            </button>

            <span className="user-chip hide-mobile">
              <span className="avatar" aria-hidden="true">{username.slice(0, 1)}</span>
              {username}
            </span>

            <button className="btn-secondary" onClick={onLogout}>
              Sign out
            </button>
          </div>
        </div>
      </header>

      {showReset && (
        <ResetDemo onClose={() => setShowReset(false)} />
      )}

      <main className="app-main">
        <div className="container">
          {showSearch && (
            <SearchBar key={spoken} initialQuery={spoken} onClose={() => { setShowSearch(false); setSpoken(''); }} />
          )}
          {children}
        </div>
      </main>
    </div>
  );
}
