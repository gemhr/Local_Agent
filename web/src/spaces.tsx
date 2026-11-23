import { useEffect, useEffectEvent, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { ArrowRight, CircleAlert, CircleCheck, Folder, Plus, Shield, Users, X } from 'lucide-react'
import { ApiError, errorText, type Member, type MemberRole, type Principal, type Space, type SpaceRole, type WorkspaceApi } from './api'

const roles: Record<SpaceRole, string> = { OWNER: '所有者', ADMIN: '管理员', MEMBER: '成员' }

export function Banner({ tone, children }: { tone: 'error' | 'notice' | 'warning'; children: ReactNode }) {
  const Icon = tone === 'notice' ? CircleCheck : CircleAlert
  return <div className={`banner ${tone}`} role={tone === 'error' ? 'alert' : 'status'}>
    <Icon size={17} aria-hidden="true" /><div>{children}</div>
  </div>
}

function useSpace(api: WorkspaceApi, id: string, onAccessLost: () => void, includeMembers: boolean) {
  const [view, setView] = useState<{ space: Space; members: Member[] }>()
  const [error, setError] = useState('')
  const [revision, setRevision] = useState(0)
  const accessLost = useEffectEvent(onAccessLost)
  useEffect(() => {
    const request = new AbortController()
    async function load() {
      try {
        const space = await api.space(id, request.signal)
        const members = includeMembers && space.kind === 'TEAM' ? await api.members(id, request.signal) : []
        if (!request.signal.aborted) setView({ space, members })
      } catch (failure) {
        if (request.signal.aborted) return
        setError(errorText(failure))
        if (failure instanceof ApiError && [403, 404].includes(failure.status)) accessLost()
      }
    }
    void load()
    return () => request.abort()
  }, [api, id, includeMembers, revision])

  function refresh() {
    setView(undefined)
    setError('')
    setRevision(value => value + 1)
  }
  return { view, error, refresh }
}

function RoleLabel({ role }: { role: SpaceRole }) {
  return <span className={`role-badge ${role.toLowerCase()}`}>{roles[role]}</span>
}

function SpaceFacts({ space }: { space: Space }) {
  return <dl className="facts">
    <div><dt>所有者 ID</dt><dd className="identifier">{space.owner_user_id}</dd></div>
    <div><dt>空间 ID</dt><dd className="identifier">{space.id}</dd></div>
    <div><dt>空间角色</dt><dd><RoleLabel role={space.role} /></dd></div>
  </dl>
}

export function SpaceIdentity({ api, spaceId, onAccessLost }: {
  api: WorkspaceApi; spaceId: string; onAccessLost: () => void
}) {
  const { view, error, refresh } = useSpace(api, spaceId, onAccessLost, false)
  return <section className="panel overview-access">
    <header className="panel-heading"><div className="tile-icon"><Shield size={19} /></div><h2>当前空间权限</h2></header>
    {error ? <><Banner tone="error">{error}</Banner><button className="button secondary" onClick={refresh}>重试</button></>
      : view ? <SpaceFacts space={view.space} /> : <p className="muted" role="status">正在验证空间权限…</p>}
  </section>
}

function CreateTeam({ api, onCreated, onClose }: {
  api: WorkspaceApi; onCreated: (space: Space) => void; onClose: () => void
}) {
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const request = useRef<AbortController | null>(null)
  useEffect(() => () => request.current?.abort(), [])

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!name.trim() || busy) return
    const controller = new AbortController()
    request.current = controller
    setBusy(true)
    setError('')
    try {
      const team = await api.createTeam(name.trim(), controller.signal)
      if (!controller.signal.aborted) onCreated(team)
    } catch (failure) {
      if (!controller.signal.aborted) setError(errorText(failure))
    } finally {
      if (!controller.signal.aborted) setBusy(false)
    }
  }

  return <section className="panel create-panel">
    <header className="panel-heading">
      <h2>创建团队空间</h2>
      <button className="icon-button" type="button" aria-label="关闭创建表单" disabled={busy} onClick={onClose}><X size={18} /></button>
    </header>
    {error && <Banner tone="error">{error}</Banner>}
    <form onSubmit={submit}>
      <label htmlFor="team-name">团队名称</label>
      <div className="inline-form">
        <input id="team-name" placeholder="例如，运行时研发" maxLength={120} required autoFocus
          value={name} onChange={event => setName(event.target.value)} />
        <button type="submit" className="button primary" disabled={busy || !name.trim()}>{busy ? '正在创建…' : '创建团队'}</button>
      </div>
    </form>
  </section>
}

