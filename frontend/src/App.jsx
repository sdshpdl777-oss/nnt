import React, { useCallback, useEffect, useState } from 'react';
import { Routes, Route, Link, useNavigate, useParams, useLocation, Navigate } from 'react-router-dom';
import { PanelLeft, SquarePen, ArrowRight } from 'lucide-react';
import Sidebar from './components/Sidebar.jsx';
import Chat from './components/Chat.jsx';
import { ClientsPage, ClientPage, ReferencesPage } from './components/Clients.jsx';
import { API_URL, REQUEST_TIMEOUT_MS, UnauthorizedError, ensureFreshToken, errorDetail, timeoutMessage, useConversations } from './conversations.js';

const EXPIRED_KEY = 'nnt.sessionExpired';

// --- Public Components ---

// TechnoFort logo: the wordmark image plus its tagline as real text, so it stays crisp at any size
const FullLogo = ({ className = '' }) => (
  <div className={`full-logo ${className}`} role="img" aria-label="TechnoFort — Igniting your digital presence">
    <img src="/technofort-wordmark.png" alt="" />
    <span>Igniting your digital presence</span>
  </div>
);

const PublicNav = () => (
  <nav className="public-nav">
    <Link to="/" className="brand" aria-label="NNT Studio by TechnoFort — home">
      <img src="/technofort-wordmark.png" alt="TechnoFort" className="brand-logo" />
    </Link>
    <div className="public-links">
      <Link to="/about">About</Link>
      <Link to="/join" className="btn btn-primary">Sign in</Link>
    </div>
  </nav>
);

const Home = () => (
  <main className="public-page hero">
    <FullLogo className="hero-logo" />
    <h1>Create on-brand images,<br />just by asking.</h1>
    <p className="lead">
      Describe what you need. NNT Studio researches styles on the web and helps you turn ideas into finished designs.
    </p>
    <Link to="/join" className="btn btn-primary btn-lg">
      Get started <ArrowRight size={17} />
    </Link>
  </main>
);

const About = () => (
  <main className="public-page narrow">
    <h2>About</h2>
    <p className="lead">
      We're building the next generation of AI creative tools using LangGraph, retrieval, and FastAPI.
    </p>
  </main>
);

