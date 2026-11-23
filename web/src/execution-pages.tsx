import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { Link, useSearchParams } from 'react-router'
import { ArrowRight, Plus } from 'lucide-react'
import { errorText, type Space, type WorkspaceApi } from './api'
import { Banner } from './spaces'
import { taskHref } from './observation-pages'

type PageProps = { api: WorkspaceApi; space: Space }
type AgentBase = { id: string; version: string; name: string; runtime_kinds: string[] }
type Agent = {
  id: string; name: string; description: string; revision: number
  base_agent_id: string; base_agent_version: string; instructions: string
}
type Instance = { id: string; name: string; kind: string; deployment: string | null; purposes: string[]; status: string }
type Step = { id?: string; task_id?: string; agent_id: string; agent_version: string; runtime_kind: string; instruction: string; result_contract?: string; requires_approval: boolean }
type Template = {
  id: string; version: string; instructions_editable: boolean; requires_ci_input: boolean
  approval_phase: string | null; runtime_purpose: string; fixed_runtime_instance_id: string | null; steps: Step[]
}
type Catalog = { agents: AgentBase[]; templates: Template[] }
type Draft = { name: string; description: string; runtime_instance_id: string | null; external_instruction: string; native_instruction: string }
type Release = {
  version: string; name: string; description: string; requires_ci_input: boolean
  runtime_instance_id: string; approval_phase: string | null; steps: Step[]
}
type Workflow = { id: string; name: string; template_id: string; template_version: string; revision: number | null; draft: Draft | null; versions: Release[] }
type TaskReceipt = { kind: string; status: string; run_id?: string; workflow_run_id?: string; events_url?: string; detail_url?: string }

function useQuery<T>({ api, space }: PageProps, path: string) {
  const [data, setData] = useState<T>()
  const [error, setError] = useState('')
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    const request = new AbortController()
    api.execution<T>(space.id, path, request.signal).then(
      result => { if (!request.signal.aborted) setData(result) },
      failure => { if (!request.signal.aborted) setError(errorText(failure)) },
    )
    return () => request.abort()
  }, [api, space.id, path, revision])
  return {
    data, setData, error,
    reload: () => { setError(''); setRevision(value => value + 1) },
  }
}

function useMutation({ api, space }: PageProps) {
  const request = useRef<AbortController | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => () => request.current?.abort(), [])

  async function save<T>(path: string, method: string, payload: object, onSuccess: (value: T) => void) {
    if (request.current) return
    const controller = new AbortController()
    request.current = controller
    setBusy(true)
    setError('')
    try {
      const result = await api.execution<T>(space.id, path, controller.signal, method, payload)
      if (!controller.signal.aborted) onSuccess(result)
    } catch (failure) {
      if (!controller.signal.aborted) setError(errorText(failure))
    } finally {
      request.current = null
      if (!controller.signal.aborted) setBusy(false)
    }
  }
  return { save, busy, error }
}

function taskUrl(space: Space, selection = '') {
  return `/tasks/new?space=${space.id}${selection}`
}

function Heading({ title, detail, actions }: { title: string; detail: string; actions?: ReactNode }) {
  return <header className="page-heading"><div><h1>{title}</h1><p>{detail}</p></div>{actions}</header>
}

function Loading({ error, retry }: { error?: string; retry: () => void }) {
  return <div className="panel state">
    {error ? <><Banner tone="error">{error}</Banner><button className="button secondary" type="button" onClick={retry}>重新加载</button></>
      : <p role="status">正在加载配置…</p>}
  </div>
}

function EntryList<T extends { id: string; name: string }>({ title, rows, selected, select }: {
  title: string; rows: T[]; selected?: string; select: (id: string) => void
}) {
  return <section className="panel directory">
    <header className="panel-heading"><h2>{title}</h2></header>
    <div className="directory-list execution-directory">{rows.map(row =>
      <button key={row.id} type="button" aria-pressed={selected === row.id} onClick={() => select(row.id)}>
        <strong>{row.name}</strong><ArrowRight size={15} />
      </button>
    )}</div>
    {rows.length === 0 && <p className="muted">暂无可访问配置。</p>}
  </section>
}

