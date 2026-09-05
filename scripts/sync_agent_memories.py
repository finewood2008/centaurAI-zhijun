#!/usr/bin/env python3
"""Sync native agent memories into local-vector-db searchable imports.

This script copies selected memory files from agent-specific stores into
local-vector-db/memory/imports/*.md. Imported files are indexed by the memory
system as custom memories, so they are searchable but are not injected by
/api/memory/context unless promoted into MEMORY.md, USER.md, or AGENTS.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = Path(os.environ.get("CENTAURAI_DATABASE_DATA_ROOT") or PROJECT_ROOT / "data").expanduser().resolve()
MEMORY_ROOT = DATA_ROOT / "memory"
IMPORTS_DIR = MEMORY_ROOT / "imports"
JOURNAL_DIR = MEMORY_ROOT / "journal"
DEFAULT_API_BASE = "http://127.0.0.1:8618"
CSRF_HEADER = "centaur-vdb"

# ── 本地 Ollama 日报摘要 ──
LOCAL_OLLAMA_URL = "http://127.0.0.1:11434"
LOCAL_SUMMARY_MODEL = os.environ.get("CENTAUR_MEMORY_AI_MODEL", "qwen3:1.7b")
LOCAL_MODEL_MIN_AVAILABLE_MEMORY_MB = 2600
# 只把每个 import 文件的前若干字符送到本机模型，控制内存和推理耗时
LLM_INPUT_MAX_CHARS_PER_AGENT = 2500
LLM_INPUT_MAX_AGENTS = 6  # 超过这个数量只送 changed 列表


def home_path(*parts: str) -> Path:
    return Path.home().joinpath(*parts)


def read_text(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def redact_sensitive(text: str) -> str:
    """Redact obvious secret-bearing lines before importing agent configs."""
    redacted = []
    secret_markers = (
        "token",
        "api_key",
        "apikey",
        "auth",
        "secret",
        "password",
        "credential",
    )
    for line in text.splitlines():
        lower = line.lower()
        if any(marker in lower for marker in secret_markers):
            key = line.split("=", 1)[0].split(":", 1)[0].strip()
            redacted.append(f"{key}: [REDACTED]")
        else:
            redacted.append(line)
    return "\n".join(redacted)


def source_text(display_path: str, content: str, count_as_source: bool = True) -> dict:
    return {
        "display_path": display_path,
        "content": content,
        "count_as_source": count_as_source,
    }


def safe_agent_name(name: str) -> str:
    safe = "".join(c for c in name.strip().lower() if c.isalnum() or c in {"-", "_"})
    return safe or "unknown"


def stable_import_text(content: str) -> str:
    """Compare generated imports without the volatile imported_at line."""
    return "\n".join(
        line for line in content.splitlines()
        if not line.startswith("- imported_at:")
    ).strip()


def short_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]


def available_memory_mb() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def local_model_summarize(
    imported_stats: list[dict],
    changed_agents: list[str],
) -> str | None:
    """用固定 localhost Ollama 对当天记忆变化生成中文日报摘要。"""
    if not changed_agents:
        return None
    memory_mb = available_memory_mb()
    if memory_mb is not None and memory_mb < LOCAL_MODEL_MIN_AVAILABLE_MEMORY_MB:
        print(
            f"local Ollama summary skipped: available memory {memory_mb} MB is below "
            f"the {LOCAL_MODEL_MIN_AVAILABLE_MEMORY_MB} MB safety threshold",
            file=sys.stderr,
        )
        return None

    now = datetime.now()
    date_str = f"{now.year}年{now.month}月{now.day}日"

    # 构建 LLM 输入：changed agents 的 import 内容摘要
    changed_agents_set = set(changed_agents)
    parts = [f"今天是 {date_str}。以下是各 AI Agent 的记忆/配置变化摘要：", ""]

    too_many = len(changed_agents_set) > LLM_INPUT_MAX_AGENTS
    for item in imported_stats:
        if item["agent"] not in changed_agents_set:
            continue
        if not too_many:
            path = item["target"]
            content = ""
            if path.exists():
                content = path.read_text(encoding="utf-8", errors="replace")
                content = content[:LLM_INPUT_MAX_CHARS_PER_AGENT]
            parts.append(
                f"## {item['agent']}（{item['sources']} 个来源，{item['size']} 字节）\n{content}\n"
            )
        else:
            parts.append(f"- {item['agent']}: {item['sources']} 个来源变更")

    parts.append("")
    parts.append(
        "请用 2-3 句简洁的中文总结今天这些 Agent 记忆的变化："
        "哪些 Agent 有新内容、涉及的领域/项目/重点信息是什么。"
        "不要复述技术细节，像日报一样概括。"
    )

    prompt = "\n".join(parts)

    payload = json.dumps(
        {
            "model": LOCAL_SUMMARY_MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": "你是半人马AI的日报助手。用简洁的中文总结当日 Agent 记忆变化，2-3 句即可。像给领导汇报。",
                },
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "think": False,
            "keep_alive": 0,
            "options": {"temperature": 0.3, "num_predict": 300, "num_ctx": 4096},
        },
        ensure_ascii=False,
    ).encode("utf-8")

    url = LOCAL_OLLAMA_URL + "/api/chat"
    headers = {"Content-Type": "application/json"}

    try:
        req = Request(url, data=payload, headers=headers, method="POST")
        with urlopen(req, timeout=45) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        data = json.loads(body)
        summary = data["message"]["content"].strip()
        return summary if summary else None
    except Exception as exc:
        print(f"local Ollama summarization failed: {exc}", file=sys.stderr)
        return None


def discover_agent_names() -> list[str]:
    """Discover agents from CCSwitch's current app list plus local agent homes."""
    names: set[str] = set()
    db = home_path(".cc-switch", "cc-switch.db")
    if db.exists():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            for (app_type,) in con.execute(
                "SELECT DISTINCT app_type FROM providers WHERE app_type IS NOT NULL"
            ):
                if app_type:
                    names.add(safe_agent_name(app_type))
            for (app_type,) in con.execute(
                "SELECT DISTINCT app_type FROM proxy_config WHERE app_type IS NOT NULL"
            ):
                if app_type:
                    names.add(safe_agent_name(app_type))
        except Exception:
            pass

    local_markers = {
        "claude": home_path(".claude"),
        "codex": home_path(".codex"),
        "hermes": home_path(".hermes"),
        "openclaw": home_path(".openclaw"),
        "gemini": home_path(".gemini"),
        "opencode": home_path(".opencode"),
    }
    for name, path in local_markers.items():
        if path.exists():
            names.add(name)

    preferred = ["claude", "claude-desktop", "codex", "gemini", "hermes", "opencode", "openclaw"]
    ordered = [name for name in preferred if name in names]
    ordered.extend(sorted(names - set(ordered)))
    return ordered


