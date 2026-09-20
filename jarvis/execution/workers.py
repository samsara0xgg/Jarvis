"""Workers are threads on the resident ``codex app-server`` (ADR 0019).

Four tools map onto the app-server wire: ``spawn_worker`` = ``thread/start``
+ ``turn/start``; ``wait_worker`` = block on the fold below; ``send_input`` =
``turn/steer`` into a running turn or ``turn/start`` on an idle one
(``turn/interrupt`` first when asked); ``close_worker`` = ``turn/interrupt``
+ ``thread/unsubscribe``. Status is Codex's own ``AgentStatus``
(``core/src/agent/status.rs``): ``pending_init | running | interrupted |
completed(final_message) | errored(error) | shutdown | not_found``, folded
from ``turn/started``, ``item/completed``, ``turn/completed`` and ``error``.
The final message is the last ``agentMessage`` of the last turn, passed
through verbatim: no schema, no parser, no verdict.

Approval requests the server raises for a worker
(``item/*/requestApproval``) are not decided here. They sit on the worker
as ``pending_approval``; ``wait_worker`` returns them so the parent can put
them to Allen, and Allen's answer comes back through
``send_input(approval=...)``. The one durable table, ``worker_edges``
(parent, child, status), holds topology only.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.codex_app_server import CodexAppServer, CodexAppServerError, CodexClient
from jarvis.execution.tools import FlatHandler, Tool, ToolContext, ToolError, tool
from jarvis.shared import CallerPrincipal
from jarvis.state.event_log import open_event_log
from jarvis.state.worker_edges import close_edges, open_edge

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

LOGGER = logging.getLogger(__name__)

_FINAL: Final = frozenset({"completed", "errored", "shutdown", "not_found"})
_APPROVAL_METHODS: Final = frozenset({
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
})
DECISIONS: Final = ("accept", "acceptForSession", "decline", "cancel")
DEFAULT_WAIT_S: Final = 60.0
MAX_WAIT_S: Final = 600.0
_INTERRUPT_SETTLE_S: Final = 15.0

@dataclass
class Worker:
    """One thread's folded status; mutated only under ``Workers._cv``."""

    thread_id: str
    turn_id: str | None = None
    status: str = "pending_init"
    final_message: str | None = None
    error: str | None = None
    pending_approval: dict[str, Any] | None = None

    def view(self) -> dict[str, Any]:
        """The status as the parent model reads it."""
        out: dict[str, Any] = {"status": self.status}
        if self.status == "completed":
            out["final_message"] = self.final_message
        if self.status == "errored":
            out["error"] = self.error
        if self.pending_approval is not None:
            out["pending_approval"] = {
                "method": self.pending_approval["method"],
                **self.pending_approval["params"],
            }
        return out

    @property
    def ready(self) -> bool:
        """True when ``wait_worker`` should return this worker."""
        return self.status in _FINAL or self.pending_approval is not None


