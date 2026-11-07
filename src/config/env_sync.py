"""Keep .env and .env.example in step with the code.

Run it three ways:

    python src/config/env_sync.py              # 只报告差异（dry-run）
    python src/config/env_sync.py --apply      # 修复 .env（新增补齐、失效移除）
    python src/config/env_sync.py --example    # 由 env_schema.py 重新生成 .env.example

Exit codes: 0 = in sync / fixed, 1 = drift found in --check mode, 2 = error.

Rules (deliberate, so a build can never silently change behaviour):

* A key that already exists in .env is **never touched**, even if its value is
  empty or differs from the default.  The user's value always wins.
* A key the code no longer reads is removed, because leaving it there makes the
  file lie about what is in effect.  The removed line is printed and the whole
  file is backed up under ``data/env_backups/`` first.
* A key the code reads but .env lacks is appended with the code's own default,
  so the deployment keeps behaving exactly as it did while the knob becomes
  visible and editable.
* Commented-out keys count as absent (``#FOO=1`` means "not set"), so they are
  appended as real keys rather than silently staying inert.

This module is stdlib-only on purpose: ``docker build`` runs it in a bare
python image before any dependency is installed.
"""

from __future__ import annotations

import argparse
import errno
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

_HERE = Path(__file__).resolve()
if __package__ in (None, ""):  # executed as a plain script
    sys.path.insert(0, str(_HERE.parent))
    sys.path.insert(0, str(_HERE.parents[2]))

try:  # normal package import
    from . import env_schema as schema
except ImportError:  # direct script execution
    import env_schema as schema  # type: ignore[no-redef]

ROOT = _HERE.parents[2]
DEFAULT_ENV = ROOT / ".env"
DEFAULT_EXAMPLE = ROOT / ".env.example"
BACKUP_DIR = ROOT / "data" / "env_backups"

# KEY=value, tolerating "export ", leading spaces and inline comments.
ASSIGN_RE = re.compile(
    r"^(?P<indent>\s*)(?P<export>export\s+)?"
    r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?P<sep>\s*=\s*)(?P<value>.*)$"
)
INLINE_COMMENT_RE = re.compile(r"\s+#.*$")

# Names the code reads from the environment, for the schema coverage audit.
READ_PATTERNS = (
    re.compile(r"os\.getenv\(\s*[\"']([A-Z][A-Z0-9_]+)[\"']"),
    re.compile(r"os\.environ\.get\(\s*[\"']([A-Z][A-Z0-9_]+)[\"']"),
    re.compile(r"os\.environ\[\s*[\"']([A-Z][A-Z0-9_]+)[\"']"),
    # settings.py reads its limits through helpers that take the name as a str
    re.compile(r"_(?:optional_limit|non_negative_int|bool_env|float_env)\(\s*[\"']([A-Z][A-Z0-9_]+)[\"']"),
)

C_OK = ""
RESET = ""
try:  # colour only when attached to a terminal
    if sys.stdout.isatty():
        C_OK = "\033[32m"
        RESET = "\033[0m"
except Exception:  # pragma: no cover - exotic stdout
    pass


@dataclass
class Entry:
    lineno: int
    key: str
    value: str


@dataclass
class Plan:
    path: Path
    exists: bool = True
    content: str = ""
    entries: list[Entry] = field(default_factory=list)
    added: list[schema.EnvVar] = field(default_factory=list)
    removed: list[Entry] = field(default_factory=list)
    duplicates: list[Entry] = field(default_factory=list)
    new_content: str = ""
    error: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed or self.duplicates)


def parse(text: str) -> list[Entry]:
    entries: list[Entry] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        match = ASSIGN_RE.match(line)
        if not match:
            continue
        entries.append(
            Entry(lineno=lineno, key=match.group("key"), value=match.group("value").strip())
        )
    return entries