def collect_hermes(include_context: bool) -> list[Path]:
    paths = [
        home_path(".hermes", "memories", "USER.md"),
        home_path(".hermes", "memories", "MEMORY.md"),
    ]
    if include_context:
        paths.append(home_path(".hermes", "SOUL.md"))
    return paths


def collect_openclaw(include_context: bool) -> list[Path]:
    workspace = home_path(".openclaw", "workspace")
    paths = [
        workspace / "USER.md",
        workspace / "MEMORY.md",
    ]
    memory_dir = workspace / "memory"
    if memory_dir.exists():
        paths.extend(sorted(memory_dir.glob("*.md")))
    if include_context:
        paths.extend(
            [
                workspace / "AGENTS.md",
                workspace / "TOOLS.md",
                workspace / "IDENTITY.md",
                workspace / "HEARTBEAT.md",
                workspace / "SOUL.md",
            ]
        )
    return paths


def collect_codex(include_context: bool) -> list[Path | dict]:
    paths: list[Path | dict] = []
    db = home_path(".codex", "memories_1.sqlite")
    if db.exists():
        parts = ["# Codex native memories", ""]
        imported = 0
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """
                SELECT raw_memory, rollout_summary, rollout_slug, generated_at, usage_count, last_usage
                FROM stage1_outputs
                ORDER BY COALESCE(last_usage, generated_at, source_updated_at, 0) DESC
                LIMIT 200
                """
            ).fetchall()
            for row in rows:
                raw = (row["raw_memory"] or "").strip()
                summary = (row["rollout_summary"] or "").strip()
                if not raw and not summary:
                    continue
                parts.extend(
                    [
                        "---",
                        "",
                        f"## {row['rollout_slug'] or 'memory'}",
                        "",
                        f"- usage_count: {row['usage_count'] or 0}",
                        f"- generated_at: {row['generated_at'] or ''}",
                        f"- last_usage: {row['last_usage'] or ''}",
                        "",
                    ]
                )
                if summary:
                    parts.extend(["### Summary", "", summary, ""])
                if raw:
                    parts.extend(["### Raw memory", "", raw, ""])
                imported += 1
        except Exception as exc:
            parts.extend([f"_Could not read Codex memory database: {exc}_", ""])
        if imported == 0 and len(parts) == 2:
            parts.extend(["_No Codex native memories found in memories_1.sqlite._", ""])
        paths.append(source_text("~/.codex/memories_1.sqlite", "\n".join(parts)))

    rules = home_path(".codex", "rules", "default.rules")
    if rules.exists():
        paths.append(rules)
    if include_context:
        config = home_path(".codex", "config.toml")
        content = read_text(config)
        if content:
            paths.append(source_text("~/.codex/config.toml", redact_sensitive(content)))
    return paths