class Workers:
    """The app-server child, its client, and every thread this daemon opened."""

    def __init__(self, socket_path: Path, event_log: Path, *, codex_bin: str = "codex") -> None:
        """The server starts lazily on the first spawn; ``event_log`` holds ``worker_edges``."""
        self._server = CodexAppServer(socket_path, codex_bin=codex_bin)
        self._event_log = event_log
        self._client: CodexClient | None = None
        self._workers: dict[str, Worker] = {}
        self._cv = threading.Condition()

    # --- lifecycle ------------------------------------------------------

    def _ensure(self) -> CodexClient:
        with self._cv:
            if self._client is not None and self._server.running:
                return self._client
            if self._client is not None:
                # ponytail: a dead server loses every thread; thread/resume could recover them.
                self._client.close()
                for w in self._workers.values():
                    if w.status not in _FINAL:
                        w.status, w.error = "errored", "codex app-server exited"
                self._cv.notify_all()
            self._server.start()
            self._client = CodexClient(
                self._server.socket_path,
                on_notification=self._on_notification,
                on_server_request=self._on_server_request,
            )
            return self._client

    def stop(self) -> None:
        """Stop the server; every open edge this daemon holds becomes ``closed``."""
        with self._cv:
            client, self._client = self._client, None
            open_ids = [t for t, w in self._workers.items() if w.status != "shutdown"]
            for w in self._workers.values():
                w.status = "shutdown"
        if client is not None:
            client.close()
        self._server.stop()
        if open_ids:
            conn = open_event_log(self._event_log)
            try:
                close_edges(conn, open_ids)
            finally:
                conn.close()

    # --- fold (reader thread) --------------------------------------------

    def _begin(self, w: Worker, turn_id: str) -> None:
        if w.turn_id != turn_id:
            w.turn_id = turn_id
            w.status = "running"
            w.final_message = None
            w.error = None

    def _on_notification(self, msg: Mapping[str, Any]) -> None:
        method = msg.get("method")
        params = msg.get("params") or {}
        with self._cv:
            w = self._workers.get(params.get("threadId", ""))
            if w is None:
                return
            if method == "turn/started":
                self._begin(w, params["turn"]["id"])
            elif method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") == "agentMessage" and w.turn_id in (None, params.get("turnId")):
                    w.final_message = item.get("text")
            elif method == "turn/completed":
                turn = params["turn"]
                if w.turn_id not in (None, turn["id"]):
                    return
                w.turn_id = turn["id"]
                w.pending_approval = None
                if turn["status"] == "failed":
                    w.status = "errored"
                    w.error = (turn.get("error") or {}).get("message", "turn failed")
                elif turn["status"] == "interrupted":
                    w.status = "interrupted"
                else:
                    w.status = "completed"
            elif method == "error" and not params.get("willRetry"):
                w.status = "errored"
                w.error = params["error"]["message"]
            else:
                return
            LOGGER.info("worker %s %s (%s)", w.thread_id, w.status, method)
            self._cv.notify_all()

    def _on_server_request(self, msg: Mapping[str, Any]) -> None:
        method = str(msg.get("method"))
        params = msg.get("params") or {}
        with self._cv:
            client = self._client
            w = self._workers.get(params.get("threadId", ""))
            if w is None or method not in _APPROVAL_METHODS:
                if client is not None:
                    client.respond_error(msg["id"], f"{method} is not routed by jarvis")
                return
            w.pending_approval = {"request_id": msg["id"], "method": method, "params": params}
            LOGGER.info("worker %s asks approval via %s", w.thread_id, method)
            self._cv.notify_all()

    # --- operations (turn thread) -----------------------------------------

    def _get(self, worker_id: str) -> Worker:
        w = self._workers.get(worker_id)
        if w is None:
            msg = f"worker {worker_id} not found"
            raise ToolError(msg, code="not_found")
        return w

    def _turn_start(self, client: CodexClient, w: Worker, text: str) -> None:
        turn = client.request(
            "turn/start",
            {"threadId": w.thread_id, "input": [{"type": "text", "text": text}]},
        )["turn"]
        with self._cv:
            self._begin(w, turn["id"])

    def spawn(self, message: str, cwd: Path, *, parent: str, conn: sqlite3.Connection) -> str:
        """``thread/start`` + ``turn/start``; record the edge; return the thread id."""
        client = self._ensure()
        thread = client.request(
            "thread/start",
            {"cwd": str(cwd), "sandbox": "workspace-write", "approvalPolicy": "on-request"},
        )["thread"]
        w = Worker(thread_id=thread["id"])
        with self._cv:
            self._workers[w.thread_id] = w
        open_edge(conn, parent=parent, child=w.thread_id)
        LOGGER.info("worker %s spawned by %s in %s", w.thread_id, parent, cwd)
        self._turn_start(client, w, message)
        return w.thread_id

    def wait(self, ids: list[str], timeout_s: float) -> dict[str, Any]:
        """Block until any worker in ``ids`` is final or asks approval; then drain the rest."""
        deadline = time.monotonic() + timeout_s
        with self._cv:
            while True:
                ready = {
                    i: self._workers[i].view() if i in self._workers else {"status": "not_found"}
                    for i in ids
                    if i not in self._workers or self._workers[i].ready
                }
                remaining = deadline - time.monotonic()
                if ready or remaining <= 0:
                    return {"status": ready, "timed_out": not ready}
                self._cv.wait(remaining)

    def send_input(self, worker_id: str, message: str, *, interrupt: bool) -> dict[str, Any]:
        """Steer a running turn, or start one; ``interrupt`` cuts the running turn first."""
        client = self._ensure()
        with self._cv:
            w = self._get(worker_id)
            if w.status == "shutdown":
                msg = f"worker {worker_id} is shut down"
                raise ToolError(msg, code="shutdown")
            previous = w.view()
            running_turn = w.turn_id if w.status == "running" else None
        if running_turn is not None and interrupt:
            client.request("turn/interrupt", {"threadId": worker_id, "turnId": running_turn})
            with self._cv:
                self._cv.wait_for(lambda: w.status != "running", timeout=_INTERRUPT_SETTLE_S)
                running_turn = w.turn_id if w.status == "running" else None
        if running_turn is not None:
            client.request(
                "turn/steer",
                {
                    "threadId": worker_id,
                    "expectedTurnId": running_turn,
                    "input": [{"type": "text", "text": message}],
                },
            )
        else:
            self._turn_start(client, w, message)
        delivered = "steer" if running_turn is not None else "turn"
        return {"worker_id": worker_id, "previous": previous, "delivered": delivered}

    def answer_approval(self, worker_id: str, decision: str) -> dict[str, Any]:
        """Reply to the worker's pending approval request with Allen's decision."""
        if decision not in DECISIONS:
            msg = f"send_input: approval must be one of {', '.join(DECISIONS)}"
            raise ToolError(msg, code="bad_approval")
        with self._cv:
            w = self._get(worker_id)
            pending, w.pending_approval = w.pending_approval, None
            client = self._client
        if pending is None or client is None:
            msg = f"worker {worker_id} has no approval pending"
            raise ToolError(msg, code="no_pending_approval")
        client.respond(pending["request_id"], {"decision": decision})
        LOGGER.info("worker %s approval %s: %s", worker_id, pending["method"], decision)
        return {"worker_id": worker_id, "answered": pending["method"], "decision": decision}

    def close(self, worker_id: str, *, conn: sqlite3.Connection) -> dict[str, Any]:
        """Interrupt if running, unsubscribe, mark the edge closed; return the previous status."""
        with self._cv:
            w = self._get(worker_id)
            previous = w.view()
            running_turn = w.turn_id if w.status == "running" else None
            client = self._client
        if client is not None and w.status != "shutdown":
            if running_turn is not None:
                client.request("turn/interrupt", {"threadId": worker_id, "turnId": running_turn})
            client.request("thread/unsubscribe", {"threadId": worker_id})
        with self._cv:
            w.status = "shutdown"
            w.pending_approval = None
            self._cv.notify_all()
        close_edges(conn, (worker_id,))
        LOGGER.info("worker %s closed", worker_id)
        return {"worker_id": worker_id, "previous": previous}


