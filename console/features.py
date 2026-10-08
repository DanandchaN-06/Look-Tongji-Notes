"""Public upstream workflow coverage; GUI and Agent responsibilities are explicit."""
UPSTREAM_COMMIT = "ef5c76fcde03b48d5bc1d74abb26779ce72dfafa"
FEATURES = [
    {"command": "/setup", "title": "账号、保存目录与模型设置", "mode": "界面操作", "view": "settings", "detail": "保存配置、检查依赖；依赖安装按说明完成。"},
    {"command": "/list", "title": "选课与课次定位", "mode": "界面操作", "view": "courses", "detail": "最近课程、全量搜索、录课链接、重新登录。"},
    {"command": "/trans", "title": "单节字幕转写", "mode": "界面操作", "view": "courses", "detail": "纯字幕或字幕与课件；自动登记到资料库。"},
    {"command": "/note", "title": "学习笔记与时间轴", "mode": "界面采集＋Agent 写作", "view": "agent", "detail": "标准或问答体笔记、可选时间轴、完成后校验与阅读。"},
    {"command": "/add", "title": "补充材料导入", "mode": "界面操作", "view": "courses", "detail": "选择多个讲义文件、命名、转换并查看实际素材。"},
    {"command": "/wiki", "title": "整理资料库并生成阅读页", "mode": "界面操作", "view": "wiki", "detail": "同步内容、生成网页、本机预览及打开保存目录。"},
    {"command": "/cheatsheet", "title": "A4 复习速查表", "mode": "Agent 写作", "view": "agent", "detail": "当前课次或整门课；HTML/LaTeX，环境检查及输出校验。"},
    {"command": "/page", "title": "发布到 GitHub Pages", "mode": "Agent 发布交接", "view": "wiki", "detail": "检查发布准备、生成目标明确的交接指令，授权后由 Agent 发布。"},
    {"command": "/ralphtrans", "title": "整门课批量转写", "mode": "界面操作", "view": "tasks", "detail": "分课程检查点、继续未完成节次、单独重试失败节次。"},
]