const Join = ({ setAuth }) => {
  const [isLogin, setIsLogin] = useState(true);
  const [formData, setFormData] = useState({ username: '', email: '', password: '' });
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [notice] = useState(() => {
    const expired = sessionStorage.getItem(EXPIRED_KEY);
    sessionStorage.removeItem(EXPIRED_KEY);
    return expired ? 'Your session expired. Please sign in again.' : '';
  });
  const navigate = useNavigate();

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    setLoading(true);

    const endpoint = isLogin ? '/api/auth/login' : '/api/auth/register';
    const payload = isLogin
      ? { username: formData.username, password: formData.password }
      : formData;

    try {
      const response = await fetch(`${API_URL}${endpoint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
      });

      if (!response.ok) throw new Error(await errorDetail(response));
      const data = await response.json();

      localStorage.setItem('token', data.access_token);
      setAuth(true);
      navigate('/chat');
    } catch (err) {
      if (err.name === 'TimeoutError') setError(timeoutMessage());
      else if (err instanceof TypeError) setError('Could not reach the server. Check your connection and try again.');
      else setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <main className="public-page auth">
      <div className="auth-card">
        <FullLogo />
        <h2>{isLogin ? 'Welcome back' : 'Create your account'}</h2>
        {notice && !error && <p className="form-notice">{notice}</p>}
        <form onSubmit={handleSubmit} className="auth-form">
          <input className="field" type="text" placeholder="Username" autoComplete="username" value={formData.username} onChange={e => setFormData({ ...formData, username: e.target.value })} required />
          {!isLogin && <input className="field" type="email" placeholder="Email address" autoComplete="email" value={formData.email} onChange={e => setFormData({ ...formData, email: e.target.value })} required />}
          <input className="field" type="password" placeholder="Password" autoComplete={isLogin ? 'current-password' : 'new-password'} value={formData.password} onChange={e => setFormData({ ...formData, password: e.target.value })} required />
          {error && <p className="form-error">{error}</p>}
          <button type="submit" className="btn btn-primary btn-block" disabled={loading}>
            {loading ? 'Please wait…' : (isLogin ? 'Continue' : 'Create account')}
          </button>
        </form>
        <p className="auth-switch">
          {isLogin ? "Don't have an account? " : 'Already have an account? '}
          <button type="button" className="link-btn" onClick={() => { setIsLogin(!isLogin); setError(''); }}>
            {isLogin ? 'Sign up' : 'Sign in'}
          </button>
        </p>
      </div>
    </main>
  );
};

// --- Authenticated Components ---

const ChatRoute = ({ conversations, streamingId, sendMessage, stop, newId, onUnauthorized }) => {
  const { chatId } = useParams();
  const navigate = useNavigate();
  const conversation = chatId ? conversations.find(c => c.id === chatId) : null;

  if (chatId && !conversation) return <Navigate to="/chat" replace />;

  const handleSend = (text, images = [], mentions = []) => {
    const id = chatId || newId();
    if (!chatId) navigate(`/chat/${id}`);
    sendMessage(id, text, images, mentions);
  };

  return (
    <Chat
      key={chatId || 'new'}
      conversation={conversation}
      onSend={handleSend}
      onStop={stop}
      streaming={streamingId !== null}
      onUnauthorized={onUnauthorized}
    />
  );
};

const PlaceholderPage = ({ title, text }) => (
  <div className="page">
    <h2>{title}</h2>
    <div className="empty-card">{text}</div>
  </div>
);

const AppShell = ({ setAuth }) => {
  const isDesktop = () => window.matchMedia('(min-width: 769px)').matches;
  const [sidebarOpen, setSidebarOpen] = useState(isDesktop);
  const navigate = useNavigate();

  const handleLogout = useCallback(() => {
    localStorage.removeItem('token');
    setAuth(false);
    navigate('/');
  }, [setAuth, navigate]);

  // A 401 or a token past its expiry: sign out and explain why on the sign-in page
  const handleExpired = useCallback(() => {
    sessionStorage.setItem(EXPIRED_KEY, '1');
    localStorage.removeItem('token');
    setAuth(false);
    navigate('/join');
  }, [setAuth, navigate]);

  // Keep the token renewed while the app is open; sign out once it has expired (e.g. after the laptop slept)
  useEffect(() => {
    const check = () => {
      if (document.visibilityState !== 'visible') return;
      ensureFreshToken().catch(err => { if (err instanceof UnauthorizedError) handleExpired(); });
    };
    check();
    const timer = setInterval(check, 60_000);
    document.addEventListener('visibilitychange', check);
    return () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', check);
    };
  }, [handleExpired]);

  const chat = useConversations({ onUnauthorized: handleExpired });
  const { pathname } = useLocation();
  const activeId = pathname.startsWith('/chat/') ? pathname.split('/')[2] : null;

  useEffect(() => {
    const mq = window.matchMedia('(min-width: 769px)');
    const onChange = () => setSidebarOpen(mq.matches);
    mq.addEventListener('change', onChange);
    return () => mq.removeEventListener('change', onChange);
  }, []);

  const closeOnMobile = () => { if (!isDesktop()) setSidebarOpen(false); };

  return (
    <div className="shell">
      {sidebarOpen && <div className="sidebar-overlay" onClick={() => setSidebarOpen(false)} />}
      <Sidebar
        open={sidebarOpen}
        onClose={closeOnMobile}
        onToggle={() => setSidebarOpen(false)}
        conversations={chat.conversations}
        activeId={activeId}
        onDelete={chat.deleteConversation}
        onLogout={handleLogout}
      />

      <main className="main">
        <header className="topbar">
          {!sidebarOpen && (
            <>
              <button className="icon-btn" onClick={() => setSidebarOpen(true)} aria-label="Open sidebar" title="Open sidebar">
                <PanelLeft size={18} />
              </button>
              <Link to="/chat" className="icon-btn" aria-label="New chat" title="New chat" onClick={closeOnMobile}>
                <SquarePen size={18} />
              </Link>
            </>
          )}
          <span className="topbar-title">NNT Studio</span>
        </header>

        <div className="main-body">
          <Routes>
            <Route path="/chat" element={<ChatRoute {...chat} onUnauthorized={handleExpired} />} />
            <Route path="/chat/:chatId" element={<ChatRoute {...chat} onUnauthorized={handleExpired} />} />
            <Route path="/dashboard/clients" element={<ClientsPage onUnauthorized={handleExpired} />} />
            <Route path="/dashboard/clients/:clientId" element={<ClientPage onUnauthorized={handleExpired} />} />
            <Route path="/dashboard/references" element={<ReferencesPage onUnauthorized={handleExpired} />} />
            <Route path="/dashboard/assets" element={<PlaceholderPage title="Assets" text="Your gallery of generated images, organized by client and project." />} />
            <Route path="*" element={<Navigate to="/chat" replace />} />
          </Routes>
        </div>
      </main>
    </div>
  );
};

// --- App Root ---

function App() {
  const [isAuthenticated, setIsAuthenticated] = useState(!!localStorage.getItem('token'));

  if (isAuthenticated) return <AppShell setAuth={setIsAuthenticated} />;

  return (
    <div className="public">
      <PublicNav />
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/about" element={<About />} />
        <Route path="/join" element={<Join setAuth={setIsAuthenticated} />} />
        <Route path="*" element={<Navigate to="/" />} />
      </Routes>
    </div>
  );
}

export default App;
