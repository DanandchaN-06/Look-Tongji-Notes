import { useEffect, useMemo, useRef, useState } from 'react';
import { createWorkflow, getCourses, getLectureArtifacts, getLectures, syncCourses } from '../api/client';
import type { ArtifactItem, Course, CourseList, Lecture, LectureArtifacts, LectureList } from '../api/client';

function selectionFromHash() {
  const params = new URLSearchParams(window.location.hash.replace(/^#/, ''));
  return { courseId: params.get('course') || '', lectureId: params.get('lecture') || '', source: params.get('source') || '', query: params.get('q') || '' };
}

function saveSelection(courseId: string, lectureId: string, source: string, query: string) {
  const params = new URLSearchParams();
  if (source !== 'local') params.set('source', source);
  if (query) params.set('q', query);
  if (courseId) params.set('course', courseId);
  if (lectureId) params.set('lecture', lectureId);
  window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}${params.size ? `#${params}` : ''}`);
}

function dateLabel(value: string | null) {
  return value || '日期未提供';
}

function ArtifactStatus({ item }: { item: ArtifactItem }) {
  const status = {
    available: ['可用', 'status-good'],
    expired: ['已失效', 'status-warn'],
    not_provided: ['平台未提供', 'status-neutral'],
    not_collected: ['未采集', 'status-neutral'],
  }[item.state];
  return <li className="artifact-row"><span><strong>{item.label}</strong><small>{item.message}</small></span><span className={`status-pill ${status[1]}`}>{status[0]}</span></li>;
}

export function CoursesPage({ onWorkflowCreated, onOpenSettings }: { onWorkflowCreated: (workflowId: string) => void; onOpenSettings: () => void }) {
  const initial = selectionFromHash();
  const [source, setSource] = useState(initial.source || (initial.courseId ? 'all' : 'local'));
  const [query, setQuery] = useState(initial.query);
  const [courseData, setCourseData] = useState<CourseList | null>(null);
  const [lectureData, setLectureData] = useState<LectureList | null>(null);
  const [artifacts, setArtifacts] = useState<LectureArtifacts | null>(null);
  const [selectedCourseId, setSelectedCourseId] = useState(initial.courseId);
  const [selectedLectureId, setSelectedLectureId] = useState(initial.lectureId);
  const [loadingCourses, setLoadingCourses] = useState(true);
  const [loadingLectures, setLoadingLectures] = useState(false);
  const [loadingArtifacts, setLoadingArtifacts] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [creatingOperation, setCreatingOperation] = useState('');
  const coursesSequence = useRef(0);
  const lecturesSequence = useRef(0);
  const artifactsSequence = useRef(0);
  const pendingSubmissions = useRef(new Map<string, string>());

  useEffect(() => {
    const sequence = ++coursesSequence.current;
    const timer = window.setTimeout(() => {
      setLoadingCourses(true);
      setError('');
      void getCourses(source, query).then((result) => {
        if (sequence !== coursesSequence.current) return;
        setCourseData(result);
        if (selectedCourseId && !result.courses.some((course) => course.id === selectedCourseId) && !window.location.hash) {
          setSelectedCourseId('');
        }
      }).catch((cause) => {
        if (sequence === coursesSequence.current) setError(cause instanceof Error ? cause.message : '读取课程失败。');
      }).finally(() => {
        if (sequence === coursesSequence.current) setLoadingCourses(false);
      });
    }, 120);
    return () => window.clearTimeout(timer);
  }, [source, query]);

  useEffect(() => {
    const sequence = ++lecturesSequence.current;
    if (!selectedCourseId) {
      setLectureData(null);
      setArtifacts(null);
      setLoadingLectures(false);
      return;
    }
    setLoadingLectures(true);
    setLectureData(null);
    setArtifacts(null);
    void getLectures(selectedCourseId).then((result) => {
      if (sequence !== lecturesSequence.current) return;
      setLectureData(result);
      if (selectedLectureId && !result.lectures.some((lecture) => lecture.id === selectedLectureId)) {
        setSelectedLectureId('');
        saveSelection(selectedCourseId, '', source, query);
      }
    }).catch((cause) => {
      if (sequence === lecturesSequence.current) setError(cause instanceof Error ? cause.message : '读取课次失败。');
    }).finally(() => {
      if (sequence === lecturesSequence.current) setLoadingLectures(false);
    });
  }, [selectedCourseId]);

  useEffect(() => {
    const sequence = ++artifactsSequence.current;
    if (!selectedCourseId || !selectedLectureId || !lectureData?.lectures.some((lecture) => lecture.id === selectedLectureId)) {
      setArtifacts(null);
      return;
    }
    setLoadingArtifacts(true);
    void getLectureArtifacts(selectedCourseId, selectedLectureId).then((result) => {
      if (sequence === artifactsSequence.current) setArtifacts(result);
    }).catch((cause) => {
      if (sequence === artifactsSequence.current) setError(cause instanceof Error ? cause.message : '读取资料状态失败。');
    }).finally(() => {
      if (sequence === artifactsSequence.current) setLoadingArtifacts(false);
    });
  }, [selectedCourseId, selectedLectureId, lectureData]);

  const selectedCourse = useMemo(() => courseData?.courses.find((course) => course.id === selectedCourseId) || lectureData?.course || null, [courseData, lectureData, selectedCourseId]);
  const selectedLecture = lectureData?.lectures.find((lecture) => lecture.id === selectedLectureId) || null;

  function chooseCourse(course: Course) {
    setSelectedCourseId(course.id);
    setSelectedLectureId('');
    setError('');
    saveSelection(course.id, '', source, query);
  }

  function chooseLecture(lecture: Lecture) {
    setSelectedLectureId(lecture.id);
    setError('');
    saveSelection(lecture.course_id, lecture.id, source, query);
  }

  async function synchronize() {
    setSyncing(true);
    setError('');
    setMessage('正在从平台同步课程与课次…');
    try {
      const result = await syncCourses();
      setMessage(`同步完成：${result.sync_job.course_count} 门课程，${result.sync_job.lecture_count} 个课次。`);
      const courses = await getCourses(source, query);
      setCourseData(courses);
    } catch (cause) {
      setMessage('');
      setError(cause instanceof Error ? cause.message : '课程同步失败；已有缓存仍可浏览。');
    } finally {
      setSyncing(false);
    }
  }

  async function startWorkflow(operation: 'transcribe' | 'slide' | 'agent_note') {
    if (!selectedLecture || !selectedCourse) return;
    const mapKey = `${selectedLecture.id}:${operation}`;
    let idempotencyKey = pendingSubmissions.current.get(mapKey);
    if (!idempotencyKey) {
      idempotencyKey = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
      pendingSubmissions.current.set(mapKey, idempotencyKey);
    }
    setCreatingOperation(operation);
    setError('');
    setMessage(operation === 'agent_note' ? '正在准备 Agent 交接…' : '正在创建单课次任务…');
    try {
      const workflow = await createWorkflow(selectedLecture.id, operation, idempotencyKey);
      pendingSubmissions.current.delete(mapKey);
      setMessage('任务已创建，正在打开任务中心…');
      onWorkflowCreated(workflow.id);
    } catch (cause) {
      setMessage('');
      setError(cause instanceof Error ? cause.message : '创建任务失败。');
      pendingSubmissions.current.delete(mapKey);
    } finally {
      setCreatingOperation('');
    }
  }

  return <section className="courses-page" aria-label="我的课程">
    <div className="courses-heading">
      <div><p className="eyebrow">COURSES</p><h1>我的课程</h1><p className="page-intro">选择课程和明确课次，查看已保存资料。</p></div>
      <button className="button button-primary" onClick={() => void synchronize()} disabled={syncing}>{syncing ? '同步中…' : '同步平台课程'}</button>
    </div>
    {error && <div className="notice notice-error" role="alert">{error}</div>}
    {message && <div className="notice notice-success" role="status">{message}</div>}
    {courseData?.sync_status && <p className="sync-caption">上次平台同步：{courseData.sync_status.last_success_at || '尚未成功'}{courseData.sync_status.state === 'failed' ? ' · 最近一次同步失败，当前仍显示已缓存课程。' : ''}</p>}
    <div className="course-workspace">
      <section className="course-panel" aria-label="课程列表">
        <div className="course-tools">
          <label className="sr-only" htmlFor="course-search">搜索课程</label>
          <input id="course-search" type="search" value={query} onChange={(event) => { setQuery(event.target.value); saveSelection(selectedCourseId, selectedLectureId, source, event.target.value); }} placeholder="搜索课程或教师" />
          <label className="sr-only" htmlFor="course-source">课程来源</label>
          <select id="course-source" value={source} onChange={(event) => { setSource(event.target.value); saveSelection(selectedCourseId, selectedLectureId, event.target.value, query); }}><option value="local">本地资料</option><option value="platform">平台课程</option><option value="all">全部来源</option></select>
        </div>
        <div className="course-list-heading"><strong>课程</strong><span>{courseData?.courses.length ?? 0}</span></div>
        {loadingCourses && <p className="list-hint" role="status">正在读取课程…</p>}
        {!loadingCourses && courseData?.courses.length === 0 && <div className="list-hint">{query || source !== 'local' ? '没有匹配的课程。可尝试同步平台课程。' : <><p>当前没有已索引的本地课程。</p><p>如果资料目录中已有课程文件，请先检查迁移预览。</p><button className="text-button" type="button" onClick={onOpenSettings}>打开设置中的资料迁移</button></>}</div>}
        <ul className="course-list">{courseData?.courses.map((course) => <li key={course.id}>
          <button className={`course-choice ${selectedCourseId === course.id ? 'selected' : ''}`} onClick={() => chooseCourse(course)} aria-pressed={selectedCourseId === course.id}>
            <strong>{course.title}</strong><span>{course.teacher || '教师信息未提供'}</span><small>{course.lecture_count} 个课次 · {course.source === 'platform' ? '平台' : '本地资料'}</small>
          </button>
        </li>)}</ul>
      </section>
      <section className="detail-panel" aria-label="课程和课次详情">
        {!selectedCourse && <div className="detail-empty"><span className="detail-mark">01</span><h2>先选择一门课程</h2><p>本地资料默认优先显示；同步平台课程不会覆盖已有资料。</p></div>}
        {selectedCourse && <>
          <div className="detail-title"><div><p className="eyebrow">COURSE</p><h2>{selectedCourse.title}</h2><p>{[selectedCourse.teacher, selectedCourse.term].filter(Boolean).join(' · ') || '教师或学期信息未提供'}</p></div><button className="text-button" onClick={() => { setSelectedCourseId(''); setSelectedLectureId(''); saveSelection('', '', source, query); }}>返回课程</button></div>
          <div className="lecture-layout">
            <div className="lecture-list-panel"><div className="course-list-heading"><strong>课次</strong><span>{lectureData?.lectures.length ?? 0}</span></div>
              {loadingLectures && <p className="list-hint" role="status">正在读取课次…</p>}
              {!loadingLectures && lectureData?.lectures.length === 0 && <p className="list-hint">这门课程还没有可显示的课次。</p>}
              <ul className="lecture-list">{lectureData?.lectures.map((lecture) => <li key={lecture.id}><button className={`lecture-choice ${selectedLectureId === lecture.id ? 'selected' : ''}`} onClick={() => chooseLecture(lecture)} aria-pressed={selectedLectureId === lecture.id}><small>{dateLabel(lecture.class_date)}</small><strong>{lecture.title}</strong><span>{lecture.platform_sub_id ? `课次 ID：${lecture.platform_sub_id}` : `本地课次 · ${lecture.id.slice(-8)}`}</span></button></li>)}</ul>
            </div>
            <div className="lecture-detail">
              {!selectedLecture && <div className="detail-empty compact"><h3>选择一个课次</h3><p>课次按上课日期排列，选择后显示该课次的资料状态。</p></div>}
              {selectedLecture && <><div className="lecture-detail-heading"><p className="eyebrow">LECTURE</p><h3>{selectedLecture.title}</h3><p>{dateLabel(selectedLecture.class_date)}{selectedLecture.platform_sub_id ? ` · ID ${selectedLecture.platform_sub_id}` : ''}</p></div>
                <h4>已有资料</h4>{loadingArtifacts && <p className="list-hint" role="status">正在检查资料…</p>}
                <ul className="artifact-list">{artifacts?.items.map((item) => <ArtifactStatus key={item.kind} item={item} />)}</ul>
                {artifacts?.notes_preview && <details className="notes-preview"><summary>预览学习笔记</summary><pre>{artifacts.notes_preview}</pre></details>}
                {artifacts && <p className="artifact-source">来源：{artifacts.source}</p>}
                <div className="lecture-actions" aria-label="开始采集">
                  <button className="button button-primary button-compact" onClick={() => void startWorkflow('transcribe')} disabled={Boolean(creatingOperation) || !selectedCourse.platform_course_id || !selectedLecture.platform_sub_id || !selectedLecture.playback_available}>{creatingOperation === 'transcribe' ? '创建中…' : '只取字幕'}</button>
                  <button className="button button-secondary button-compact" onClick={() => void startWorkflow('slide')} disabled={Boolean(creatingOperation) || !selectedCourse.platform_course_id || !selectedLecture.platform_sub_id || !selectedLecture.playback_available}>{creatingOperation === 'slide' ? '创建中…' : '只取课件'}</button>
                  <button className="button button-secondary button-compact" onClick={() => void startWorkflow('agent_note')} disabled={Boolean(creatingOperation) || !selectedCourse.platform_course_id || !selectedLecture.platform_sub_id || artifacts?.items.find((item) => item.kind === 'transcript')?.state !== 'available'}>{creatingOperation === 'agent_note' ? '准备中…' : '交给 Agent 写笔记'}</button>
                </div>
              </>}
            </div>
          </div>
        </>}
      </section>
    </div>
  </section>;
}
