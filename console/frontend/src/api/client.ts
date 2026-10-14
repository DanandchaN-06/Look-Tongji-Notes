import type { SettingsData, SettingsGroup, SettingsDraft } from '../types/settings';

let launchToken = '';

export function readLaunchToken(): boolean {
  const params = new URLSearchParams(window.location.search);
  launchToken = params.get('token') ?? '';
  if (launchToken) {
    window.history.replaceState(null, '', window.location.pathname + window.location.hash);
  }
  // /v2/ is served only after launch-token or HttpOnly-cookie validation.
  // After refresh the cookie authenticates same-origin requests without JS persistence.
  return Boolean(launchToken) || ['/v2', '/v2/'].includes(window.location.pathname);
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (launchToken) headers.set('X-Console-Token', launchToken);
  if (init.body) headers.set('Content-Type', 'application/json');
  const response = await fetch(path, { ...init, headers, credentials: 'same-origin' });
  const payload = await response.json() as { ok: boolean; data?: T; error?: { message?: string } };
  if (!response.ok || !payload.ok || payload.data === undefined) {
    throw new Error(payload.error?.message || `请求失败（${response.status}）`);
  }
  return payload.data;
}

export function getSettings(): Promise<SettingsData> {
  return request<SettingsData>('/api/v2/settings');
}

export type Course = { id: string; platform_course_id: string | null; title: string; teacher: string | null; term: string | null; source: string; identity_kind: string; lecture_count: number };
export type Lecture = { id: string; course_id: string; platform_sub_id: string | null; title: string; class_date: string | null; playback_available: boolean };
export type CourseList = { source: string; query: string; courses: Course[]; sync_status: { state: string; last_success_at: string | null; error: { message?: string } } | null };
export type LectureList = { course: Course; lectures: Lecture[] };
export type ArtifactItem = { kind: string; label: string; state: 'not_collected' | 'available' | 'expired' | 'not_provided'; message: string; name?: string };
export type LectureArtifacts = { lecture_id: string; course_id: string; items: ArtifactItem[]; notes_preview: string; source: string };
export type WorkflowAttempt = { id: string; step_id: string; number: number; state: string; started_at: string | null; ended_at: string | null; exit_code: number | null; progress: Record<string, unknown>; error_code: string | null; error: Record<string, unknown>; log_path?: string | null };
export type WorkflowStep = { id: string; workflow_id: string; name: string; ordinal: number; state: string; artifact: Record<string, unknown>; error_code: string | null; error: Record<string, unknown>; attempts: WorkflowAttempt[] };
export type Workflow = { id: string; lecture_id: string; operation: string; state: string; settings_revision_id: string | null; target: Record<string, unknown> & { course_title?: string; lecture_title?: string; operation_label?: string; class_date?: string | null }; created_at: string; updated_at: string; cancel_requested: boolean; steps: WorkflowStep[] };
export type WorkflowLog = { offset: number; next_offset: number; size: number; text: string; truncated: boolean };
export type ReaderState = { requested_generation: number; published_generation: number; state: string; last_error: string; site_url: string };
export type MigrationPreview = {
  preview_id: string; course_count: number; lecture_count: number; conflicts: Array<{ kind: string; identity: string; reason: string }>;
  mappings: Array<{ identity_kind: string; course_identity: string; title: string; lectures: Array<{ identity_kind: string; identity: string; title: string; class_date: string | null }> }>;
  legacy_batches: Array<{ path: string; course_id: string; lectures: Array<{ sub_id: string; status: string; needs_review?: boolean }> }>;
};

export function getCourses(source: string, query: string): Promise<CourseList> {
  const params = new URLSearchParams({ source, q: query });
  return request(`/api/v2/courses?${params.toString()}`);
}

export function syncCourses(): Promise<{ sync_job: { course_count: number; lecture_count: number }; cache: CourseList['sync_status'] }> {
  return request('/api/v2/courses/sync', { method: 'POST', body: '{}' });
}

export function getLectures(courseId: string): Promise<LectureList> {
  return request(`/api/v2/courses/${encodeURIComponent(courseId)}/lectures`);
}

export function getLectureArtifacts(courseId: string, lectureId: string): Promise<LectureArtifacts> {
  return request(`/api/v2/courses/${encodeURIComponent(courseId)}/lectures/${encodeURIComponent(lectureId)}/artifacts`);
}

export function getWorkflows(): Promise<Workflow[]> {
  return request('/api/v2/workflows');
}

export function getWorkflow(workflowId: string): Promise<Workflow> {
  return request(`/api/v2/workflows/${encodeURIComponent(workflowId)}`);
}

