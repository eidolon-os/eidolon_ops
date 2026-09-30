#!/usr/bin/env python3
"""Read-only Git inventory for sibling projects and their registered worktrees."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

SKIP_DIRS = {"node_modules", "build", "dist", "managed_components", "__pycache__", "venv"}


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(path), *args],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"},
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def discover(root: Path) -> list[Path]:
    """Descend through containers (e.g. vendor), but not through project repos."""
    found = []

    def fail(error: OSError) -> None:
        raise error

    for directory, dirs, files in os.walk(root, onerror=fail, followlinks=False):
        path = Path(directory)
        if ".git" in dirs or ".git" in files:
            found.append(path)
            dirs[:] = []
        else:
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS)
    return sorted(found)


def changes(path: Path) -> list[dict]:
    # -z preserves whitespace/newlines and avoids Git's C-quoted path format.
    fields = iter(git(path, "status", "--porcelain=v1", "-z", "--untracked-files=all").split("\0"))
    result = []
    for field in fields:
        if not field:
            continue
        entry = {"status": field[:2], "path": field[3:]}
        if "R" in field[:2] or "C" in field[:2]:
            entry["original_path"] = next(fields)
        result.append(entry)
    return result


def worktrees(path: Path) -> list[dict]:
    result, entry = [], {}
    for field in git(path, "worktree", "list", "--porcelain", "-z").split("\0"):
        if not field:
            if entry:
                result.append(entry)
                entry = {}
            continue
        key, _, value = field.partition(" ")
        entry[key] = value
    if entry:
        result.append(entry)
    return result


def audit(path: Path, root: Path, base: str = "main", tag: str | None = None) -> dict:
    report = {
        "project": str(path.relative_to(root)) or ".",
        "path": str(path),
        "base": base,
        "errors": [],
        "worktrees": [],
    }
    try:
        if Path(git(path, "rev-parse", "--show-toplevel").strip()).resolve() != path.resolve():
            raise RuntimeError("不是独立 Git 仓库根目录")
        shallow = git(path, "rev-parse", "--is-shallow-repository").strip() == "true"
        if shallow:
            report["errors"].append("浅克隆：提交包含关系可能不完整")
        try:
            report["base_sha"] = git(
                path, "rev-parse", "--verify", f"refs/heads/{base}^{{commit}}"
            ).strip()
        except RuntimeError:
            report["base_sha"] = None
            report["errors"].append(f"没有可用的本地 {base} 分支")
        tag_sha = None
        if tag:
            report["tag"] = tag
            try:
                tag_sha = git(path, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}").strip()
            except RuntimeError:
                report["errors"].append(f"没有可用的本地 tag {tag}")
        for item in worktrees(path):
            wt_path = Path(item["worktree"])
            head = item.get("HEAD", "")
            wt = {
                "path": str(wt_path),
                "head": head,
                "branch": item.get("branch", "").removeprefix("refs/heads/") or "DETACHED",
                "primary": wt_path.resolve() == path.resolve(),
                "stale": "prunable" in item or not (wt_path / ".git").exists(),
                "locked": "locked" in item,
                "changes": None,
                "errors": [],
            }
            report["worktrees"].append(wt)
            if tag:
                wt["tag_matches"] = head == tag_sha if tag_sha else None
            if not wt["stale"]:
                try:
                    if (
                        Path(git(wt_path, "rev-parse", "--show-toplevel").strip()).resolve()
                        != wt_path.resolve()
                    ):
                        raise RuntimeError("worktree 目录不再是原仓库根目录")
                    wt["changes"] = changes(wt_path)
                    # Detect concurrent checkout/commit rather than mixing two snapshots.
                    if git(wt_path, "rev-parse", "HEAD").strip() != head:
                        wt["errors"].append("检查期间 HEAD 变化，请重跑")
                except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
                    wt["errors"].append(str(error))
            if report["base_sha"]:
                try:
                    counts = git(
                        path,
                        "rev-list",
                        "--left-right",
                        "--count",
                        f"{report['base_sha']}...{head}",
                    ).split()
                    wt["behind"], wt["ahead"] = map(int, counts)
                    if wt["ahead"]:
                        wt.update(commits=[], equivalent_patches=0, preserved_refs=[])
                        wt["commits"] = git(
                            path,
                            "log",
                            "-5",
                            "--format=%h %s",
                            f"{report['base_sha']}..{head}",
                            "--",
                        ).splitlines()
                        cherry = git(path, "cherry", report["base_sha"], head).splitlines()
                        wt["equivalent_patches"] = sum(line.startswith("-") for line in cherry)
                        # Patch equivalence is evidence, not proof of current content:
                        # a patch on base may subsequently have been reverted.
                        wt["preserved_refs"] = git(
                            path,
                            "for-each-ref",
                            f"--contains={head}",
                            "--format=%(refname:short)",
                            "refs/heads",
                            "refs/tags",
                        ).splitlines()
                except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
                    wt["errors"].append(str(error))
        if (
            report["base_sha"]
            and git(path, "rev-parse", f"refs/heads/{base}").strip() != report["base_sha"]
        ):
            report["errors"].append("检查期间基线变化，请重跑")
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        report["errors"].append(str(error))
    return report


def needs_attention(repo: dict) -> bool:
    return bool(repo["errors"]) or any(
        w["errors"]
        or w["stale"]
        or w["changes"]
        or w.get("ahead", 0)
        or (w["primary"] and w["branch"] != repo["base"])
        or ("tag_matches" in w and w["tag_matches"] is not True)
        for w in repo["worktrees"]
    )


def display(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)[1:-1].replace("|", "\\|")


def relation(wt: dict, base: str) -> str:
    if "ahead" not in wt:
        return "无法判断"
    if wt["ahead"]:
        return f"未合入 {wt['ahead']}，落后 {wt['behind']}"
    return f"已包含于 {base}" + (f"（落后 {wt['behind']}）" if wt["behind"] else "（相同提交）")


def render(report: dict, details: bool = False) -> str:
    repos = report["repositories"]
    attention = sum(needs_attention(r) for r in repos)
    dirty_repos = sum(any(w["changes"] for w in r["worktrees"]) for r in repos)
    unmerged_repos = sum(any(w.get("ahead", 0) for w in r["worktrees"]) for r in repos)
    stale_count = sum(w["stale"] for r in repos for w in r["worktrees"])
    lines = [
        f"项目状态检查 · {report['checked_at']}",
        f"目录：{report['root']}",
        f"共 {len(repos)} 个仓库，{attention} 个需要关注。",
        f"未提交：{dirty_repos} 个仓库；有提交未进入基线：{unmerged_repos} 个仓库；失效 worktree 登记：{stale_count} 条。",
        "基于本地分支和提交；不联网、不修改 Git 状态。HEAD 是当前提交，分支单独显示。",
        "扫描到仓库根目录即停止，包含 vendor 等容器目录；不扫描项目内部依赖、隐藏缓存及符号链接。",
        "",
        "| 项目 | 当前分支 / HEAD | 未提交（全部 worktree） | 主目录相对基线 |",
        "| --- | --- | --- | --- |",
    ]
    if repos and repos[0].get("tag"):
        all_worktrees = [w for r in repos for w in r["worktrees"]]
        matched = sum(w.get("tag_matches") is True for w in all_worktrees)
        lines.insert(
            3, f"tag {display(repos[0]['tag'])}：{matched}/{len(all_worktrees)} 个 HEAD 一致。"
        )
    for repo in repos:
        primary = next((w for w in repo["worktrees"] if w["primary"]), None)
        dirty = sum(len(w["changes"] or []) for w in repo["worktrees"])
        unknown = any(w["changes"] is None for w in repo["worktrees"])
        state = f"{dirty} 项" + ("，部分无法读取" if unknown else "")
        head = f"{primary['branch']} / {primary['head'][:8]}" if primary else "无法读取"
        rel = relation(primary, repo["base"]) if primary else "无法判断"
        lines.append(f"| {display(repo['project'])} | {display(head)} | {state} | {rel} |")
    for repo in repos:
        if not details and not needs_attention(repo):
            continue
        lines.extend(["", f"{display(repo['project'])}（基线：{display(repo['base'])}）"])
        for error in repo["errors"]:
            lines.append(f"  检查异常：{display(error)}")
        for wt in repo["worktrees"]:
            notable = (
                wt["stale"]
                or wt["errors"]
                or wt["changes"]
                or wt.get("ahead", 0)
                or (wt["primary"] and wt["branch"] != repo["base"])
                or ("tag_matches" in wt and wt["tag_matches"] is not True)
            )
            if not details and not notable:
                continue
            lines.append(f"  {display(wt['path'])}")
            lines.append(
                f"    {display(wt['branch'])} @{wt['head'][:8]}；{relation(wt, repo['base'])}"
            )
            if wt["stale"]:
                lines.append("    失效 worktree 登记；无法确认原目录的未提交内容，不能当作干净。")
            if wt["locked"]:
                lines.append("    worktree 已锁定。")
            if wt["primary"] and wt["branch"] != repo["base"]:
                lines.append(f"    当前未检出基线分支 {display(repo['base'])}。")
            if "tag_matches" in wt:
                lines.append(
                    f"    HEAD 与 tag {display(repo['tag'])}："
                    + {True: "一致", False: "不同", None: "无法判断"}[wt["tag_matches"]]
                )
            if wt["changes"]:
                tracked = sum(c["status"] != "??" for c in wt["changes"])
                lines.append(
                    f"    未提交：已跟踪 {tracked} 项，未跟踪 {len(wt['changes']) - tracked} 项。"
                )
                if details:
                    lines.extend(f"      {c['status']} {display(c['path'])}" for c in wt["changes"])
            if wt.get("ahead"):
                lines.append(
                    f"    {wt['equivalent_patches']} 个非合并提交在基线历史中有等价补丁；不代表当前内容已保留。"
                )
                if details:
                    refs = wt["preserved_refs"]
                    lines.append(
                        "    包含该 HEAD 的本地分支/tag："
                        + (", ".join(map(display, refs)) or "无，需保留提交引用")
                    )
                    lines.extend(f"      {display(commit)}" for commit in wt["commits"])
                    if wt["ahead"] > len(wt["commits"]):
                        lines.append("      ……仅展示最近 5 个未合入提交")
            lines.extend(f"    检查异常：{display(e)}" for e in wt["errors"])
    lines.extend(
        [
            "",
            "未合入按提交祖先关系判断；cherry-pick、squash、rebase 或回退后的实际内容仍需 review。",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="项目集合目录，默认 eidolon_ops 的父目录",
    )
    parser.add_argument("--base", default="main", help="比较的本地分支，默认 main")
    parser.add_argument("--tag", help="额外检查各 worktree 的 HEAD 是否等于指定本地 tag")
    parser.add_argument("--details", action="store_true", help="展开全部 worktree 及未提交文件")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    parser.add_argument(
        "--check", action="store_true", help="发现待处理状态返回 1；检查异常始终返回 2"
    )
    args = parser.parse_args(argv)
    root = args.root.expanduser().resolve()
    try:
        if not root.is_dir():
            raise ValueError(f"目录不存在：{root}")
        paths = discover(root)
        if not paths:
            raise ValueError(f"未找到仓库：{root}")
        with ThreadPoolExecutor(max_workers=8) as pool:
            repos = list(pool.map(lambda p: audit(p, root, args.base, args.tag), paths))
        report = {
            "schema_version": 1,
            "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "root": str(root),
            "repositories": repos,
        }
        print(
            json.dumps(report, ensure_ascii=False, indent=2)
            if args.json
            else render(report, args.details)
        )
        if any(r["errors"] or any(w["errors"] for w in r["worktrees"]) for r in repos):
            return 2
        return int(args.check and any(needs_attention(r) for r in repos))
    except (OSError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
