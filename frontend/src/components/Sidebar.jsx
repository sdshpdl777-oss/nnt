import React from 'react';
import { Link, NavLink, useNavigate } from 'react-router-dom';
import { SquarePen, Users, Image as ImageIcon, Images, PanelLeft, Trash2, LogOut } from 'lucide-react';

function usernameFromToken() {
  try {
    const token = localStorage.getItem('token');
    return JSON.parse(atob(token.split('.')[1])).sub || 'You';
  } catch {
    return 'You';
  }
}

export default function Sidebar({ open, onClose, onToggle, conversations, activeId, onDelete, onLogout }) {
  const navigate = useNavigate();
  const username = usernameFromToken();

  const handleDelete = (e, id) => {
    e.preventDefault();
    e.stopPropagation();
    onDelete(id);
    if (id === activeId) navigate('/chat');
  };

  return (
    <aside className={`sidebar ${open ? 'open' : 'closed'}`}>
      <div className="sidebar-inner">
        <div className="sidebar-top">
          <Link to="/chat" className="brand" onClick={onClose} aria-label="NNT Studio by TechnoFort — new chat">
            <img src="/technofort-wordmark.png" alt="TechnoFort" className="brand-logo" />
          </Link>
          <button className="icon-btn" onClick={onToggle} aria-label="Close sidebar" title="Close sidebar">
            <PanelLeft size={18} />
          </button>
        </div>

        <nav className="sidebar-nav">
          <Link to="/chat" className="nav-item" onClick={onClose}>
            <SquarePen size={17} />
            <span>New chat</span>
          </Link>
          <NavLink to="/dashboard/clients" className="nav-item" onClick={onClose}>
            <Users size={17} />
            <span>Clients</span>
          </NavLink>
          <NavLink to="/dashboard/references" className="nav-item" onClick={onClose}>
            <Images size={17} />
            <span>References</span>
          </NavLink>
          <NavLink to="/dashboard/assets" className="nav-item" onClick={onClose}>
            <ImageIcon size={17} />
            <span>Assets</span>
          </NavLink>
        </nav>

        <div className="sidebar-section">
          <div className="section-label">Recents</div>
          {conversations.length === 0 ? (
            <p className="section-empty">Your chats will show up here.</p>
          ) : (
            conversations.map(c => (
              <Link
                key={c.id}
                to={`/chat/${c.id}`}
                className={`chat-item ${c.id === activeId ? 'active' : ''}`}
                onClick={onClose}
                title={c.title}
              >
                <span className="chat-item-title">{c.title}</span>
                <button className="chat-item-delete" onClick={e => handleDelete(e, c.id)} aria-label="Delete chat">
                  <Trash2 size={14} />
                </button>
              </Link>
            ))
          )}
        </div>

        <div className="sidebar-footer">
          <div className="profile">
            <span className="avatar">{username.slice(0, 1).toUpperCase()}</span>
            <span className="profile-name">{username}</span>
          </div>
          <button className="icon-btn" onClick={onLogout} aria-label="Log out" title="Log out">
            <LogOut size={17} />
          </button>
        </div>
      </div>
    </aside>
  );
}
