import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router'
import { Activity, ArrowRight, CheckCircle2, ClipboardCheck, Clock3 } from 'lucide-react'
import { ApiError, errorText, type Space, type WorkspaceApi } from './api'
import { Banner } from './spaces'
import { RunEvents } from './task-events'

type Kind = 'AGENT' | 'WORKFLOW'
type Task = {
  task_kind: Kind; task_id: string; space_id: string; space_name: string; title: string; definition_id: string
  owner_user_id: string; version: string; runtime_kind: string; source_status: string; status: string
  created_at: string; updated_at: string; started_at: string | null; ended_at: string | null; reason: string | null
  can_cancel: boolean; can_resume: boolean; can_approve: boolean
}
type Result = { availability: string; output: string | null; read_error_code?: string }
type Approval = {
  approval_id: string; step_id: string; status: string; binding_digest: string; decided_at: string | null
  decided_by: string | null; consumed_at: string | null; created_at: string
}
type Artifact = {
  workflow_run_id: string; step_id: string; attempt: number; producer_run_id: string
  runtime_instance_id: string; relative_path: string; size: number; sha256: string
}
type RepairArtifact = {
  schema_version: string; workflow_run_id: string; changed_paths: string[]; explanation: string
  baseline_revision: string; before: string; after: string; diff: string
  before_sha256: string; after_sha256: string; artifact_digest: string; diff_digest: string
}
type Step = {
  step_id: string; attempt: number; position: number; status: string; platform_run_id: string | null
  agent_id: string; agent_version: string; runtime_kind: string; runtime_instance_id: string | null
  runtime_status: string | null; runtime_observation: string | null; result_available: boolean
  reason: string | null; artifact_ref?: Artifact
}
type OracleResult = { case_id: string; expected_value: number; actual_value: number | null; oracle_status: string; reason: string | null }
type Replay = { replay_id: string; technical_status: string; receipt_digest: string; results: Record<'baseline' | 'candidate', OracleResult[]> }
type RepairGate = { decision: 'PASS' | 'FAIL' | 'BLOCKED'; url: string; baseline_run_id: string; candidate_run_id: string }
type Detail = Task & {
  result?: Result; configuration_revision?: number; runtime_instance_id?: string; input_digest?: string
  workflow?: {
    approval?: Approval; steps: Step[]; status: string; current_step: string; reason?: string
    repair_artifact_available?: boolean
    repair_context?: { incident_id: string; execution_id: string }
    repair_diagnosis?: { diagnosis_status: string; root_cause_hypothesis: string; failure_mechanism: string }
    repair_replay?: Replay | null; repair_evidence_digest?: string | null
  }
}
type ApprovalItem = Approval & {
  workflow_run_id: string; title: string; workflow_version: string; workflow_status: string
  space_id: string; space_name: string; owner_user_id: string; can_approve: boolean; awaiting_owner: boolean
}
type Page<T> = { items: T[]; next_offset: number | null }
type Props = { api: WorkspaceApi; space: Space }

const labels: Record<string, string> = {
  PENDING: '待执行', RUNNING: '运行中', ACTIVE: '运行中', WAITING_APPROVAL: '等待审批',
  AWAITING_OWNER: '已批准，待发起人继续', PAUSED: '已暂停', UNKNOWN: '待核实',
  SUCCEEDED: '已完成', FAILED: '已失败', REJECTED: '已拒绝', CANCELLING: '取消处理中',
  CANCELLED: '已取消', RESERVED: '已预留', APPROVED: '已批准', INVALIDATED: '已失效',
}
const statusFilters = ['PENDING', 'RUNNING', 'WAITING_APPROVAL', 'AWAITING_OWNER', 'PAUSED', 'UNKNOWN',
  'SUCCEEDED', 'FAILED', 'REJECTED', 'CANCELLING', 'CANCELLED']
