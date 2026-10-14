import { useEffect, useState } from 'react';
import { applyMigration, getSettings, pickAndTestVisionModel, previewMigration, saveSettings, testTextModel } from '../api/client';
import type { MigrationPreview } from '../api/client';
import { SettingsForm } from '../components/SettingsForm';
import type { SettingsData, SettingsDraft, SettingsGroup } from '../types/settings';

function toDraft(settings: SettingsData): SettingsDraft {
  return {
    ...settings,
    account: { ...settings.account, password: '' },
    text_model: { ...settings.text_model, api_key: '', clear_api_key: false },
    vision_model: { ...settings.vision_model, api_key: '' },
  };
}

export function SettingsPage() {
  const [draft, setDraft] = useState<SettingsDraft | null>(null);
  const [dirtyGroups, setDirtyGroups] = useState<Set<SettingsGroup>>(() => new Set());
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [textTest, setTextTest] = useState('');
  const [visionTest, setVisionTest] = useState('');
  const [testingTextModel, setTestingTextModel] = useState(false);
  const [testingVisionModel, setTestingVisionModel] = useState(false);
  const [migrationPreview, setMigrationPreview] = useState<MigrationPreview | null>(null);
  const [migrationBusy, setMigrationBusy] = useState(false);
  const [migrationMessage, setMigrationMessage] = useState('');

  async function load() {
    setLoading(true);
    setError('');
    try {
      const settings = await getSettings();
      setDraft(toDraft(settings));
      setDirtyGroups(new Set());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '读取设置失败。');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void load(); }, []);

  function change(group: SettingsGroup, field: string, value: string | number | boolean) {
    setDraft((current) => current ? {
      ...current,
      [group]: { ...current[group], [field]: value },
    } as SettingsDraft : current);
    setDirtyGroups((current) => new Set(current).add(group));
    setSuccess('');
    setError('');
    if (group === 'text_model') setTextTest('');
    if (group === 'vision_model') setVisionTest('');
  }

  async function save() {
    if (!draft || dirtyGroups.size === 0) return;
    setSaving(true);
    setError('');
    setSuccess('');
    try {
      const result = await saveSettings(draft, dirtyGroups);
      setDraft(toDraft(result.settings));
      setDirtyGroups(new Set());
      setSuccess('设置已保存，新任务将使用此版本。');
      if (dirtyGroups.has('text_model')) setTextTest('');
      if (dirtyGroups.has('vision_model')) setVisionTest('');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '保存设置失败。');
    } finally {
      setSaving(false);
    }
  }

  async function runTextModelTest() {
    if (dirtyGroups.has('text_model')) {
      setTextTest('请先保存文本模型更改，再运行连接测试。');
      return;
    }
    if (!window.confirm('将向已配置的模型服务请求模型列表；不会发送生成请求。服务商仍可能按其规则计费。继续吗？')) return;
    setTestingTextModel(true);
    setTextTest('正在检查文本模型连接…');
    try {
      const result = await testTextModel();
      setTextTest(result.message);
    } catch (cause) {
      setTextTest(cause instanceof Error ? cause.message : '文本模型连接测试失败。');
    } finally {
      setTestingTextModel(false);
    }
  }

  async function runVisionModelTest() {
    if (dirtyGroups.has('vision_model')) {
      setVisionTest('请先保存视觉模型更改，再运行识图测试。');
      return;
    }
    if (!window.confirm('将打开系统图片选择框，并把选中的截图发送给视觉模型服务；可能产生费用。只有在确认图片可上传时继续。')) return;
    setTestingVisionModel(true);
    setVisionTest('正在等待选择图片…');
    try {
      setVisionTest(await pickAndTestVisionModel());
    } catch (cause) {
      setVisionTest(cause instanceof Error ? cause.message : '视觉模型测试失败。');
    } finally {
      setTestingVisionModel(false);
    }
  }

  async function runMigrationPreview() {
    setMigrationBusy(true);
    setMigrationMessage('正在只读扫描资料目录…');
    try {
      const result = await previewMigration();
      setMigrationPreview(result);
      setMigrationMessage(`预览完成：${result.course_count} 门课程、${result.lecture_count} 个课次；发现 ${result.conflicts.length} 项需要核对。`);
    } catch (cause) {
      setMigrationMessage(cause instanceof Error ? cause.message : '迁移预览失败。');
    } finally {
      setMigrationBusy(false);
    }
  }

  async function applyMigrationPreview() {
    if (!migrationPreview || migrationBusy) return;
    const { course_count, lecture_count, conflicts } = migrationPreview;
    if (!window.confirm(`将先备份数据库、配置及预览中的资料清单，再导入 ${course_count} 门课程和 ${lecture_count} 个课次索引。${conflicts.length ? ` ${conflicts.length} 项冲突会保留待核对。` : ''}原始课程文件不会移动或删除。继续吗？`)) return;
    setMigrationBusy(true);
    setMigrationMessage('正在创建备份并导入索引…');
    try {
      const result = await applyMigration(migrationPreview.preview_id);
      setMigrationMessage(result.backup_created ? '导入完成，备份已创建。课程页刷新后可查看索引。' : '迁移已完成。');
      setMigrationPreview(null);
    } catch (cause) {
      setMigrationMessage(cause instanceof Error ? cause.message : '迁移未完成；请重新生成预览。');
    } finally {
      setMigrationBusy(false);
    }
  }

  if (loading) return <div className="loading-state" aria-busy="true"><span className="loading-mark" />正在读取设置…</div>;
  if (!draft) return <section className="error-state"><h1>无法打开设置</h1><p>{error}</p><button className="button button-secondary" onClick={() => void load()}>重新读取</button></section>;

  return <SettingsForm
    draft={draft}
    onChange={change}
    onSave={() => void save()}
    saving={saving}
    dirty={dirtyGroups.size > 0}
    error={error}
    success={success}
    onTestTextModel={() => void runTextModelTest()}
    onTestVisionModel={() => void runVisionModelTest()}
    textTest={textTest}
    visionTest={visionTest}
    canTestTextModel={draft.text_model.configured && !dirtyGroups.has('text_model')}
    canTestVisionModel={draft.vision_model.configured && !dirtyGroups.has('vision_model')}
    testingTextModel={testingTextModel}
    testingVisionModel={testingVisionModel}
    migrationPreview={migrationPreview}
    migrationBusy={migrationBusy}
    migrationMessage={migrationMessage}
    onPreviewMigration={() => void runMigrationPreview()}
    onApplyMigration={() => void applyMigrationPreview()}
  />;
}