def build_plan(path: Path, keep_removed: bool = False, stamp: str = "") -> Plan:
    plan = Plan(path=path)
    if path.is_dir():
        plan.exists = False
        plan.error = (
            f"{path} 是一个目录，不是文件。通常是启动前 .env 不存在，docker 自动建了目录："
            f"rmdir {path} && cp {DEFAULT_EXAMPLE.name} {path.name} 后重试。"
        )
        return plan
    if not path.is_file():
        plan.exists = False
        return plan

    # newline="" keeps CRLF intact; read_text() would normalise it to LF and the
    # whole-file rewrite below would then look like a mangled config on Windows.
    with path.open("r", encoding="utf-8", newline="") as handle:
        plan.content = handle.read()
    plan.entries = parse(plan.content)
    known = schema.keys(include_internal=True)
    # Windows users edit .env in Notepad; rewriting CRLF as LF would produce a
    # whole-file diff and look like the tool mangled their config.
    eol = "\r\n" if "\r\n" in plan.content else "\n"

    seen: set[str] = set()
    kept: list[str] = []
    for lineno, line in enumerate(plan.content.splitlines(), 1):
        match = ASSIGN_RE.match(line)
        if not match:
            kept.append(line)
            continue
        key = match.group("key")
        if key not in known:
            if schema.is_foreign(key):
                kept.append(line)  # COMPOSE_*/DOCKER_*/TZ: 属于外围工具，保留
                continue
            plan.removed.append(Entry(lineno, key, match.group("value").strip()))
            if keep_removed:
                note = f"# [env_sync {stamp}] 已从代码移除，此行不再生效（保留原值便于回滚）："
                kept.append(f"{note} {line.strip()}")
            continue
        if key in seen:
            plan.duplicates.append(Entry(lineno, key, match.group("value").strip()))
            continue
        seen.add(key)
        kept.append(line)

    present = {entry.key for entry in plan.entries}
    plan.added = [var for var in schema.documented_vars() if var.key not in present]

    if plan.added:
        while kept and not kept[-1].strip():
            kept.pop()
        kept.append("")
        kept.append("# " + "=" * 69)
        kept.append("# 以下配置由 env_sync 自动补全：代码里有、.env 里没有，已按代码默认值写入。")
        kept.append("# 默认值不改变原有行为；需要调整直接改值即可，之后不会被覆盖。")
        kept.append("# " + "=" * 69)
        section: Optional[str] = None
        for var in plan.added:
            if var.section != section:
                section = var.section
                kept.append("")
                kept.append(f"# --- {section}" + (f" —— {schema.section_blurb(section)}" if schema.section_blurb(section) else ""))
            if var.comment:
                kept.extend(schema._comment_lines(var.comment))
            kept.append(f"{var.key}={var.default}")

    plan.new_content = eol.join(kept).rstrip("\r\n") + eol
    return plan


def backup(path: Path) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = BACKUP_DIR / f"{path.name}.{stamp}.bak"
    shutil.copy2(path, target)
    return target


