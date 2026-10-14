import type { ReactNode } from 'react';
import type { SettingsDraft, SettingsGroup } from '../types/settings';
import type { MigrationPreview } from '../api/client';

interface SettingsFormProps {
  draft: SettingsDraft;
  onChange: (group: SettingsGroup, field: string, value: string | number | boolean) => void;
  onSave: () => void;
  saving: boolean;
  dirty: boolean;
  error: string;
  success: string;
  onTestTextModel: () => void;
  onTestVisionModel: () => void;
  textTest: string;
  visionTest: string;
  canTestTextModel: boolean;
  canTestVisionModel: boolean;
  testingTextModel: boolean;
  testingVisionModel: boolean;
  migrationPreview: MigrationPreview | null;
  migrationBusy: boolean;
  migrationMessage: string;
  onPreviewMigration: () => void;
  onApplyMigration: () => void;
}

const sections = [
  ['account', '账号与保存'], ['asr', '字幕识别'], ['note', '笔记与 Agent'],
  ['text-model', '文本模型'], ['vision-model', '视觉模型'], ['slides', '课件处理'],
  ['tasks', '任务执行'], ['reader', '阅读与发布'], ['migration', '资料迁移'],
] as const;

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return <label className="field"><span className="field-label">{label}</span>{children}{hint && <span className="field-hint">{hint}</span>}</label>;
}

function TextInput({ id, value, onChange, type = 'text', placeholder }: {
  id: string; value: string; onChange: (value: string) => void; type?: string; placeholder?: string;
}) {
  return <input id={id} type={type} value={value} placeholder={placeholder} onChange={(event) => onChange(event.target.value)} />;
}

function SelectInput({ id, value, options, onChange, disabled = false, disabledOptions = [] }: {
  id: string; value: string; options: Array<[string, string]>; onChange: (value: string) => void; disabled?: boolean; disabledOptions?: string[];
}) {
  return <select id={id} value={value} disabled={disabled} onChange={(event) => onChange(event.target.value)}>
    {options.map(([option, label]) => <option value={option} key={option} disabled={disabledOptions.includes(option)}>{label}</option>)}
  </select>;
}

