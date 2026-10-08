# Look Tongji Notes 本地控制台

控制台 v0.4.4，为现有课程 CLI 提供本地图形界面。无需前端构建，不绑定特定 Agent。支持 Windows；其他系统的启动与界面代码使用标准 Python/浏览器接口，仍需对应平台验证。

## 安装与启动

要求 Python 3.11+。在项目根目录执行：

```bash
python -m venv .venv
# Windows
.venv\Scripts\python -m pip install -r console/requirements.txt
.venv\Scripts\python -m playwright install chromium
.venv\Scripts\python console/start.py
# macOS / Linux 使用 .venv/bin/python 执行同样的命令
```

Windows 可双击 `console/start.cmd`，或运行 `console/start.ps1`。启动器优先使用项目 `.venv`；需要复用其他环境时设置 `LOOK_TONGJI_PYTHON`，或在忽略的 `console/python.local` 中填写解释器完整路径。该本地配置不应分享。

若 Windows 默认禁止 `.ps1`，直接使用 CMD 入口；也可仅对这次启动运行 `powershell -NoProfile -ExecutionPolicy Bypass -File console/start.ps1`，无需更改全局执行策略。

还需要 ffmpeg 才能抽取音频；读取图片的桥接工具需要 Node.js 18+。制作 LaTeX 速查表需要 XeLaTeX；发布网站需要 gh CLI。缺少 tkinter 时仍可手动填写目录和材料路径。

启动仅监听 `127.0.0.1`，自动选择空闲端口。关闭浏览器后服务仍运行，在启动终端按 Ctrl+C 停止；会取消排队任务并停止子进程。仅支持浏览器作为显示窗口，不包含安装器或内置 Python。

```bash
python console/start.py --no-window --print-url
python console/start.py --port 8766
```

`--print-url` 额外输出机器可读的 `CONSOLE_URL=...`；启动日志中的访问地址包含本次会话令牌，不要公开分享。初次打开后地址栏移除令牌，HttpOnly 会话 Cookie 保持刷新可用。

## 使用流程

1. 在设置中心保存同济账号、知识库目录，以及需要时使用的视觉模型。
2. 获取课程，选择节次，采集字幕与课件，导入补充材料。
3. 在 Agent 工作台按课程名与节次名选择任务对象，核对教师、上课日期和范围。也可从课程页自动带入，或展开高级定位填写 ID。然后选择通用 Agent、Codex、Claude Code、Cursor、Gemini CLI 或 OpenCode。
4. 复制或保存指令，交给具有本地文件读写能力的 Agent 执行。该选择只改变交接说明，不会启动工具、发送消息或调用写作模型。
5. 回到控制台校验当前任务要求的文件，再构建知识库和启动本地预览。

笔记与时间轴由 Agent 撰写。CLI 成功只表示素材采集或构建命令完成。文件校验检查文件存在且非空，不证明内容准确、公式正确或 PDF 恰好一页；请自行复核。

速查表默认写到当前节次的 `原始数据/<course_id>_<sub_id>_cheatsheet.html` 或 `.tex`。LaTeX 任务还需生成同名 PDF。取消时间轴选项后，笔记校验不再要求时间轴。问答体笔记与标准笔记分别校验。

修改知识库的任务串行执行，预览服务独立运行。批量转写取消使用检查点；关闭控制台可能强制终止正在处理的节次，恢复运行时以批处理状态为准。

## 数据处理

- 账号和密钥保存于本机 `.env` 和 `vision-support/config.json`，API 不回传密码或密钥。
- 字幕转写使用哔哩哔哩 BcutASR，音频会上传至云端。启动转写前界面会明确提示。
- 视觉模型会接收所选图片；点击测试前请确认图片可以发送给该平台。
- 发布准备检查会重新检测站点、认证和明显私密文件，不代表完整安全审计。确认课程材料可以公开，且 `.env`、认证状态、API 密钥和私人材料不在站点中。

