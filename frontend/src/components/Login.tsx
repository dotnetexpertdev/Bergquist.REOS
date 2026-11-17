import { useState, FormEvent } from 'react';
import { login } from '../api';
import { BrandMark, PRODUCT_TAGLINE, WORKSPACE_NAME } from '../brand';

interface LoginProps {
  onLogin: (username: string) => void;
}

export default function Login({ onLogin }: LoginProps) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      const response = await login(username, password);
      if (response.success && response.username) {
        onLogin(response.username);
      } else {
        setError(response.message || 'Login failed');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Login failed');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="login">
      <aside className="login-hero">
        <div className="login-brand"><BrandMark size={34} /> {WORKSPACE_NAME} · {PRODUCT_TAGLINE}</div>
        <div>
          <h2>Every document filed.<br />Every bill <em>accounted for.</em></h2>
          <p>Bills, receipts, leases and statements come in by email or upload, are read and filed by property,
            checked against QuickBooks, and wait for your approval. Nothing is ever paid without you.</p>
        </div>
        <div className="login-points">
          <div><strong>Originals preserved</strong>Stored once, fingerprinted</div>
          <div><strong>No duplicates</strong>Checked before anything is created</div>
          <div><strong>You approve</strong>The app never moves money</div>
        </div>
      </aside>

      <div className="login-panel">
        <div className="login-card">
          <h1>Morning Assistant</h1>
          <p className="login-lede">Sign in to start your morning review.</p>

          <form onSubmit={handleSubmit}>
            <div className="field">
              <label htmlFor="login-username">Username</label>
              <input
                id="login-username"
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                required
                autoFocus
                autoComplete="username"
              />
            </div>

            <div className="field">
              <label htmlFor="login-password">Password</label>
              <input
                id="login-password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                autoComplete="current-password"
              />
            </div>

            {error && <div role="alert" className="notice-error">{error}</div>}

            <button type="submit" className="btn-primary" disabled={loading}>
              {loading ? 'Signing in...' : 'Sign in'}
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}
