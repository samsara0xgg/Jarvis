"""The tool menu: the registry, its plugin connections and the tables checked against it."""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jarvis.deployment.models import default_meaning_dir
from jarvis.execution.archive_tool import build_archive_tool
from jarvis.execution.conversation_tool import build_conversation_tool
from jarvis.execution.dashboard_tool import build_dashboard_tool
from jarvis.execution.job_ledger_tool import build_job_ledger_tool, build_record_application_tool
from jarvis.execution.mail_tool import build_mail_tool
from jarvis.execution.my_day_tool import build_my_day_tool
from jarvis.execution.path_resolver import FileTargetsConfigError, configure_file_targets
from jarvis.execution.tidy_tool import build_tidy_tool
from jarvis.execution.workers import Workers, make_worker_tools
from jarvis.runtime.config import (
    DashboardSettings,
    Role,
    RuntimeBootstrapError,
    WebToolsSettings,
    WorkersSettings,
)
from jarvis.runtime.daily_report import DailyReportService, past_day_answer
from jarvis.runtime.dashboard import CLOSE, PAGES, SETTINGS_CATEGORIES, MailDrafts, ViewState
from jarvis.runtime.home import Home, mail_text
from jarvis.shared import CallerPrincipal
from jarvis.state import embedding

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from jarvis.decision.tier0 import Tier0Table
    from jarvis.deployment import RuntimePaths
    from jarvis.execution.tools import (
        Tool,
        ToolContext,
        ToolRegistry,
    )
    from jarvis.runtime.doing_run import DoingSchedule
    from jarvis.runtime.job_mail import JobMail
    from jarvis.runtime.plugin_connections import PluginConnections
    from jarvis.runtime.work_state import WorkStateService
    from jarvis.state.ledger import LedgerSources

LOGGER = logging.getLogger(__name__)


def _on_menu(table: Tier0Table, registry: ToolRegistry, role: Role) -> Tier0Table:
    """The Tier 0 rows the registry can serve.

    A brain holds only the tools its role builds; a row naming another would fail the boot's
    cross-check, so it is dropped (ADR 0170). The night-run tools are held, as proxies. A device
    tool no terminal has declared yet is held too (ADR 0225): its row waits on the menu.
    """
    if role != "brain":
        return table
    held = {tool.name for tool in (*registry.get_definitions(), *registry.device_catalogue())}
    return tuple(row for row in table if row.tool_name in held)


def _regex_router_tools(registry: ToolRegistry) -> tuple[Tool, ...]:
    """What Tier 0 rows may name: the menu's, and a brain's device tools no terminal declared yet.

    A row that waits for its terminal (ADR 0225) must not fail the boot's cross-check.
    """
    return tuple({
        tool.name: tool
        for tool in (*registry.for_caller(CallerPrincipal.REGEX_ROUTER),
                     *registry.device_catalogue())
        if CallerPrincipal.REGEX_ROUTER in tool.allowed_callers
    }.values())


def _install_open_path(block: Mapping[str, Any]) -> None:
    """Hand `tools.open_path` to L4's resolver; malformed bookmarks fail the boot."""
    try:
        configure_file_targets(block)
    except FileTargetsConfigError as exc:
        msg = f"runtime: tools.open_path invalid: {exc}"
        raise RuntimeBootstrapError(msg) from exc


