# 控制台 V2 本地交付说明

**交付标识：** TJLOOK 控制台 V2 本地重构快照（2026-10-12）  
**交付类型：** 脱敏源码 ZIP；不是 Windows 便携运行包，也没有发布到 GitHub。

## 下载、解压与运行源码

1. 将 ZIP 完整解压到一个有写入权限的文件夹，不要在压缩包内运行。
2. 安装 Python 3.11 或更新版本，并在解压目录打开终端。
3. 创建虚拟环境并安装项目依赖：

   ```powershell
   py -3.11 -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   .venv\Scripts\python -m playwright install chromium
   ```

4. 按 `console/README.md` 配置 CLI 所需账号和资料目录，再运行 `python console/start.py --v2` 打开 V2 管理器。重建前端才需要 Node.js；ZIP 已包含构建后的 V2 页面资源。

源码包不含 Python、Chromium、FFmpeg、账号、登录会话、课程资料或个人配置。字幕转写需要另行安装 FFmpeg；云端识别与课程平台仍受其实际服务状态和账号权限影响。Agent 写作由用户选择的 Agent 执行。

已安装的个人应用使用既有唯一桌面入口打开 V2 管理器和原阅读页，不需要用源码 ZIP 覆盖应用目录。个人安装的运行时、浏览器、配置、账号和资料均保存在现有位置。

## 已完成的验收

- 本地全量后端套件：194 项通过；独立浏览器验收：36 项通过；前端 TypeScript 检查及 Vite 构建通过。`python console/start.py --v2 --no-window --print-url` 真实启动后返回 V2 页面（HTTP 200）。
- 实际本机重启后双次启动复用同一管理器；V2 首页、课程列表、课次与素材接口正常。
- 课程列表按各课程最新的有日期课次倒序；没有日期的课程排在后面，同日期时按课程名称稳定排序。
- 每门课程内部的课次按实际 `class_date` 从早到晚固定排序；支持未补零日期（如 `2026-9-5`），无效或缺失日期排在末尾。课程列表的最近课次排序复用相同日期解析规则。
- 任务完成校验和课次成果详情在本地数据库尚未索引某课次时，会按课程/课次身份在资料目录中查找唯一 manifest，并验证字幕哈希/SRT 或课件索引；重复身份清单拒绝自动认领，避免误判或认领错课次。
- 已保留账号配置并迁移 2 门课程、4 个课次，其中数据结构课程 3 个课次；三个课次的素材接口均返回已有资料。
- 原阅读页通过管理器和独立阅读服务均可访问；管理器返回的首页字节与配置所指资料库中的 `site/index.html` 一致。
- V2 SQLite 完整性检查为 `ok`，结构版本为 5。真实配置、缓存、运行时、浏览器和原资料未随程序文件升级覆盖。

复验命令（需要已安装的 Python 依赖、Playwright 浏览器，以及前端开发依赖）：

```powershell
python -m unittest discover -s console -p 'test_*.py'
python -m unittest console.browser_smoke
npm --prefix console/frontend run build
git diff --check
```

## 验收边界

自动化测试使用临时数据库、模拟课程和离线替身。没有在本次最终验收中真实登录同济平台、下载课程媒体、调用 BcutASR、提交视觉请求或让 Agent 撰写内容。Whisper 替代识别仍不可用。素材接口返回资料只证明文件记录可读取，不证明笔记内容、时间轴或排版正确。

源码 ZIP 不携带便携运行环境，不能声称在未安装 Python 和依赖的全新 Windows 设备上开箱即用。没有执行 GitHub Release、Issue、PR 或课程资料发布。

分发检查使用显式仓库文件清单生成源码 ZIP，不含 `.git`、个人交接资料、仓库本地指令、CI 配置、`.env`、`state/`、浏览器/运行时和课程资料。打包扫描未发现本机路径、签名媒体链接或实际密钥；唯一命中是测试用的 `synthetic-server-only-key` 假值，已人工确认不是凭据。ZIP 旁的 `PACKAGE_MANIFEST.json` 记录每个文件的 SHA-256 与整包哈希，`SHA256SUMS.txt` 可单独校验整包。

## 更新回退

升级器只更新显式列出的程序文件；升级前在应用目录之外生成并校验回退副本。回退可用 `console.upgrade.restore_program_files(backup_dir, app_project_dir)` 恢复某次程序文件备份，恢复后从既有桌面入口重启。该操作不应把备份恢复到 `state/`、账号配置或课程资料目录。

保留原始阅读网页和资料库；如果某次回退后需要检查课程成果，可从控制台重新打开课程及课次，或用原阅读入口确认已构建的页面。