def collect_claude(include_context: bool) -> list[Path | dict]:
    paths: list[Path | dict] = []
    for candidate in (
        home_path(".claude", "CLAUDE.md"),
        home_path("CLAUDE.md"),
    ):
        if candidate.exists():
            paths.append(candidate)

    root_config = home_path(".claude.json")
    if root_config.exists():
        try:
            data = json.loads(root_config.read_text(encoding="utf-8", errors="replace"))
            projects = data.get("projects", {}) if isinstance(data, dict) else {}
            parts = [
                "# Claude project memory summary",
                "",
                f"- projects: {len(projects)}",
                "",
            ]
            for project_path, project in sorted(projects.items())[:50]:
                if not isinstance(project, dict):
                    continue
                parts.extend(
                    [
                        "---",
                        "",
                        f"## {project_path}",
                        "",
                        f"- trust_dialog_accepted: {project.get('hasTrustDialogAccepted', '')}",
                        f"- onboarding_seen_count: {project.get('projectOnboardingSeenCount', '')}",
                        f"- last_version_base: {project.get('lastVersionBase', '')}",
                        f"- last_start_time: {project.get('lastStartTime', '')}",
                        f"- example_files: {', '.join(project.get('exampleFiles') or [])}",
                        "",
                    ]
                )
            paths.append(source_text("~/.claude.json project summary", "\n".join(parts)))
        except Exception as exc:
            paths.append(
                source_text(
                    "~/.claude.json project summary",
                    f"_Could not read Claude project summary: {exc}_",
                )
            )

    if include_context:
        settings = home_path(".claude", "settings.json")
        content = read_text(settings)
        if content:
            paths.append(source_text("~/.claude/settings.json", redact_sensitive(content)))
    return paths


def collect_claude_desktop(include_context: bool) -> list[Path | dict]:
    paths: list[Path | dict] = []
    candidates = [
        home_path(".config", "Claude", "claude_desktop_config.json"),
        home_path(".config", "Claude", "settings.json"),
    ]
    for candidate in candidates:
        content = read_text(candidate)
        if content:
            paths.append(source_text(f"~/{candidate.relative_to(Path.home())}", redact_sensitive(content)))
    if not paths:
        paths.append(
            source_text(
                "claude-desktop",
                "Claude Desktop is listed as an agent/app, but no local memory or config source was found.",
                count_as_source=False,
            )
        )
    return paths


def collect_gemini(include_context: bool) -> list[Path | dict]:
    paths: list[Path | dict] = []
    for candidate in (
        home_path(".gemini", "GEMINI.md"),
        home_path(".gemini", "settings.json"),
        home_path(".config", "gemini", "settings.json"),
    ):
        content = read_text(candidate)
        if content:
            paths.append(source_text(f"~/{candidate.relative_to(Path.home())}", redact_sensitive(content)))
    if not paths:
        paths.append(
            source_text(
                "gemini",
                "Gemini is listed as an agent/app, but no local memory or config source was found.",
                count_as_source=False,
            )
        )
    return paths


def collect_opencode(include_context: bool) -> list[Path | dict]:
    paths: list[Path | dict] = []
    for candidate in (
        home_path(".opencode", "AGENTS.md"),
        home_path(".opencode", "MEMORY.md"),
        home_path(".config", "opencode", "opencode.json"),
    ):
        content = read_text(candidate)
        if content:
            paths.append(source_text(f"~/{candidate.relative_to(Path.home())}", redact_sensitive(content)))
    if not paths:
        paths.append(
            source_text(
                "opencode",
                "OpenCode is listed as an agent/app, but no local memory or config source was found.",
                count_as_source=False,
            )
        )
    return paths


COLLECTORS = {
    "claude": collect_claude,
    "claude-desktop": collect_claude_desktop,
    "codex": collect_codex,
    "gemini": collect_gemini,
    "hermes": collect_hermes,
    "opencode": collect_opencode,
    "openclaw": collect_openclaw,
}


def collect_unknown_agent(agent_name: str, include_context: bool) -> list[Path | dict]:
    return [
        source_text(
            agent_name,
            f"{agent_name} is listed as an agent/app, but local-vector-db does not yet have a memory adapter for it.",
            count_as_source=False,
        )
    ]