const stamp = (value?: string | null) => value ? new Date(value).toLocaleString() : '未记录'
export const taskHref = (kind: string, id: string, space: string) => `/tasks/${kind}/${encodeURIComponent(id)}?space=${encodeURIComponent(space)}`
const isRevocation = (failure: unknown) => failure instanceof ApiError && [401, 403, 404].includes(failure.status)

function Status({ value }: { value: string }) {
  return <span className={`task-status status-${value.toLowerCase()}`}>{labels[value] ?? value}</span>
}

function useObservation<T>(api: WorkspaceApi, path: string | null, pollMs = 0) {
  const [snapshot, setSnapshot] = useState<{ path: string; data: T }>()
  const [failure, setFailure] = useState<{ path: string; text: string }>()
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    if (path === null) return
    const target = path
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    async function read() {
      try {
        const data = await api.observation<T>(target, controller.signal)
        if (!controller.signal.aborted) { setSnapshot({ path: target, data }); setFailure(undefined) }
      } catch (error) {
        if (controller.signal.aborted) return
        if (isRevocation(error)) setSnapshot(undefined)
        setFailure({ path: target, text: errorText(error) })
        if (isRevocation(error)) return
      }
      if (pollMs && !controller.signal.aborted) timer = setTimeout(() => void read(), pollMs)
    }
    void read()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [api, path, pollMs, revision])
  return {
    data: snapshot?.path === path ? snapshot.data : undefined, error: failure?.path === path ? failure.text : undefined,
    reload: () => setRevision(value => value + 1),
    clear: () => { setSnapshot(undefined); setRevision(value => value + 1) },
  }
}

function TaskRows({ tasks }: { tasks: Task[] }) {
  if (!tasks.length) return <div className="empty"><strong>暂无匹配的任务</strong><p>任务被后端接纳后会显示在这里。</p></div>
  return <ul className="task-rows">{tasks.map(task => <li key={`${task.task_kind}:${task.task_id}`}>
    <Link to={taskHref(task.task_kind, task.task_id, task.space_id)}>
      <div><strong>{task.title}</strong><small>{task.task_kind === 'AGENT' ? '单 Agent' : `Workflow · v${task.version}`} · {task.space_name}</small></div>
      <span className="task-runtime">{task.runtime_kind}</span>
      <Status value={task.status} /><time>{stamp(task.updated_at)}</time><ArrowRight size={16} />
    </Link>
  </li>)}</ul>
}

export function TaskBoard({ api, space }: Props) {
  const [scope, setScope] = useState('space')
  const [kind, setKind] = useState('')
  const [status, setStatus] = useState('')
  const [offset, setOffset] = useState(0)
  const params = new URLSearchParams({ offset: String(offset), limit: '20' })
  if (scope === 'space') params.set('space_id', space.id)
  if (scope === 'mine') params.set('mine', 'true')
  if (kind) params.set('kind', kind)
  if (status) params.set('status', status)
  const query = useObservation<Page<Task>>(api, `/workspace/tasks?${params}`, 2500)
  return <>
    <header className="page-heading"><div><h1>任务看板</h1><p>追踪当前有权访问的 Agent 和 Workflow 任务。</p></div>
      <Link className="button primary" to={`/tasks/new?space=${space.id}`}>创建任务 <ArrowRight size={16} /></Link></header>
    <section className="panel">
      <div className="task-filters">
        <div><label htmlFor="board-scope">任务范围</label><select id="board-scope" value={scope} onChange={e => { setScope(e.target.value); setOffset(0) }}>
          <option value="space">当前 Space</option><option value="mine">我创建的任务</option><option value="all">全部可访问任务</option>
        </select></div>
        <div><label htmlFor="board-kind">任务类型</label><select id="board-kind" value={kind} onChange={e => { setKind(e.target.value); setOffset(0) }}>
          <option value="">全部类型</option><option value="AGENT">单 Agent</option><option value="WORKFLOW">Workflow</option>
        </select></div>
        <div><label htmlFor="board-status">状态</label><select id="board-status" value={status} onChange={e => { setStatus(e.target.value); setOffset(0) }}>
          <option value="">全部状态</option>{statusFilters.map(value => <option key={value} value={value}>{labels[value]}</option>)}
        </select></div>
      </div>
      {query.error && <Banner tone="error">{query.error}</Banner>}
      {query.data ? <TaskRows tasks={query.data.items} /> : !query.error && <p role="status">正在读取任务…</p>}
      <div className="form-actions pagination">
        {offset > 0 && <button className="button secondary" onClick={() => setOffset(Math.max(0, offset - 20))}>上一页</button>}
        {query.data?.next_offset != null && <button className="button secondary" onClick={() => setOffset(query.data!.next_offset!)}>下一页</button>}
        <button className="button secondary" onClick={query.reload}>刷新</button>
      </div>
    </section>
  </>
}