def _web_search_credentials(web: WebToolsSettings) -> tuple[str | None, str | None]:
    """Return `(search_provider, api_key)` for `tools.web.*`.

    A blank `search_provider` picks by which key the environment holds,
    `TAVILY_API_KEY` first, then `EXA_API_KEY`; with neither it returns
    `(None, None)` and no `web_search` is registered.

    The key is resolved HERE, from the env var named by
    `tools.web.search_api_key_env` — same indirection the LLM presets
    use (`api_key_env`), so no credential is ever written into
    `config/jarvis.yaml`. L4 receives the resolved value; it does not
    read YAML or the environment itself.

    `search_api_key_env` is OPTIONAL: left blank it derives from the
    provider (`exa` -> `EXA_API_KEY`). An explicit name that belongs to
    a DIFFERENT provider is refused rather than honoured — pointing
    `search_provider: tavily` at `EXA_API_KEY` would post one vendor's
    credential to the other vendor's server, which is a leak, not a
    misconfiguration to paper over. Refusing yields no key, so
    `_resolve_search_backend` degrades to `ddgs` with its own warning.

    Best-effort like its `tools.web` siblings: a missing block, an
    unknown provider name, or an unset variable all degrade inside
    `_resolve_search_backend` rather than failing boot.
    """
    provider = web.search_provider
    if not provider:
        for name in ("tavily", "exa"):
            found = os.environ.get(f"{name.upper()}_API_KEY", "").strip()
            if found:
                return name, found
        return None, None

    key_env = f"{provider.upper()}_API_KEY"
    if web.search_api_key_env:
        key_env = web.search_api_key_env
        if not _key_env_matches_provider(key_env, provider):
            LOGGER.error(
                "tools.web.search_api_key_env=%r does not belong to "
                "search_provider=%r — refusing to send that credential to the "
                "wrong vendor; leave the key blank to derive it automatically",
                key_env, provider,
            )
            return provider, None

    api_key = os.environ.get(key_env)
    return provider, (api_key.strip() if api_key else None)


def _key_env_matches_provider(key_env: str, provider: str) -> bool:
    """Is `key_env` plausibly the credential for `provider`?

    Only guards the case that actually leaks: a variable named for one
    KNOWN provider while a different KNOWN provider is selected. A name
    mentioning neither (an operator's own convention, e.g.
    `JARVIS_SEARCH_KEY`) is left alone — this is a footgun guard, not a
    naming policy.
    """
    known = ("exa", "tavily")
    selected = provider.strip().lower()
    if selected not in known:
        return True
    upper = key_env.upper()
    mentioned = [name for name in known if name.upper() in upper]
    return not mentioned or selected in mentioned


def _dashboard_state(
    settings: DashboardSettings,
) -> tuple[ViewState | None, ViewState | None, MailDrafts | None]:
    """The Dashboard's view, the view the mail page reads, and the mail drafts (None = off).

    The mail page needs the view (ADR 0176: its open letter is the view's open item); with
    ``dashboard.mail.enabled`` alone it stays off.
    """
    view = ViewState() if settings.view else None
    mail_view = view if settings.mail else None
    return view, mail_view, None if mail_view is None else MailDrafts(mail_view)


def _register_dashboard_tool(registry: ToolRegistry, view: ViewState | None) -> None:
    """``show_on_dashboard`` (ADR 0176), registered only with ``dashboard.view.enabled``."""
    for tool in build_dashboard_tool(
        (*PAGES, CLOSE), None if view is None else view.present, SETTINGS_CATEGORIES
    ):
        registry.register(tool)


def _register_workers(
    registry: ToolRegistry, paths: RuntimePaths, settings: WorkersSettings,
) -> Workers | None:
    """ADR 0019: workers are threads on one ``codex app-server``.

    Off unless ``tools.workers.enabled`` and ``codex`` is on this machine;
    ``tools.workers.roots`` lists the folders a worker may be started in.
    The server starts lazily on the first spawn, so a one-shot CLI turn
    pays nothing; the daemon stops it at shutdown.
    """
    if not settings.enabled:
        return None
    codex = shutil.which("codex")
    if codex is None:
        LOGGER.warning("tools.workers is on but codex is not on PATH; no workers this boot")
        return None
    workers = Workers(paths.root / "codex.sock", paths.event_log, codex_bin=codex)
    for worker_tool in make_worker_tools(workers, settings.roots):
        registry.register(worker_tool)
    return workers


def _register_job_ledger(registry: ToolRegistry, job_mail: JobMail | None) -> None:
    """``job_ledger`` reads what ``GET /inherent/jobs`` serves, ``record_application`` adds a row.

    Both are absent while job mail is off.
    """
    for tool in (
        *build_job_ledger_tool(None if job_mail is None else job_mail.ledger),
        *build_record_application_tool(None if job_mail is None else job_mail.add_application),
    ):
        registry.register(tool)