def write_in_place(path: Path, content: str) -> None:
    """Rewrite the file without changing its inode.

    Required when the path is a bind mount (``/app/.env`` inside the
    container): the mount point is not a regular directory entry, so any
    rename onto it fails with EBUSY. Truncating the existing file keeps the
    inode, and therefore keeps the mount, intact. A crash midway leaves a
    truncated file, which is why apply_plan backs the file up first.
    """
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def write_atomic(path: Path, content: str) -> None:
    tmp = path.with_name(f"{path.name}.env_sync.tmp")
    mode = None
    try:
        mode = path.stat().st_mode
    except OSError:
        pass
    try:
        with tmp.open("w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        if mode is not None:
            try:
                os.chmod(tmp, mode)
            except OSError:
                pass
        try:
            os.replace(tmp, path)
        except OSError as exc:
            # EBUSY/EXDEV: path is a mount point (docker bind mount), so the
            # tmp+rename swap is impossible. Fall back to an in-place rewrite
            # instead of failing the sync on every single container start.
            if exc.errno not in (errno.EBUSY, errno.EXDEV, errno.EPERM):
                raise
            write_in_place(path, content)
            print(f"[env] {path.name} 是挂载文件，已改为原地写入（rename 被拒绝：{exc.strerror}）")
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def describe(plan: Plan, quiet: bool) -> list[str]:
    out: list[str] = []
    name = plan.path.name
    if not plan.exists:
        if plan.error:
            out.append(f"[env] {name}: {plan.error}")
        else:
            out.append(
                f"[env] 未找到 {name}，跳过（首次部署把 .env.example 复制成 .env 即可）。"
            )
        return out

    if not plan.changed:
        if not quiet:
            out.append(f"[env] {name}: 已与代码一致（{len(plan.entries)} 项），无需改动。")
        return out

    out.append(
        f"[env] {name}: 新增 {len(plan.added)} 项，移除 {len(plan.removed)} 项，"
        f"重复 {len(plan.duplicates)} 项"
    )
    for var in plan.added:
        shown = "" if var.secret else var.default
        out.append(f"  + {var.key}={shown}")
    for entry in plan.removed:
        out.append(f"  - {entry.key}  （第 {entry.lineno} 行，已从配置中移除）")
    for entry in plan.duplicates:
        out.append(f"  ~ {entry.key}  （第 {entry.lineno} 行重复定义，已删除该行）")
    return out


def apply_plan(plan: Plan, allow_backup_failure: bool, quiet: bool) -> int:
    if not plan.exists or not plan.changed:
        return 0
    if not allow_backup_failure:
        try:
            saved = backup(plan.path)
        except OSError as exc:
            print(
                f"[env] 无法备份 {plan.path}（{exc}），已中止以免丢失配置。"
                "确认无需备份可加 --no-backup。",
                file=sys.stderr,
            )
            return 2
        if not quiet:
            print(f"[env] 已备份到 {saved.relative_to(ROOT) if saved.is_relative_to(ROOT) else saved}")
    try:
        write_atomic(plan.path, plan.new_content)
    except OSError as exc:
        print(f"[env] 写入 {plan.path} 失败：{exc}", file=sys.stderr)
        return 2
    print(f"{C_OK}[env] {plan.path.name} 已同步{RESET}")
    return 0


def regenerate_example(path: Path, write: bool, quiet: bool) -> int:
    rendered = schema.render_example()
    current = path.read_text(encoding="utf-8") if path.is_file() else None
    if current == rendered:
        if not quiet:
            print(f"[env] {path.name}: 与 env_schema.py 一致。")
        return 0
    if not write:
        print(f"[env] {path.name} 与 env_schema.py 不一致，运行 --example 可重新生成。")
        return 1
    if path.is_file():
        try:
            backup(path)
        except OSError:
            pass
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(rendered)
    print(f"{C_OK}[env] {path.name} 已按 env_schema.py 重新生成{RESET}")
    return 0


def audit() -> int:
    """Fail when the code reads a variable the schema does not declare.

    Without this gate a newly added ``os.getenv("FOO")`` would be treated as a
    stale key by the sync step and deleted from the user's .env -- the exact
    failure this tool exists to prevent.
    """
    known = schema.keys(include_internal=True)
    found: dict[str, list[str]] = {}
    # This file talks *about* os.getenv (docs, audit patterns) -- scanning it
    # would report its own examples as undeclared variables.
    skip = {Path(__file__).resolve(), (ROOT / "src" / "config" / "env_schema.py").resolve()}
    for path in sorted((ROOT / "src").rglob("*.py")):
        if path.resolve() in skip:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for pattern in READ_PATTERNS:
            for name in pattern.findall(text):
                found.setdefault(name, []).append(str(path.relative_to(ROOT)))
    undeclared = {name: where for name, where in found.items() if name not in known}
    if undeclared:
        print("[env] 以下环境变量被代码读取，但未在 src/config/env_schema.py 中声明：")
        for name, where in sorted(undeclared.items()):
            print(f"  ! {name}  <- {', '.join(sorted(set(where)))}")
        print("[env] 请在 env_schema.py 里补上声明，否则同步步骤会把它当作失效配置删除。")
        return 1
    if os.getenv("ENV_SYNC_VERBOSE"):
        print(f"[env] 审计通过：{len(found)} 个变量均已声明。")
    return 0


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="env_sync",
        description="把 .env / .env.example 与 src/config/env_schema.py 对齐。",
    )
    parser.add_argument("--file", action="append", default=None, metavar="PATH",
                        help="要同步的 dotenv 文件（可重复），默认 .env")
    parser.add_argument("--apply", action="store_true", help="写入改动（默认只报告）")
    parser.add_argument("--check", action="store_true", help="有差异时退出码 1")
    parser.add_argument("--example", action="store_true", help="重新生成 .env.example")
    parser.add_argument("--check-example", action="store_true",
                        help=".env.example 与 schema 不一致时退出码 1（构建门禁用）")
    parser.add_argument("--audit", action="store_true",
                        help="校验代码里读取的每个变量都已声明")
    parser.add_argument("--keep-removed", action="store_true",
                        help="失效配置只注释掉，不删除")
    parser.add_argument("--no-backup", action="store_true", help="改动前不备份")
    parser.add_argument("--quiet", action="store_true", help="只输出结论行")
    args = parser.parse_args(list(argv) if argv is not None else None)

    status = 0
    if args.audit:
        status = max(status, audit())
    if args.check_example or args.example:
        status = max(status, regenerate_example(DEFAULT_EXAMPLE, args.example, args.quiet))

    targets = [Path(p) for p in (args.file or [str(DEFAULT_ENV)])]
    if args.file is None and (args.audit or args.example or args.check_example):
        targets = []  # 显式只做 schema 相关操作，不必顺带检查 .env
    for target in targets:
        plan = build_plan(target, keep_removed=args.keep_removed,
                          stamp=datetime.now().strftime("%Y-%m-%d"))
        lines = describe(plan, args.quiet)
        if args.quiet:
            lines = lines[:1]  # 只留结论行，明细需要时不加 --quiet
        for line in lines:
            print(line)
        if not plan.changed:
            continue
        if args.apply:
            status = max(status, apply_plan(plan, args.no_backup, args.quiet))
        elif args.check:
            status = max(status, 1)
    return status


if __name__ == "__main__":
    sys.exit(main())