function ActivitySection({ api, path, title }: { api: WorkspaceApi; path: string; title: string }) {
  const query = useObservation<Page<Task>>(api, path, 10_000)
  return <section className="panel"><header className="panel-heading"><Activity size={18} /><h2>{title}</h2></header>
    {query.error && <Banner tone="error">{query.error}</Banner>}
    {query.data ? <TaskRows tasks={query.data.items} /> : !query.error && <p role="status">正在读取…</p>}
  </section>
}

export function HomeActivity({ api, space }: Props) {
  const approvals = useObservation<Page<ApprovalItem>>(api, `/workspace/approvals?status=PENDING&space_id=${space.id}&limit=3`, 10_000)
  return <div className="activity-grid">
    <ActivitySection api={api} path={`/workspace/tasks?space_id=${space.id}&limit=4`} title="最近任务" />
    <ActivitySection api={api} path={`/workspace/tasks?space_id=${space.id}&status=RUNNING&limit=3`} title="运行中任务" />
    <section className="panel"><header className="panel-heading"><ClipboardCheck size={18} /><h2>待我审批</h2></header>
      {approvals.error && <Banner tone="error">{approvals.error}</Banner>}
      {approvals.data ? <ApprovalRows entries={approvals.data.items} /> : !approvals.error && <p role="status">正在读取审批…</p>}
    </section>
    <ActivitySection api={api} path={`/workspace/tasks?space_id=${space.id}&status=AWAITING_OWNER&mine=true&limit=3`} title="等待我继续" />
  </div>
}

function ApprovalRows({ entries }: { entries: ApprovalItem[] }) {
  if (!entries.length) return <div className="empty">暂无匹配的审批。</div>
  return <ul className="task-rows">{entries.map(entry => <li key={entry.approval_id}>
    <Link to={taskHref('WORKFLOW', entry.workflow_run_id, entry.space_id)}>
      <div><strong>{entry.title}</strong><small>v{entry.workflow_version} · {entry.step_id} · {entry.space_name}</small></div>
      <Status value={entry.awaiting_owner ? 'AWAITING_OWNER' : entry.status} />
      <time>{stamp(entry.decided_at ?? entry.created_at)}</time><ArrowRight size={16} />
    </Link>
  </li>)}</ul>
}

export function ApprovalCenter({ api, space }: Props) {
  const [status, setStatus] = useState('PENDING')
  const [offset, setOffset] = useState(0)
  const query = useObservation<Page<ApprovalItem>>(api,
    `/workspace/approvals?space_id=${space.id}&offset=${offset}&limit=20${status ? `&status=${status}` : ''}`, 2500)
  return <>
    <header className="page-heading"><div><h1>审批中心</h1><p>{space.name} · 查看审批依据、决定及待继续状态。</p></div></header>
    <section className="panel">
      <div className="task-filters"><div><label htmlFor="approval-status">审批状态</label><select id="approval-status" value={status}
        onChange={e => { setStatus(e.target.value); setOffset(0) }}>
        <option value="PENDING">待我审批</option><option value="APPROVED">已批准</option>
        <option value="REJECTED">已拒绝</option><option value="AWAITING_OWNER">等待发起人继续</option><option value="">最近历史</option>
      </select></div></div>
      {query.error && <Banner tone="error">{query.error}</Banner>}
      {query.data ? <ApprovalRows entries={query.data.items} /> : !query.error && <p role="status">正在读取审批…</p>}
      <div className="form-actions pagination">
        {offset > 0 && <button className="button secondary" onClick={() => setOffset(Math.max(0, offset - 20))}>上一页</button>}
        {query.data?.next_offset != null && <button className="button secondary" onClick={() => setOffset(query.data!.next_offset!)}>下一页</button>}
      </div>
    </section>
  </>
}

