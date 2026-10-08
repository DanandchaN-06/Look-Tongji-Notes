"""Self-test for the console: boots the server in-process and exercises the API.

Run:  python console/selftest.py
Exit code 0 means every checked endpoint behaved as expected.
"""

from __future__ import annotations

import json
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

CONSOLE_DIR = Path(__file__).resolve().parent
if str(CONSOLE_DIR.parent) not in sys.path:
    sys.path.insert(0, str(CONSOLE_DIR.parent))

from console.server import make_server  # noqa: E402

FAILURES: list[str] = []
CHECKS = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(f"{name} {detail}")


def request(base: str, path: str, token: str, *, method: str = "GET",
            body: dict | None = None, headers: dict | None = None,
            omit_token: bool = False) -> tuple[int, object]:
    url = base + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if not omit_token:
        req.add_header("X-Console-Token", token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw


def main() -> int:
    token = secrets.token_urlsafe(24)
    httpd = make_server("127.0.0.1", 0, token)
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}"

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)

    print(f"\n控制台自检 (端口 {port})\n" + "-" * 52)

    print("\n[1] 页面与静态资源")
    status, body = request(base, f"/?token={token}", token)
    check("首页可访问", status == 200 and "课程助手" in str(body))
    status, body = request(base, "/app.css", token, omit_token=True)
    check("样式表可访问（免令牌）", status == 200 and "sidebar" in str(body))
    status, body = request(base, "/app.js", token, omit_token=True)
    check("脚本可访问（免令牌）", status == 200 and "function" in str(body))

    print("\n[2] 访问控制")
    status, _ = request(base, "/api/status", token, omit_token=True)
    check("无令牌访问 API 被拒绝", status == 401, f"得到 {status}")
    status, _ = request(base, "/api/status", token, headers={"X-Console-Token": "wrong"})
    check("错误令牌被拒绝", status == 401 or status == 403, f"得到 {status}")
    status, _ = request(base, f"/?token={token}", token, headers={"Host": "evil.example.com"})
    check("非本地 Host 被拒绝", status == 403, f"得到 {status}")
    status, _ = request(base, "/api/status", token, headers={"Origin": "http://evil.example.com"})
    check("非本地 Origin 被拒绝", status == 403, f"得到 {status}")
    status, _ = request(base, "/../scripts/look_tongji.py", token, omit_token=True)
    check("路径穿越未泄露文件", status in (403, 404), f"得到 {status}")

    print("\n[3] 状态与配置接口")
    status, data = request(base, "/api/status", token)
    check("status 返回 200", status == 200 and isinstance(data, dict), str(status))
    check("status 含依赖信息", isinstance(data, dict) and "missing_deps" in data)
    check("status 含工具探测", isinstance(data, dict) and "tools" in data)
    check("status 不含明文密码",
          "password" not in json.dumps(data, ensure_ascii=False).lower()
          or '"password_set"' in json.dumps(data).lower())

    status, data = request(base, "/api/config", token)
    check("config 返回 200", status == 200 and isinstance(data, dict))
    cred = (data or {}).get("credentials", {}) if isinstance(data, dict) else {}
    check("config 不返回密码明文", "TONGJI_PASSWORD" not in json.dumps(data, ensure_ascii=False))
    check("config 返回密码存在标记", "password_set" in cred)

    print("\n[4] 视觉模型配置校验")
    status, data = request(base, "/api/config/vision", token, method="POST",
                           body={"provider": "nope", "model": "x", "api_key": "k"})
    check("未知平台被拒绝", status == 400, f"得到 {status}")
    status, data = request(base, "/api/config/vision", token, method="POST",
                           body={"provider": "custom", "model": "m", "api_key": "k", "base_url": ""})
    check("自定义平台缺 baseUrl 被拒绝", status == 400, f"得到 {status}")
    status, data = request(base, "/api/config/vision", token, method="POST",
                           body={"provider": "openai", "model": "", "api_key": "k"})
    check("缺模型名被拒绝", status == 400, f"得到 {status}")

    print("\n[5] 任务参数白名单")
    status, data = request(base, "/api/tasks", token, method="POST",
                           body={"kind": "shell", "params": {"cmd": "whoami"}})
    check("未知任务类型被拒绝", status == 400, f"得到 {status}")
    status, data = request(base, "/api/tasks", token, method="POST",
                           body={"kind": "transcribe", "params": {}})
    check("缺课程/节次 ID 被拒绝", status == 400, f"得到 {status}")
    status, data = request(base, "/api/tasks", token, method="POST",
                           body={"kind": "note", "params": {"course_id": "1", "sub_id": "2",
                                                            "note_style": "hack"}})
    check("非法笔记风格被拒绝", status == 400, f"得到 {status}")

    print("\n[6] 参数构造（不执行）")
    from console.runner import build_argv
    argv = build_argv("note", {"course_id": "101", "sub_id": "202", "note_style": "dialogue"})
    check("note 参数不含密码类字段", all("password" not in a.lower() for a in argv), str(argv))
    check("note 使用非交互模式", "--no-workspace-prompt" in argv and "--no-material-prompt" in argv)
    argv = build_argv("batch", {"course_id": "101", "max_retries": 999})
    check("批量重试次数被限幅", "10" in argv, str(argv))

    print("\n[7] 产物检测与指令生成")
    status, data = request(base, "/api/artifacts?course_id=101&sub_id=202", token)
    check("artifacts 返回 200", status == 200 and "artifacts" in (data or {}))
    status, data = request(
        base, "/api/agent/instruction?kind=note&course_id=101&sub_id=202&note_style=standard", token)
    check("指令接口返回 200", status == 200 and isinstance(data, dict))
    instruction = (data or {}).get("instruction", "")
    check("缺素材时给出警告", bool((data or {}).get("warnings")))
    check("指令不含密码字段", "password" not in instruction.lower())

    print("\n[8] 任务生命周期（构建任务，可快速结束）")
    status, data = request(base, "/api/tasks", token, method="POST",
                           body={"kind": "index", "params": {}})
    if status == 200:
        task_id = data["task"]["id"]
        check("任务已创建", bool(task_id))
        deadline = time.time() + 120
        final = None
        while time.time() < deadline:
            status, detail = request(base, f"/api/tasks/{task_id}", token)
            final = (detail or {}).get("task", {})
            if final.get("status") in ("succeeded", "failed", "cancelled"):
                break
            time.sleep(1.5)
        check("任务进入终态", final is not None and final.get("status") in
              ("succeeded", "failed", "cancelled"), str(final and final.get("status")))
        status, log = request(base, f"/api/tasks/{task_id}/log?offset=0", token)
        text = "\n".join(log.get("lines", [])) if isinstance(log, dict) else ""
        check("日志可读取", isinstance(log, dict) and "lines" in log)
        check("日志响应含截断标记", isinstance(log, dict) and "truncated" in log
              and "dropped" in log, str(sorted(log.keys())) if isinstance(log, dict) else "")
        check("日志无明文凭据",
              "TONGJI_PASSWORD" not in text and "password=" not in text.lower())
        check("日志已屏蔽令牌片段", "Token: <redacted>" in text or "token:" not in text.lower()
              or "<redacted>" in text)
    else:
        check("任务已创建", False, f"HTTP {status} {data}")

    print("\n[9] Agent 指令路径正确性")
    from console import agent_bridge
    from console.core import REPO_ROOT
    fake = {
        "lecture_dir": "C:/ws/raw/高等数学 上/第 3 讲/原始数据",
        "has_transcript": True,
        "has_slides": True,
        "transcript_txt": "C:/ws/raw/高等数学 上/第 3 讲/原始数据/1_2.txt",
        "transcript_srt": "C:/ws/raw/高等数学 上/第 3 讲/原始数据/1_2.srt",
        "slides_dir": "C:/ws/raw/高等数学 上/第 3 讲/原始数据/slides",
        "materials": ["C:/ws/raw/高等数学 上/第 3 讲/原始数据/materials/讲义"],
        "notes": "C:/ws/raw/高等数学 上/第 3 讲/原始数据/1_2_notes.md",
        "has_notes": True,
    }
    instr = agent_bridge.build_agent_instruction(
        kind="note", course_id="1", sub_id="2", note_style="standard", artifacts=fake
    )["instruction"]
    repo_display = str(REPO_ROOT).replace("\\", "/")
    check("指令给出的是仓库根目录作为项目", repo_display in instr, instr[:120])
    check("时间轴校验脚本指向仓库 scripts/", f"{repo_display}/scripts/timeline_tools.py" in instr)
    check("输出文件位于知识库而非仓库", "C:/ws/raw/高等数学 上/第 3 讲/原始数据/1_2_notes.md" in instr)
    check("路径分隔符已统一（无反斜杠）", "\\" not in instr)
    check("中文与空格路径原样保留", "高等数学 上" in instr)

    sheet = agent_bridge.build_agent_instruction(
        kind="cheatsheet", course_id="1", sub_id="2", artifacts=fake
    )["instruction"]
    check("速查表模板指向仓库", f"{repo_display}/CheatingSheetTemplate/CheatingSheet.tex" in sheet)
    check("速查表模板路径未指向知识库", "C:/ws/CheatingSheetTemplate" not in sheet)

    wiki = agent_bridge.build_agent_instruction(kind="wiki", course_id="1", sub_id="2",
                                                artifacts=fake)["instruction"]
    check("知识库指令使用仓库内 CLI", f'{repo_display}/scripts/look_tongji.py' in wiki)

    print("\n[10] HTTP keep-alive 上的请求体排空")
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        payload_body = json.dumps({"kind": "index", "params": {}})
        conn.request("POST", "/api/tasks", body=payload_body, headers={
            "Content-Type": "application/json", "X-Console-Token": "wrong-token"})
        first = conn.getresponse()
        first.read()
        check("未授权 POST 被拒绝", first.status == 401, f"得到 {first.status}")
        # The same TCP connection must still be usable: if the rejected body had
        # been left unread, this request would be parsed from the wrong offset.
        conn.request("GET", "/api/tasks", headers={"X-Console-Token": token})
        second = conn.getresponse()
        raw_second = second.read().decode("utf-8")
        check("同连接后续请求未被错位", second.status == 200 and '"ok": true' in raw_second,
              f"得到 {second.status} {raw_second[:120]}")
    finally:
        conn.close()

    print("\n[11] 补充材料（/add）参数构造")
    argv = build_argv("add", {"course_id": "1", "sub_id": "2",
                              "materials": ["讲义=C:/a/b.pdf", "C:/c/d.docx"]})
    check("add 带 --material", argv.count("--material") == 2, str(argv))
    check("材料名与路径成对保留", "讲义=C:/a/b.pdf" in argv and "C:/c/d.docx" in argv)
    check("add 为非交互模式", "--no-material-prompt" in argv)

    print("\n[12] manifest 优先的产物识别")
    import os
    import shutil as _shutil
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="ctl-ws-"))
    raw = tmp / "raw" / "高等数学" / "第 1 讲" / "原始数据"
    (raw / "slides").mkdir(parents=True)
    (raw / "slides" / "001.png").write_bytes(b"x")
    (raw / "777_888.txt").write_text("字幕", encoding="utf-8")
    # Decoy: a teacher handout must never be mistaken for the transcript.
    (raw / "教师_讲义.txt").write_text("诱饵", encoding="utf-8")
    (raw / "777_888_timeline.txt").write_text("00:00-01:00：x", encoding="utf-8")
    (raw / "manifest.json").write_text(json.dumps({
        "course_id": "777", "sub_id": "888", "base_name": "777_888",
        "artifacts": {"transcript_txt": str(raw / "777_888.txt"),
                      "slides_dir": str(raw / "slides")},
    }, ensure_ascii=False), encoding="utf-8")
    cfg = tmp / "config.json"
    cfg.write_text(json.dumps({"workspace_root": str(tmp), "owner_name": "t",
                               "site_name": "t"}), encoding="utf-8")
    previous_config = os.environ.get("LOOK_TONGJI_CONFIG_PATH")
    os.environ["LOOK_TONGJI_CONFIG_PATH"] = str(cfg)
    try:
        a = agent_bridge.detect_artifacts("777", "888")
        check("按 manifest 中的 id 找到节次", a["lecture_dir"] == str(raw), a["lecture_dir"])
        check("base_name 取自 manifest", a["base_name"] == "777_888", a["base_name"])
        check("字幕取自 manifest 记录", a["transcript_txt"] == str(raw / "777_888.txt"))
        check("诱饵文件未被误判为字幕", "教师_讲义" not in a["transcript_txt"])
        check("截图数量正确", a["slide_count"] == 1, str(a["slide_count"]))
        check("时间轴被识别", a["has_timeline"] is True)
    finally:
        if previous_config is None:
            os.environ.pop("LOOK_TONGJI_CONFIG_PATH", None)
        else:
            os.environ["LOOK_TONGJI_CONFIG_PATH"] = previous_config
        _shutil.rmtree(tmp, ignore_errors=True)

    print("\n[13] 启动器端口选择")
    from console import start as starter
    import socket as _socket
    with _socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        busy = holder.getsockname()[1]
        holder.listen(1)
        chosen = starter.pick_port(busy)
    check("占用端口会被跳过", chosen != busy, f"{busy} -> {chosen}")

    print("\n[14] 启动器不得使用 os.execve")
    # os.execve is emulated on Windows and crashed the launcher with a
    # segmentation fault. Switching interpreters must go through a child
    # process instead. This guard exists because the broken path is only
    # reachable when the entry interpreter is NOT the configured one, so it is
    # easy to reintroduce without noticing.
    start_src = (CONSOLE_DIR / "start.py").read_text(encoding="utf-8")
    # Match a real call (`os.execve(`), not the explanatory mention in the
    # docstring of why it must not be used.
    check("未调用 os.execve", "os.execve(" not in start_src)
    check("改用子进程方式切换解释器", "subprocess.run([path, here" in start_src)
    check("保留防重入护栏", "LOOK_TONGJI_CONSOLE_REEXEC" in start_src)

    print("\n[15] 原生路径选择框")
    from console import native as _native
    picker_ok = _native.picker_available()
    check("选择框可用性探测返回布尔值", isinstance(picker_ok, bool),
          str(_native._candidates()))
    picked_python = _native.find_picker_python()
    check("选择框解释器路径与探测一致", bool(picked_python) == picker_ok,
          f"{picked_python}")
    check("解释器探测结果被缓存", _native.find_picker_python() == picked_python)

    # Dry-run mode opens no window, so this exercises the whole endpoint path
    # (helper process, JSON contract, error mapping) without a modal dialog.
    os.environ["LOOK_TONGJI_CONSOLE_NO_DIALOG"] = "1"
    try:
        status, data = request(base, "/api/pick", token, method="POST",
                               body={"mode": "dir", "title": "t"})
        check("目录选择接口正确响应环境", (status == 200 and data.get("ok") is True) if picker_ok else status == 400, f"{status} {data}")
        check("取消或环境缺失有明确反馈", (data or {}).get("cancelled") is True if picker_ok else bool(data.get("error")), str(data))

        status, data = request(base, "/api/pick", token, method="POST",
                               body={"mode": "files", "title": "t"})
        check("文件选择接口正确响应环境", (status == 200 and data.get("ok") is True) if picker_ok else status == 400, f"{status} {data}")

        status, data = request(base, "/api/pick", token, method="POST",
                               body={"mode": "everything"})
        check("非法 mode 被拒绝", status == 400, f"得到 {status}")

        status, data = request(base, "/api/status", token)
        console_info = (data or {}).get("console") or {}
        check("status 暴露选择框可用性",
              isinstance(console_info.get("picker_available"), bool), str(console_info))
        check("status 报告所用解释器", console_info.get("picker_python") == (picked_python or ""))
    finally:
        os.environ.pop("LOOK_TONGJI_CONSOLE_NO_DIALOG", None)

    print("\n[16] 未知路由")
    status, _ = request(base, "/api/does-not-exist", token)
    check("未知接口返回 404", status == 404, f"得到 {status}")

    httpd.shutdown()
    httpd.server_close()

    print("\n" + "-" * 52)
    if FAILURES:
        print(f"失败 {len(FAILURES)} / 共 {CHECKS} 项：")
        for item in FAILURES:
            print("  - " + item)
        return 1
    print(f"全部通过（{CHECKS} 项检查）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