export function createWorkflow(lectureId: string, operation: 'transcribe' | 'slide' | 'agent_note', key: string): Promise<Workflow> {
  return request('/api/v2/workflows', {
    method: 'POST',
    headers: { 'Idempotency-Key': key },
    body: JSON.stringify({ lecture_id: lectureId, operation }),
  });
}

export function getAgentInstruction(workflowId: string): Promise<{ instruction: string }> {
  return request(`/api/v2/workflows/${encodeURIComponent(workflowId)}/agent-instruction`);
}

export function verifyAgentOutput(workflowId: string): Promise<{ ready: boolean; message: string; workflow: Workflow }> {
  return request(`/api/v2/workflows/${encodeURIComponent(workflowId)}/verify-agent`, { method: 'POST', body: '{}' });
}

export function retryWorkflow(workflowId: string, key: string): Promise<Workflow> {
  return request(`/api/v2/workflows/${encodeURIComponent(workflowId)}/retry`, {
    method: 'POST', headers: { 'Idempotency-Key': key }, body: '{}',
  });
}

export function cancelWorkflow(workflowId: string): Promise<Workflow> {
  return request(`/api/v2/workflows/${encodeURIComponent(workflowId)}/cancel`, { method: 'POST', body: '{}' });
}

export function getWorkflowLog(workflowId: string, attemptId: string, offset: number): Promise<WorkflowLog> {
  const params = new URLSearchParams({ offset: String(offset) });
  return request(`/api/v2/workflows/${encodeURIComponent(workflowId)}/attempts/${encodeURIComponent(attemptId)}/log?${params}`);
}

export function getReaderState(): Promise<ReaderState> {
  return request('/api/v2/reader');
}

export function requestReaderBuild(): Promise<ReaderState> {
  return request('/api/v2/reader/build', { method: 'POST', body: '{}' });
}

export function openReader(): Promise<ReaderState & { ready: boolean; url_path: string }> {
  return request('/api/v2/reader/open', { method: 'POST', body: '{}' });
}

export function previewMigration(): Promise<MigrationPreview> {
  return request('/api/v2/migrations/preview', { method: 'POST', body: '{}' });
}

export function applyMigration(previewId: string): Promise<{ preview_id: string; state: string; backup_created: boolean }> {
  return request('/api/v2/migrations/apply', { method: 'POST', body: JSON.stringify({ preview_id: previewId }) });
}

export function testTextModel(): Promise<{ success: boolean; message: string }> {
  return request('/api/v2/settings/test/text_model', { method: 'POST', body: '{}' });
}

async function requestLegacy<T>(path: string, body: Record<string, unknown>): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (launchToken) headers['X-Console-Token'] = launchToken;
  const response = await fetch(path, {
    method: 'POST',
    headers,
    credentials: 'same-origin',
    body: JSON.stringify(body),
  });
  const payload = await response.json() as { ok: boolean; error?: string; returncode?: number; paths?: string[]; cancelled?: boolean };
  if (!response.ok || !payload.ok) {
    throw new Error(payload.error || `请求失败（${response.status}）`);
  }
  return payload as T;
}

export async function pickAndTestVisionModel(): Promise<string> {
  const selection = await requestLegacy<{ paths: string[]; cancelled: boolean }>('/api/pick', {
    mode: 'files', title: '选择一张课件截图进行视觉模型测试',
  });
  const image = selection.paths?.[0];
  if (selection.cancelled || !image) return '已取消图片选择。';
  const result = await requestLegacy<{ ok: boolean; returncode?: number }>('/api/vision/test', { image });
  return result.ok ? '视觉模型测试成功。' : `视觉模型测试失败（退出码 ${result.returncode ?? '未知'}）。`;
}

export async function saveSettings(
  draft: SettingsDraft,
  dirtyGroups: Set<SettingsGroup>,
): Promise<{ revision_id: string; settings: SettingsData }> {
  const body: Partial<SettingsDraft> = {};
  for (const group of dirtyGroups) {
    const value = { ...draft[group] } as Record<string, unknown>;
    if (group === 'text_model' && !value.api_key) delete value.api_key;
    if (group === 'vision_model' && !value.api_key) delete value.api_key;
    if (group === 'account' && !value.password) delete value.password;
    if (group === 'text_model') delete value.configured, delete value.key_configured, delete value.providers;
    if (group === 'vision_model') delete value.configured, delete value.key_configured, delete value.providers;
    if (group === 'account') {
      delete value.username_set;
      delete value.password_set;
      if (!value.workspace_root) {
        delete value.workspace_root;
        delete value.owner_name;
        delete value.site_name;
      }
    }
    body[group] = value as never;
  }
  return request('/api/v2/settings', { method: 'POST', body: JSON.stringify(body) });
}
