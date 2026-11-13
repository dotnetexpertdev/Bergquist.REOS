import { useEffect, useState, useCallback } from 'react';
import { Routes, Route, Navigate, useNavigate } from 'react-router-dom';
import { getSession, logout } from './api';
import Login from './components/Login';
import Layout from './components/Layout';
import DocumentList from './components/DocumentList';
import DocumentDetail from './components/DocumentDetail';
import Settings from './components/Settings';

interface AuthState {
  loading: boolean;
  authenticated: boolean;
  username: string | null;
}

export default function App() {
  const [auth, setAuth] = useState<AuthState>({
    loading: true,
    authenticated: false,
    username: null,
  });

  const navigate = useNavigate();

  const checkSession = useCallback(async () => {
    try {
      const session = await getSession();
      setAuth({
        loading: false,
        authenticated: session.authenticated,
        username: session.username || null,
      });
    } catch {
      setAuth({
        loading: false,
        authenticated: false,
        username: null,
      });
    }
  }, []);

  useEffect(() => {
    checkSession();
  }, [checkSession]);

  const handleLogin = useCallback((username: string) => {
    setAuth({
      loading: false,
      authenticated: true,
      username,
    });
  }, []);

  const handleLogout = useCallback(async () => {
    await logout();
    setAuth({
      loading: false,
      authenticated: false,
      username: null,
    });
    navigate('/login');
  }, [navigate]);

  if (auth.loading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100vh' }}>
        <div className="spinner" />
      </div>
    );
  }

  if (!auth.authenticated) {
    return <Login onLogin={handleLogin} />;
  }

  return (
    <Layout username={auth.username!} onLogout={handleLogout}>
      <Routes>
        <Route path="/" element={<DocumentList />} />
        <Route path="/documents/:id" element={<DocumentDetail />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Layout>
  );
}
