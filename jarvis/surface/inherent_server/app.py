"""The inherent FastAPI app factory — ADR-0003 Step 7 + ADR-0005 §5.2.

L5 surface. :func:`create_app` builds the app the runtime serves in ``serve_inherent`` (Step 8),
registering each area's routes in the order below; ``jarvis.surface.inherent_server.guards``
puts every route except the liveness probe behind the local key and a local Host.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI

from jarvis.surface.device_pairing import register_pairing_routes
from jarvis.surface.inherent_server.agents import register_claude_routes, register_codex_routes
from jarvis.surface.inherent_server.ask import register_ask_route
from jarvis.surface.inherent_server.attachments import register_attachment_routes
from jarvis.surface.inherent_server.cancel import register_cancel_route
from jarvis.surface.inherent_server.cards import register_card_routes
from jarvis.surface.inherent_server.commitments import register_commitment_route
from jarvis.surface.inherent_server.controls import register_controls_route
from jarvis.surface.inherent_server.conversation import (
    register_conversation_route,
    register_think_route,
    register_turn_timings_route,
)
from jarvis.surface.inherent_server.data import register_data_routes
from jarvis.surface.inherent_server.day import (
    register_day_mark_route,
    register_day_route,
    register_doing_answer_route,
    register_doing_tidy_route,
    register_footprint_route,
    register_highlight_route,
    register_track_route,
)
from jarvis.surface.inherent_server.dictation import register_dictation_routes
from jarvis.surface.inherent_server.home import register_brief_route, register_today_routes
from jarvis.surface.inherent_server.imports import register_import_routes
from jarvis.surface.inherent_server.jobs import register_job_routes
from jarvis.surface.inherent_server.lifelog import register_lifelog_routes
from jarvis.surface.inherent_server.mail import (
    register_mail_draft_routes,
    register_mail_list_routes,
    register_mail_page_routes,
)
from jarvis.surface.inherent_server.manager import register_manager_routes
from jarvis.surface.inherent_server.memory import register_memory_routes
from jarvis.surface.inherent_server.night import register_night_routes
from jarvis.surface.inherent_server.notices import register_notice_routes
from jarvis.surface.inherent_server.phone import register_phone_routes
from jarvis.surface.inherent_server.push import register_push_route
from jarvis.surface.inherent_server.reactions import register_reaction_route
from jarvis.surface.inherent_server.settings import register_settings_routes
from jarvis.surface.inherent_server.setup import register_setup_routes
from jarvis.surface.inherent_server.sockets import register_terminal_route, register_ws_route
from jarvis.surface.inherent_server.submit import register_share_route, register_submit_route
from jarvis.surface.inherent_server.topics import register_topics_route
from jarvis.surface.inherent_server.trips import register_departure_route, register_ride_routes
from jarvis.surface.inherent_server.usage import register_usage_history_route, register_usage_routes
from jarvis.surface.inherent_server.v2 import register_v2_routes
from jarvis.surface.inherent_server.view import register_view_route
from jarvis.surface.inherent_server.work import register_work_routes

if TYPE_CHECKING:
    from jarvis.surface.inherent_server.deps import InherentDeps


def create_app(deps: InherentDeps) -> FastAPI:  # noqa: PLR0915 — one closed route table.
    """Build the FastAPI app with every area's routes registered, in the order below.

    The factory takes the injected deps once and closes over them in
    the route handlers — Step 8's ``serve_inherent`` owns the uvicorn
    lifecycle, so this module does not register startup / shutdown
    hooks.

    Args:
        deps: Bound runtime dependencies (submit callback + shared
            broadcaster). Must outlive every request the app serves.

    Returns:
        A fully-configured :class:`fastapi.FastAPI` instance ready
        for uvicorn to serve.
    """
    app = FastAPI(
        title="Jarvis Inherent (ADR-0003 Step 1, text-only)",
        version="0.1.0",
    )

    register_submit_route(app, deps)
    register_ask_route(app, deps)
    register_attachment_routes(app, deps)
    register_share_route(app, deps)
    register_reaction_route(app, deps)
    register_cancel_route(app, deps)
    register_controls_route(app, deps)
    register_ws_route(app, deps)
    register_v2_routes(app, deps)
    register_terminal_route(app, deps)
    register_phone_routes(app, deps)
    register_lifelog_routes(app, deps)
    register_import_routes(app, deps)
    register_push_route(app, deps)

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        """Liveness probe — used by ops scripts to confirm the daemon is up."""
        return {"status": "ok"}

    register_manager_routes(app, deps)
    register_usage_routes(app, deps)
    register_work_routes(app, deps)
    register_conversation_route(app, deps)
    register_topics_route(app, deps)
    register_card_routes(app, deps)
    register_think_route(app, deps)
    register_turn_timings_route(app, deps)
    register_day_route(app, deps)
    register_track_route(app, deps)
    register_footprint_route(app, deps)
    register_usage_history_route(app, deps)
    register_highlight_route(app, deps)
    register_day_mark_route(app, deps)
    register_doing_answer_route(app, deps)
    register_doing_tidy_route(app, deps)
    register_commitment_route(app, deps)
    register_today_routes(app, deps)
    register_mail_list_routes(app, deps)
    register_brief_route(app, deps)
    register_settings_routes(app, deps)
    register_mail_page_routes(app, deps)
    register_view_route(app, deps)
    register_mail_draft_routes(app, deps)
    register_notice_routes(app, deps)
    register_job_routes(app, deps)
    register_departure_route(app, deps)
    register_ride_routes(app, deps)
    register_memory_routes(app, deps)
    register_data_routes(app, deps)
    register_setup_routes(app, deps)
    register_dictation_routes(app, deps)
    register_night_routes(app, deps.night)
    register_pairing_routes(app, deps.pairing)
    register_codex_routes(app, deps)
    register_claude_routes(app, deps)

    return app
