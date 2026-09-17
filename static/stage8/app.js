(() => {
  const $ = (id) => document.getElementById(id);
  const state = { missionId: null, mission: null, plan: null, review: null, job: null, ciRunId: null, authToken: '' };
  const sample = {
    feature_id: '5g-rrc-reconnect',
    feature_document: 'Improve reconnection after radio link failure.',
    code_diff: 'RRC retry/timer logic changed.',
    affected_modules: ['RRC', 'radio-link-control'],
    meeting_summary: 'Focus on reconnect timeout and degraded RF environment.',
    developer_notes: 'Preserve compatibility with existing UE retry behavior.',
    retrieval_evidence: []
  };

  const show = (id, value) => {
    const node = $(id); node.replaceChildren();
    if (value === undefined || value === null || value === '') { const e = document.createElement('span'); e.className = 'empty'; e.textContent = '暂无结果'; node.append(e); return; }
    const pre = document.createElement('pre'); pre.className = 'json'; pre.textContent = typeof value === 'string' ? value : JSON.stringify(value, null, 2); node.append(pre);
  };
  const toast = (message) => { const node = $('toast'); node.textContent = message; node.style.display = 'block'; window.clearTimeout(toast.timer); toast.timer = window.setTimeout(() => { node.style.display = 'none'; }, 4500); };
  async function api(path, options = {}) {
    const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
    const token = state.authToken.trim();
    if (token) headers.Authorization = token.toLowerCase().startsWith('bearer ') ? token : `Bearer ${token}`;
    const response = await fetch(path, { ...options, headers });
    const raw = await response.text(); let body; try { body = raw ? JSON.parse(raw) : {}; } catch { body = raw; }
    if (!response.ok) throw new Error(`${response.status}: ${body?.detail || body?.error?.message || raw || 'request failed'}`);
    return body;
  }
  const setBusy = (button, busy) => { button.disabled = busy; if (busy) { button.dataset.label = button.textContent; button.textContent = 'Running…'; } else if (button.dataset.label) button.textContent = button.dataset.label; };
  const context = () => ({ feature_id: $('feature-id').value.trim(), feature_document: $('feature-document').value, code_diff: $('code-diff').value, affected_modules: $('affected-modules').value.split(',').map(x => x.trim()).filter(Boolean), meeting_summary: $('meeting-summary').value, developer_notes: 'Demo input; review before production use.', retrieval_evidence: [] });
  function renderMission(mission) { state.mission = mission; const badge = $('mission-badge'); badge.textContent = mission.status; badge.className = 'badge ' + (mission.status === 'COMPLETED' ? 'ok' : mission.status === 'TRIAGING' ? 'warn' : mission.status === 'FAILED' ? 'fail' : ''); $('mission-ids').textContent = `Mission ID: ${mission.mission_id} · version ${mission.version} · feature ${mission.feature_id}`; }
  function renderTriage(job) {
    const node = $('triage-result'); node.replaceChildren();
    const triage = job?.triage;
    if (!triage) { const empty = document.createElement('span'); empty.className = 'empty'; empty.textContent = '暂无 Triage 结果'; node.append(empty); return; }
    const result = triage.result || triage;
    const heading = document.createElement('h4'); heading.textContent = 'Backend Triage Result'; node.append(heading);
    const summary = document.createElement('pre'); summary.className = 'json'; summary.textContent = JSON.stringify(result, null, 2); node.append(summary);
    const request = triage.ticket_request;
    if (request?.status === 'APPROVAL_REQUIRED') {
      const pending = document.createElement('p'); pending.className = 'warning'; pending.textContent = `Tool Approval Pending · approval_id: ${request.approval_id || '暂无'}`; node.append(pending);
    }
  }
  $('auth-token').addEventListener('input', (event) => { state.authToken = event.currentTarget.value; });
  function enableWorkflow() {
    const missionStatus = state.mission?.status;
    const reviewPending = state.review?.status === 'PENDING';
    const jobRunning = state.job?.status === 'RUNNING';
    $('run-planning').disabled = !state.missionId || !['CREATED', 'CONTEXT_READY'].includes(missionStatus);
    $('approve-review').disabled = !reviewPending; $('reject-review').disabled = !reviewPending;
    $('start-execution').disabled = state.review?.status !== 'APPROVED' || missionStatus !== 'READY_FOR_EXECUTION' || Boolean(state.job);
    $('submit-success').disabled = $('submit-product').disabled = $('submit-test-data').disabled = !jobRunning;
  }
  $('load-sample').addEventListener('click', () => { $('feature-id').value = sample.feature_id; $('mission-title').value = '5G RRC reconnect optimization'; $('mission-summary').value = sample.feature_document; $('feature-document').value = sample.feature_document; $('code-diff').value = sample.code_diff; $('meeting-summary').value = sample.meeting_summary; $('affected-modules').value = sample.affected_modules.join(', '); toast('已载入 deterministic demo scenario'); });
  $('create-mission').addEventListener('click', async (event) => { const button = event.currentTarget; setBusy(button, true); try { const mission = await api('/api/stage8/missions', { method: 'POST', body: JSON.stringify({ feature_id: $('feature-id').value.trim(), title: $('mission-title').value.trim(), summary: $('mission-summary').value }) }); state.missionId = mission.mission_id; renderMission(mission); enableWorkflow(); toast('Mission created'); } catch (error) { toast(error.message); } finally { setBusy(button, false); } });
  $('run-planning').addEventListener('click', async (event) => { const button = event.currentTarget; setBusy(button, true); try { const result = await api(`/api/stage8/missions/${state.missionId}/planning`, { method: 'POST', body: JSON.stringify({ mission_id: state.missionId, context: context() }) }); state.plan = result.test_plan; state.review = result.review; show('understanding', result.feature_understanding); show('risks', result.risk_analysis); show('test-plan', result.test_plan); renderMission(result.mission); $('review-meta').textContent = `Review ${result.review.status} · subject ${result.review.subject_id} · v${result.review.subject_version} · digest ${result.review.subject_digest}`; toast('Planning workflow completed'); } catch (error) { toast(error.message); } finally { setBusy(button, false); enableWorkflow(); } });
  async function decideReview(kind, button) { setBusy(button, true); try { const result = await api(`/api/stage8/reviews/${state.review.review_id}/${kind}`, { method: 'POST', body: JSON.stringify({ mission_id: state.missionId, subject_id: state.review.subject_id, subject_version: state.review.subject_version, subject_digest: state.review.subject_digest, decided_by: 'web-demo' }) }); state.review = result; $('review-meta').textContent = `Review ${result.status} · subject ${result.subject_id} · v${result.subject_version} · digest ${result.subject_digest}`; renderMission(await api(`/api/stage8/missions/${state.missionId}`)); toast(`Business Review ${result.status}`); } catch (error) { toast(error.message); } finally { setBusy(button, false); enableWorkflow(); } }
  $('approve-review').addEventListener('click', (event) => decideReview('approve', event.currentTarget)); $('reject-review').addEventListener('click', (event) => decideReview('reject', event.currentTarget));
  $('start-execution').addEventListener('click', async (event) => { const button = event.currentTarget; setBusy(button, true); try { state.job = await api(`/api/stage8/missions/${state.missionId}/execution`, { method: 'POST', body: JSON.stringify({ case_id: $('case-id').value, environment_id: $('environment-id').value, executor_id: $('executor-id').value, parameters: {} }) }); show('job-result', state.job); renderMission(await api(`/api/stage8/missions/${state.missionId}`)); toast('External Execution Job started'); } catch (error) { toast(error.message); } finally { setBusy(button, false); enableWorkflow(); } });
  async function submitResult(status, sampleKind, button) { setBusy(button, true); try { const result = await api(`/api/stage8/executions/${state.job.execution_id}/result`, { method: 'POST', body: JSON.stringify({ execution_id: state.job.execution_id, status, actual_result: status === 'SUCCEEDED' ? 'Reconnected within target timeout' : 'Reconnect timeout exceeded', expected_result: 'Reconnect within target timeout', failure_signature: sampleKind ? `RRC_RECONNECT_${sampleKind}_LIKE` : null, logs: sampleKind ? [`mock ${sampleKind}-like evidence`, 'timer expired in degraded RF environment'] : [] }) }); state.job = result; show('job-result', result); renderTriage(result); renderMission(await api(`/api/stage8/missions/${state.missionId}`)); toast('Result callback accepted'); } catch (error) { toast(error.message); } finally { setBusy(button, false); enableWorkflow(); } }
  $('submit-success').addEventListener('click', (event) => submitResult('SUCCEEDED', null, event.currentTarget)); $('submit-product').addEventListener('click', (event) => submitResult('FAILED', 'PRODUCT', event.currentTarget)); $('submit-test-data').addEventListener('click', (event) => submitResult('FAILED', 'TEST_DATA', event.currentTarget));

  const toolboxSamples = {
    'feature-understanding': { context: sample },
    'risk-analysis': { feature_context: sample, feature_understanding: { feature_id: sample.feature_id, summary: 'Reconnect retry/timer behavior changed.', change_points: ['Retry timer'], affected_components: sample.affected_modules, clarifications: [], known_constraints: [], evidence: [] } },
    'test-planning': { feature_understanding: { feature_id: sample.feature_id, summary: 'Reconnect retry/timer behavior changed.', change_points: ['Retry timer'], affected_components: sample.affected_modules, clarifications: [], known_constraints: [], evidence: [] }, risk_analysis: { feature_id: sample.feature_id, risks: [], summary: 'Validate timeout and degraded RF behavior.' } },
    'failure-triage': { execution_id: '' }, 'ci-guardian': { ci_run_id: '' }
  };
  function refreshToolboxSample() { if ($('agent-select').value === 'ci-guardian') toolboxSamples['ci-guardian'].ci_run_id = state.ciRunId || ''; $('toolbox-input').value = JSON.stringify(toolboxSamples[$('agent-select').value], null, 2); }
  $('agent-select').addEventListener('change', refreshToolboxSample); refreshToolboxSample();
  $('run-agent').addEventListener('click', async (event) => { const button = event.currentTarget; setBusy(button, true); try { const agent = $('agent-select').value; const payload = JSON.parse($('toolbox-input').value); if (agent === 'failure-triage' && !payload.execution_id) throw new Error('Failure Triage requires an execution_id from a real job'); if (agent === 'ci-guardian' && !payload.ci_run_id) throw new Error('CI Guardian requires a ci_run_id from an ingested run'); const path = agent === 'ci-guardian' ? `/api/stage8/ci/runs/${encodeURIComponent(payload.ci_run_id)}/analyze` : `/api/stage8/agents/${agent}/run`; const result = await api(path, { method: 'POST', body: JSON.stringify(agent === 'ci-guardian' ? {} : payload) }); show('toolbox-output', result); } catch (error) { toast(error.message); } finally { setBusy(button, false); } });

  const ciRun = (id, previous = false) => {
    const now = Date.now(); const day = previous ? 2 : 1;
    const started = new Date(now - day * 86400000); const completed = new Date(started.getTime() + 20 * 60000);
    const executionCompleted = new Date(started.getTime() + 15 * 60000); const changed = new Date(started.getTime() - 10 * 60000);
    return { ci_run_id: id, suite_id: 'rrc-regression', started_at: started.toISOString(), completed_at: completed.toISOString(), status: 'COMPLETED', branch: 'main', executions: [{ execution_id: `${id}-e1`, case_id: 'rrc-reconnect-timeout', status: previous ? 'PASS' : 'FAILED', failure_signature: previous ? null : 'RRC_TIMEOUT', classification: previous ? null : 'PRODUCT', environment_id: 'degraded-rf-lab', executor_id: 'ci-executor', completed_at: executionCompleted.toISOString(), affected_components: ['RRC'], logs: previous ? [] : ['reconnect timer expired'] }], related_feature_ids: ['5g-rrc-reconnect'], change_timeline: previous ? [] : [{ change_id: `${id}-change`, change_type: 'CODE_COMMIT', occurred_at: changed.toISOString(), source_ref: 'commit/demo-rrc', summary: 'RRC retry/timer logic changed', affected_components: ['RRC'], feature_id: '5g-rrc-reconnect', version: 'demo' }] };
  };
  $('load-ci').addEventListener('click', async (event) => { const button = event.currentTarget; setBusy(button, true); try { const suffix = Date.now(); await api('/api/stage8/ci/runs', { method: 'POST', body: JSON.stringify(ciRun(`demo-prev-${suffix}`, true)) }); const current = await api('/api/stage8/ci/runs', { method: 'POST', body: JSON.stringify(ciRun(`demo-current-${suffix}`)) }); state.ciRunId = current.ci_run_id; refreshToolboxSample(); $('ci-status').textContent = `Current CI Run: ${state.ciRunId} · previous run ingested`; $('analyze-ci').disabled = $('read-ci').disabled = false; toast('Previous and current CI runs ingested'); } catch (error) { toast(error.message); } finally { setBusy(button, false); } });
  async function readAnalysis(analyze, button) { setBusy(button, true); try { const result = await api(analyze ? `/api/stage8/ci/runs/${state.ciRunId}/analyze` : `/api/stage8/ci/runs/${state.ciRunId}/analysis`, { method: analyze ? 'POST' : 'GET' }); show('ci-result', result); } catch (error) { toast(error.message); } finally { setBusy(button, false); } }
  $('analyze-ci').addEventListener('click', (event) => readAnalysis(true, event.currentTarget)); $('read-ci').addEventListener('click', (event) => readAnalysis(false, event.currentTarget));
  document.querySelectorAll('.tab').forEach(tab => tab.addEventListener('click', () => { document.querySelectorAll('.tab,.panel').forEach(node => node.classList.remove('active')); tab.classList.add('active'); $(tab.dataset.tab).classList.add('active'); }));
})();
