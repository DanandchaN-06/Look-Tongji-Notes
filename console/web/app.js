/* Look Tongji Notes console front-end. Vanilla JS, no build step. */
(function () {
  'use strict';

  const S = {
    token: '',
    status: null,
    config: null,
    courses: [],
    courseId: '',
    lectures: [],
    lecturesCourseId: '',
    subId: '',
    tasks: [],
    currentTaskId: '',
    logOffset: 0,
    truncatedNoted: false,
    serveTaskId: '',
    poll: null,
    logBusy: false,
    logEpoch: 0,
    submitting: false,
    agentMetadata: null,
    courseEpoch: 0,
  };

  /* ---------------- token + api ---------------- */

  function readToken() {
    const params = new URLSearchParams(location.search);
    const token = params.get('token') || '';
    if (token) {
      S.token = token;
      // Drop the token from the address bar but keep the view hash.
      history.replaceState(null, '', location.pathname + location.hash);
    }
  }

  async function api(path, options) {
    const opts = Object.assign({ method: 'GET' }, options || {});
    opts.headers = Object.assign({ 'X-Console-Token': S.token }, opts.headers || {});
    if (opts.body && typeof opts.body !== 'string') {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(opts.body);
    }
    const res = await fetch(path, opts);
    let data = null;
    try { data = await res.json(); } catch (e) { data = { ok: false, error: '响应不是合法 JSON' }; }
    if (!res.ok || data.ok === false) {
      throw new Error(data && data.error ? data.error : ('请求失败 (' + res.status + ')'));
    }
    return data;
  }

  /* ---------------- tiny dom helpers ---------------- */

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.prototype.slice.call(document.querySelectorAll(sel));

  function esc(text) {
    return String(text == null ? '' : text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function banner(message, kind) {
    const el = $('#banner');
    if (!message) { el.className = 'banner hidden'; el.textContent = ''; return; }
    el.className = 'banner ' + (kind || 'info');
    el.textContent = message;
  }

  /* ---------------- navigation ---------------- */

  const VIEWS = ['home', 'settings', 'courses', 'tasks', 'agent', 'wiki'];

  function showView(name) {
    if (VIEWS.indexOf(name) === -1) name = 'home';
    $$('.nav-item').forEach((b) => {
      b.classList.toggle('is-active', b.dataset.view === name);
      if (b.dataset.view === name) b.setAttribute('aria-current', 'page');
      else b.removeAttribute('aria-current');
    });
    $$('.view').forEach((v) => v.classList.toggle('is-active', v.dataset.view === name));
    if (location.hash.slice(1) !== name) {
      history.replaceState(null, '', location.pathname + '#' + name);
    }
    if (name === 'tasks') { refreshTasks(); renderBatchCourses(); }
    if (name === 'wiki') refreshWikiState();
  }

  $$('.nav-item').forEach((b) => b.addEventListener('click', () => showView(b.dataset.view)));
  $$('[data-goto]').forEach((b) => b.addEventListener('click', () => showView(b.dataset.goto)));
  window.addEventListener('hashchange', () => showView(location.hash.slice(1)));
  $('#btnHelp').addEventListener('click', () => $('#helpDialog').showModal());
  $('#btnCloseHelp').addEventListener('click', () => $('#helpDialog').close());

  /* ---------------- status ---------------- */

  function statValue(el, text, cls) {
    el.textContent = text;
    el.className = 'stat-value' + (cls ? ' ' + cls : '');
  }

  function renderDeps(target, tools, missing) {
    const rows = Object.keys(tools || {}).map((name) => {
      const path = tools[name];
      return '<tr><td>' + esc(name) + '</td><td class="state ' + (path ? 'ok' : 'bad') + '">' +
        (path ? '已找到' : '缺失') + '</td><td class="mono">' + esc(path || '—') + '</td></tr>';
    }).join('');
    const missingRow = (missing && missing.length)
      ? '<tr><td colspan="3" class="state bad">缺少 Python 依赖：' + esc(missing.join(', ')) + '</td></tr>'
      : '';
    target.innerHTML = '<table class="deps"><thead><tr><th>组件</th><th>状态</th><th>位置</th></tr></thead><tbody>' +
      rows + missingRow + '</tbody></table>';
  }

  function markStep(step, done) {
    const li = document.querySelector('.steps li[data-step="' + step + '"]');
    if (li) li.classList.toggle('done', !!done);
  }

  async function loadStatus() {
    try {
      const data = await api('/api/status');
      S.status = data;

      const cred = data.credentials || {};
      statValue($('#statCred'), cred.configured ? '已配置' : '未配置', cred.configured ? 'ok' : 'warn');
      const ws = data.workspace_config || data.workspace;
      statValue($('#statWs'), ws && ws.workspace_root ? '已设置' : '未设置', ws && ws.workspace_root ? 'ok' : 'warn');
      const vis = data.vision_model || data.vision_support || {};
      const depsReady = !!(data.deps_ok && data.tools && data.tools.ffmpeg);
      $('#btnNextStep').textContent = !cred.configured || !ws || !ws.workspace_root || !depsReady ? '完善账号与保存设置' : '选择课程并采集素材';
      $('#nextStepHint').textContent = ws && ws.workspace_root ? '素材、笔记和阅读网页保存在你选择的本机目录。' : '先选择课程资料保存目录；可以随时打开查看文件。';
      statValue($('#statVision'), vis.configured ? '已保存，需测试识图' : '未配置', vis.configured ? 'ok' : 'warn');
      statValue($('#statDeps'), depsReady ? '正常' : '缺少依赖', depsReady ? 'ok' : 'warn');

      const pill = $('#statusPill');
      if (cred.configured && depsReady && ws && ws.workspace_root) {
        pill.className = 'pill pill-ok';
        pill.textContent = '采集环境就绪';
      } else if (!depsReady) {
        pill.className = 'pill pill-warn';
        pill.textContent = '缺少依赖';
      } else {
        pill.className = 'pill pill-warn';
        pill.textContent = '待完成配置';
      }
      $('#repoPath').textContent = data.repo_root || '';
      $('#howtoRepo').textContent = data.repo_root || '';

      renderDeps($('#depsTable'), data.tools, data.missing_deps);
      renderDeps($('#depsTable2'), data.tools, data.missing_deps);

      // Native dialogs need an interpreter with tkinter; disable the buttons
      // rather than letting the user click into an error.
      const pickerOk = !!(data.console && data.console.picker_available);
      wirePicker($('#btnPickWs'), $('#wsPickHint'), pickerOk);
      wirePicker($('#btnPickMaterials'), null, pickerOk);

      markStep('cred', cred.configured);
      markStep('ws', !!(ws && ws.workspace_root));
      markStep('vision', !!vis.configured);
      markStep('collect', !!(ws && ws.lecture_count > 0));
      markStep('note', !!(ws && ws.site_built));

      if ($('.view[data-view=home]').classList.contains('is-active')) {
        if (!depsReady) {
          const missing = (data.missing_deps || []).slice();
          if (!data.tools || !data.tools.ffmpeg) missing.push('ffmpeg');
          banner('缺少运行依赖：' + missing.join('、') + '。请先安装后再使用。', 'err');
          if (data.ffmpeg_error) banner(data.ffmpeg_error, 'err');
        } else if (!cred.configured) {
          banner('尚未配置同济账号。请到「设置中心」填写，密码只保存在本机 .env 文件中。', 'warn');
        }
      }
    } catch (err) {
      banner('无法读取环境状态：' + err.message, 'err');
      const pill = $('#statusPill');
      pill.className = 'pill pill-warn';
      pill.textContent = '状态不可用';
    }
  }

  /* ---------------- settings ---------------- */

  function fillSettings() {
    api('/api/config').then((data) => {
      S.config = data;
      const cred = data.credentials || {};
      $('#credHint').textContent = cred.password_set
        ? '已保存密码。留空则保持原密码不变。'
        : '尚未保存密码。';
      $('#envRepo').value = cred.gh_pages_repo || '';

      const ws = data.workspace || {};
      if (ws.workspace_root) {
        $('#wsRoot').value = ws.workspace_root;
        $('#wsOwner').value = ws.owner_name || '';
        $('#wsSite').value = ws.site_name || '';
      }

      const vis = data.vision || {};
      buildProviderSelect(vis.providers || []);
      if (vis.provider) $('#visProvider').value = vis.provider;
      // Apply the catalog defaults first, then restore what was actually saved.
      // The other order silently replaced a custom model name with the first
      // catalogue entry.
      onProviderChange();
      if (vis.model) $('#visModel').value = vis.model;
      if (vis.base_url) $('#visBase').value = vis.base_url;
      renderVisionHint();
      $('#visKey').placeholder = vis.api_key_set ? '已保存密钥，留空则不变' : '请输入 API Key';
    }).catch((err) => banner('读取配置失败：' + err.message, 'err'));
  }

  function buildProviderSelect(providers) {
    const sel = $('#visProvider');
    sel.innerHTML = providers.map((p) =>
      '<option value="' + esc(p.id) + '">' + esc(p.name) + '</option>').join('');
    sel._providers = providers;
  }

  function currentProvider() {
    const sel = $('#visProvider');
    const list = sel._providers || [];
    return list.filter((p) => p.id === sel.value)[0] || null;
  }

  // Models that cannot accept images. Configuring one as the "vision model"
  // looks fine in the UI but fails on the very first slide screenshot, so warn
  // before the user wastes a run on it.
  const TEXT_ONLY_MODELS = [
    /^deepseek-/i,
    /^moonshot-v1/i,
    /^glm-4(?!v)/i,
    /^gpt-3\.5/i,
  ];

  function visionWarning(providerId, model) {
    const name = (model || '').trim();
    if (!name) return '';
    if (!TEXT_ONLY_MODELS.some((re) => re.test(name))) return '';
    if (providerId === 'deepseek') {
      return name + ' 是文本模型预设，尚未验证图片输入。请选择支持视觉的模型，并通过「测试识图」确认接口可用。';
    }
    return name + ' 通常不支持图片输入，可能无法读取课件截图。请确认所选模型支持视觉。';
  }

  function renderVisionHint() {
    const host = $('#visHint');
    if (!host) return;
    const p = currentProvider();
    const message = visionWarning($('#visProvider').value, $('#visModel').value);
    host.innerHTML = message ? '<div class="banner warn">' + esc(message) + '</div>' : '';
  }

  function onProviderChange() {
    const p = currentProvider();
    if (!p) return;
    $('#visBase').value = p.baseUrl || '';
    const dl = $('#visModels');
    dl.innerHTML = (p.models || []).map((m) => '<option value="' + esc(m) + '"></option>').join('');
    $('#visModels').innerHTML = dl.innerHTML;
    if (p.models && p.models.length) $('#visModel').value = p.models[0];
    else $('#visModel').value = '';
    renderVisionHint();
  }

  $('#visProvider').addEventListener('change', onProviderChange);
  $('#visModel').addEventListener('input', renderVisionHint);

  $('#btnSaveCred').addEventListener('click', async () => {
    const body = {
      username: $('#envUser').value.trim(),
      gh_pages_repo: $('#envRepo').value.trim(),
    };
    const pass = $('#envPass').value;
    if (pass) body.password = pass;
    if (!body.username && !pass && !body.gh_pages_repo) {
      banner('请至少填写学号、密码或仓库其中之一。', 'warn');
      return;
    }
    try {
      await api('/api/config/credentials', { method: 'POST', body });
      $('#envPass').value = '';
      banner('凭据已保存到本机 .env（该文件已被 .gitignore 排除）。', 'ok');
      loadStatus();
      fillSettings();
    } catch (err) {
      banner('保存失败：' + err.message, 'err');
    }
  });

  $('#btnSaveWs').addEventListener('click', async () => {
    const body = {
      workspace_root: $('#wsRoot').value.trim(),
      owner_name: $('#wsOwner').value.trim(),
      site_name: $('#wsSite').value.trim(),
      migrate: $('#wsMigrate').checked,
    };
    if (!body.workspace_root) {
      banner('请填写知识库保存路径。', 'warn');
      return;
    }
    if (body.migrate && !confirm('确认迁移？这会把已有课程内容移动到新路径。此操作不可自动撤销。')) return;
    try {
      await api('/api/config/workspace', { method: 'POST', body });
      banner(body.migrate ? '知识库设置已保存，内容已迁移。' : '知识库设置已保存，已有文件未被移动。', 'ok');
      loadStatus();
      fillSettings();
    } catch (err) {
      banner('保存失败：' + err.message, 'err');
    }
  });

  /* ---------------- native pickers ---------------- */

  // A browser can never read a real local path, so selection is delegated to a
  // native dialog opened by the local server process.
  async function pickNative(mode, initial, title) {
    return api('/api/pick', { method: 'POST', body: { mode: mode, initial: initial, title: title } });
  }

  function withBusyButton(button, busyText, task) {
    const original = button.textContent;
    button.disabled = true;
    button.textContent = busyText;
    return Promise.resolve()
      .then(task)
      .finally(() => {
        button.disabled = false;
        button.textContent = original;
      });
  }

  function wirePicker(button, hint, enabled) {
    button.disabled = !enabled;
    button.title = enabled ? '' : '本机未找到可用的系统对话框解释器';
    if (enabled) return;
    if (hint) hint.textContent = '本机未找到带 tkinter 的 Python 解释器，请手动填写路径。';
  }

  $('#btnPickWs').addEventListener('click', () => {
    withBusyButton($('#btnPickWs'), '等待选择…', async () => {
      try {
        const data = await pickNative('dir', $('#wsRoot').value.trim(),
          '选择课程知识库保存目录');
        if (data.cancelled || !data.path) return;
        $('#wsRoot').value = data.path;
        $('#wsPickHint').textContent = '已选择：' + data.path;
        if (!$('#wsSite').value.trim()) {
          const leaf = data.path.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || '';
          if (leaf) $('#wsSite').value = leaf + '的课程知识库';
        }
      } catch (err) {
        banner('打开文件夹选择框失败：' + err.message, 'err');
      }
    });
  });

  $('#btnPickMaterials').addEventListener('click', () => {
    if (!requireLecture()) return;
    withBusyButton($('#btnPickMaterials'), '等待选择…', async () => {
      try {
        const data = await pickNative('files', '', '选择补充材料（可多选）');
        if (data.cancelled || !(data.paths || []).length) return;
        const existing = $('#materialList').value
          .split('\n').map((s) => s.trim()).filter(Boolean);
        (data.paths || []).forEach((p) => {
          if (!existing.some((line) => line.endsWith(p))) existing.push(p);
        });
        $('#materialList').value = existing.join('\n');
      } catch (err) {
        banner('打开文件选择框失败：' + err.message, 'err');
      }
    });
  });

  $('#btnSaveVision').addEventListener('click', async () => {
    const p = currentProvider();
    const body = {
      provider: $('#visProvider').value,
      model: $('#visModel').value.trim(),
      base_url: $('#visBase').value.trim(),
      api_format: p ? p.apiFormat : '',
      api_key: $('#visKey').value,
    };
    try {
      await api('/api/config/vision', { method: 'POST', body });
      $('#visKey').value = '';
      banner('视觉模型配置已保存到 vision-support/config.json。', 'ok');
      loadStatus();
      fillSettings();
    } catch (err) {
      banner('保存失败：' + err.message, 'err');
    }
  });

  $('#btnTestVision').addEventListener('click', async () => {
    const out = $('#visionTestOut');
    const hint = $('#visionTestHint');
    const image = prompt('输入一张本地图片的完整路径（需位于项目或知识库目录内）：', '');
    if (!image) return;
    hint.textContent = '测试中…';
    out.classList.remove('hidden');
    out.textContent = '正在调用识图模型…';
    try {
      const data = await api('/api/vision/test', { method: 'POST', body: { image: image } });
      out.textContent = (data.stdout || '') + (data.stderr ? '\n--- stderr ---\n' + data.stderr : '');
      hint.textContent = data.ok ? '测试成功。' : ('测试失败，退出码 ' + data.returncode);
    } catch (err) {
      out.textContent = err.message;
      hint.textContent = '测试失败。';
    }
  });

  /* ---------------- courses ---------------- */

  async function loadCourses(options) {
    options = options && options.agent ? options : {};
    const msg = $('#courseMsg');
    const btn = $('#btnLoadCourses');
    btn.disabled = true;
    msg.textContent = '正在登录并获取课程列表，首次可能需要数十秒…';
    try {
      const query = '/api/courses?query=' + encodeURIComponent(options.agent ? '' : $('#courseQuery').value.trim()) +
        '&all=' + (options.agent || $('#courseAll').checked ? '1' : '0') + '&force_login=' + ($('#courseForceLogin').checked ? '1' : '0');
      const data = await api(query);
      S.courses = data.courses || [];
      renderCourses();
      renderAgentTarget();
      renderBatchCourses();
      msg.textContent = '共 ' + S.courses.length + ' 门课程。';
    } catch (err) {
      msg.textContent = '';
      banner('获取课程失败：' + err.message, 'err');
    } finally {
      btn.disabled = false;
    }
  }

  function renderCourses() {
    const host = $('#courseList');
    if (!S.courses.length) {
      host.innerHTML = '<div class="empty">暂无课程。请先配置账号并获取课程。</div>';
      return;
    }
    host.innerHTML = S.courses.map((c) =>
      '<button type="button" class="list-item' + (c.course_id === S.courseId ? ' is-active' : '') +
      '" data-course="' + esc(c.course_id) + '"><div class="li-title">' + esc(c.title || '(无标题)') +
      '</div><div class="li-meta">' + esc(c.teacher || '') + ' · ' + esc(c.course_id) + '</div></button>'
    ).join('');
    $$('#courseList .list-item').forEach((el) => {
      el.addEventListener('click', () => selectCourse(el.dataset.course));
    });
  }

  async function selectCourse(courseId) {
    const epoch = ++S.courseEpoch;
    S.courseId = courseId;
    S.subId = '';
    S.lectures = [];
    S.lecturesCourseId = '';
    S.agentMetadata = null;
    $('#agentCourse').value = courseId;
    $('#agentSub').value = '';
    invalidateInstruction();
    renderAgentTarget();
    updateContext();
    renderCourses();
    const host = $('#lectureList');
    host.innerHTML = '<div class="empty">正在获取节次…</div>';
    $('#lectureActions').hidden = true;
    $('#artifactPanel').innerHTML = '<div class="empty">请先选择节次。</div>';
    if (!courseId) {
      host.innerHTML = '<div class="empty">请先选择课程。</div>';
      return;
    }
    try {
      const data = await api('/api/lectures?course_id=' + encodeURIComponent(courseId));
      if (S.courseId !== courseId || epoch !== S.courseEpoch) return false;
      S.lectures = data.lectures || [];
      S.lecturesCourseId = courseId;
      renderLectures();
      renderAgentTarget();
      return true;
    } catch (err) {
      if (S.courseId !== courseId || epoch !== S.courseEpoch) return false;
      host.innerHTML = '<div class="empty">获取节次失败：' + esc(err.message) + '</div>';
    }
  }

  function renderLectures() {
    const host = $('#lectureList');
    if (!S.lectures.length) {
      host.innerHTML = '<div class="empty">该课程没有节次。</div>';
      return;
    }
    host.innerHTML = S.lectures.map((l) =>
      '<button type="button" class="list-item' + (l.sub_id === S.subId ? ' is-active' : '') +
      '" data-sub="' + esc(l.sub_id) + '"><div class="li-title">' + esc(l.sub_title || '(无标题)') +
      (l.has_playback ? '' : ' <span class="pill pill-warn">无回放</span>') +
      '</div><div class="li-meta">' + esc(l.date || '') + ' · ' + esc(l.sub_id) + '</div></button>'
    ).join('');
    $$('#lectureList .list-item').forEach((el) => {
      el.addEventListener('click', () => selectLecture(el.dataset.sub));
    });
  }

  async function selectLecture(subId) {
    S.subId = subId;
    renderLectures();
    $('#lectureActions').hidden = !subId;
    $('#agentCourse').value = S.courseId;
    $('#agentSub').value = subId;
    invalidateInstruction();
    renderAgentTarget();
    await refreshArtifacts();
    updateContext();
  }

  async function refreshArtifacts() {
    const host = $('#artifactPanel');
    if (!S.courseId || !S.subId) { host.innerHTML = '<div class="empty">请先选择节次。</div>'; return; }
    const courseId = S.courseId, subId = S.subId;
    try {
      const data = await api('/api/artifacts?course_id=' + encodeURIComponent(courseId) +
        '&sub_id=' + encodeURIComponent(subId));
      if (S.courseId !== courseId || S.subId !== subId) return;
      S.agentMetadata = data.artifacts;
      renderAgentTarget();
      renderArtifacts(host, data.artifacts);
      renderArtifacts($('#agentArtifacts'), data.artifacts);
    } catch (err) {
      host.innerHTML = '<div class="empty">读取素材失败：' + esc(err.message) + '</div>';
    }
  }

  function artCard(label, ok, value, path) {
    return '<div class="art ' + (ok ? 'ok' : 'miss') + '"><span class="a-label">' + esc(label) +
      '</span><span class="a-value">' + esc(value) + '</span>' +
      (path ? '<span class="a-path">' + esc(path) + '</span>' : '') + '</div>';
  }

  function renderArtifacts(host, a) {
    if (!a) { host.innerHTML = '<div class="empty">暂无数据。</div>'; return; }
    if (!a.workspace_configured) {
      host.innerHTML = '<div class="empty">尚未配置知识库路径。请先到「设置中心」设置。</div>';
      return;
    }
    if (!a.lecture_dir) {
      host.innerHTML = '<div class="empty">该节次还没有本地素材。可在「任务中心」或上方按钮开始采集。</div>';
      return;
    }
    host.innerHTML = [
      artCard('字幕文本', !!a.transcript_txt, a.transcript_txt ? '已生成' : '缺失', a.transcript_txt),
      artCard('字幕 SRT', !!a.transcript_srt, a.transcript_srt ? '已生成' : '缺失', a.transcript_srt),
      artCard('课件截图', a.slide_count > 0, a.slide_count + ' 张', a.slides_dir),
      artCard('时间轴大纲', !!a.timeline, a.timeline ? '已生成' : '缺失', a.timeline),
      artCard('学习笔记', a.has_notes, a.has_notes ? '已生成' : '缺失', a.notes || a.dialogue),
      artCard('补充材料', (a.materials || []).length > 0, (a.materials || []).length + ' 项', ''),
    ].join('');
  }

  $('#btnLoadCourses').addEventListener('click', loadCourses);
  $('#courseQuery').addEventListener('keydown', (e) => { if (e.key === 'Enter') loadCourses(); });

  $('#btnCollect').addEventListener('click', () => {
    if (!requireLecture()) return;
    startTask('note', Object.assign(collectParams(), { note_style: 'standard' }),
      '采集字幕与课件');
  });

  $('#btnSlidesOnly').addEventListener('click', () => {
    if (!requireLecture()) return;
    startTask('slide', collectParams(), '下载课件截图');
  });

  function collectParams() {
    return { course_id: S.courseId, sub_id: S.subId, force_login: $('#courseForceLogin').checked,
      force_transcribe: $('#forceTranscribe').checked,
      limit: Number($('#downloadLimit').value), concurrency: Number($('#downloadConcurrency').value),
      retries: Number($('#downloadRetries').value), timeout: Number($('#downloadTimeout').value) };
  }
  $('#btnTranscriptOnly').addEventListener('click', () => {
    if (requireLecture()) startTask('transcribe', collectParams(), '单节字幕转写');
  });
  $('#btnResolveLecture').addEventListener('click', () => withBusyButton($('#btnResolveLecture'), '定位中…', async () => {
    try {
      const data = await api('/api/lecture/resolve', { method: 'POST', body: { url: $('#lectureUrl').value.trim() } });
      if (!await selectCourse(data.course_id)) return;
      if (data.sub_id) await selectLecture(data.sub_id);
      banner('已定位课程，请核对课次后开始采集。', 'ok');
    } catch (err) { banner(err.message, 'err'); }
  }));

  $('#btnImportMaterials').addEventListener('click', () => {
    if (!requireLecture()) return;
    const raw = $('#materialList').value.split('\n').map((s) => s.trim()).filter(Boolean);
    if (!raw.length) { banner('请先填写至少一个材料路径。', 'warn'); return; }
    const materials = raw.map((line) => {
      const idx = line.indexOf('=');
      if (idx > 0) return line.slice(0, idx).trim() + '=' + line.slice(idx + 1).trim();
      return line;
    });
    startTask('add', { course_id: S.courseId, sub_id: S.subId, materials: materials }, '导入补充材料');
  });

  $('#btnToAgent').addEventListener('click', () => {
    if (!requireLecture()) return;
    showView('agent');
    $('#agentCourse').value = S.courseId;
    $('#agentSub').value = S.subId;
    invalidateInstruction();
    refreshArtifacts();
  });

  function requireLecture() {
    if (!S.courseId || !S.subId) { banner('请先选择课程与节次。', 'warn'); return false; }
    return true;
  }

  /* ---------------- tasks ---------------- */

  async function startTask(kind, params, label) {
    if (S.submitting) return;
    if (kind === 'note' || kind === 'transcribe' || kind === 'batch') {
      if (!confirm('转写会将课程音频上传至哔哩哔哩云端 ASR 处理。确认继续采集？')) return;
    }
    S.submitting = true;
    try {
      const data = await api('/api/tasks', { method: 'POST', body: { kind: kind, params: params } });
      showView('tasks');
      S.currentTaskId = data.task.id;
      S.logEpoch += 1;
      S.truncatedNoted = false;
      S.logOffset = 0;
      $('#taskLog').textContent = '任务已启动：' + label + '\n';
      await refreshTasks();
      startPolling();
    } catch (err) {
      banner('启动任务失败：' + err.message, 'err');
    } finally { S.submitting = false; }
  }

  async function refreshTasks() {
    try {
      const data = await api('/api/tasks');
      S.tasks = data.tasks || [];
      renderTasks();
    } catch (err) { /* silent */ }
  }

  function statusText(t) {
    return ({ queued: '排队中', running: '进行中', succeeded: '已完成',
              failed: '失败', cancelled: '已取消' })[t.status] || t.status;
  }

  function renderTasks() {
    const host = $('#taskList');
    if (!S.tasks.length) { host.innerHTML = '<div class="empty">暂无任务。</div>'; return; }
    host.innerHTML = S.tasks.map((t) =>
      '<button type="button" class="list-item' + (t.id === S.currentTaskId ? ' is-active' : '') +
      '" data-task="' + esc(t.id) + '"><div class="li-title">' + esc(t.label) +
      ' <span class="pill ' + (t.status === 'succeeded' ? 'pill-ok' :
        (t.status === 'failed' ? 'pill-warn' : 'pill-muted')) + '">' + statusText(t) + '</span></div>' +
      '<div class="li-meta">' + esc(new Date(t.created_at * 1000).toLocaleTimeString()) +
      (t.progress ? ' · ' + esc(t.progress.label) + (t.progress.percent == null ? '' : ' · ' + t.progress.percent + '%') : '') + '</div></button>'
    ).join('');
    $$('#taskList .list-item').forEach((el) => {
      el.addEventListener('click', () => selectTask(el.dataset.task));
    });
  }

  function selectTask(id) {
    S.currentTaskId = id;
    S.logEpoch += 1;
    S.logOffset = 0;
    S.truncatedNoted = false;
    $('#taskLog').textContent = '';
    renderTasks();
    // Load immediately, then keep following only if the task is still alive.
    pollLog();
    const task = S.tasks.filter((t) => t.id === id)[0];
    if (task) renderTaskDetail(task);
    if (task && (task.status === 'running' || task.status === 'queued')) startPolling();
    else stopPolling();
  }

  const MAX_LOG_CHARS = 400000;

  async function pollLog() {
    if (!S.currentTaskId || S.logBusy) return;
    const taskId = S.currentTaskId;
    const epoch = S.logEpoch;
    S.logBusy = true;
    try {
      const data = await api('/api/tasks/' + taskId + '/log?offset=' + S.logOffset);
      if (taskId !== S.currentTaskId || epoch !== S.logEpoch) return;
      const el = $('#taskLog');
      if (data.lines && data.lines.length) {
        el.textContent += data.lines.join('\n') + '\n';
        // Keep the DOM bounded: a long batch run can emit far more than the
        // browser can hold comfortably.
        if (el.textContent.length > MAX_LOG_CHARS) {
          el.textContent = '（更早的日志已超过页面保留上限）\n' +
            el.textContent.slice(-MAX_LOG_CHARS);
        }
        el.scrollTop = el.scrollHeight;
      }
      if (data.truncated && !S.truncatedNoted) {
        S.truncatedNoted = true;
        el.textContent = '（更早的日志已超过服务端保留上限被丢弃）\n' + el.textContent;
      }
      S.logOffset = data.offset;
      const t = data.task;
      renderTaskDetail(t);
      $('#taskDetailTitle').textContent = t.label + ' · ' + statusText(t);
      $('#btnCancelTask').hidden = !(t.status === 'running' || t.status === 'queued');
      if (t.status === 'succeeded' || t.status === 'failed' || t.status === 'cancelled') {
        stopPolling();
        refreshTasks();
        refreshArtifacts().catch(() => {});
        refreshWikiState().catch(() => {});
        refreshBatchState();
      }
    } catch (err) { /* retry on the next tick */ }
    finally {
      S.logBusy = false;
      if (epoch !== S.logEpoch && S.currentTaskId) pollLog();
    }
  }

  function startPolling() {
    stopPolling();
    S.poll = setInterval(() => { pollLog(); refreshTasks(); }, 1200);
  }
  function stopPolling() { if (S.poll) { clearInterval(S.poll); S.poll = null; } }

  $('#btnRefreshTasks').addEventListener('click', refreshTasks);
  $('#btnCancelTask').addEventListener('click', async () => {
    if (!S.currentTaskId) return;
    try {
      await api('/api/tasks/' + S.currentTaskId + '/cancel', { method: 'POST', body: {} });
    } catch (err) { banner('取消失败：' + err.message, 'err'); }
  });

  $('#btnBatch').addEventListener('click', () => {
    const courseId = $('#batchCourseChoice').value;
    if (!courseId) return;
    startTask('batch', { course_id: courseId, max_retries: Number($('#batchRetries').value),
      retry_failed: $('#batchRetryFailed').checked }, '整门课批量转写');
  });
  $('#btnIndex').addEventListener('click', () => startTask('index', {}, '建立知识库索引'));

  function renderTaskDetail(task) {
    window.ConsoleProgress.render(task);
    $('#btnCancelTask').disabled = !!task.cancel_requested;
    $('#btnCancelTask').textContent = task.cancel_requested ? '正在停止…' : '取消任务';
    const next = $('#btnTaskNext');
    next.hidden = task.status !== 'succeeded' || !['note', 'slide', 'transcribe', 'add', 'index', 'build', 'batch'].includes(task.kind);
    next.textContent = ['index', 'build'].includes(task.kind) ? '去资料库查看阅读网页' : '去 Agent 工作台写作';
    next.onclick = async () => {
      if (task.params && task.params.course_id) {
        if (!await selectCourse(task.params.course_id)) return;
        if (task.params.sub_id) await selectLecture(task.params.sub_id);
      }
      showView(['index', 'build'].includes(task.kind) ? 'wiki' : 'agent');
    };
  }

  function renderBatchCourses() {
    const select = $('#batchCourseChoice'), previous = select.value || S.courseId;
    select.innerHTML = '<option value="">选择要批量处理的课程</option>' + S.courses.map((c) =>
      '<option value="' + esc(c.course_id) + '">' + esc(c.title || c.course_id) + '</option>').join('');
    select.value = previous;
    refreshBatchState();
  }
  async function refreshBatchState() {
    const id = $('#batchCourseChoice').value;
    $('#btnBatch').disabled = !id;
    if (!id) { $('#batchRecovery').textContent = '请先在选课页获取课程，再选择批量处理对象。'; return; }
    try {
      const data = await api('/api/batch/state?course_id=' + encodeURIComponent(id));
      if ($('#batchCourseChoice').value !== id) return;
      $('#batchRecovery').textContent = data.available ? '上次记录：已完成 ' + data.done + ' / ' + data.total +
        ' 节，失败 ' + data.failed + ' 节，待处理 ' + (data.pending + data.running) + ' 节。继续时跳过已完成课次。' : '暂无记录，将从可播放的课次开始。';
    } catch (err) { $('#batchRecovery').textContent = '读取记录失败：' + err.message; }
  }
  $('#batchCourseChoice').addEventListener('change', refreshBatchState);

  /* ---------------- agent ---------------- */

  function selectedAgentContext() {
    const courseId = $('#agentCourse').value.trim();
    const subId = $('#agentSub').value.trim();
    const course = S.courses.find((c) => String(c.course_id) === courseId) || {};
    const lecture = courseId === S.lecturesCourseId ? S.lectures.find((l) => String(l.sub_id) === subId) || {} : {};
    const candidate = S.agentMetadata || {};
    const local = String(candidate.course_id) === courseId && String(candidate.sub_id) === subId ? candidate : {};
    return {
      course_title: local.course_title || course.title || '',
      teacher: local.teacher || course.teacher || lecture.lecturer_name || '',
      session_title: local.session_title || lecture.sub_title || '',
      date: local.date || lecture.date || '',
    };
  }

  function renderAgentTarget() {
    const courseId = $('#agentCourse').value.trim();
    const subId = $('#agentSub').value.trim();
    const context = selectedAgentContext();
    const courseSelect = $('#agentCourseChoice');
    courseSelect.innerHTML = '<option value="">选择课程（先点击获取课程）</option>' + S.courses.map((c) =>
      '<option value="' + esc(c.course_id) + '">' + esc(c.title || c.course_id) +
      (c.teacher ? ' · ' + esc(c.teacher) : '') + '</option>').join('');
    if (courseId && !S.courses.some((c) => String(c.course_id) === courseId)) {
      courseSelect.innerHTML += '<option value="' + esc(courseId) + '">' + esc(context.course_title || ('按 ID 定位：' + courseId)) + '</option>';
    }
    courseSelect.value = courseId;
    const lectures = courseId === S.lecturesCourseId ? S.lectures : [];
    const lectureSelect = $('#agentLectureChoice');
    lectureSelect.innerHTML = '<option value="">' + (courseId ? '选择节次；尚无列表时可按 ID 定位' : '请先选择课程') + '</option>' + lectures.map((l) =>
      '<option value="' + esc(l.sub_id) + '">' + esc(l.sub_title || l.sub_id) +
      (l.date ? ' · ' + esc(l.date) : '') + '</option>').join('');
    if (subId && !lectures.some((l) => String(l.sub_id) === subId)) {
      lectureSelect.innerHTML += '<option value="' + esc(subId) + '">' + esc(context.session_title || ('按 ID 定位：' + subId)) + '</option>';
    }
    lectureSelect.value = subId;
    lectureSelect.disabled = !courseId;
    if ($('input[name=agentKind]:checked').value === 'cheatsheet' && $('#sheetScope').value === 'course') {
      lectureSelect.disabled = true;
      $('#agentTargetContext').textContent = '任务对象：整门课程 ' + (context.course_title || courseId || '待选择') + ' 的已有笔记。';
      return;
    }
    const host = $('#agentTargetContext');
    if (!courseId || !subId) {
      host.textContent = courseId ? '已选课程：' + (context.course_title || courseId) + '。请继续选择要处理的课次。' : '尚未选择节次。选好课程和课次后，核对这里的任务对象。';
      return;
    }
    host.innerHTML = '<strong>' + esc(context.course_title || ('课程 ' + courseId)) + '</strong>' +
      '<span>教师：' + esc(context.teacher || '尚未提供') + '</span>' +
      '<span>当前课次：' + esc(context.session_title || ('节次 ' + subId)) + '</span>' +
      '<span>上课日期：' + esc(context.date || '尚未提供') + '</span>' +
      '<span>任务对象：仅当前节次。课程 ID ' + esc(courseId) + ' · 节次 ID ' + esc(subId) + '</span>';
  }

  $('#btnAgentLoadCourses').addEventListener('click', () => withBusyButton($('#btnAgentLoadCourses'), '获取中…', () => loadCourses({agent: true})));
  $('#agentCourseChoice').addEventListener('change', () => selectCourse($('#agentCourseChoice').value));
  $('#agentLectureChoice').addEventListener('change', () => selectLecture($('#agentLectureChoice').value));

  function agentLabel() {
    const el = $('#agentTool');
    return el.value === 'generic' ? 'Agent' : el.selectedOptions[0].textContent;
  }

  function invalidateInstruction() {
    $('#agentInstruction').value = '';
    $('#agentWarnings').textContent = '';
    $('#verifyResult').textContent = '';
    $('#btnCopy').disabled = true;
    $('#btnDownloadInstruction').disabled = true;
  }

  function updateAgentChoice() {
    const label = agentLabel();
    $('#agentCopyTitle').textContent = '复制到 ' + label;
    $('#agentHowtoTitle').textContent = '在 ' + label + ' 中的操作步骤';
    try { localStorage.setItem('look-console-agent', $('#agentTool').value); } catch (e) { /* optional */ }
    invalidateInstruction();
  }
  try {
    const saved = localStorage.getItem('look-console-agent');
    if (saved && Array.from($('#agentTool').options).some((o) => o.value === saved)) $('#agentTool').value = saved;
  } catch (e) { /* optional */ }
  updateAgentChoice();
  $('#agentTool').addEventListener('change', updateAgentChoice);
  ['#agentCourse', '#agentSub'].forEach((id) => $(id).addEventListener('input', () => {
    S.agentMetadata = null;
    invalidateInstruction();
    renderAgentTarget();
  }));
  $$('input[name=noteStyle], #optTimeline, #sheetFormat, #sheetScope, #sheetOutput').forEach((el) => el.addEventListener('change', () => { invalidateInstruction(); updateAgentScope(); renderAgentTarget(); }));

  function instructionSelection() {
    return new URLSearchParams({
      kind: $('input[name=agentKind]:checked').value,
      agent: $('#agentTool').value,
      course_id: $('#agentCourse').value.trim(),
      sub_id: $('#agentSub').value.trim(),
      note_style: $('input[name=noteStyle]:checked').value,
      include_timeline: $('#optTimeline').checked ? '1' : '0',
      cheatsheet_format: $('#sheetFormat').value,
      cheatsheet_scope: $('#sheetScope').value,
      cheatsheet_output: $('#sheetOutput').value.trim(),
      publish_repository: $('#envRepo').value.trim(),
    }).toString();
  }

  function instructionQuery() {
    const query = new URLSearchParams(instructionSelection());
    Object.entries(selectedAgentContext()).forEach(([key, value]) => query.set(key, value));
    return query.toString();
  }

  async function makeInstruction() {
    const courseId = $('#agentCourse').value.trim();
    const subId = $('#agentSub').value.trim();
    const kind = (document.querySelector('input[name=agentKind]:checked') || {}).value || 'note';
    const style = (document.querySelector('input[name=noteStyle]:checked') || {}).value || 'standard';
    if (!validAgentSelection(kind, courseId, subId)) return;
    try {
      const selection = instructionSelection();
      const qs = '/api/agent/instruction?' + instructionQuery();
      const data = await api(qs);
      if (selection !== instructionSelection()) return;
      $('#agentInstruction').value = data.instruction || '';
      $('#btnCopy').disabled = !data.instruction;
      $('#btnDownloadInstruction').disabled = !data.instruction;
      S.agentMetadata = data.artifacts;
      renderAgentTarget();
      renderArtifacts($('#agentArtifacts'), data.artifacts);
      const warnHost = $('#agentWarnings');
      const warnings = data.warnings || [];
      warnHost.innerHTML = warnings.length
        ? '<div class="banner warn">' + warnings.map(esc).join('<br>') + '</div>' : '';
    } catch (err) {
      banner('生成指令失败：' + err.message, 'err');
    }
  }

  $('#btnMakeInstruction').addEventListener('click', () => withBusyButton($('#btnMakeInstruction'), '生成中…', makeInstruction));

  document.querySelectorAll('input[name=agentKind]').forEach((el) => {
    el.addEventListener('change', () => {
      updateAgentScope();
      renderAgentTarget();
      invalidateInstruction();
    });
  });

  function updateAgentScope() {
      const kind = $('input[name=agentKind]:checked').value;
      $('#noteOptions').style.display = kind === 'note' ? '' : 'none';
      $('#sheetOptions').hidden = kind !== 'cheatsheet';
      $('#agentTargetCard').hidden = kind === 'wiki' || kind === 'publish';
      $('#agentLectureChoice').disabled = !$('#agentCourse').value || (kind === 'cheatsheet' && $('#sheetScope').value === 'course');
      $('#btnVerify').textContent = kind === 'publish' ? '检查发布准备' : '校验 Agent 产物';
      $('#agentScopeSummary').textContent = ({
        note: '学习笔记：针对当前节次的字幕、课件和补充材料撰写。',
        cheatsheet: 'A4 速查表：提炼' + ($('#sheetScope').value === 'course' ? '整门课程' : '当前节次') + '已有笔记，生成可打印的 HTML 或 LaTeX。',
        wiki: '重建知识库：对当前知识库的全部课程建立索引并构建站点。',
        publish: '发布阅读网页：为当前资料库生成 GitHub Pages 交接指令，由你授权 Agent 后发布。',
      })[kind];
  }
  function validAgentSelection(kind, courseId, subId) {
    if (['wiki', 'publish'].includes(kind)) return true;
    if (!courseId || (!(kind === 'cheatsheet' && $('#sheetScope').value === 'course') && !subId)) {
      banner('请先选择课程' + (kind === 'cheatsheet' && $('#sheetScope').value === 'course' ? '。' : '与课次。'), 'warn'); return false;
    }
    return true;
  }
  $('#btnSheetEnvironment').addEventListener('click', () => startTask('cheatsheet', {format: $('#sheetFormat').value}, '检查速查表生成环境'));

  $('#btnCopy').addEventListener('click', async () => {
    const text = $('#agentInstruction').value;
    if (!text) { banner('请先生成指令。', 'warn'); return; }
    try {
      await navigator.clipboard.writeText(text);
      banner('指令已复制到剪贴板。', 'ok');
    } catch (e) {
      const ta = $('#agentInstruction');
      ta.select();
      const copied = document.execCommand('copy');
      ta.setAttribute('readonly', 'readonly');
      banner(copied ? '指令已复制。' : '请按 Ctrl+C 手动复制选中的指令，或点击「保存指令」。', copied ? 'ok' : 'warn');
    }
  });

  $('#btnDownloadInstruction').addEventListener('click', () => {
    const text = $('#agentInstruction').value;
    if (!text) return;
    const url = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = url; link.download = 'agent-instruction.txt'; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });

  $('#btnCheckArtifacts').addEventListener('click', async () => {
    const courseId = $('#agentCourse').value.trim();
    if (courseId !== S.courseId) {
      S.lectures = [];
      S.lecturesCourseId = '';
      S.agentMetadata = null;
      renderLectures();
    }
    S.courseId = courseId;
    S.subId = $('#agentSub').value.trim();
    renderCourses();
    renderAgentTarget();
    await refreshArtifacts();
  });

  $('#btnVerify').addEventListener('click', async () => {
    const courseId = $('#agentCourse').value.trim();
    const subId = $('#agentSub').value.trim();
    const kind = $('input[name=agentKind]:checked').value;
    if (kind === 'publish') { await publishCheck(); return; }
    if (!validAgentSelection(kind, courseId, subId)) return;
    try {
      const selection = instructionSelection();
      const query = instructionQuery();
      const data = await api('/api/agent/verify?' + query);
      if (selection !== instructionSelection()) return;
      if (data.artifacts) renderArtifacts($('#agentArtifacts'), data.artifacts);
      const r = $('#verifyResult');
      if (data.ready) {
        r.textContent = data.message;
        banner('校验通过：' + data.message, 'ok');
      } else {
        const missing = data.missing || [];
        r.textContent = '尚未检测到：' + missing.join('、') + '。';
        banner('尚未检测到：' + missing.join('、') + '。请确认 Agent 已完成写入。', 'warn');
      }
    } catch (err) {
      banner('校验失败：' + err.message, 'err');
    }
  });

  /* ---------------- wiki & publish ---------------- */

  async function refreshWikiState() {
    try {
      const data = await api('/api/status');
      S.status = data;
      const ws = data.workspace_config || {};
      $('#wikiLocation').textContent = ws.workspace_root ? '资料保存在本机：' + ws.workspace_root : '尚未选择保存目录，请先到账号与保存设置中配置。';
      const host = $('#wikiState');
      const siteOk = !!ws.site_built;
      host.innerHTML = [
        artCard('知识库目录', !!ws.workspace_root, ws.workspace_root ? '已设置' : '未设置', ws.workspace_root),
        artCard('已采集节次', ws.lecture_count > 0, (ws.lecture_count || 0) + ' 节'),
        artCard('站点首页', siteOk, siteOk ? '已构建' : '未构建'),
      ].join('');
      const pub = $('#publishState');
      pub.innerHTML = [
        artCard('gh CLI', !!data.tools.gh, data.tools.gh ? '已安装' : '未安装'),
        artCard('目标仓库', !!(data.credentials || {}).gh_pages_repo,
          (data.credentials || {}).gh_pages_repo || '未设置'),
        artCard('站点就绪', siteOk, siteOk ? '可以发布' : '需先构建'),
      ].join('');
    } catch (err) { /* silent */ }
  }

  $('#btnBuildSite').addEventListener('click', () => startTask('build', {}, '构建知识库站点'));

  $('#btnServe').addEventListener('click', async () => {
    if (S.serveTaskId) { banner('已有预览任务，请先停止后再启动。', 'warn'); return; }
    $('#btnServe').disabled = true;
    try {
      const data = await api('/api/tasks', {
        method: 'POST',
        body: { kind: 'serve', params: {} },
      });
      S.serveTaskId = data.task.id;
      $('#btnServeStop').hidden = false;
      $('#serveHint').textContent = '预览服务启动中…';
      S.currentTaskId = data.task.id;
      S.logEpoch += 1;
      S.logOffset = 0;
      $('#taskLog').textContent = '';
      startPolling();
      const deadline = Date.now() + 15000;
      while (Date.now() < deadline && S.serveTaskId === data.task.id) {
        const detail = await api('/api/tasks/' + data.task.id);
        if (detail.task.preview_url) {
          const link = document.createElement('a');
          link.href = detail.task.preview_url; link.target = '_blank'; link.rel = 'noopener noreferrer';
          link.textContent = '打开知识库预览'; link.className = 'btn btn-primary';
          $('#serveHint').replaceChildren(link);
          return;
        }
        if (['failed', 'cancelled', 'succeeded'].includes(detail.task.status)) {
          S.serveTaskId = ''; $('#btnServeStop').hidden = true;
          throw new Error(detail.task.error || '预览进程已结束，请到任务中心查看日志');
        }
        await new Promise((resolve) => setTimeout(resolve, 300));
      }
      $('#serveHint').textContent = '服务尚未就绪，请在任务中心查看日志，或停止后重试。';
    } catch (err) {
      banner('启动预览失败：' + err.message, 'err');
    } finally { $('#btnServe').disabled = false; }
  });

  $('#btnServeStop').addEventListener('click', async () => {
    if (!S.serveTaskId) return;
    try {
      await api('/api/tasks/' + S.serveTaskId + '/cancel', { method: 'POST', body: {} });
      S.serveTaskId = '';
      $('#btnServeStop').hidden = true;
      $('#serveHint').textContent = '预览服务已停止。';
    } catch (err) { banner('停止失败：' + err.message, 'err'); }
  });

  $('#btnOpenWorkspace').addEventListener('click', async () => {
    const ws = (S.status && (S.status.workspace_config || S.status.workspace)) || {};
    if (!ws.workspace_root) { banner('尚未设置知识库目录。', 'warn'); return; }
    try { await api('/api/reveal', { method: 'POST', body: { path: ws.workspace_root } }); }
    catch (err) { banner('打开失败：' + err.message, 'err'); }
  });

  async function publishCheck() {
    try {
      const data = await api('/api/publish/check');
      $('#publishReport').innerHTML = (data.checks || []).map((c) => '<p class="' + (c.ok ? 'ok' : 'warn') + '">' +
        (c.ok ? '✓ ' : '待完成：') + esc(c.label) + '</p>').join('') +
        (data.findings || []).map((f) => '<p>' + esc(f.path + '：' + f.reason) + '</p>').join('') + '<p class="hint">' + esc(data.notice) + '</p>';
      if ($('input[name=agentKind]:checked').value === 'publish') $('#verifyResult').textContent = data.ready ? '发布准备检查通过，仍需授权 Agent 发布。' : '发布准备尚未完成，请到资料库查看检查项。';
    } catch (err) { banner('发布检查失败：' + err.message, 'err'); }
  }
  $('#btnPublishCheck').addEventListener('click', () => withBusyButton($('#btnPublishCheck'), '检查中…', publishCheck));
  $('#btnPublishInstruction').addEventListener('click', () => {
    $('input[name=agentKind][value=publish]').checked = true;
    updateAgentScope(); invalidateInstruction(); showView('agent'); makeInstruction();
  });

  function updateContext() {
    const c = S.courses.find((x) => x.course_id === S.courseId) || {}, l = S.lectures.find((x) => x.sub_id === S.subId) || {};
    $('#currentContext').textContent = S.courseId ? '当前课程：' + (c.title || S.courseId) + (S.subId ? ' · ' + (l.sub_title || S.subId) : ' · 待选课次') : '先配置保存目录，再选择课程与课次';
    $('#lectureScope').textContent = S.subId ? '采集和导入对象：' + (c.title || S.courseId) + ' · ' + (l.sub_title || S.subId) : '请先选择课次。';
  }
  $('#btnNextStep').addEventListener('click', () => {
    const ws = S.status && S.status.workspace_config;
    showView(!S.status || !S.status.credentials.configured || !ws || !ws.workspace_root ? 'settings' : 'courses');
  });
  $('#btnRecheck').addEventListener('click', () => withBusyButton($('#btnRecheck'), '检查中…', loadStatus));
  api('/api/features').then((data) => {
    $('#featureCoverage').innerHTML = (data.features || []).map((f) => '<button type="button" class="coverage-entry" data-target="' + esc(f.view) + '"><strong>' + esc(f.command + ' ' + f.title) + '</strong><span>' + esc(f.mode + ' · ' + f.detail) + '</span></button>').join('');
    $$('#featureCoverage button').forEach((b) => b.addEventListener('click', () => { $('#helpDialog').close(); showView(b.dataset.target); }));
  }).catch(() => { $('#featureCoverage').textContent = '功能目录读取失败，请刷新页面。'; });

  /* ---------------- boot ---------------- */

  readToken();
  renderAgentTarget();
  // The server also accepts its HttpOnly session cookie after a page refresh.

  loadStatus();
  fillSettings();
  refreshTasks();
  if (location.hash) showView(location.hash.slice(1));
  setInterval(() => { if ($('.view[data-view=home]').classList.contains('is-active')) loadStatus(); }, 20000);
})();