## 验证

```bash
python console/selftest.py
python -m unittest console.test_regressions console.test_workflow console.test_cli_adapter console.test_progress console.test_console_features console.test_transcriber_reuse console.test_media_tools -v
python -m unittest console.browser_smoke -v
node --check console/web/app.js
```

后两组 Python 测试在隔离目录、模拟课程及独立浏览器中运行，不需要账号、视觉密钥或云端 ASR。浏览器测试需要已经安装的 Chromium。`selftest.py` 也检查当前本机环境，其结果应结合依赖配置理解。

本地验证覆盖 67 项现有自检、后端回归、真实 CLI 的索引/构建/预览/关闭，以及六页面的四种窗口宽度。测试细节及外部服务验证范围见交付中的 `VALIDATION.md`；不能把离线测试等同于整个云端链路已验证。


## 向上游交付

建议先提交 issue 讨论本地 GUI 的范围，再将兼容 CLI 的修复与新增 `console/` 分开提交 PR。源码为主交付物，ZIP 用于试用，安装器可在维护者接受架构后另做。保留 MIT 许可证及原项目来源。

## v0.4.4 功能与入口

知识库就是本机的课程资料文件夹，raw 保存原始素材，courses 保存整理内容，site 保存阅读网页。可从首页解释和资料库页了解采集、写作、同步、构建、预览的关系。

任务显示阶段、排队位置、耗时和实际数量；未知云端识别进度不显示虚构百分比，字幕与课件分别显示。批量检查点按课程保存，并可选择重新尝试失败课次。

字幕生成流程中的 MB/GB 是录课源视频的下载量，不是字幕文件大小。当前并行下载方式先下载视频，再提取音频送去识别；完成后保存 TXT/SRT 文本。界面会明确标出视频下载与音频提取阶段。

单节纯字幕、回放链接定位、采集高级选项均有界面入口。速查表支持当前课次或整门课程，可指定资料库内输出；生成和校验使用同一文件路径。发布入口生成具体的 Agent 交接指令，实际上线需用户授权。

9 个原工作流均有对应入口，CLI 与技能保留；详见交付根目录 FEATURE_COVERAGE.md。文件检测不证明内容或排版质量。

原项目的阅读网页完整保留。空资料库也使用原版网页模板显示 0 门课程、0 个课次；采集后重新构建即可显示课程内容。GUI 控制台与阅读站点使用各自的本地入口。

## 重复采集与失败重试

默认复用同账号、同课次已完成且校验通过的 TXT/SRT；旧版本产物通过 ASR 分段内容核对，无法确认完整性时继续处理。云端识别失败会复用音频，不重新下载视频。音频和完整的待提取视频缓存位于项目 state/media/，不写入公开阅读站点。

采集高级选项中的“强制重新转写”会忽略已有字幕及音频，重新下载并识别。CLI 可用 transcribe/note --force-transcribe；批量命令仅影响待处理课次。相同课次的并发任务会等待锁，避免重复传输和覆盖结果。

运行中的子进程已加载旧代码；等当前任务结束后重启控制台，进度文案和强制选项才会完整更新。旧进程此前删除的临时素材无法由新缓存恢复。

## FFmpeg 可用性

环境检查会实际运行 ffmpeg -version。PATH 中的空文件或失效入口不会被视为就绪；Windows 会尝试已安装的 WinGet FFmpeg 程序。需要指定其他位置时，用 LOOK_TONGJI_FFMPEG 配置可执行文件的完整路径。

媒体下载前先检查工具，无法启动会立即停止并说明原因。分片断连时最多尝试三次，从当前分片已保存的字节续传；不支持规范 Range 的服务器才转用 FFmpeg 流式下载。

便携版的入口实现位于 `portable_launcher.py`，该文件供发行包组装为 `app/launcher.py` 使用；从源码运行请使用上文的 `start.py`。便携包依赖和校验信息随 Release 提供。