# --- the four tools ----------------------------------------------------------


def _codex_errors(fn: FlatHandler) -> FlatHandler:
    """A dead or refusing app-server is a tool error, not a crash."""

    def wrapped(args: Mapping[str, Any], ctx: ToolContext) -> Mapping[str, Any]:
        try:
            return fn(args, ctx)
        except CodexAppServerError as exc:
            raise ToolError(str(exc), code="codex_app_server") from exc

    wrapped.__name__ = fn.__name__
    return wrapped


def make_worker_tools(workers: Workers) -> tuple[Tool, ...]:
    """Bind the four worker tools to one :class:`Workers`."""
    llm = frozenset({CallerPrincipal.JARVIS_LLM})

    @tool(
        description=(
            "Start a Codex worker on a task and return its worker_id at once. The worker "
            "runs in its own thread with a workspace-write sandbox rooted at cwd and asks "
            "for approval before anything outside it. Use wait_worker to get its final "
            "message; close_worker when you are done with it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "The task, in plain text."},
                "cwd": {
                    "type": "string",
                    "description": (
                        "Absolute path of the project directory the worker may write in. "
                        "Defaults to the home directory."
                    ),
                },
            },
            "required": ["message"],
        },
        allowed_callers=llm,
        risk_level="L2",
        read_only=False,
    )
    @_codex_errors
    def spawn_worker(args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        message = str(args.get("message", "")).strip()
        if not message:
            msg = "spawn_worker: message is empty"
            raise ToolError(msg, code="empty_message")
        cwd = Path(str(args.get("cwd") or Path.home())).expanduser()
        if not cwd.is_dir():
            msg = f"spawn_worker: cwd {cwd} is not a directory"
            raise ToolError(msg, code="bad_cwd")
        worker_id = workers.spawn(message, cwd, parent=ctx.action_id, conn=ctx.conn)
        return {"worker_id": worker_id, "status": "running"}

    @tool(
        description=(
            "Wait until any of the given workers reaches a final status (completed with its "
            "final_message, errored, shutdown, not_found) or asks for approval "
            "(pending_approval, to be put to Allen and answered with send_input). Returns "
            "the ones that are ready; timed_out=true and an empty status when none is. "
            "Prefer long waits over polling."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "worker_ids to wait on; returns when the first is ready.",
                },
                "timeout_s": {
                    "type": "number",
                    "description": (
                        f"Seconds to wait, default {DEFAULT_WAIT_S:g}, max {MAX_WAIT_S:g}."
                    ),
                },
            },
            "required": ["ids"],
        },
        allowed_callers=llm,
        risk_level="L0",
        read_only=True,
    )
    def wait_worker(args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        ids = [str(i) for i in args.get("ids") or []]
        if not ids:
            msg = "wait_worker: ids is empty"
            raise ToolError(msg, code="empty_ids")
        raw = args.get("timeout_s", DEFAULT_WAIT_S)
        timeout_s = min(max(float(raw), 0.0), MAX_WAIT_S)
        return workers.wait(ids, timeout_s)

    @tool(
        description=(
            "Send a message to an existing worker: a follow-up task if it is idle, delivered "
            "into the running turn otherwise. interrupt=true stops the running turn first. "
            "With approval set, instead answers the worker's pending approval request with "
            "Allen's decision and sends no message."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "worker_id from spawn_worker."},
                "message": {"type": "string", "description": "Message text for the worker."},
                "interrupt": {
                    "type": "boolean",
                    "description": "Stop the running turn before delivering. Default false.",
                },
                "approval": {
                    "type": "string",
                    "enum": list(DECISIONS),
                    "description": "Allen's answer to the worker's pending_approval.",
                },
            },
            "required": ["id"],
        },
        allowed_callers=llm,
        risk_level="L2",
        read_only=False,
    )
    @_codex_errors
    def send_input(args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        worker_id = str(args.get("id", ""))
        approval = args.get("approval")
        if approval is not None:
            return workers.answer_approval(worker_id, str(approval))
        message = str(args.get("message", "")).strip()
        if not message:
            msg = "send_input: message is empty"
            raise ToolError(msg, code="empty_message")
        return workers.send_input(worker_id, message, interrupt=bool(args.get("interrupt")))

    @tool(
        description=(
            "Close a worker when it is no longer needed, interrupting its turn if one is "
            "running. Returns the status it had before closing."
        ),
        input_schema={
            "type": "object",
            "properties": {"id": {"type": "string", "description": "worker_id to close."}},
            "required": ["id"],
        },
        allowed_callers=llm,
        risk_level="L1",
        read_only=False,
    )
    @_codex_errors
    def close_worker(args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return workers.close(str(args.get("id", "")), conn=ctx.conn)

    return (spawn_worker, wait_worker, send_input, close_worker)