def _register_mail(registry: ToolRegistry, home: Home) -> None:
    """``mail`` (ADR 0242): the overview ``GET /inherent/mail`` serves, a search, one message."""
    for tool in build_mail_tool(home.mail, home.gmail, mail_text):
        registry.register(tool)


def _register_conversation(registry: ToolRegistry, memory_db: Path, runtime_root: Path) -> None:
    """``conversation`` (ADR 0243): his words and hers, searched, summarized by day, opened.

    ADR 0260: the search also goes by meaning, with the model under the runtime root.
    """
    embedder = embedding.shared(default_meaning_dir(runtime_root))
    for tool in build_conversation_tool(memory_db, embedder=embedder):
        registry.register(tool)


def _register_archive(registry: ToolRegistry, memory_db: Path, runtime_root: Path) -> None:
    """``archive`` (ADR 0259): the payments and Claude conversations he handed over."""
    embedder = embedding.shared(default_meaning_dir(runtime_root))
    for tool in build_archive_tool(memory_db, embedder=embedder):
        registry.register(tool)


def _register_tidy(registry: ToolRegistry, doing: DoingSchedule | None) -> None:
    """``tidy_day`` (ADR 0310): he asks her to tidy today's timeline now."""
    for tool in build_tidy_tool(None if doing is None else doing.tidy):
        registry.register(tool)


def _register_my_day(registry: ToolRegistry, sources: LedgerSources) -> None:
    """``my_day`` (ADR 0244): today and what he does now, a day, a span, one project."""
    for tool in build_my_day_tool(sources):
        registry.register(tool)


@dataclass(frozen=True)
class Catalog:
    """What :func:`register_catalog` hands back besides the registry."""

    registry: ToolRegistry
    workers: Workers | None
    plugins: PluginConnections
    job_mail: JobMail | None
    home: Home


def register_catalog(  # noqa: PLR0913 - one call site per caller, the dependencies are the point
    *,
    build_registry: Callable[[], ToolRegistry],
    offer_devices: Callable[[ToolRegistry], None],
    paths: RuntimePaths,
    workers: WorkersSettings,
    view: ViewState | None,
    make_plugins: Callable[[ToolRegistry], PluginConnections],
    make_late: Callable[[PluginConnections], tuple[JobMail | None, Home]],
    memory_db: Path,
    doing: DoingSchedule | None,
    ledger: LedgerSources,
) -> Catalog:
    """Every tool the daemon serves, registered in its order from dependencies already built.

    The one place that order lives. The daemon calls it with real dependencies, the tool exam
    with stand-ins; the menu's order is part of the cached prompt prefix, so it must not differ.
    ``make_late`` builds what needs the plugin connections initialized: job mail and the home.
    """
    registry = build_registry()
    # ADR 0225: a brain's menu offers what the paired terminals declared, before any is here.
    offer_devices(registry)
    started = _register_workers(registry, paths, workers)
    _register_dashboard_tool(registry, view)
    plugins = make_plugins(registry)
    plugins.initialize()
    job_mail, home = make_late(plugins)
    _register_job_ledger(registry, job_mail)
    _register_mail(registry, home)
    _register_conversation(registry, memory_db, paths.root)
    _register_archive(registry, memory_db, paths.root)
    _register_tidy(registry, doing)
    _register_my_day(registry, ledger)
    plugins.refresh_search()  # ADR 0247: every built-in tool is registered now
    return Catalog(registry, started, plugins, job_mail, home)


def _work_state_tool_refresh(
    service: WorkStateService,
    daily_report: DailyReportService,
) -> Callable[[Mapping[str, Any], ToolContext], dict[str, Any]]:
    """Bind the service to the flat tool's ``(args, ctx)`` handler shape.

    A question about a past day gets that day's saved report instead of an analysis of now.
    """

    def refresh(args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        # force too: the model passed force=true for "what did I do yesterday" (2026-10-01).
        past = past_day_answer(daily_report, ctx.conn, args.get("question"))
        if past is not None:
            return past
        return service.refresh(
            ctx.conn,
            question=args.get("question"),
            note=args.get("note"),
            force=bool(args.get("force", False)),
            trigger="conversation",
            action_id=ctx.action_id,
        )

    return refresh