def render_import(agent_name: str, paths: list[Path | dict], include_context: bool) -> tuple[str, int]:
    now = datetime.now().isoformat(timespec="seconds")
    parts = [
        f"# Imported {agent_name} memory",
        "",
        f"- imported_at: {now}",
        f"- source_agent: {agent_name}",
        f"- include_context_files: {str(include_context).lower()}",
        "- note: This file is generated by scripts/sync_agent_memories.py.",
        "- scope: searchable archive; not automatically injected into agent startup context.",
        "",
    ]

    imported = 0
    seen: set[str] = set()
    for raw_source in paths:
        count_as_source = True
        if isinstance(raw_source, dict):
            display_path = str(raw_source.get("display_path", "generated"))
            content = raw_source.get("content", "")
            count_as_source = bool(raw_source.get("count_as_source", True))
            dedupe_key = display_path
        else:
            path = raw_source.expanduser().resolve()
            content = read_text(path)
            try:
                rel = path.relative_to(Path.home())
                display_path = f"~/{rel}"
            except ValueError:
                display_path = str(path)
            dedupe_key = str(path)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        if content is None or not content.strip():
            continue
        parts.extend(
            [
                "---",
                "",
                f"## Source: {display_path}",
                "",
                content.strip(),
                "",
            ]
        )
        if count_as_source:
            imported += 1

    if imported == 0:
        parts.extend(["_No memory files found._", ""])
    return "\n".join(parts), imported


def write_import(agent_name: str, content: str, dry_run: bool) -> tuple[Path, bool]:
    target = IMPORTS_DIR / f"{agent_name}.md"
    changed = True
    if target.exists():
        old = target.read_text(encoding="utf-8", errors="replace")
        changed = stable_import_text(old) != stable_import_text(content)
    if not dry_run and changed:
        IMPORTS_DIR.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return target, changed


def journal_is_empty(content: str, journal_date: str) -> bool:
    stripped = content.strip()
    return stripped in {"", f"# {journal_date}"}


def remove_daemon_sections(content: str) -> str:
    """Remove previous auto-generated memory-daemon sections, preserving manual notes."""
    lines = content.splitlines()
    kept: list[str] = []
    skipping = False
    for line in lines:
        if line.startswith("<!-- memory-daemon:"):
            skipping = True
            continue
        if skipping:
            is_auto_heading = (
                line == "## 记忆摘要"
                or line == "## 今日摘要"
                or line.endswith(" memory-daemon")
            )
            if line.startswith("## ") and not is_auto_heading:
                skipping = False
            else:
                continue
        kept.append(line)
    return "\n".join(kept).rstrip()


