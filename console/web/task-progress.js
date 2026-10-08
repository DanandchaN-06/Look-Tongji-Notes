/* Presentation of measured task progress. Unknown phases stay indeterminate. */
(function () {
  'use strict';
  const esc = (s) => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  function amount(value, unit) {
    if (unit !== '字节') return value + ' ' + unit;
    if (value >= 1048576) return (value / 1048576).toFixed(1) + ' MB';
    if (value >= 1024) return (value / 1024).toFixed(1) + ' KB';
    return value + ' 字节';
  }
  function elapsed(seconds) {
    const value = Math.max(0, Math.floor(seconds || 0));
    return value < 60 ? value + ' 秒' : Math.floor(value / 60) + ' 分 ' + value % 60 + ' 秒';
  }
  function render(task) {
    const p = task.progress || { label: '等待任务阶段信息', percent: null, tracks: [], elapsed_seconds: 0 };
    const host = document.querySelector('#taskProgress');
    host.hidden = false;
    host.dataset.state = task.status;
    document.querySelector('#taskStage').textContent = p.label;
    document.querySelector('#taskPercent').textContent = p.percent == null ? '阶段处理中' : p.percent + '%';
    const bar = document.querySelector('#taskProgressBar');
    if (p.percent == null) bar.removeAttribute('value');
    else bar.value = p.percent;
    document.querySelector('#taskElapsed').textContent = (task.status === 'queued' ? '已等待 ' : '已耗时 ') + elapsed(p.elapsed_seconds) +
      (task.queue_position ? ' · 队列第 ' + task.queue_position + ' 个' : '');
    document.querySelector('#taskTracks').innerHTML = (p.tracks || []).map((track) => {
      const media = track.unit === '字节' && (track.track === 'transcript' || ['字幕', '字幕生成流程'].includes(track.name));
      const name = media ? '字幕生成流程' : track.name;
      const label = media ? '下载录课视频（用于提取音频）' : track.label;
      const count = track.total ? (media ? ' · 已下载 ' : ' · 已处理 ') + amount(track.completed, track.unit) + ' / ' + amount(track.total, track.unit) : '';
      const failed = track.failed ? ' · 失败 ' + track.failed + ' ' + track.unit : '';
      return '<div class="progress-track"><span>' + esc(name + '：' + label + count + failed) + '</span>' +
        '<progress max="100" aria-label="' + esc(track.name + '阶段进度') + '"' + (track.percent == null ? '' : ' value="' + track.percent + '"') + '></progress></div>';
    }).join('');
    const hint = document.querySelector('#taskProgressHint');
    const downloadingMedia = (p.tracks || []).some((track) => track.unit === '字节' && (track.track === 'transcript' || ['字幕', '字幕生成流程'].includes(track.name)));
    hint.textContent = downloadingMedia ? '这里的大小是录课源视频下载量，不是字幕文件大小。下载完成后还需抽取音频并识别生成 TXT / SRT 字幕。' : task.status === 'running' && p.percent == null
      ? '当前阶段未提供可靠的总进度，显示实际步骤与耗时。可以切换页面，任务继续运行。'
      : task.kind === 'note' ? '这是素材采集进度。笔记正文需在 Agent 工作台交接后生成。' : '百分比按已处理数量计算；失败项会单独标出。';
  }
  window.ConsoleProgress = { render: render };
})();