function AgentForm(props: PageProps & { agents: AgentBase[]; entry?: Agent; onDone: (agent: Agent) => void }) {
  const { agents, entry, onDone, space } = props
  const [name, setName] = useState(entry?.name ?? '')
  const [description, setDescription] = useState(entry?.description ?? '')
  const [instructions, setInstructions] = useState(entry?.instructions ?? '')
  const [baseId, setBaseId] = useState(entry?.base_agent_id ?? agents[0]?.id ?? '')
  const base = agents.find(item => item.id === baseId)
  const { save, busy, error } = useMutation(props)

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!base) return
    void save<Agent>(entry ? `agents/${entry.id}` : 'agents', entry ? 'PUT' : 'POST', {
      name, description, instructions, base_agent_id: base.id, base_agent_version: base.version,
      ...(entry && { expected_revision: entry.revision }),
    }, onDone)
  }

  return <section className="panel">
    <header className="panel-heading"><h2>{entry ? entry.name : '新建 Agent 配置'}</h2>
      {entry && <Link className="button secondary" to={taskUrl(space, `&config=${entry.id}`)}>用此 Agent 创建任务</Link>}
    </header>
    {entry && <p className="muted form-meta">修订 {entry.revision} · Native</p>}
    {error && <Banner tone="error">{error}</Banner>}
    <form className="definition-form" onSubmit={submit}>
      <div><label htmlFor="agent-name">Agent 名称</label><input id="agent-name" value={name} required maxLength={120} onChange={event => setName(event.target.value)} /></div>
      <div><label htmlFor="agent-base">Native 底座</label><select id="agent-base" required value={baseId} onChange={event => setBaseId(event.target.value)}>
        {agents.map(agent => <option key={agent.id} value={agent.id}>{agent.name} · {agent.id} @{agent.version}</option>)}
      </select></div>
      <div><label htmlFor="agent-description">描述</label><input id="agent-description" value={description} maxLength={1000} onChange={event => setDescription(event.target.value)} /></div>
      <div><label htmlFor="agent-instructions">任务指令</label><textarea id="agent-instructions" required rows={6} maxLength={4000}
        value={instructions} onChange={event => setInstructions(event.target.value)} /></div>
      <p className="muted">保存的指令与提交时的任务说明一起作为文本输入。底座仍管理模型、系统指令和工具。</p>
      <button className="button primary" type="submit" disabled={busy || !base}>{busy ? '正在保存…' : '保存 Agent'}</button>
    </form>
  </section>
}

export function AgentsPage(props: PageProps) {
  const { space } = props
  const catalog = useQuery<Catalog>(props, 'execution-catalog')
  const agents = useQuery<Agent[]>(props, 'agents')
  const [selection, setSelection] = useState<string>()
  const [notice, setNotice] = useState('')
  const manage = space.role !== 'MEMBER'
  const entry = agents.data?.find(item => item.id === selection)

  function saved(value: Agent) {
    agents.setData(items => [...(items ?? []).filter(item => item.id !== value.id), value])
    setSelection(value.id)
    setNotice(`已保存「${value.name}」的修订 ${value.revision}。`)
  }

  return <>
    <Heading title="Agent" detail="查看已注册入口，保存可用于 Native 任务的配置。"
      actions={manage && <button className="button primary" type="button" onClick={() => { setSelection('new'); setNotice('') }}><Plus size={16} /> 新建 Agent</button>} />
    {notice && <Banner tone="notice">{notice}</Banner>}
    {catalog.data && <section className="panel catalog-panel">
      <h2>受信 Agent</h2>
      {catalog.data.agents.map(agent => <div className="catalog-item" key={agent.id}>
        <div><strong>{agent.name}</strong><p className="muted identifier">{agent.id} @{agent.version} · {agent.runtime_kinds.join(' / ')} · 文本输入与输出</p></div>
        <Link className="button secondary" to={taskUrl(space)}>创建任务</Link>
      </div>)}
    </section>}
    {catalog.data && agents.data ? <div className="space-grid">
      <EntryList rows={agents.data} title="保存的 Agent" selected={selection} select={id => { setSelection(id); setNotice('') }} />
      {manage && (entry || selection === 'new') ? <AgentForm key={entry ? `${entry.id}:${entry.revision}` : 'new'}
        {...props} agents={catalog.data.agents} entry={entry} onDone={saved} />
        : entry ? <section className="panel">
          <div className="panel-heading"><h2>{entry.name}</h2></div>
          <p className="muted">{entry.description || `${entry.base_agent_id} · 修订 ${entry.revision}`}</p>
          <p className="instructions">{entry.instructions}</p>
          <Link className="button primary" to={taskUrl(space, `&config=${entry.id}`)}>用此 Agent 创建任务</Link>
        </section> : <div className="panel state">选择已保存的 Agent 以查看配置。</div>}
    </div> : <Loading error={catalog.error || agents.error} retry={() => { catalog.reload(); agents.reload() }} />}
  </>
}