def append_sync_journal(
    imported_stats: list[dict],
    changed_agents: list[str],
    dry_run: bool,
    llm_summary: str | None = None,
) -> tuple[Path, bool]:
    """Write a concise memory summary for today's automatic import state."""
    now = datetime.now()
    journal_date = now.date().isoformat()
    journal_path = JOURNAL_DIR / f"{journal_date}.md"
    existing = (
        journal_path.read_text(encoding="utf-8", errors="replace")
        if journal_path.exists()
        else ""
    )
    event_basis = json.dumps(
        {
            "date": journal_date,
            "changed_agents": changed_agents,
            "stats": [
                {
                    "agent": item["agent"],
                    "sources": item["sources"],
                    "size": item["size"],
                    "content_hash": item["content_hash"],
                }
                for item in imported_stats
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    event_id = short_hash(event_basis)
    marker = f"<!-- memory-daemon:summary:{event_id} -->"
    if marker in existing:
        return journal_path, False

    detected_agents = [item["agent"] for item in imported_stats]
    importable_agents = [item["agent"] for item in imported_stats if item["sources"] > 0]
    detected_list = "、".join(detected_agents) if detected_agents else "暂无检测结果"
    importable_list = "、".join(importable_agents) if importable_agents else "暂无可导入记忆来源"
    changed_text = "、".join(changed_agents) if changed_agents else "未发现实质变化"
    lines = [
        marker,
        "## 记忆摘要",
        "",
        f"- 已检测到的 Agent：{detected_list}。",
        f"- 当前已有可导入记忆的来源：{importable_list}。",
        f"- 今日记忆变化：{changed_text}。",
        f"- 后续按 `agent` 名称获取上下文时，会优先使用对应来源的导入记忆。",
    ]
    if llm_summary:
        lines.extend(["", "## 今日摘要", "", llm_summary])
    lines.append("")
    entry = "\n".join(lines)

    base = remove_daemon_sections(existing)
    if journal_is_empty(base, journal_date):
        new_content = f"# {journal_date}\n\n{entry}"
    else:
        new_content = base.rstrip() + "\n\n" + entry

    if not dry_run:
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        journal_path.write_text(new_content.rstrip() + "\n", encoding="utf-8")
    return journal_path, True


def reindex(api_base: str, attempts: int, delay_seconds: float) -> dict:
    url = api_base.rstrip("/") + "/api/memory/reindex"
    req = Request(url, method="POST", headers={"X-Requested-By": CSRF_HEADER})
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                data = json.loads(body) if body.strip().startswith("{") else {"body": body}
        except (URLError, TimeoutError) as exc:
            if attempt >= attempts:
                print(f"reindex skipped: cannot reach {url}: {exc}", file=sys.stderr)
                return {"ok": False, "error": str(exc)}
            print(
                f"reindex retry {attempt}/{attempts}: cannot reach {url}: {exc}",
                file=sys.stderr,
            )
            time.sleep(delay_seconds)
            continue
        print(f"reindex response: {body}")
        return {"ok": True, **data}
    return {"ok": False, "error": "retry loop exhausted"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import Hermes/OpenClaw memories into local-vector-db/memory/imports."
    )
    parser.add_argument(
        "--include-context",
        action="store_true",
        help="Also import context/persona files such as AGENTS.md, TOOLS.md, SOUL.md.",
    )
    parser.add_argument(
        "--no-reindex",
        action="store_true",
        help="Only write import files; do not call /api/memory/reindex.",
    )
    parser.add_argument(
        "--api-base",
        default=os.environ.get("VECTOR_DB_API_BASE", DEFAULT_API_BASE),
        help=f"local-vector-db API base URL. Default: {DEFAULT_API_BASE}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be imported without writing files or reindexing.",
    )
    parser.add_argument(
        "--reindex-attempts",
        type=int,
        default=12,
        help="Number of attempts to call /api/memory/reindex. Default: 12.",
    )
    parser.add_argument(
        "--reindex-delay",
        type=float,
        default=5.0,
        help="Seconds to wait between reindex attempts. Default: 5.",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip local Ollama daily summary generation.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    agent_names = discover_agent_names()
    sources = {}
    for agent_name in agent_names:
        collector = COLLECTORS.get(agent_name)
        if collector:
            sources[agent_name] = collector(args.include_context)
        else:
            sources[agent_name] = collect_unknown_agent(agent_name, args.include_context)
    print(f"discovered agents: {', '.join(agent_names) if agent_names else '(none)'}")

    total_files = 0
    imported_stats = []
    changed_agents = []
    for agent_name, paths in sources.items():
        content, imported = render_import(agent_name, paths, args.include_context)
        target, changed = write_import(agent_name, content, args.dry_run)
        total_files += imported
        if changed:
            changed_agents.append(agent_name)
        rendered_size = len(content.encode("utf-8"))
        imported_stats.append(
            {
                "agent": agent_name,
                "target": target,
                "sources": imported,
                "size": rendered_size,
                "content_hash": short_hash(stable_import_text(content)),
                "changed": changed,
            }
        )
        if args.dry_run:
            action = "would write" if changed else "would skip unchanged"
        else:
            action = "wrote" if changed else "skipped unchanged"
        print(f"{action}: {target} ({imported} source files)")

    if args.dry_run:
        print(f"dry run complete: {total_files} source files found")
        return 0

    if args.no_reindex:
        print("reindex skipped by --no-reindex")
        return 0

    # ── 本地模型日报摘要 ──
    llm_summary = None
    if changed_agents and not args.no_llm:
        llm_summary = local_model_summarize(
            imported_stats=imported_stats,
            changed_agents=changed_agents,
        )
        if llm_summary:
            print(f"LLM summary: {llm_summary}")

    journal_path, journal_changed = append_sync_journal(
        imported_stats=imported_stats,
        changed_agents=changed_agents,
        dry_run=args.dry_run,
        llm_summary=llm_summary,
    )
    if journal_changed:
        print(f"wrote journal: {journal_path}")
    else:
        print(f"journal unchanged: {journal_path}")

    if not changed_agents and not journal_changed:
        print("reindex skipped: no import or journal changes")
        return 0

    result = reindex(
        args.api_base,
        attempts=max(args.reindex_attempts, 1),
        delay_seconds=max(args.reindex_delay, 0.0),
    )
    if not result.get("ok"):
        print(
            "Import files were written. Start local-vector-db and run this script again, "
            "or call POST /api/memory/reindex.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
