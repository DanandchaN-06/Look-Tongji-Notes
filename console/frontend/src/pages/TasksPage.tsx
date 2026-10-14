import { useEffect, useRef, useState } from 'react';
import { cancelWorkflow, getAgentInstruction, getReaderState, getWorkflow, getWorkflowLog, getWorkflows, openReader, requestReaderBuild, retryWorkflow, verifyAgentOutput } from '../api/client';
import type { ReaderState, Workflow, WorkflowAttempt, WorkflowLog } from '../api/client';

const stateLabels: Record<string, string> = {
  queued: '排队中', running: '运行中', succeeded: '已完成', failed: '失败', cancelled: '已取消', interrupted: '已中断', blocked: '需确认', waiting_user: '等待处理', waiting_agent: '等待 Agent',
};

function when(value: string | null | undefined) {
  if (!value) return '等待开始';
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
}

function progressLabel(attempt: WorkflowAttempt | undefined) {
  const progress = attempt?.progress as { label?: string; tracks?: Array<{ name?: string; label?: string; completed?: number; total?: number; unit?: string }> } | undefined;
  const label = progress?.label || '';
  const details = progress?.tracks?.map((track) => {
    if (track.completed === undefined || track.total === undefined) return `${track.name || ''}${track.label || ''}`;
    return `${track.name || ''}${track.label || ''} · ${track.completed}/${track.total}${track.unit || ''}`;
  }).filter(Boolean) || [];
  return [label, ...details].filter(Boolean).join(' · ') || '等待进度信息';
}

function newestAttempt(workflow: Workflow | null) {
  return workflow?.steps.flatMap((step) => step.attempts).sort((a, b) => b.number - a.number)[0] || null;
}