function ResultPanel({ result }: { result?: Result }) {
  return <div className="result-block">
    <h3>已确认结果</h3>
    {result?.availability === 'CONFIRMED' ? <pre>{result.output}</pre>
      : <p className="muted">{result?.read_error_code ? `结果读取失败 · ${result.read_error_code}` : '完整结果暂不可用。事件区可能包含部分输出。'}</p>}
  </div>
}

function StepResult({ api, step }: { api: WorkspaceApi; step: Step }) {
  const query = useObservation<Result>(api, `/runs/${encodeURIComponent(step.platform_run_id!)}/confirmed-result`)
  return <><ResultPanel result={query.data} />{query.error && <p className="muted">{query.error}</p>}</>
}

function ArtifactCard({ artifact, approved }: { artifact: Artifact; approved: boolean }) {
  return <section className="artifact-block">
    <h3><CheckCircle2 size={16} /> 已验证产物引用</h3>
    <p className="identifier">{artifact.relative_path}</p>
    <dl className="facts">
      <div><dt>来源</dt><dd>{artifact.step_id} · Attempt {artifact.attempt} · {artifact.runtime_instance_id}</dd></div>
      <div><dt>来源 Run</dt><dd className="identifier">{artifact.producer_run_id}</dd></div>
      <div><dt>大小</dt><dd>{artifact.size} 字节</dd></div>
      <div><dt>SHA-256</dt><dd className="identifier">{artifact.sha256}</dd></div>
      <div><dt>准出</dt><dd>{approved ? '通过' : '产物已记录，当前步骤未准出'}</dd></div>
    </dl>
  </section>
}