function WorkflowForm(props: PageProps & {
  entry: Workflow; template: Template; runtimes: Instance[]; onDone: (workflow: Workflow, notice: string) => void
}) {
  const { entry, template, runtimes, onDone } = props
  const saved = entry.draft!
  const [draft, setDraft] = useState(saved)
  const dirty = Object.keys(saved).some(key => draft[key as keyof Draft] !== saved[key as keyof Draft])
  const { save, busy, error } = useMutation(props)
  const choices = runtimes.filter(instance => instance.purposes.includes(template.runtime_purpose)
    && (!template.fixed_runtime_instance_id || instance.id === template.fixed_runtime_instance_id))
  const update = (key: keyof Draft, value: string) => setDraft(previous => ({ ...previous, [key]: value }))

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const { name, description, runtime_instance_id, external_instruction, native_instruction } = draft
    void save<Workflow>(`workflows/${entry.id}`, 'PUT', {
      expected_revision: entry.revision, name, description, runtime_instance_id: runtime_instance_id || null,
      ...(template.instructions_editable && { external_instruction, native_instruction }),
    }, updated => onDone(updated, '草稿已保存。'))
  }

  function publish() {
    void save<Workflow>(`workflows/${entry.id}/publish`, 'POST', { expected_revision: entry.revision },
      updated => onDone(updated, `已发布版本 ${updated.versions[0].version}。`))
  }

  return <section className="panel">
    <header className="panel-heading"><h2>编辑草稿</h2><span className="role-badge">修订 {entry.revision}</span></header>
    {error && <Banner tone="error">{error}</Banner>}
    <form className="definition-form" onSubmit={submit}>
      <div><label htmlFor="workflow-name">Workflow 名称</label><input id="workflow-name" maxLength={120} required
        value={draft.name} onChange={event => update('name', event.target.value)} /></div>
      <div><label htmlFor="workflow-description">描述</label><input id="workflow-description" maxLength={1000}
        value={draft.description} onChange={event => update('description', event.target.value)} /></div>
      <div><label htmlFor="workflow-runtime">OpenCode 实例</label><select id="workflow-runtime" value={draft.runtime_instance_id ?? ''}
        onChange={event => update('runtime_instance_id', event.target.value)}>
        <option value="">请选择获准实例</option>
        {choices.map(instance => <option key={instance.id} value={instance.id}>{instance.name} · {instance.deployment}</option>)}
      </select>
      {choices.length === 0 && <p className="muted">当前身份没有符合此模板用途的 OpenCode 实例，草稿仍可保存。</p>}</div>
      {template.steps.map((step, index) => {
        const field = index === 0 ? 'external_instruction' : 'native_instruction'
        return <section className="workflow-step" key={step.id}>
          <h3>{index + 1} · {step.runtime_kind}</h3>
          <p className="muted">{step.agent_id} @{step.agent_version} · {step.result_contract}{step.requires_approval && ' · 产物验证后审批'}</p>
          <label htmlFor={field}>{step.runtime_kind} 任务指令</label>
          <textarea id={field} required rows={4} maxLength={4000} readOnly={!template.instructions_editable}
            value={draft[field]} onChange={event => update(field, event.target.value)} />
        </section>
      })}
      {template.requires_ci_input && <p className="muted">任务输入为 incident_id 和 execution_id。产物路径、结构化输出及下游校验由模板固定。</p>}
      <div className="form-actions">
        <button className="button secondary" type="submit" disabled={busy}>保存草稿</button>
        <button className="button primary" type="button" disabled={busy || dirty || !draft.runtime_instance_id} onClick={publish}>发布版本</button>
      </div>
      {dirty && <p className="muted">保存草稿后可以发布。</p>}
    </form>
  </section>
}