function MemberEditor({ api, principal, space, members, refresh, onIdentityMutation }: {
  api: WorkspaceApi; principal: Principal; space: Space; members: Member[];
  refresh: () => void; onIdentityMutation: () => void
}) {
  const [userId, setUserId] = useState('')
  const [role, setRole] = useState<MemberRole>('MEMBER')
  const [confirmation, setConfirmation] = useState<string>()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const request = useRef<AbortController | null>(null)
  useEffect(() => () => request.current?.abort(), [])

  async function mutate(target: string, nextRole?: MemberRole) {
    if (busy) return
    const controller = new AbortController()
    request.current = controller
    setError('')
    setBusy(true)
    try {
      if (nextRole) await api.putMember(space.id, target, nextRole, controller.signal)
      else await api.removeMember(space.id, target, controller.signal)
      if (controller.signal.aborted) return
      if (target === principal.user_id) onIdentityMutation()
      else refresh()
    } catch (failure) {
      if (!controller.signal.aborted) setError(errorText(failure))
    } finally {
      if (!controller.signal.aborted) setBusy(false)
    }
  }

  return <section className="panel members-panel">
    <header className="panel-heading"><div className="tile-icon"><Users size={19} /></div><h2>团队成员</h2></header>
    {!space.can_manage_members && <p className="muted">你可以查看成员。只有团队所有者和管理员可以管理成员。</p>}
    {error && <Banner tone="error">{error}</Banner>}
    {space.can_manage_members && <form className="member-form" onSubmit={event => {
      event.preventDefault()
      void mutate(userId.trim(), role)
    }}>
      <div><label htmlFor="member-id">用户 ID</label>
        <input id="member-id" placeholder="同一租户内的用户 ID" required autoComplete="off" value={userId}
          onChange={event => setUserId(event.target.value)} />
      </div>
      <div><label htmlFor="member-role">空间角色</label>
        <select id="member-role" value={role} onChange={event => setRole(event.target.value as MemberRole)}>
          <option value="MEMBER">成员</option><option value="ADMIN">管理员</option>
        </select>
      </div>
      <button type="submit" className="button primary" disabled={busy || !userId.trim()}><Plus size={15} /> 添加或更新成员</button>
    </form>}
    {members.length === 1 && <p className="muted roster-note">团队目前只有所有者。</p>}
    <ul className="member-list">
      {members.map(member => <li key={member.user_id}>
        <div className="member-person">
          <span className="avatar"><Users size={16} /></span>
          <div><strong>{member.display_name ?? '未设置名称'}{member.user_id === principal.user_id && '（你）'}</strong>
            <small className="identifier">{member.user_id}</small></div>
        </div>
        <div className="member-actions">
          {!member.active && <span className="muted">已停用</span>}
          <RoleLabel role={member.role} />
          {space.can_manage_members && member.role !== 'OWNER' && (confirmation === member.user_id
            ? <><button type="button" className="text-button danger" disabled={busy} onClick={() => void mutate(member.user_id)}>确认移除</button>
              <button type="button" className="text-button" disabled={busy} onClick={() => setConfirmation(undefined)}>取消</button></>
            : <button type="button" className="text-button danger" disabled={busy} onClick={() => setConfirmation(member.user_id)}>移除</button>)}
        </div>
      </li>)}
    </ul>
  </section>
}

function SpaceDetail({ api, principal, id, onAccessLost, onIdentityMutation }: {
  api: WorkspaceApi; principal: Principal; id: string; onAccessLost: () => void; onIdentityMutation: () => void
}) {
  const { view, error, refresh } = useSpace(api, id, onAccessLost, true)
  if (error) return <div className="panel state"><Banner tone="error">{error}</Banner><button className="button secondary" onClick={refresh}>重试</button></div>
  if (!view) return <div className="panel state" role="status">正在加载空间详情…</div>
  const { space, members } = view
  const Icon = space.kind === 'TEAM' ? Users : Folder
  return <div className="space-detail">
    <section className="panel">
      <header className="panel-heading"><div className="tile-icon accent"><Icon size={20} /></div>
        <div><h2>{space.name}</h2><p className="muted">{space.kind === 'TEAM' ? '团队空间' : '个人空间'}</p></div>
      </header>
      <SpaceFacts space={space} />
      {space.kind === 'PERSONAL' && <p className="muted personal-note">仅你可以访问此个人空间。</p>}
    </section>
    {space.kind === 'TEAM' && <MemberEditor api={api} principal={principal} space={space} members={members}
      refresh={refresh} onIdentityMutation={onIdentityMutation} />}
  </div>
}

export function SpacesPage({ api, principal, spaces, selected, onSelect, onCreated, onAccessLost, onIdentityMutation }: {
  api: WorkspaceApi; principal: Principal; spaces: Space[]; selected: Space; onSelect: (id: string) => void;
  onCreated: (space: Space) => void; onAccessLost: () => void; onIdentityMutation: () => void
}) {
  const [creating, setCreating] = useState(false)
  return <>
    <header className="page-heading">
      <div><h1>空间管理</h1><p>切换个人与团队空间，查看团队角色和成员。</p></div>
      <button type="button" className="button primary" onClick={() => setCreating(true)} disabled={creating}><Plus size={17} /> 新建团队</button>
    </header>
    {creating && <CreateTeam api={api} onCreated={space => { setCreating(false); onCreated(space) }} onClose={() => setCreating(false)} />}
    <div className="space-grid">
      <section className="panel directory">
        <header className="panel-heading"><h2>可访问空间</h2></header>
        <div className="directory-list">{spaces.map(space => {
          const Icon = space.kind === 'PERSONAL' ? Folder : Users
          return <button key={space.id} type="button" aria-pressed={selected.id === space.id} onClick={() => onSelect(space.id)}>
            <span className="tile-icon"><Icon size={17} /></span>
            <span><strong>{space.name}</strong><small>{space.kind === 'PERSONAL' ? '个人' : '团队'} · {roles[space.role]}</small></span>
            <ArrowRight size={15} />
          </button>
        })}</div>
        {!spaces.some(space => space.kind === 'TEAM') && <p className="muted directory-note">暂无可访问的团队空间。</p>}
      </section>
      <SpaceDetail key={selected.id} id={selected.id} api={api} principal={principal}
        onAccessLost={onAccessLost} onIdentityMutation={onIdentityMutation} />
    </div>
  </>
}
