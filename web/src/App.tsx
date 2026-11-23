import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, NavLink, Navigate, Route, Routes, useNavigate, useSearchParams } from 'react-router'
import {
  Activity, ArrowRight, Bot, ChevronDown, CircleCheck, ClipboardCheck, Fingerprint,
  Folder, House, KeyRound, ListTodo, LogOut, Plus, Users, Workflow,
} from 'lucide-react'
import { errorText, type Principal, type Space, WorkspaceApi } from './api'
import { Banner, SpaceIdentity, SpacesPage } from './spaces'
import { AgentsPage, RuntimePage, TasksPage, WorkflowsPage } from './execution-pages'
import { ApprovalCenter, HomeActivity, TaskBoard, TaskDetailPage } from './observation-pages'

type Session = { api: WorkspaceApi; principal: Principal }

function Brand() {
  return <div className="brand">
    <span className="brand-mark" aria-hidden="true"><Workflow size={19} /></span>
    <span>AgentCore<small>Workspace</small></span>
  </div>
}

function Login({ expired, onLogin }: { expired: boolean; onLogin: (token: string, principal: Principal) => void }) {
  const [token, setToken] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const request = useRef<AbortController | null>(null)
  useEffect(() => () => request.current?.abort(), [])

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const credential = token.trim()
    if (!credential || pending) return
    const controller = new AbortController()
    request.current = controller
    setError('')
    setPending(true)
    try {
      const { principal } = await new WorkspaceApi(credential).principal(controller.signal)
      if (!controller.signal.aborted) onLogin(credential, principal)
    } catch (failure) {
      if (!controller.signal.aborted) setError(errorText(failure))
    } finally {
      if (!controller.signal.aborted) setPending(false)
    }
  }

  return <main className="login-layout">
    <div className="login-brand"><Brand /></div>
    <section className="login-card" aria-labelledby="login-title">
      <div className="tile-icon accent"><KeyRound size={22} /></div>
      <h1 id="login-title">登录工作台</h1>
      <p className="muted">使用已有的 Bearer Token 访问个人空间和团队空间。</p>
      {(error || expired) && <Banner tone="error">{error || '会话已过期，请重新输入 Token。'}</Banner>}
      <form onSubmit={submit}>
        <label htmlFor="token">Bearer Token</label>
        <input id="token" type="password" autoComplete="off" spellCheck={false} required
          placeholder="粘贴 Token" value={token} onChange={event => setToken(event.target.value)} />
        <button className="button primary" disabled={pending || !token.trim()} type="submit">
          {pending ? '正在验证…' : '进入工作台'} <ArrowRight size={16} />
        </button>
      </form>
      <p className="login-note"><Fingerprint size={15} /> Token 仅保存在当前页面，刷新后需重新输入。</p>
    </section>
  </main>
}

function Overview({ principal, spaces, selected, manageUrl }: {
  principal: Principal; spaces: Space[]; selected: Space; manageUrl: string
}) {
  const teams = spaces.filter(space => space.kind === 'TEAM')
  return <>
    <header className="page-heading">
      <div><h1>工作台概览</h1><p>查看当前身份、空间和你可以访问的团队。</p></div>
      <Link className="button secondary" to={manageUrl}>管理空间 <ArrowRight size={16} /></Link>
    </header>
    <section className="overview-hero">
      <div className="tile-icon accent">{selected.kind === 'TEAM' ? <Users size={22} /> : <Folder size={22} />}</div>
      <div className="overview-title"><p>当前工作空间</p><h2>{selected.name}</h2></div>
      <Link className="button hero-link" to={manageUrl}>查看空间详情 <ArrowRight size={16} /></Link>
    </section>
    <div className="overview-grid">
      <section className="panel">
        <header className="panel-heading"><div className="tile-icon"><Fingerprint size={19} /></div><h2>当前身份</h2></header>
        <dl className="facts">
          <div><dt>用户 ID</dt><dd className="identifier">{principal.user_id}</dd></div>
          <div><dt>所属租户</dt><dd className="identifier">{principal.tenant_id}</dd></div>
          <div><dt>身份与角色</dt><dd>{principal.principal_kind} · {principal.roles.join('、')}</dd></div>
        </dl>
      </section>
      <section className="panel">
        <header className="panel-heading"><div className="tile-icon"><Users size={19} /></div><h2>可访问团队</h2></header>
        {teams.length === 0
          ? <div className="empty"><strong>还没有团队空间</strong><p>创建团队，或由已有团队的管理员添加你。</p></div>
          : <div className="overview-teams">{teams.map(team =>
            <Link key={team.id} to={`/spaces?space=${team.id}`}>
              <span>{team.name}</span><ArrowRight size={15} />
            </Link>
          )}</div>}
      </section>
    </div>
  </>
}