function TaskDetail({ api, kind, id }: { api: WorkspaceApi; kind: Kind; id: string }) {
  const query = useObservation<Detail>(api, `/workspace/tasks/${kind}/${encodeURIComponent(id)}`, 2500)
  const [pending, setPending] = useState(false)
  const [message, setMessage] = useState<{ tone: 'notice' | 'error'; text: string }>()
  const [confirmCancel, setConfirmCancel] = useState(false)
  const [selectedRun, setSelectedRun] = useState<string>()
  const [gateInput, setGateInput] = useState('')
  const [gateID, setGateID] = useState('')
  const task = query.data
  const wf = task?.workflow
  const approval = wf?.approval
  const actionPath = kind === 'WORKFLOW' ? `/workflow-runs/${id}` : `/runs/${id}`
  const patchQuery = useObservation<RepairArtifact>(api, wf?.repair_artifact_available ? `${actionPath}/repair-artifact` : null)
  const patch = patchQuery.data
  const gate = useObservation<RepairGate>(api, wf?.repair_replay && gateID ? `${actionPath}/repair-gate/${encodeURIComponent(gateID)}` : null)
  const [verifiedPatch, setVerifiedPatch] = useState<RepairArtifact>()
  useEffect(() => {
    if (!patch || patch.schema_version !== 'ci-repair-artifact.v1' || patch.workflow_run_id !== id
      || typeof patch.diff !== 'string' || !patch.diff || !globalThis.crypto?.subtle) return
    let active = true
    void crypto.subtle.digest('SHA-256', new TextEncoder().encode(patch.diff)).then(buffer => {
      const digest = Array.from(new Uint8Array(buffer), n => n.toString(16).padStart(2, '0')).join('')
      if (active && digest === patch.diff_digest) setVerifiedPatch(patch)
    }).catch(() => { if (active) setVerifiedPatch(undefined) })
    return () => { active = false }
  }, [patch, id])
  const canDecide = !wf?.repair_context || (!!patch && patch === verifiedPatch)
  const diffBinding = wf?.repair_context && patch === verifiedPatch && patch ? { diff_digest: patch.diff_digest } : {}

  async function perform(action: string, body?: object) {
    if (pending) return
    setPending(true)
    setMessage(undefined)
    try {
      await api.taskAction(`${actionPath}/${action}`, new AbortController().signal, body)
      setMessage({ tone: 'notice', text: '操作已提交。' })
      setConfirmCancel(false)
    } catch (failure) {
      setMessage({ tone: 'error', text: errorText(failure) })
      if (isRevocation(failure)) query.clear()
    } finally {
      setPending(false)
      query.reload()
    }
  }

  if (!task) return query.error ? <Banner tone="error">{query.error}</Banner>
    : <p className="panel state" role="status">正在读取任务详情…</p>

  const current = wf?.steps.find(step => step.step_id === wf.current_step)
  const observed = current?.platform_run_id ?? wf?.steps.find(step => step.platform_run_id)?.platform_run_id
  const eventRunId = kind === 'AGENT' ? task.task_id : selectedRun ?? observed
  const live = (task.status === 'RUNNING' || task.status === 'CANCELLING') && (!wf || eventRunId === observed)
  return <>
    <header className="page-heading task-heading"><div>
      <Link className="text-button" to={`/tasks?space=${task.space_id}`}>任务看板</Link>
      <h1>{task.title}</h1><p>{task.space_name} · {task.runtime_kind} · {task.definition_id} @{task.version}</p>
    </div><Status value={task.status} /></header>
    {query.error && <Banner tone="error">{query.error}</Banner>}
    {message && <Banner tone={message.tone}>{message.text}</Banner>}
    <div className="observation-layout">
      <div className="space-detail">
        {wf?.repair_context && <section className="panel">
          <header className="panel-heading"><h2>CI 修复证据</h2></header>
          <dl className="facts">
            <div><dt>Incident</dt><dd className="identifier">{wf.repair_context.incident_id}</dd></div>
            <div><dt>Execution</dt><dd className="identifier">{wf.repair_context.execution_id}</dd></div>
            {wf.repair_diagnosis && <div><dt>Native 诊断</dt><dd>{wf.repair_diagnosis.root_cause_hypothesis} · {wf.repair_diagnosis.failure_mechanism}</dd></div>}
          </dl>
          {patchQuery.error && <Banner tone="error">{patchQuery.error}</Banner>}
          {patch && patch === verifiedPatch && <>
            <p>{patch.changed_paths.join('，')} · {patch.explanation}</p>
            <dl className="facts">
              <div><dt>基线</dt><dd className="identifier">{patch.baseline_revision}</dd></div>
              <div><dt>修改前 SHA-256</dt><dd className="identifier">{patch.before_sha256}</dd></div>
              <div><dt>修改后 SHA-256</dt><dd className="identifier">{patch.after_sha256}</dd></div>
              <div><dt>产物 SHA-256</dt><dd className="identifier">{patch.artifact_digest}</dd></div>
              <div><dt>Diff SHA-256</dt><dd className="identifier">{patch.diff_digest}</dd></div>
            </dl>
            <details><summary>修改前后内容</summary><pre className="repair-diff">{patch.before}</pre><pre className="repair-diff">{patch.after}</pre></details>
            <h3>冻结 Git Diff</h3><pre className="repair-diff">{patch.diff}</pre>
          </>}
        </section>}
        {wf?.repair_replay && <section className="panel">
          <header className="panel-heading"><h2>确定性回放</h2><strong>{wf.repair_replay.technical_status}</strong></header>
          <dl className="facts">
            <div><dt>Replay Receipt</dt><dd className="identifier">{wf.repair_replay.replay_id}</dd></div>
            <div><dt>收据 SHA-256</dt><dd className="identifier">{wf.repair_replay.receipt_digest}</dd></div>
            <div><dt>独立评测证据 SHA-256</dt><dd className="identifier">{wf.repair_evidence_digest}</dd></div>
          </dl>
          <table><thead><tr><th>版本</th><th>用例</th><th>预期</th><th>解码值</th><th>Oracle</th></tr></thead>
            <tbody>{(['baseline', 'candidate'] as const).flatMap(role => wf.repair_replay!.results[role].map(result =>
              <tr key={`${role}:${result.case_id}`}><td>{role}</td><td>{result.case_id}</td><td>{result.expected_value}</td>
                <td>{result.actual_value ?? '—'}</td><td>{result.oracle_status}{result.reason && ` · ${result.reason}`}</td></tr>))}</tbody>
          </table>
          <h3>AgentEvalOps Release Gate</h3>
          <form className="form-actions" onSubmit={event => {
            event.preventDefault(); setGateID(gateInput.trim()); gate.clear()
          }}>
            <label>Gate ID <input value={gateInput} onChange={event => setGateInput(event.target.value)} required /></label>
            <button className="button secondary" type="submit">核对门禁</button>
          </form>
          {gate.error ? <p>门禁未确认。</p> : gate.data ? <p>{gate.data.decision} · <a href={gate.data.url} target="_blank" rel="noreferrer">查看评测详情</a></p>
            : <p>{gateID ? '正在核对门禁…' : '等待独立评测'}</p>}
        </section>}
        {approval && <section className="panel approval-panel">
          <header className="panel-heading"><ClipboardCheck size={20} /><h2>人工审批</h2><Status value={approval.status} /></header>
          {task.status === 'AWAITING_OWNER' && <p>已批准，等待原任务发起人继续执行。</p>}
          <dl className="facts">
            <div><dt>步骤</dt><dd>{approval.step_id} · v{task.version}</dd></div>
            <div><dt>决定人</dt><dd className="identifier">{approval.decided_by ?? '待决定'}</dd></div>
            <div><dt>决定时间</dt><dd>{stamp(approval.decided_at)}</dd></div>
            <div><dt>消费时间</dt><dd>{stamp(approval.consumed_at)}</dd></div>
            <div><dt>绑定摘要</dt><dd className="identifier">{approval.binding_digest}</dd></div>
          </dl>
          {task.can_approve && approval.status === 'PENDING' && <div className="form-actions">
            {wf?.repair_context && <p className="muted">批准绑定当前 Diff。候选文件变化会使批准失效。</p>}
            {!canDecide && <p className="muted">完整 Diff 通过摘要核对后可以审批。</p>}
            <button className="button primary" disabled={pending || !canDecide} onClick={() => void perform(`approvals/${approval.approval_id}/decision`,
              { decision: 'APPROVE', binding_digest: approval.binding_digest, ...diffBinding })}>批准</button>
            <button className="button secondary" disabled={pending || !canDecide} onClick={() => void perform(`approvals/${approval.approval_id}/decision`,
              { decision: 'REJECT', binding_digest: approval.binding_digest, ...diffBinding })}>拒绝</button>
          </div>}
          {approval.status === 'PENDING' && !task.can_approve && <p className="muted">等待有审批权限的成员处理。</p>}
        </section>}
        {wf ? <ol className="workflow-timeline">{wf.steps.map(step => <li key={step.step_id} className="panel">
          <header className="panel-heading"><span className="step-number">{step.position + 1}</span>
            <div><h2>{step.step_id} · {step.runtime_kind}</h2><p className="muted">{step.agent_id} @{step.agent_version}
              {step.runtime_instance_id && ` · ${step.runtime_instance_id}`}</p></div>
            <Status value={step.status === 'RESERVED'
              ? step.runtime_status ?? (step.runtime_observation === 'UNKNOWN' ? 'UNKNOWN' : step.status)
              : step.status} />
          </header>
          <p className="identifier">{step.platform_run_id ?? '尚无 Platform Run ID'}</p>
          {step.runtime_status && step.runtime_status !== step.status && step.status !== 'RESERVED' &&
            <p className="muted">Runtime 状态 {labels[step.runtime_status] ?? step.runtime_status}</p>}
          {step.reason && <p className="muted">原因 {step.reason}</p>}
          {step.artifact_ref && <ArtifactCard artifact={step.artifact_ref} approved={step.status === 'SUCCEEDED'} />}
          {step.platform_run_id && step.runtime_status === 'SUCCEEDED' && step.result_available && step.runtime_observation !== 'UNVERIFIED'
            ? <StepResult key={step.platform_run_id} api={api} step={step} />
            : <ResultPanel />}
          {step.platform_run_id && <button className="text-button" onClick={() => setSelectedRun(step.platform_run_id!)}>查看此步骤事件</button>}
        </li>)}</ol> : <section className="panel"><ResultPanel result={task.result} /></section>}
        {selectedRun && <button className="button secondary" onClick={() => setSelectedRun(undefined)}>跟踪当前步骤</button>}
        {eventRunId && <RunEvents key={eventRunId} api={api} runId={eventRunId} active={live} onRevoked={query.clear} />}
      </div>
      <aside className="panel task-meta">
        <header className="panel-heading"><Clock3 size={18} /><h2>执行信息</h2></header>
        <dl className="facts">
          <div><dt>任务 ID</dt><dd className="identifier">{task.task_id}</dd></div>
          <div><dt>发起人</dt><dd className="identifier">{task.owner_user_id}</dd></div>
          <div><dt>来源状态</dt><dd>{task.source_status}</dd></div>
          {wf && <div><dt>{task.status === 'WAITING_APPROVAL' || task.status === 'AWAITING_OWNER' ? '下一步骤' : '当前步骤'}</dt><dd>{wf.current_step}</dd></div>}
          {task.runtime_instance_id && <div><dt>Runtime 实例</dt><dd>{task.runtime_instance_id}</dd></div>}
          {task.configuration_revision && <div><dt>配置修订</dt><dd>{task.configuration_revision}</dd></div>}
          {task.input_digest && <div><dt>输入摘要</dt><dd className="identifier">{task.input_digest}</dd></div>}
          <div><dt>创建</dt><dd>{stamp(task.created_at)}</dd></div>
          <div><dt>开始</dt><dd>{stamp(task.started_at)}</dd></div>
          <div><dt>结束</dt><dd>{stamp(task.ended_at)}</dd></div>
          {task.reason && <div><dt>原因</dt><dd>{task.reason}</dd></div>}
        </dl>
        <div className="form-actions pagination">
          {task.can_resume && <button className="button primary" disabled={pending} onClick={() => void perform('resume')}>继续执行</button>}
          {task.can_cancel && !confirmCancel && <button className="button secondary" disabled={pending} onClick={() => setConfirmCancel(true)}>取消任务</button>}
          {task.can_cancel && confirmCancel && <><button className="button secondary" disabled={pending} onClick={() => void perform('cancel')}>确认取消</button>
            <button className="button secondary" onClick={() => setConfirmCancel(false)}>保留任务</button></>}
          {pending && <span role="status">正在提交…</span>}
        </div>
      </aside>
    </div>
  </>
}

export function TaskDetailPage({ api }: { api: WorkspaceApi }) {
  const { kind, taskId } = useParams()
  if ((kind !== 'AGENT' && kind !== 'WORKFLOW') || !taskId) return <Banner tone="error">任务链接无效。</Banner>
  return <TaskDetail key={`${kind}:${taskId}`} api={api} kind={kind} id={taskId} />
}
