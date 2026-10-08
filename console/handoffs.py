"""Course-level cheatsheet and publishing handoff specifications."""
import re
from pathlib import Path


def sheet_target(root: Path | None, raw_dir: str, course_id: str, sub_id: str, fmt: str,
                 scope: str = "lecture", output: str = "") -> Path:
    if fmt not in ("html", "tex") or scope not in ("lecture", "course"):
        raise ValueError("不支持的速查表格式或范围")
    if not course_id or not re.fullmatch(r"[A-Za-z0-9_-]+", course_id) or (scope == "lecture" and not re.fullmatch(r"[A-Za-z0-9_-]+", sub_id)):
        raise ValueError("请提供有效的课程与课次标识")
    if output:
        if root is None:
            raise ValueError("自定义输出前请先设置资料库目录")
        target = Path(output).expanduser()
        target = (target if target.is_absolute() else root / target).resolve()
        if not target.is_relative_to(root.resolve()) or target.suffix.lower() != "." + fmt:
            raise ValueError("输出必须位于资料库目录内，并使用所选格式的扩展名")
        return target
    if scope == "course":
        if root is None:
            raise ValueError("整门课速查表需要先设置资料库目录")
        # IDs are filename identity, never a path supplied by the browser.
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", course_id)[:100]
        return root / "cheatsheets" / f"{safe_id}_cheatsheet.{fmt}"
    base = Path(raw_dir) if raw_dir else root
    if base is None:
        raise ValueError("请先设置资料库并采集当前课次")
    return base / f"{course_id}_{sub_id}_cheatsheet.{fmt}"


def sheet_spec(repo: str, fmt: str) -> list[str]:
    return [f"【模板说明】{repo}/CheatingSheetTemplate/README.md",
            f"【模板文件】{repo}/CheatingSheetTemplate/CheatingSheet.tex",
            "【排版与检查】",
            "- 阅读模板说明，按主题整合所有列出的笔记；保留公式、条件和关键结论。",
            "- 一张竖向 A4、四栏，正文约 5pt，标题约 6–7pt；确保打印时可读。",
            "- HTML 使用自包含文件、打印 CSS 和 A4 页面设置；完成后用浏览器打印预览检查并可另存 PDF。",
            "- LaTeX 使用当前系统可合法使用的字体；不依赖 Apple 专属字体，缺字体时替换为已安装的 CJK 字体。",
            "- LaTeX 完成后用 XeLaTeX 编译同名 PDF，核对只有一页且没有溢出；必要时缩减内容。",
            "- 创建输出父目录，重读并核对内容、公式及排版。"]


def publish_instruction(agent: str, repo: str, workspace: str, repository: str) -> dict:
    valid = bool(re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", repository))
    destination = repository if valid else "（未设置，请先确认 owner/repo）"
    url = "https://" + repository.split('/')[0] + ".github.io/" + repository.split('/')[1] + "/" if valid else "部署完成后从 GitHub Pages 设置获取"
    lines = [f"请使用 {agent} 准备并验证课程阅读网页的 GitHub Pages 发布。",
             f"【项目】{repo}", f"【只允许上传的静态目录】{workspace}/site/", f"【目标仓库】{destination}",
             "【任务范围】发布已构建的静态阅读网页；先检查公开内容和目标，再执行部署。",
             "1. 检查 gh --version 与 gh auth status；缺少认证时引导用户在终端登录，不读取或显示令牌。",
             "2. 确认 site/index.html 存在，检查待上传文件清单，排除 .env、认证状态、密钥和带签名的私密链接。",
             "3. 确认用户允许公开课程材料，展示目标仓库与待上传目录；取得明确发布授权后再上传。",
             "4. 阅读工作区 .github/workflows/pages.yml 与仓库 Pages 设置，优先使用现有的 GitHub Pages Actions 工作流。",
             "5. 如采用 gh-pages 分支方式，只在单独的临时发布目录提交 site/ 内容并推送目标分支，避免把整个资料库上传。",
             "6. 核对实际可用的 gh 命令及扩展；不要假定 gh pages deploy 是内置命令。",
             f"7. 等待部署工作流完成，打开并验证实际 Pages 地址（预期：{url}），报告工作流结果和可访问链接。",
             "官方部署说明：https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages",
             "注意：配置保留在本机，不把密码、API Key 或访问令牌放入对话。"]
    warnings = [] if valid else ["请先在设置中心填写 GitHub Pages 的 owner/repo。"]
    return {"title": "准备发布阅读网页", "instruction": "\n".join(lines),
            "verification": ["确认部署工作流成功及实际 Pages 链接可访问"], "warnings": warnings}