export function SettingsForm({ draft, onChange, onSave, saving, dirty, error, success,
  onTestTextModel, onTestVisionModel, textTest, visionTest, canTestTextModel, canTestVisionModel,
  testingTextModel, testingVisionModel, migrationPreview, migrationBusy, migrationMessage,
  onPreviewMigration, onApplyMigration }: SettingsFormProps) {
  const set = (group: SettingsGroup, field: string) => (value: string | number | boolean) => onChange(group, field, value);
  const textModelReady = draft.text_model.configured
    || Boolean(draft.text_model.model.trim() && draft.text_model.base_url.trim()
      && (draft.text_model.provider === 'ollama' || draft.text_model.key_configured || draft.text_model.api_key.trim()));
  return <>
    <div className="settings-toolbar">
      <div>
        <p className="eyebrow">PREFERENCES</p>
        <h1>设置</h1>
        <p className="page-intro">账号、模型和任务行为集中在这里管理。</p>
      </div>
      <button className="button button-primary" type="button" onClick={onSave} disabled={saving || !dirty}>
        {saving ? '保存中…' : '保存更改'}
      </button>
    </div>
    {error && <div className="notice notice-error" role="alert">{error}</div>}
    {success && <div className="notice notice-success" role="status">{success}</div>}
    <div className="settings-layout">
      <nav className="settings-nav" aria-label="设置分组">
        {sections.map(([id, label], index) => <a href={`#settings-${id}`} key={id}>
          <span className="nav-index">{String(index + 1).padStart(2, '0')}</span>{label}
        </a>)}
      </nav>
      <div className="settings-groups">
        <section className="settings-card" id="settings-account" aria-labelledby="heading-account">
          <header className="card-heading"><span className="section-number">01</span><div><h2 id="heading-account">账号与保存</h2><p>账号密码不会回显；新密码留空时保留原设置。</p></div></header>
          <div className="field-grid">
            <Field label="同济账号" hint={draft.account.username_set ? '账号已保存' : '尚未保存账号'}>
              <TextInput id="tongji-username" value={draft.account.username} onChange={set('account', 'username')} />
            </Field>
            <Field label="账号密码" hint={draft.account.password_set ? '当前已配置' : '当前未配置'}>
              <TextInput id="tongji-password" type="password" value={draft.account.password} placeholder="输入新密码" onChange={set('account', 'password')} />
            </Field>
            <Field label="资料保存目录" hint="已有课程资料不会因修改路径而移动。">
              <TextInput id="workspace-root" value={draft.account.workspace_root} onChange={set('account', 'workspace_root')} />
            </Field>
            <Field label="知识库署名"><TextInput id="owner-name" value={draft.account.owner_name} onChange={set('account', 'owner_name')} /></Field>
            <Field label="阅读页名称"><TextInput id="site-name" value={draft.account.site_name} onChange={set('account', 'site_name')} /></Field>
          </div>
        </section>

        <section className="settings-card" id="settings-asr" aria-labelledby="heading-asr">
          <header className="card-heading"><span className="section-number">02</span><div><h2 id="heading-asr">字幕识别</h2><p>BcutASR 为当前默认服务，音频会上传到云端；本地 Whisper 尚未完成集成验证。</p></div></header>
          <div className="field-grid">
            <Field label="识别服务"><SelectInput id="asr-provider" value={draft.asr.provider} disabled options={ [['bcutasr', 'BcutASR']] } onChange={set('asr', 'provider')} /></Field>
            <div className="status-field"><span className="field-label">服务状态</span><span className="status-pill status-neutral">尚未检测可用性</span></div>
            <div className="status-field"><span className="field-label">音频上传目标</span><span>{draft.asr.upload_target}</span></div>
          </div>
        </section>

        <section className="settings-card" id="settings-note" aria-labelledby="heading-note">
          <header className="card-heading"><span className="section-number">03</span><div><h2 id="heading-note">笔记与 Agent</h2><p>默认沿用当前 Agent 工作流；切换模型前请先配置文本模型。</p></div></header>
          <div className="field-grid">
            <Field label="笔记生成路线"><SelectInput id="note-route" value={draft.note.route} options={ [['external_agent', '外部 Agent（默认）'], ['model', textModelReady ? '文本模型' : '文本模型（未配置）']] } disabledOptions={textModelReady ? [] : ['model']} onChange={set('note', 'route')} /></Field>
            <Field label="笔记风格"><SelectInput id="note-style" value={draft.note.style} options={ [['standard', '标准'], ['dialogue', '问答对话']] } onChange={set('note', 'style')} /></Field>
            <label className="check-field"><input type="checkbox" checked={draft.note.include_timeline} onChange={(event) => set('note', 'include_timeline')(event.target.checked)} /><span>生成时间轴</span></label>
          </div>
          {!textModelReady && draft.note.route === 'model' && <a className="inline-link" href="#settings-text-model">文本模型尚未配置，前往文本模型设置 →</a>}
        </section>

        <section className="settings-card" id="settings-text-model" aria-labelledby="heading-text-model">
          <header className="card-heading"><span className="section-number">04</span><div><h2 id="heading-text-model">文本模型</h2><p>密钥只存本机；连接测试仅查询模型列表，不发送生成请求。</p></div><div className="card-actions"><span className={`status-pill ${draft.text_model.configured ? 'status-good' : 'status-neutral'}`}>{draft.text_model.configured ? '已配置' : '未配置'}</span><button className="button button-secondary button-compact" type="button" disabled={!canTestTextModel || testingTextModel} onClick={onTestTextModel}>{testingTextModel ? '测试中…' : '测试文本连接'}</button></div></header>
          <div className="field-grid">
            <Field label="平台"><SelectInput id="text-provider" value={draft.text_model.provider} options={ [['openai-compatible', 'OpenAI 兼容接口'], ['ollama', 'Ollama 本地服务']] } onChange={set('text_model', 'provider')} /></Field>
            <Field label="模型名称"><TextInput id="text-model" value={draft.text_model.model} placeholder="例如 gpt-4o-mini" onChange={set('text_model', 'model')} /></Field>
            <Field label="服务地址"><TextInput id="text-base-url" value={draft.text_model.base_url} placeholder="https://…/v1" onChange={set('text_model', 'base_url')} /></Field>
            <Field label="API Key" hint={draft.text_model.key_configured ? '已有密钥；留空会保留。' : '密钥只在本次输入期间保留在页面内存。'}>
              <TextInput id="text-api-key" type="password" value={draft.text_model.api_key} placeholder={draft.text_model.key_configured ? '已配置，输入新密钥以替换' : '输入 API Key'} onChange={set('text_model', 'api_key')} />
            </Field>
            {draft.text_model.key_configured && <label className="check-field"><input type="checkbox" checked={draft.text_model.clear_api_key} onChange={(event) => set('text_model', 'clear_api_key')(event.target.checked)} /><span>清除已保存的密钥</span></label>}
          </div>
          {textTest && <p className="test-result" role="status">{textTest}</p>}
        </section>

        <section className="settings-card" id="settings-vision-model" aria-labelledby="heading-vision-model">
          <header className="card-heading"><span className="section-number">05</span><div><h2 id="heading-vision-model">视觉模型</h2><p>测试会发送所选截图给模型服务，可能产生费用；与文本测试独立。</p></div><div className="card-actions"><span className={`status-pill ${draft.vision_model.configured ? 'status-good' : 'status-neutral'}`}>{draft.vision_model.configured ? '已配置' : '未配置'}</span><button className="button button-secondary button-compact" type="button" disabled={!canTestVisionModel || testingVisionModel} onClick={onTestVisionModel}>{testingVisionModel ? '等待选择…' : '选择图片并测试'}</button></div></header>
          <div className="field-grid">
            <Field label="平台"><SelectInput id="vision-provider" value={draft.vision_model.provider} options={draft.vision_model.providers.map((provider) => [provider.id, provider.name])} onChange={set('vision_model', 'provider')} /></Field>
            <Field label="模型名称"><TextInput id="vision-model" value={draft.vision_model.model} placeholder="输入支持图片的模型" onChange={set('vision_model', 'model')} /></Field>
            <Field label="服务地址"><TextInput id="vision-base-url" value={draft.vision_model.base_url} placeholder="https://…" onChange={set('vision_model', 'base_url')} /></Field>
            <Field label="API Key" hint={draft.vision_model.key_configured ? '已有密钥；留空会保留。' : '本地 Ollama / LM Studio 可不填。'}>
              <TextInput id="vision-api-key" type="password" value={draft.vision_model.api_key} placeholder={draft.vision_model.key_configured ? '已配置，输入新密钥以替换' : '输入 API Key'} onChange={set('vision_model', 'api_key')} />
            </Field>
          </div>
          {visionTest && <p className="test-result" role="status">{visionTest}</p>}
        </section>

        <section className="settings-card" id="settings-slides" aria-labelledby="heading-slides">
          <header className="card-heading"><span className="section-number">06</span><div><h2 id="heading-slides">课件处理</h2><p>仅影响新建任务；当前合法范围为并发 1–16、重试 0–10、超时 5–300 秒。</p></div></header>
          <div className="field-grid field-grid-three">
            <Field label="课件并发"><TextInput id="slides-concurrency" type="number" value={String(draft.slides.concurrency)} onChange={(value) => set('slides', 'concurrency')(Number(value))} /></Field>
            <Field label="重试次数"><TextInput id="slides-retries" type="number" value={String(draft.slides.retries)} onChange={(value) => set('slides', 'retries')(Number(value))} /></Field>
            <Field label="单次超时（秒）"><TextInput id="slides-timeout" type="number" value={String(draft.slides.timeout_seconds)} onChange={(value) => set('slides', 'timeout_seconds')(Number(value))} /></Field>
            <label className="check-field"><input type="checkbox" checked={draft.slides.allow_without_screenshots} onChange={(event) => set('slides', 'allow_without_screenshots')(event.target.checked)} /><span>没有截图时继续处理</span></label>
          </div>
        </section>

        <section className="settings-card" id="settings-tasks" aria-labelledby="heading-tasks">
          <header className="card-heading"><span className="section-number">07</span><div><h2 id="heading-tasks">任务执行</h2><p>字幕识别并发固定为 1；不同课次可安全并行。</p></div></header>
          <div className="field-grid">
            <Field label="不同课次并发"><TextInput id="course-parallelism" type="number" value={String(draft.tasks.course_parallelism)} onChange={(value) => set('tasks', 'course_parallelism')(Number(value))} /></Field>
            <Field label="失败自动重试"><TextInput id="retry-limit" type="number" value={String(draft.tasks.retry_limit)} onChange={(value) => set('tasks', 'retry_limit')(Number(value))} /></Field>
            <div className="status-field"><span className="field-label">字幕识别并发</span><span>1（固定）</span></div>
          </div>
        </section>

        <section className="settings-card" id="settings-reader" aria-labelledby="heading-reader">
          <header className="card-heading"><span className="section-number">08</span><div><h2 id="heading-reader">阅读与发布</h2><p>完成后自动更新本地阅读页；发布仍由用户明确触发。</p></div></header>
          <div className="field-grid">
            <label className="check-field"><input type="checkbox" checked={draft.reader.auto_update} onChange={(event) => set('reader', 'auto_update')(event.target.checked)} /><span>任务完成后自动更新阅读页</span></label>
            <Field label="发布仓库" hint="格式：owner/repository。公开发布保持显式操作。"><TextInput id="publish-repository" value={draft.reader.publish_repository} placeholder="owner/repository" onChange={set('reader', 'publish_repository')} /></Field>
          </div>
        </section>
        <section className="settings-card" id="settings-migration" aria-labelledby="heading-migration">
          <header className="card-heading"><span className="section-number">09</span><div><h2 id="heading-migration">资料迁移</h2><p>先只读扫描；应用前会备份数据库、配置和扫描到的索引文件。原始资料保持原位。</p></div></header>
          <div className="field-grid">
            <div className="task-actions">
              <button className="button button-secondary" type="button" onClick={onPreviewMigration} disabled={migrationBusy}>{migrationBusy ? '处理中…' : '生成只读预览'}</button>
              <button className="button button-primary" type="button" onClick={onApplyMigration} disabled={migrationBusy || !migrationPreview}>{migrationBusy ? '处理中…' : '备份并导入索引'}</button>
            </div>
            {migrationMessage && <p role="status">{migrationMessage}</p>}
            {migrationPreview && <>
              <p>预览包含 {migrationPreview.course_count} 门课程、{migrationPreview.lecture_count} 个课次。</p>
              {migrationPreview.mappings.length > 0 && <details className="notes-preview"><summary>查看课程与课次映射</summary><ul>{migrationPreview.mappings.slice(0, 100).map((course) => <li key={`${course.identity_kind}-${course.course_identity}`}><strong>{course.title}</strong><ul>{course.lectures.map((lecture) => <li key={`${lecture.identity_kind}-${lecture.identity}`}>{lecture.class_date ? `${lecture.class_date} · ` : ''}{lecture.title}</li>)}</ul></li>)}</ul>{migrationPreview.mappings.length > 100 && <p>仅显示前 100 门课程。</p>}</details>}
              {migrationPreview.conflicts.length > 0 && <div className="notice notice-error"><strong>有 {migrationPreview.conflicts.length} 项资料需要人工核对</strong><ul>{migrationPreview.conflicts.map((item, index) => <li key={`${item.kind}-${index}`}>{item.reason}</li>)}</ul><p>这些项目会保留待核对，不按标题合并。</p></div>}
              {migrationPreview.legacy_batches.length > 0 && <p>发现 {migrationPreview.legacy_batches.length} 条旧批次记录，包含 {migrationPreview.legacy_batches.reduce((count, batch) => count + batch.lectures.length, 0)} 个课次状态；原记录会随迁移记录保留。</p>}
              {migrationPreview.legacy_batches.some((batch) => batch.lectures.some((lecture) => lecture.needs_review)) && <p>旧批次中有未知状态，会保留为待核对记录，不会推断为已完成。</p>}
            </>}
          </div>
        </section>
        <div className="bottom-save"><span>{dirty ? '有尚未保存的更改' : '所有设置已保存'}</span><button className="button button-primary" type="button" onClick={onSave} disabled={saving || !dirty}>{saving ? '保存中…' : '保存更改'}</button></div>
      </div>
    </div>
  </>;
}