function Workspace({ session, onLogout }: { session: Session; onLogout: () => void }) {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const [spaces, setSpaces] = useState<Space[]>([])
  const [error, setError] = useState('')
  const [notice, setNotice] = useState<{ message: string; tone: 'notice' | 'warning' }>()
  const [loading, setLoading] = useState(true)
  const [revision, setRevision] = useState(0)
  const requestedId = params.get('space')
  const personal = spaces.find(space => space.kind === 'PERSONAL')
  const selected = spaces.find(space => space.id === requestedId) ?? personal

  useEffect(() => {
    const request = new AbortController()
    session.api.spaces(request.signal).then(
      data => { if (!request.signal.aborted) setSpaces(data) },
      failure => { if (!request.signal.aborted) setError(errorText(failure)) },
    ).finally(() => { if (!request.signal.aborted) setLoading(false) })
    return () => request.abort()
  }, [session, revision])

  useEffect(() => {
    if (loading || error || !selected || requestedId === selected.id) return
    if (requestedId) setNotice({ message: '当前空间不可访问，已切回个人空间。', tone: 'warning' })
    setParams({ space: selected.id }, { replace: true })
  }, [loading, error, selected, requestedId, setParams])

  function reload() {
    setLoading(true)
    setError('')
    setRevision(value => value + 1)
  }

  function choose(id: string) {
    setNotice(undefined)
    setParams({ space: id })
  }

  function created(space: Space) {
    setNotice({ message: `已创建团队「${space.name}」。`, tone: 'notice' })
    reload()
    navigate(`/spaces?space=${space.id}`)
  }

  function accessLost() {
    if (!personal || personal.id === selected?.id) return
    setNotice({ message: '当前空间不可访问，已切回个人空间。', tone: 'warning' })
    setParams({ space: personal.id }, { replace: true })
    reload()
  }

  const search = selected ? `?space=${selected.id}` : ''
  const manageUrl = `/spaces${search}`
  const { principal } = session
  return <div className="app-shell">
    <aside className="sidebar">
      <Brand />
      <nav aria-label="主导航">
        <p className="nav-label">工作台</p>
        <NavLink to={`/${search}`} end><House size={18} /> <span>Workspace 首页</span></NavLink>
        <NavLink to={`/tasks${search}`} end><ListTodo size={18} /> <span>我的任务</span></NavLink>
        <NavLink to={`/approvals${search}`}><ClipboardCheck size={18} /> <span>审批中心</span></NavLink>
        <NavLink to={`/agents${search}`}><Bot size={18} /> <span>Agent</span></NavLink>
        <NavLink to={`/workflows${search}`}><Workflow size={18} /> <span>Workflow</span></NavLink>
        <NavLink to={`/runtime${search}`}><Activity size={18} /> <span>Runtime</span></NavLink>
        <NavLink to={manageUrl}><Folder size={18} /> <span>空间管理</span></NavLink>
      </nav>
      <div className="sidebar-footer"><span className="avatar"><Fingerprint size={17} /></span>
        <div><strong>已登录</strong><small title={principal.tenant_id}>{principal.tenant_id}</small></div>
      </div>
    </aside>
    <div className="workspace">
      <header className="topbar">
        <div className="switcher">
          <Folder size={17} aria-hidden="true" />
          <select aria-label="切换空间" value={selected?.id ?? ''} onChange={event => choose(event.target.value)} disabled={loading || !selected}>
            {!selected && <option value="">选择空间</option>}
            {spaces.map(space => <option key={space.id} value={space.id}>{space.kind === 'PERSONAL' ? '个人' : '团队'} · {space.name}</option>)}
          </select>
          <ChevronDown size={15} aria-hidden="true" />
        </div>
        <div className="top-actions">
          <span className="top-user" title={principal.user_id}><CircleCheck size={16} /> {principal.user_id.slice(0, 8)}</span>
          {selected && <Link className="button secondary" to={`/tasks/new${search}`}><Plus size={16} /> 创建任务</Link>}
          <button className="icon-button" aria-label="退出登录" title="退出登录" onClick={onLogout} type="button"><LogOut size={17} /></button>
        </div>
      </header>
      <main className="content">
        {notice && <Banner tone={notice.tone}>{notice.message}</Banner>}
        {loading ? <div className="panel state" role="status">正在加载空间…</div>
          : error ? <div className="panel state"><Banner tone="error">{error}</Banner><button className="button secondary" onClick={reload}>重新加载</button></div>
            : selected ? <Routes>
              <Route path="/" element={<>
                <Overview principal={principal} spaces={spaces} selected={selected} manageUrl={manageUrl} />
                <SpaceIdentity key={selected.id} api={session.api} spaceId={selected.id} onAccessLost={accessLost} />
                <HomeActivity key={`activity:${selected.id}`} api={session.api} space={selected} />
              </>} />
              <Route path="/spaces" element={<SpacesPage api={session.api} principal={principal} spaces={spaces}
                selected={selected} onSelect={choose} onCreated={created} onAccessLost={accessLost} onIdentityMutation={reload} />} />
              <Route path="/agents" element={<AgentsPage key={selected.id} api={session.api} space={selected} />} />
              <Route path="/workflows" element={<WorkflowsPage key={selected.id} api={session.api} space={selected} />} />
              <Route path="/runtime" element={<RuntimePage key={selected.id} api={session.api} space={selected} />} />
              <Route path="/tasks/new" element={<TasksPage key={selected.id} api={session.api} space={selected} />} />
              <Route path="/tasks" element={<TaskBoard key={selected.id} api={session.api} space={selected} />} />
              <Route path="/tasks/:kind/:taskId" element={<TaskDetailPage api={session.api} />} />
              <Route path="/approvals" element={<ApprovalCenter key={selected.id} api={session.api} space={selected} />} />
              <Route path="*" element={<Navigate to={`/${search}`} replace />} />
            </Routes> : <Banner tone="error">服务端未返回个人空间。<button className="text-button" onClick={reload}>重试</button></Banner>}
      </main>
    </div>
  </div>
}

export function App() {
  const [session, setSession] = useState<Session | null>(null)
  const [expired, setExpired] = useState(false)

  function login(token: string, principal: Principal) {
    const api = new WorkspaceApi(token, () => {
      setExpired(true)
      setSession(current => current?.api === api ? null : current)
    })
    setExpired(false)
    setSession({ api, principal })
  }

  return session ? <Workspace session={session} onLogout={() => { setExpired(false); setSession(null) }} />
    : <Login expired={expired} onLogin={login} />
}