export function WorkflowsPage(props: PageProps) {
  const { space } = props
  const catalog = useQuery<Catalog>(props, 'execution-catalog')
  const workflows = useQuery<Workflow[]>(props, 'workflows')
  const runtimes = useQuery<Instance[]>(props, 'runtime-instances')
  const [selection, setSelection] = useState('')
  const [templateId, setTemplateId] = useState('')
  const [notice, setNotice] = useState('')
  const [chosenRelease, setChosenRelease] = useState('')
  const copy = useMutation(props)
  const entry = workflows.data?.find(item => item.id === selection)
  const selectedTemplate = catalog.data?.templates.find(item => item.id === templateId)
  const editorTemplate = catalog.data?.templates.find(item => item.id === entry?.template_id && item.version === entry.template_version)
  const release = entry?.versions.find(item => item.version === chosenRelease) ?? entry?.versions[0]
  const manage = space.role !== 'MEMBER'

  function saved(value: Workflow, message: string) {
    workflows.setData(items => [value, ...(items ?? []).filter(item => item.id !== value.id)])
    setSelection(value.id)
    setNotice(message)
  }

  function copyTemplate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!selectedTemplate) return
    void copy.save<Workflow>('workflows', 'POST', {
      template_id: selectedTemplate.id, template_version: selectedTemplate.version, name: selectedTemplate.id,
    }, value => { setChosenRelease(''); saved(value, '已复制模板，可以编辑草稿。') })
  }

  return <>
    <Heading title="Workflow" detail="复制两步串行模板，编辑草稿并发布可执行版本。" />
    {notice && <Banner tone="notice">{notice}</Banner>}
    {catalog.data && runtimes.data && workflows.data ? <>
      <section className="panel catalog-panel">
        <header className="panel-heading"><h2>受信模板</h2></header>
        {manage ? <form className="inline-form" onSubmit={copyTemplate}>
          <select aria-label="Workflow 模板" required value={templateId} onChange={event => setTemplateId(event.target.value)}>
            <option value="">选择模板</option>
            {catalog.data.templates.map(item => <option key={item.id} value={item.id}>{item.id} @{item.version}</option>)}
          </select>
          <button type="submit" className="button primary" disabled={copy.busy || !selectedTemplate}><Plus size={16} /> 复制模板</button>
        </form> : <p className="muted">{catalog.data.templates.map(item => item.id).join(' · ')}</p>}
        {copy.error && <Banner tone="error">{copy.error}</Banner>}
      </section>
      <div className="space-grid">
        <EntryList title="空间 Workflow" rows={workflows.data} selected={selection} select={id => { setSelection(id); setChosenRelease(''); setNotice('') }} />
        <div className="space-detail">
          {entry?.draft && editorTemplate && <WorkflowForm key={`${entry.id}:${entry.revision}:${entry.versions.length}`}
            {...props} entry={entry} template={editorTemplate} runtimes={runtimes.data} onDone={saved} />}
          {entry?.draft && !editorTemplate && <Banner tone="error">已有草稿所依赖的受信模板版本不可用。</Banner>}
          <section className="panel">
            <h2>已发布版本</h2>
            {entry && release ? <>
              <label htmlFor="release-version">查看版本</label>
              <select id="release-version" value={release.version} onChange={event => setChosenRelease(event.target.value)}>
                {entry.versions.map(item => <option key={item.version} value={item.version}>版本 {item.version} · {item.name}</option>)}
              </select>
              <p className="instructions">{release.runtime_instance_id} · {release.approval_phase ?? '无审批步骤'}</p>
              {release.steps.map(step => <p className="release-step" key={step.task_id}><strong>{step.runtime_kind} · {step.agent_id} @{step.agent_version}</strong><span className="instructions">{step.instruction}</span></p>)}
              <Link className="button primary" to={taskUrl(space, `&workflow=${entry.id}&version=${release.version}`)}>用此版本创建任务</Link>
            </> : <p className="muted">{entry ? '尚未发布，草稿不可执行。' : '请选择一个 Workflow。'}</p>}
          </section>
        </div>
      </div>
    </> : <Loading error={catalog.error || workflows.error || runtimes.error} retry={() => { catalog.reload(); workflows.reload(); runtimes.reload() }} />}
  </>
}

const purposeLabels: Record<string, string> = {
  AGENT_TEXT: '单 Agent 文本任务', WORKFLOW_TEXT: '文本接力 Workflow', WORKFLOW_CI_ARTIFACT: '受控 CI 产物 Workflow',
}

