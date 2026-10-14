import { useState } from 'react';
import { SettingsPage } from './pages/SettingsPage';
import { CoursesPage } from './pages/CoursesPage';
import { TasksPage } from './pages/TasksPage';

type Page = 'courses' | 'tasks' | 'settings';

export function App({ tokenPresent }: { tokenPresent: boolean }) {
  const [page, setPage] = useState<Page>('settings');
  const [focusWorkflowId, setFocusWorkflowId] = useState('');
  const nav = [['courses', '我的课程'], ['tasks', '任务中心'], ['settings', '设置']] as const;

  if (!tokenPresent) return <div className="launch-error"><div className="brand-mark">TJ</div><h1>请从课程助手启动器打开</h1><p>访问令牌仅在本次运行中有效，不会保存在浏览器存储里。</p></div>;
  return <div className="app-shell">
    <aside className="sidebar">
      <a className="brand" href="/reader/" aria-label="打开原阅读网页">
        <span className="brand-mark">TJ</span><span><strong>课程助手</strong><small>LOCAL CONSOLE</small></span>
      </a>
      <div className="sidebar-caption">工作区</div>
      <nav className="primary-nav" aria-label="主导航">
        {nav.map(([id, label], index) => <button key={id} className={`primary-nav-item ${page === id ? 'active' : ''}`} onClick={() => setPage(id)} aria-current={page === id ? 'page' : undefined}>
          <span className={`nav-symbol nav-symbol-${id}`} aria-hidden="true">{['▦', '◷', '⚙'][index]}</span>{label}{page === id && <span className="active-rail" />}
        </button>)}
      </nav>
      <div className="sidebar-bottom"><span className="local-indicator" />仅本机访问<span className="version-label">V2</span></div>
    </aside>
    <main className="main-area">
      <header className="topbar"><div className="breadcrumb"><span>控制台</span><span className="crumb-separator">/</span><strong>{nav.find(([id]) => id === page)?.[1]}</strong></div><a href="/reader/" className="legacy-link">原阅读网页 ↗</a></header>
      <div className={`page-content ${page === 'settings' ? 'page-content-settings' : ''}`}>
        {page === 'settings' && <SettingsPage />}
        {page === 'courses' && <CoursesPage onWorkflowCreated={(workflowId) => { setFocusWorkflowId(workflowId); setPage('tasks'); }} onOpenSettings={() => setPage('settings')} />}
        {page === 'tasks' && <TasksPage focusWorkflowId={focusWorkflowId} />}
      </div>
    </main>
  </div>;
}
