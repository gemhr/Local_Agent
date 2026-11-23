export type Principal = {
  user_id: string
  tenant_id: string
  principal_kind: 'HUMAN' | 'SERVICE'
  roles: string[]
}

export type MemberRole = 'ADMIN' | 'MEMBER'
export type SpaceRole = 'OWNER' | MemberRole

export type Space = {
  id: string
  name: string
  tenant_id: string
  owner_user_id: string
  kind: 'PERSONAL' | 'TEAM'
  role: SpaceRole
  can_manage_members: boolean
  created_at: string
  updated_at: string
}

export type Member = {
  user_id: string
  display_name: string | null
  role: SpaceRole
  active: boolean
}

const messages: Record<string, string> = {
  SPACE_MEMBER_USER_NOT_FOUND: '找不到该租户内可加入的有效用户。请核对用户 ID。',
  SPACE_MEMBER_NOT_FOUND: '该成员已不在团队中。',
  SPACE_OWNER_IMMUTABLE: '团队所有者的角色不可修改，也不能移除。',
  SPACE_MEMBERS_UNAVAILABLE: '该空间没有团队成员列表。',
  VALIDATION_ERROR: '提交的信息格式有误，请检查后重试。',
  WORKSPACE_REVISION_CONFLICT: '配置已被更新，请刷新并重新编辑。',
  WORKSPACE_WORKFLOW_RUNTIME_REQUIRED: '请先选择符合模板用途的 OpenCode 实例。',
  WORKSPACE_WORKFLOW_INSTRUCTIONS_FIXED: 'CI 模板的任务指令由合同固定。',
  RUNTIME_INSTANCE_PURPOSE_DENIED: '该实例没有获准用于当前任务。',
  RUNTIME_INSTANCE_UNAVAILABLE: '当前 Runtime 实例不可用。',
  WORKFLOW_PUBLISHED_VERSION_UNAVAILABLE: '找不到此空间内可执行的发布版本。',
  WORKFLOW_DEFINITION_DRIFT: '当前执行目标与发布版本不一致。',
  WORKSPACE_ADMISSION_UNCONFIRMED: '暂未确认接纳。可重试同一提交以查询接纳结果。',
  WORKSPACE_RUNTIME_BUSY: 'Runtime 容量已满，请重试同一提交。',
  WORKSPACE_REQUEST_CONFLICT: '提交标识已用于其他任务。',
  WORKFLOW_APPROVAL_DECISION_CONFLICT: '审批已被处理或状态变化，请查看最新状态。',
  WORKFLOW_APPROVAL_INVALIDATED: '此审批已失效。',
  WORKFLOW_APPROVAL_BINDING_MISMATCH: '审批绑定已变化，请重新读取审批记录。',
}

export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message)
  }
}

function statusMessage(status: number): string {
  if (status === 401) return 'Token 无效或已过期，请重新输入。'
  if (status === 403) return '当前身份没有执行此操作的权限。'
  if (status === 404) return '当前身份无法访问此资源，或资源已不存在。'
  if (status === 429) return '操作过于频繁，请稍后重试。'
  if (status >= 500) return '服务暂时不可用，请稍后重试。'
  return '请求未完成，请检查输入后重试。'
}

export function errorText(error: unknown): string {
  return error instanceof ApiError ? error.message : '请求未完成，请稍后重试。'
}

export class WorkspaceApi {
  #token: string

  constructor(token: string, private readonly onUnauthorized?: () => void) {
    this.#token = token
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const timeout = AbortSignal.timeout(12_000)
    const signal = init.signal ? AbortSignal.any([init.signal, timeout]) : timeout
    const headers = new Headers({ Authorization: `Bearer ${this.#token}` })
    if (init.body) headers.set('Content-Type', 'application/json')
    let response: Response
    try {
      response = await fetch(`/api/v1${path}`, {
        ...init, headers, signal, cache: 'no-store', credentials: 'omit', redirect: 'error',
      })
    } catch (error) {
      if (init.signal?.aborted) throw error
      throw new ApiError(0, timeout.aborted ? '请求超时，请稍后重试。' : '无法连接工作台后端，请检查服务是否已启动。')
    }
    if (!response.ok) {
      if (response.status === 401) this.onUnauthorized?.()
      const body = await response.json().catch(() => null)
      const code = body?.error?.code
      const message = typeof code === 'string' && Object.hasOwn(messages, code) ? messages[code] : statusMessage(response.status)
      throw new ApiError(response.status, message)
    }
    return response.status === 204 ? undefined as T : response.json()
  }

  principal(signal: AbortSignal) {
    return this.request<{ principal: Principal }>('/principal', { signal })
  }

  spaces(signal: AbortSignal) {
    return this.request<Space[]>('/spaces', { signal })
  }

  space(id: string, signal: AbortSignal) {
    return this.request<Space>(`/spaces/${encodeURIComponent(id)}`, { signal })
  }

  createTeam(name: string, signal: AbortSignal) {
    return this.request<Space>('/spaces', { method: 'POST', body: JSON.stringify({ name }), signal })
  }

  members(id: string, signal: AbortSignal) {
    return this.request<Member[]>(`/spaces/${encodeURIComponent(id)}/members`, { signal })
  }

  putMember(id: string, userId: string, role: MemberRole, signal: AbortSignal) {
    return this.request<Member>(`/spaces/${encodeURIComponent(id)}/members/${encodeURIComponent(userId)}`, {
      method: 'PUT', body: JSON.stringify({ role }), signal,
    })
  }

  removeMember(id: string, userId: string, signal: AbortSignal) {
    return this.request<void>(`/spaces/${encodeURIComponent(id)}/members/${encodeURIComponent(userId)}`, {
      method: 'DELETE', signal,
    })
  }

  execution<T>(spaceId: string, path: string, signal: AbortSignal, method = 'GET', body?: object) {
    return this.request<T>(`/spaces/${encodeURIComponent(spaceId)}/${path}`, {
      signal, method, body: body && JSON.stringify(body),
    })
  }

  observation<T>(path: string, signal: AbortSignal) {
    return this.request<T>(path, { signal })
  }

  taskAction(path: string, signal: AbortSignal, body?: object) {
    return this.request<unknown>(path, { method: 'POST', signal, body: body && JSON.stringify(body) })
  }

  async eventStream(runId: string, cursor: number, signal: AbortSignal) {
    const response = await fetch(`/api/v1/runs/${encodeURIComponent(runId)}/events?after_cursor=${cursor}`, {
      headers: { Authorization: `Bearer ${this.#token}`, Accept: 'text/event-stream' },
      signal, cache: 'no-store', credentials: 'omit', redirect: 'error',
    })
    if (response.status === 401) this.onUnauthorized?.()
    if (!response.ok || !response.body) throw new ApiError(response.status, statusMessage(response.status))
    return response.body.pipeThrough(new TextDecoderStream())
  }
}