export function TasksPage({ focusWorkflowId }: { focusWorkflowId: string }) {
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [selectedId, setSelectedId] = useState(focusWorkflowId);
  const [detail, setDetail] = useState<Workflow | null>(null);
  const [selectedAttemptId, setSelectedAttemptId] = useState('');
  const [log, setLog] = useState<WorkflowLog | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [listSequence, setListSequence] = useState(0);
  const [actionBusy, setActionBusy] = useState(false);
  const [agentInstruction, setAgentInstruction] = useState('');
  const [agentMessage, setAgentMessage] = useState('');
  const [agentBusy, setAgentBusy] = useState(false);
  const [reader, setReader] = useState<ReaderState | null>(null);
  const [readerMessage, setReaderMessage] = useState('');
  const [readerBusy, setReaderBusy] = useState(false);
  const detailSequence = useRef(0);
  const logOffset = useRef(0);
  const logInFlight = useRef(false);

  useEffect(() => {
    let alive = true;
    let busy = false;
    async function refreshReader() {
      if (busy) return;
      busy = true;
      try { const state = await getReaderState(); if (alive) setReader(state); }
      catch { /* the task list remains usable while reader status is unavailable */ }
      finally { busy = false; }
    }
    void refreshReader();
    const timer = window.setInterval(() => void refreshReader(), 1500);
    return () => { alive = false; window.clearInterval(timer); };
  }, []);

  async function showReader() {
    const tab = window.open('about:blank', '_blank');
    setReaderBusy(true);
    setReaderMessage('正在检查阅读页…');
    try {
      const result = await openReader();
      setReader(result);
      if (result.ready && result.url_path) {
        if (tab) tab.location.href = result.url_path;
        else window.location.assign(result.url_path);
        setReaderMessage('已打开当前阅读页。');
      } else {
        tab?.close();
        setReaderMessage('阅读页正在首次构建；完成后可再次打开。');
      }
    } catch (cause) {
      tab?.close();
      setReaderMessage(cause instanceof Error ? cause.message : '打开阅读页失败。');
    } finally { setReaderBusy(false); }
  }

  async function rebuildReader() {
    setReaderBusy(true);
    setReaderMessage('已提交阅读页更新；多个课次会合并构建。');
    try { setReader(await requestReaderBuild()); }
    catch (cause) { setReaderMessage(cause instanceof Error ? cause.message : '提交阅读页更新失败。'); }
    finally { setReaderBusy(false); }
  }

  useEffect(() => {
    let alive = true;
    let busy = false;
    async function refresh() {
      if (busy) return;
      busy = true;
      const sequence = listSequence + 1;
      try {
        const result = await getWorkflows();
        if (!alive) return;
        setWorkflows(result);
        setSelectedId((current) => current || focusWorkflowId || result[0]?.id || '');
        setError('');
      } catch (cause) {
        if (alive) setError(cause instanceof Error ? cause.message : '读取任务失败。');
      } finally {
        if (alive) { setLoading(false); setListSequence(sequence); }
        busy = false;
      }
    }
    void refresh();
    const timer = window.setInterval(() => void refresh(), 1200);
    return () => { alive = false; window.clearInterval(timer); };
  }, [focusWorkflowId]);

  useEffect(() => {
    if (!selectedId) { setDetail(null); return; }
    let alive = true;
    let busy = false;
    const sequence = ++detailSequence.current;
    async function refresh() {
      if (busy) return;
      busy = true;
      try {
        const result = await getWorkflow(selectedId);
        if (alive && sequence === detailSequence.current) {
          setDetail(result);
          const latest = newestAttempt(result);
          setSelectedAttemptId((current) => result.steps.some((step) => step.attempts.some((attempt) => attempt.id === current)) ? current : latest?.id || '');
        }
      } catch (cause) {
        if (alive && sequence === detailSequence.current) setError(cause instanceof Error ? cause.message : '读取任务详情失败。');
      } finally { busy = false; }
    }
    void refresh();
    const timer = window.setInterval(() => void refresh(), 1200);
    return () => { alive = false; window.clearInterval(timer); };
  }, [selectedId, listSequence]);

  useEffect(() => {
    if (!detail || detail.operation !== 'agent_note' || detail.state !== 'waiting_agent') {
      setAgentInstruction('');
      return;
    }
    let alive = true;
    void getAgentInstruction(detail.id).then((result) => {
      if (alive) setAgentInstruction(result.instruction);
    }).catch((cause) => {
      if (alive) setAgentMessage(cause instanceof Error ? cause.message : '读取 Agent 交接指令失败。');
    });
    return () => { alive = false; };
  }, [detail?.id, detail?.operation, detail?.state]);

  const attempts = detail?.steps.flatMap((step) => step.attempts) || [];
  const selectedAttempt = attempts.find((attempt) => attempt.id === selectedAttemptId) || null;
  async function runAction(action: 'retry' | 'cancel') {
    if (!detail || actionBusy) return;
    setActionBusy(true);
    setError('');
    try {
      if (action === 'retry') {
        await retryWorkflow(detail.id, crypto.randomUUID());
      } else {
        await cancelWorkflow(detail.id);
      }
      setListSequence((value) => value + 1);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '任务操作失败。');
    } finally {
      setActionBusy(false);
    }
  }
  async function verifyAgent() {
    if (!detail || agentBusy) return;
    setAgentBusy(true);
    try {
      const result = await verifyAgentOutput(detail.id);
      setAgentMessage(result.message);
      setListSequence((value) => value + 1);
    } catch (cause) {
      setAgentMessage(cause instanceof Error ? cause.message : '校验 Agent 成果失败。');
    } finally { setAgentBusy(false); }
  }
  async function copyAgentInstruction() {
    if (!agentInstruction) return;
    try {
      await navigator.clipboard.writeText(agentInstruction);
      setAgentMessage('交接指令已复制。Agent 的实际执行仍需由你完成。');
    } catch {
      setAgentMessage('无法自动复制；可以选中下方指令手动复制。');
    }
  }
  useEffect(() => {
    if (!detail || !selectedAttempt) { setLog(null); return; }
    const workflowId = detail.id;
    const attemptId = selectedAttempt.id;
    let alive = true;
    logOffset.current = 0;
    setLog({ offset: 0, next_offset: 0, size: 0, text: '', truncated: false });
    async function refreshLog() {
      if (logInFlight.current) return;
      logInFlight.current = true;
      try {
        const result = await getWorkflowLog(workflowId, attemptId, logOffset.current);
        if (!alive) return;
        logOffset.current = result.next_offset;
        setLog((current) => ({
          ...result,
          text: (current?.text || '') + result.text,
        }));
      } catch (cause) {
        if (alive) setError(cause instanceof Error ? cause.message : '读取任务日志失败。');
      } finally { logInFlight.current = false; }
    }
    void refreshLog();
    const timer = window.setInterval(() => void refreshLog(), 1200);
    return () => { alive = false; window.clearInterval(timer); };
  }, [detail?.id, selectedAttempt?.id]);

  return <section className="tasks-page" aria-label="任务中心">
    <div className="courses-heading"><div><p className="eyebrow">TASK CENTER</p><h1>任务中心</h1><p className="page-intro">查看明确课次、当前阶段和保留的执行历史。</p></div></div>
    {error && <div className="notice notice-error" role="alert">{error}</div>}
    <section className="task-progress-card reader-update-card" aria-label="课程阅读页"><div className="task-history-heading"><div><h3>课程阅读页</h3><p>{reader?.state === 'building' || reader?.state === 'queued' ? '新成果正在合并生成阅读页；现有阅读页仍可打开。' : reader?.state === 'failed' ? `上次更新失败：${reader.last_error}` : reader?.state === 'ready' ? `已更新到第 ${reader.published_generation} 批成果。` : '完成字幕、课件或笔记后，可自动更新阅读页。'}</p></div><div className="task-actions"><button className="button button-secondary button-compact" onClick={() => void showReader()} disabled={readerBusy}>{readerBusy ? '处理中…' : '打开阅读页'}</button><button className="button button-secondary button-compact" onClick={() => void rebuildReader()} disabled={readerBusy || reader?.state === 'queued' || reader?.state === 'building'}>更新阅读页</button></div></div>{readerMessage && <p role="status">{readerMessage}</p>}</section>
    <div className="task-workspace">
      <section className="task-list-panel" aria-label="任务列表">
        <div className="course-list-heading"><strong>任务</strong><span>{workflows.length}</span></div>
        {loading && <p className="list-hint" role="status">正在读取任务…</p>}
        {!loading && workflows.length === 0 && <div className="detail-empty compact"><h3>还没有采集任务</h3><p>从课程页选择一个明确课次，开始只取字幕或只取课件。</p></div>}
        <ul className="task-list">{workflows.map((workflow) => <li key={workflow.id}>
          <button className={`task-choice ${workflow.id === selectedId ? 'selected' : ''}`} onClick={() => setSelectedId(workflow.id)} aria-pressed={workflow.id === selectedId}>
            <span className="task-choice-top"><strong>{String(workflow.target.operation_label || workflow.operation)}</strong><span className={`status-pill status-${workflow.state}`}>{stateLabels[workflow.state] || workflow.state}</span></span>
            <b>{String(workflow.target.course_title || '未知课程')}</b><span>{String(workflow.target.lecture_title || `课次 ID ${workflow.lecture_id}`)}</span><small>{when(workflow.created_at)}</small>
          </button>
        </li>)}</ul>
      </section>
      <section className="task-detail-panel" aria-label="任务详情">
        {!selectedId && <div className="detail-empty"><span className="detail-mark">02</span><h2>等待任务提交</h2><p>任务会冻结提交时的课程、课次和设置摘要，之后课程名称变化也不会改写历史。</p></div>}
        {detail && <>
          <div className="task-detail-heading"><div><p className="eyebrow">WORKFLOW</p><h2>{String(detail.target.operation_label || detail.operation)} · {String(detail.target.lecture_title || `课次 ID ${detail.lecture_id}`)}</h2><p>{String(detail.target.course_title || '未知课程')}{detail.target.class_date ? ` · ${String(detail.target.class_date)}` : ''}</p></div><div className="task-actions"><span className={`status-pill status-${detail.state}`}>{stateLabels[detail.state] || detail.state}</span>
            {(detail.state === 'failed' || detail.state === 'cancelled' || detail.state === 'interrupted') && <button className="button button-secondary button-compact" disabled={actionBusy} onClick={() => void runAction('retry')}>{actionBusy ? '正在恢复…' : '恢复任务'}</button>}
            {(detail.state === 'queued' || detail.state === 'running') && <button className="button button-secondary button-compact" disabled={actionBusy || detail.cancel_requested} onClick={() => void runAction('cancel')}>{detail.cancel_requested ? '正在停止…' : '取消任务'}</button>}
          </div></div>
          <div className="task-target-line"><span>课次身份</span><strong>{String(detail.target.platform_sub_id || detail.lecture_id)}</strong><span>提交时间</span><strong>{when(detail.created_at)}</strong></div>
          {detail.state === 'succeeded' && <div className="task-actions task-reader-action"><button className="button button-secondary button-compact" onClick={() => void showReader()} disabled={readerBusy}>阅读课程资料</button><span>{reader?.state === 'building' || reader?.state === 'queued' ? '本站仍在合并本次成果。' : reader?.state === 'failed' ? '阅读页更新失败，可在上方重试；旧页面仍保留。' : ''}</span></div>}
          <section className="task-progress-card"><h3>执行进度</h3>{detail.steps.map((step) => <div className="task-step" key={step.id}><span className={`step-indicator step-${step.state}`} />
            <div><strong>{step.name === 'transcribe' ? '只取字幕' : step.name === 'slide' ? '只取课件' : step.name === 'agent_note' ? '外部 Agent 写作' : step.name}</strong><p>{step.name === 'agent_note' ? '交接指令不会被视为执行完成。' : progressLabel(step.attempts[step.attempts.length - 1])}</p>
              {step.error && typeof step.error.message === 'string' && step.error.message && <p className="task-error">{step.error.message}</p>}
            </div><span className={`status-pill status-${step.state}`}>{stateLabels[step.state] || step.state}</span>
          </div>)}</section>
          {detail.operation === 'agent_note' && detail.state === 'waiting_agent' && <section className="agent-handoff" aria-label="外部 Agent 交接">
            <h3>等待外部 Agent</h3>
            <p>复制下面指令并交给你使用的 Agent。保存或复制指令不会标记完成；Agent 执行由你控制。</p>
            {(detail.steps.find((step) => step.name === 'agent_note')?.artifact.warnings as string[] | undefined)?.map((warning) => <p className="task-error" key={warning}>{warning}</p>)}
            <label htmlFor="agent-instruction">交接指令</label>
            <textarea id="agent-instruction" readOnly value={agentInstruction} rows={10} placeholder="正在读取交接指令…" />
            <div className="task-actions">
              <button className="button button-secondary button-compact" onClick={() => void copyAgentInstruction()} disabled={!agentInstruction}>复制指令</button>
              <button className="button button-primary button-compact" onClick={() => void verifyAgent()} disabled={agentBusy}>{agentBusy ? '正在检查…' : '检查 Agent 成果'}</button>
            </div>
            {agentMessage && <p role="status">{agentMessage}</p>}
          </section>}
          <section className="task-history"><div className="task-history-heading"><h3>执行记录</h3>{attempts.length > 0 && <label><span className="sr-only">选择执行记录</span><select value={selectedAttemptId} onChange={(event) => setSelectedAttemptId(event.target.value)}>{attempts.slice().reverse().map((attempt) => <option key={attempt.id} value={attempt.id}>第 {attempt.number} 次 · {stateLabels[attempt.state] || attempt.state}</option>)}</select></label>}</div>
            {!selectedAttempt && <p className="list-hint">此任务还没有执行记录。</p>}
            {selectedAttempt && <><p className="attempt-meta">开始 {when(selectedAttempt.started_at)}{selectedAttempt.ended_at ? ` · 结束 ${when(selectedAttempt.ended_at)}` : ''}{selectedAttempt.exit_code !== null ? ` · 退出码 ${selectedAttempt.exit_code}` : ''}</p>
              {typeof selectedAttempt.error?.message === 'string' && selectedAttempt.error.message && <p className="task-error">{selectedAttempt.error.message}</p>}
              <pre className="workflow-log" aria-label="脱敏任务日志">{log?.text || '暂无日志输出。'}</pre>
              {log?.truncated && <p className="attempt-meta">日志较长，当前显示从第 {log.offset} 字节开始的片段。</p>}
            </>}
          </section>
        </>}
      </section>
    </div>
  </section>;
}
