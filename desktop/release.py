"""Solo-maintainer release helper: validate, confirm, then push one immutable tag.

No commits, force pushes, credentials, build tools, or GitHub API tokens are managed
here. Actions remains the only builder/publisher. --check never changes Git state;
--notes-output only renders the version's changelog for the existing workflow.
"""
import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
VERSION = r"(?:0|[1-9][0-9]{0,7})(?:\.(?:0|[1-9][0-9]{0,7})){2,3}"


def version_key(version):
    if not isinstance(version, str) or not re.fullmatch(VERSION, version):
        raise ValueError("版本号请使用 0.2.0 或 1.2.3.4，不加 v、前导零或预发布后缀。")
    parts = tuple(map(int, version.split(".")))
    return parts + (0,) * (4 - len(parts))


def release_notes(root, version):
    version_key(version)
    text = (Path(root) / "CHANGELOG.md").read_text(encoding="utf-8-sig")
    headings = list(re.finditer(r"^## \[([^]\r\n]+)\][^\r\n]*$", text, re.M))
    matches = [(i, heading) for i, heading in enumerate(headings) if heading[1] == version]
    if len(matches) != 1:
        raise ValueError(f"请在 CHANGELOG.md 中保留一个且仅一个 ## [{version}] 更新说明段落。")
    index, heading = matches[0]
    end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
    notes = text[heading.end():end].strip()
    if not notes or len(notes) > 45000:
        raise ValueError("本版更新说明应有实际内容，且不超过 45000 个字符。")
    return f"# CreatorHub v{version}\n\n{notes}\n"


def git_executable():
    found = shutil.which("git")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            path = Path(base) / "Git" / "cmd" / "git.exe"
            if path.is_file():
                return str(path)
    raise ValueError("请先安装 Git，再运行发布命令。")


def git_runner(root):
    executable = git_executable()

    def run(*args):
        result = subprocess.run([executable, *args], cwd=root, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=120,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            # Do not echo a remote URL, credential-helper output or credentials.
            raise ValueError(f"Git {args[0]} 未完成。请检查 Git、origin 连接和登录状态后重试；已有标签保持不变。")
        return result.stdout.strip()
    return run


def plan_release(root, version, git):
    key = version_key(version)
    notes = release_notes(root, version)
    if git("status", "--porcelain", "--untracked-files=all"):
        raise ValueError("工作区还有未提交的改动。请先提交代码和 CHANGELOG.md，再发布。")
    head = git("rev-parse", "HEAD")
    tag = f"v{version}"
    local = git("tag", "--list", tag)
    if local and git("rev-parse", f"refs/tags/{tag}^{{commit}}") != head:
        raise ValueError("本地同名标签指向其他提交，请使用新的版本号；不会移动或覆盖标签。")
    remote = git("ls-remote", "--tags", "origin", "refs/tags/v*")
    remote_versions = []
    for line in remote.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        ref = fields[1].removesuffix("^{}")
        if ref == f"refs/tags/{tag}":
            raise ValueError("远端已存在此版本标签。构建失败请到 Actions 重跑；发布修复请使用新版本号。")
        if ref.startswith("refs/tags/v"):
            value = ref.removeprefix("refs/tags/v")
            if re.fullmatch(VERSION, value):
                remote_versions.append(version_key(value))
    if remote_versions and key <= max(remote_versions):
        raise ValueError("新版本应高于已推送的正式版本；此简化入口不用于补发旧版。")
    return {"version": version, "tag": tag, "head": head, "local_tag": bool(local), "notes": notes}


def publish_plan(root, plan, git):
    # Recheck after the human confirmation, then pin the tag to the reviewed commit.
    current = plan_release(root, plan["version"], git)
    if current["head"] != plan["head"] or current["notes"] != plan["notes"]:
        raise ValueError("确认期间代码或更新说明有变化，请重新运行发布命令。")
    tag = plan["tag"]
    if not current["local_tag"]:
        git("tag", "-a", tag, plan["head"], "-m", f"发布 CreatorHub v{plan['version']}")
    # On failure keep the local tag. Re-running can retry it, never force-replace it.
    git("push", "origin", f"refs/tags/{tag}:refs/tags/{tag}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="CreatorHub 发布助手：检查后确认推送版本标签，由 Actions 构建发布。")
    parser.add_argument("version", help="本次版本号，例如 0.2.0")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="只检查，不创建或推送标签")
    mode.add_argument("--notes-output", type=Path, help="仅生成中文发布说明到 build/ 或 dist/，供 CI 使用")
    args = parser.parse_args(argv)
    try:
        if args.notes_output is not None:
            notes = release_notes(ROOT, args.version)
            output = (ROOT / args.notes_output).resolve()
            if not any(output.is_relative_to((ROOT / folder).resolve()) for folder in ("build", "dist")):
                raise ValueError("发布说明输出应位于项目 build/ 或 dist/ 目录。")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(notes, encoding="utf-8")
            print(f"已生成 v{args.version} 中文发布说明。")
            return 0
        git = git_runner(ROOT)
        plan = plan_release(ROOT, args.version, git)
        print(plan["notes"])
        print(f"准备发布 {plan['tag']}，提交 {plan['head'][:12]}。")
        print("仅推送这个版本标签；Actions 测试、构建成功后才公开安装包。")
        if args.check:
            print("检查通过；尚未创建或推送标签。")
            return 0
        if input(f"输入 {plan['tag']} 确认发布（回车取消）：").strip() != plan["tag"]:
            print("已取消，Git 状态未改变。")
            return 0
        publish_plan(ROOT, plan, git)
        print(f"已推送 {plan['tag']}。请在 GitHub Actions 查看构建；此时尚不代表安装包已发布。")
        return 0
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        message = str(exc) if isinstance(exc, ValueError) else "文件或 Git 操作未完成，请检查环境后重试；已有标签保持不变。"
        print(f"发布检查未通过：{message}")
        return 1
    except (EOFError, KeyboardInterrupt):
        print("操作已中断。若已开始推送，请到 Actions 查看状态后再重试；已有标签保持不变。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