export function RuntimePage(props: PageProps) {
  const runtimes = useQuery<Instance[]>(props, 'runtime-instances')
  return <>
    <Heading title="Runtime" detail="查看当前身份可使用的受信实例和已授权用途。" />
    {runtimes.data ? <div className="overview-grid">
      {runtimes.data.map(item => <section className="panel" key={`${item.kind}:${item.id}`}>
        <header className="panel-heading"><h2>{item.name}</h2><span className="role-badge">{item.status}</span></header>
        <p>{item.kind}{item.deployment && ` · ${item.deployment}`}</p>
        <ul className="runtime-purposes">{item.purposes.map(purpose => <li key={purpose}>{purposeLabels[purpose]}</li>)}</ul>
        {item.deployment === 'MANAGED' && <p className="muted">当前为同机托管模拟。</p>}
      </section>)}
      {!runtimes.data.some(item => item.kind === 'OPENCODE' && item.purposes.includes('AGENT_TEXT')) &&
        <div className="panel state">当前身份没有获准用于普通文本任务的 OpenCode 实例。</div>}
    </div> : <Loading error={runtimes.error} retry={runtimes.reload} />}
  </>
}

function TaskForm(props: PageProps & { catalog: Catalog; agents: Agent[]; runtimes: Instance[]; workflows: Workflow[] }) {
  const [params] = useSearchParams()
  const { catalog, agents, runtimes, workflows } = props
  const [mode, setMode] = useState(params.has('workflow') || params.has('version') ? 'WORKFLOW' : 'AGENT')
  const [workflowId, setWorkflowId] = useState(params.get('workflow') ?? '')
  const [version, setVersion] = useState(params.get('version'))
  const [configId, setConfigId] = useState(params.get('config') ?? '')
  const [runtime, setRuntime] = useState('NATIVE')
  const [instanceId, setInstanceId] = useState('')
  const [input, setInput] = useState('')
  const [incident, setIncident] = useState('')
  const [execution, setExecution] = useState('')
  const [receipt, setReceipt] = useState<TaskReceipt>()
  const key = useRef<{ payload: string; requestId: string } | undefined>(undefined)
  const [timeout, setTimeoutSeconds] = useState('')
  const { save, busy, error } = useMutation(props)
  const config = agents.find(item => item.id === configId)
  const workflow = workflows.find(item => item.id === workflowId)
  const release = version === null ? workflow?.versions[0] : workflow?.versions.find(item => item.version === version)
  const invalidVersion = version !== null && !release
  const textInstances = runtimes.filter(item => item.kind === 'OPENCODE' && item.purposes.includes('AGENT_TEXT'))
  const base = catalog.agents[0]
  const canSubmit = mode === 'WORKFLOW' ? !!release : !!base && (!configId || !!config) && (runtime === 'NATIVE' || !!instanceId)

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!canSubmit || busy || receipt) return
    const payload = mode === 'WORKFLOW' && release && workflow ? {
      kind: mode, workflow_id: workflow.id, version: release.version,
      ...(release.requires_ci_input && { incident_id: incident.trim(), execution_id: execution.trim() }),
    } : {
      kind: 'AGENT', runtime_kind: runtime, input,
      ...(config ? { config_id: config.id, expected_revision: config.revision } : { agent_id: base.id, agent_version: base.version }),
      ...(runtime === 'OPENCODE' && { runtime_instance_id: instanceId }),
      ...(timeout && { timeout_seconds: Number(timeout) }),
    }
    const serialized = JSON.stringify(payload)
    if (key.current?.payload !== serialized) key.current = { payload: serialized, requestId: crypto.randomUUID() }
    void save<TaskReceipt>('tasks', 'POST', { ...payload, request_id: key.current.requestId }, setReceipt)
  }

  return <section className="panel task-panel">
    {receipt ? <>
      <Banner tone="notice">任务已由服务端接纳。</Banner>
      <dl className="facts">
        <div><dt>{receipt.kind === 'AGENT' ? 'Run ID' : 'WorkflowRun ID'}</dt><dd className="identifier">{receipt.run_id ?? receipt.workflow_run_id}</dd></div>
        <div><dt>当前状态</dt><dd>{receipt.status}</dd></div>
        <div><dt>查询接口</dt><dd className="identifier">{receipt.events_url ?? receipt.detail_url}</dd></div>
      </dl>
      <Link className="button primary task-reset" to={taskHref(receipt.kind, (receipt.run_id ?? receipt.workflow_run_id)!, props.space.id)}>查看任务详情</Link>
      <button className="button secondary task-reset" type="button" onClick={() => { key.current = undefined; setReceipt(undefined) }}>再创建一个任务</button>
    </> : <>
      {error && <Banner tone="error">{error}</Banner>}
      <form className="definition-form" onSubmit={submit}>
        <div><label htmlFor="task-kind">任务类型</label><select id="task-kind" value={mode} onChange={event => setMode(event.target.value)}>
          <option value="AGENT">单 Agent</option><option value="WORKFLOW">Workflow</option>
        </select></div>
        {mode === 'AGENT' ? <>
          <div><label htmlFor="task-agent">选择 Agent</label><select id="task-agent" value={configId}
            onChange={event => { setConfigId(event.target.value); setRuntime('NATIVE') }}>
            <option value="">注册入口 · {base?.id} @{base?.version}</option>
            {agents.map(item => <option key={item.id} value={item.id}>{item.name} · 修订 {item.revision}</option>)}
          </select></div>
          <div><label htmlFor="task-runtime">Runtime</label><select id="task-runtime" value={runtime} onChange={event => setRuntime(event.target.value)}>
            <option value="NATIVE">Native</option><option value="OPENCODE" disabled={!!configId || textInstances.length === 0}>OpenCode</option>
          </select>
          {textInstances.length === 0 && !configId && <p className="muted">当前没有获准用于单 Agent 的 OpenCode 实例。</p>}</div>
          {runtime === 'OPENCODE' && <div><label htmlFor="task-instance">OpenCode 实例</label><select id="task-instance" value={instanceId} required onChange={event => setInstanceId(event.target.value)}>
            <option value="">请选择实例</option>
            {textInstances.map(item => <option key={item.id} value={item.id}>{item.name} · {item.deployment}</option>)}
          </select></div>}
          <div><label htmlFor="task-input">任务说明</label><textarea id="task-input" required rows={6} maxLength={4000} value={input} onChange={event => setInput(event.target.value)} /></div>
          <div><label htmlFor="task-timeout">超时秒数（可选）</label><input id="task-timeout" type="number" min={1} max={120} value={timeout}
            onChange={event => setTimeoutSeconds(event.target.value)} /></div>
        </> : <>
          <div><label htmlFor="task-workflow">已发布 Workflow</label><select id="task-workflow" required value={workflowId}
            onChange={event => { setWorkflowId(event.target.value); setVersion(invalidVersion ? '' : null) }}>
            <option value="">请选择发布的 Workflow</option>
            {workflows.filter(item => item.versions.length > 0).map(item => <option key={item.id} value={item.id}>{item.versions[0].name}</option>)}
          </select></div>
          {invalidVersion && <Banner tone="error">{version ? `指定的发布版本「${version}」不可用，请选择有效版本。` : '请选择有效的发布版本。'}</Banner>}
          {workflow && workflow.versions.length > 0 && <div><label htmlFor="task-version">发布版本</label>
            <select id="task-version" required aria-invalid={invalidVersion} value={release?.version ?? ''} onChange={event => setVersion(event.target.value)}>
            {invalidVersion && <option value="" disabled>请选择有效版本</option>}
            {workflow.versions.map(item => <option key={item.version} value={item.version}>版本 {item.version}</option>)}
          </select></div>}
          {release && <>
          <p className="muted">绑定实例 {release.runtime_instance_id}</p>
          {release.requires_ci_input && <>
            <div><label htmlFor="incident-id">Incident ID</label><input id="incident-id" required value={incident} onChange={event => setIncident(event.target.value)} /></div>
            <div><label htmlFor="execution-id">Execution ID</label><input id="execution-id" required value={execution} onChange={event => setExecution(event.target.value)} /></div>
          </>}</>}
        </>}
        <button type="submit" className="button primary" disabled={busy || !canSubmit}>{busy ? '正在提交…' : '提交任务'}</button>
      </form>
    </>}
  </section>
}

export function TasksPage(props: PageProps) {
  const [params] = useSearchParams()
  const catalog = useQuery<Catalog>(props, 'execution-catalog')
  const agents = useQuery<Agent[]>(props, 'agents')
  const runtimes = useQuery<Instance[]>(props, 'runtime-instances')
  const workflows = useQuery<Workflow[]>(props, 'workflows')
  return <>
    <Heading title="创建任务" detail={`当前空间 · ${props.space.name}`} />
    {catalog.data && agents.data && runtimes.data && workflows.data
      ? <TaskForm key={JSON.stringify([params.get('workflow'), params.get('version')])}
          {...props} catalog={catalog.data} agents={agents.data} runtimes={runtimes.data} workflows={workflows.data} />
      : <Loading error={catalog.error || agents.error || runtimes.error || workflows.error}
        retry={() => { catalog.reload(); agents.reload(); runtimes.reload(); workflows.reload() }} />}
  </>
}
