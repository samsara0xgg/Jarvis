"""ADR 0366: a letter, the Gmail draft that holds it, and the events that remember it.

A draft is a real Gmail draft (``gmail_createDraft``): it shows in Gmail on every device and
survives a restart. The tool records each one it writes (the draft's and its message's Gmail ids,
the conversation, the letter) as ``mail.draft_written``, and how each ended as
``mail.draft_closed`` (``replaced``, ``discarded`` or ``sent``), so a send from any device, and the
Dashboard's mail page, can name a draft. The server can neither update nor delete a draft: a
revision writes a new one and moves the old message to Trash, and where Gmail refuses the result
says the older draft is still in Drafts.

:class:`Letters` composes a letter from what he said (recipients by name or address, a reply, its
thread and subject), writes and discards drafts, and sends: the draft itself when the letter is
unchanged, else a new draft that replaces it. A send that fails leaves no draft this call wrote.

Layer rules: execution; ``jarvis.execution.mail_gmail``, ``mail_people`` and
``jarvis.state.event_log``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution import speakable
from jarvis.execution.mail_people import Who, own_marks
from jarvis.execution.tools import ToolError
from jarvis.shared.text import split_person
from jarvis.state.event_log import emit_event, iter_events_of_types

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence
    from datetime import datetime, tzinfo
    from sqlite3 import Connection

    from jarvis.execution.mail_gmail import Draft, Mailbox
    from jarvis.execution.mail_people import People
    from jarvis.state.mail_message import Mail, Person

SHORT: Final = 8
"""How much of a draft's event id is its number: the start of it names the draft."""
BODY_MAX: Final = 20000
"""The most characters a letter's body takes here."""
BODY_SHOWN: Final = 2000
"""How much of the body a result shows."""
_REPLY: Final = re.compile(
    r"^\s*(?:re|\u56de\u590d|\u7b54\u590d|\u56de\u8986)\s*[:\uff1a]", re.IGNORECASE,
)
_SUBJECT_CHARS: Final = 80
_WRITTEN: Final = "mail.draft_written"
_CLOSED: Final = "mail.draft_closed"


def bare(shown: str) -> str:
    """The address in ``Name <address>`` (or the address itself)."""
    return split_person(shown)[1]


def reply_subject(subject: str) -> str:
    """``Re: `` and the subject, unless it already starts with ``Re:`` or its Chinese form."""
    return subject if _REPLY.match(subject) else f"Re: {subject}".rstrip()


@dataclass(frozen=True)
class Letter:
    """A letter as it will go: recipients as ``Name <address>`` (or an address), and its text."""

    to: tuple[str, ...]
    cc: tuple[str, ...]
    bcc: tuple[str, ...]
    subject: str
    body: str
    thread: str = ""
    """The conversation it answers; empty for a new letter."""

    def addresses(self, group: Sequence[str]) -> list[str]:
        """The bare addresses of ``group`` (``to``, ``cc`` or ``bcc``)."""
        return [bare(one) for one in group]

    def same_as(self, other: Letter) -> bool:
        """Whether it goes to the same addresses with the same subject, body and thread."""
        def key(one: Letter) -> tuple[object, ...]:
            return (
                [a.lower() for a in one.addresses(one.to)],
                [a.lower() for a in one.addresses(one.cc)],
                [a.lower() for a in one.addresses(one.bcc)],
                one.subject, one.body, one.thread,
            )
        return key(self) == key(other)

    def to_payload(self) -> dict[str, Any]:
        """The event's form."""
        return {
            "to": list(self.to), "cc": list(self.cc), "bcc": list(self.bcc),
            "subject": self.subject, "body": self.body, "thread": self.thread,
        }

    @classmethod
    def from_payload(cls, data: object) -> Letter:
        """A letter from an event's form; empty fields where it is not one."""
        got = data if isinstance(data, dict) else {}

        def names(key: str) -> tuple[str, ...]:
            value = got.get(key)
            return tuple(str(one) for one in value) if isinstance(value, list) else ()

        return cls(
            names("to"), names("cc"), names("bcc"), str(got.get("subject") or ""),
            str(got.get("body") or ""), str(got.get("thread") or ""),
        )


@dataclass(frozen=True)
class Written:
    """A draft the tool wrote, and how it ended."""

    uid: str
    """The id of its ``mail.draft_written`` event."""
    draft_id: str
    message_id: str
    thread: str
    """Its conversation in Gmail: the one it answers, or the new one Gmail gave it."""
    letter: Letter
    at_ms: int
    reply_to: str = ""
    replaces: str = ""
    ended: str = ""
    """Empty while the draft is open; else ``replaced``, ``discarded`` or ``sent``."""
    in_drafts: bool = False
    """It ended, but Gmail would not move it to Trash, so it is still in Drafts."""
    replaced_by: str = ""
    """The event id of the draft that took its place."""
    by: str = ""
    """``owner`` when his own edit on the Dashboard's page wrote it; empty when the tool did."""

    @property
    def short(self) -> str:
        """The draft's number."""
        return self.uid[:SHORT]

    @property
    def is_open(self) -> bool:
        """Neither replaced, discarded nor sent."""
        return not self.ended


@dataclass(frozen=True)
class Cause:
    """What an event is written under: the tool call, or (None) Allen's own edit on the page."""

    action_id: str = ""
    source: str | None = None
    actor: str | None = None
    by: str = ""
    """``owner`` for the page's edit; it is kept on the draft that edit writes."""

    def fields(self) -> dict[str, Any]:
        """The keyword arguments of ``emit_event`` for it."""
        return {
            "source_event_id": self.source,
            "correlation": {"action_id": self.action_id} if self.action_id else None,
            "actor": self.actor,
        }


# --- the ledger --------------------------------------------------------------------------------


def drafts(conn: Connection) -> list[Written]:
    """Every draft the tool wrote, oldest first, each with how it ended."""
    found: dict[str, Written] = {}
    for event in iter_events_of_types(conn, [_WRITTEN, _CLOSED]):
        said = event.payload
        draft_id = str(said.get("draft_id") or "")
        if event.type == _WRITTEN:
            found[draft_id] = Written(
                event.event_uid, draft_id, str(said.get("message_id") or ""),
                str(said.get("thread_id") or ""), Letter.from_payload(said.get("letter")),
                event.ts_epoch_ms, str(said.get("reply_to") or ""),
                str(said.get("replaces") or ""), by=str(said.get("by") or ""),
            )
        elif (before := found.get(draft_id)) is not None:
            found[draft_id] = replace(
                before, ended=str(said.get("how") or "discarded"),
                in_drafts=bool(said.get("in_drafts")),
            )
    after = {one.replaces: one.uid for one in found.values() if one.replaces}
    return [replace(one, replaced_by=after.get(one.uid, "")) for one in found.values()]


def named(held: Sequence[Written], given: str) -> Written:
    """The draft ``given`` names: its number (the start of its event id) or Gmail's draft id."""
    said = given.strip()
    found = [
        one for one in held
        if one.draft_id == said or (len(said) >= SHORT and one.uid.startswith(said.lower()))
    ]
    if len(found) != 1:
        msg = f"mail: {given} does not name one of his drafts; use the draft number a draft gave"
        raise ToolError(msg, code="invalid_arguments")
    return found[0]


def open_in(conn: Connection, thread: str) -> Written | None:
    """The newest draft still open in a conversation: the one the Dashboard shows under it."""
    found = [one for one in drafts(conn) if one.is_open and one.thread == thread]
    return found[-1] if found else None


def how_it_ended(one: Written, held: Sequence[Written]) -> str:
    """A closed draft as a sentence: how it ended, and what took its place."""
    if one.ended == "replaced":
        by = next((other for other in held if other.uid == one.replaced_by), None)
        if by is not None:
            ending = f"; it ended as {by.ended}" if by.ended and by.ended != "replaced" else ""
            return f"was replaced by draft {by.short}{ending}"
        return "was replaced by a newer draft"
    return {"sent": "was sent already", "discarded": "was discarded"}.get(one.ended, "is closed")


def record_written(  # noqa: PLR0913 — the draft Gmail made, its letter and why it was written.
    conn: Connection, made: Draft, letter: Letter, cause: Cause, *, reply_to: str = "",
    replaces: str = "",
) -> Written:
    """Write the ``mail.draft_written`` row for a draft Gmail just made."""
    payload: dict[str, Any] = {
        "draft_id": made.id, "message_id": made.message, "thread_id": made.thread,
        "letter": letter.to_payload(),
    }
    if cause.action_id:
        payload["action_id"] = cause.action_id
    if reply_to:
        payload["reply_to"] = reply_to
    if replaces:
        payload["replaces"] = replaces
    if cause.by:
        payload["by"] = cause.by
    event = emit_event(conn, type=_WRITTEN, payload=payload, **cause.fields())
    return Written(
        event.event_uid, made.id, made.message, made.thread, letter, event.ts_epoch_ms,
        reply_to, replaces, by=cause.by,
    )


def record_closed(  # noqa: PLR0913 — the draft, how it ended and what Gmail did about it.
    conn: Connection, draft: Written, how: str, cause: Cause, *, in_drafts: bool = False,
    undoes: str = "", message_id: str = "",
) -> None:
    """Write the ``mail.draft_closed`` row: the draft ended as ``how``."""
    payload: dict[str, Any] = {"draft_id": draft.draft_id, "how": how}
    if cause.action_id:
        payload["action_id"] = cause.action_id
    if undoes:
        payload["undoes"] = undoes
    if in_drafts:
        payload["in_drafts"] = True
    if message_id:
        payload["message_id"] = message_id
    emit_event(conn, type=_CLOSED, payload=payload, **cause.fields())


# --- composing ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Composed:
    """A letter and who it goes to, as the tool built it."""

    letter: Letter
    who: tuple[Who, ...]
    """Everyone it goes to (to, cc, bcc), each once, with his own marked."""
    reply_to: str = ""
    """The id of the mail it answers."""
    answers: str = ""
    """That mail in words: who wrote it and when."""
    notes: tuple[str, ...] = field(default_factory=tuple)
    """How each name was found: the last mail with that person."""

    def marked(self, group: Sequence[str]) -> list[str]:
        """``group`` as ``Name <address>``, with (you) after his own."""
        mine = set(own_marks(self.who))
        return [
            f"{one} (you)" if bare(one).lower() in mine else one for one in group
        ]


def _who(one: Person, own: Collection[str]) -> Who:
    return Who(one.name, one.address, one.address.lower() in own)


def reply_recipients(
    mail: Mail, own: Collection[str], to: list[Who], cc: list[Who], *, everyone: bool,
) -> tuple[list[Who], list[Who]]:
    """Who an answer to ``mail`` goes to: its Reply-To, else its From (its To when it is his).

    ``to`` given by him replaces that; ``everyone`` adds the mail's To and Cc without his own
    addresses (its To to the To, its Cc to the Cc).
    """
    target = mail.reply_to or (
        (mail.sender,) if mail.sender.address.lower() not in own else mail.to
    )
    out_to = to or [_who(one, own) for one in target if one.address]
    out_cc = list(cc)
    if everyone:
        out_to += [_who(one, own) for one in mail.to if one.address.lower() not in own]
        out_cc += [_who(one, own) for one in mail.cc if one.address.lower() not in own]
    return out_to, out_cc


def _once(groups: Sequence[Sequence[Who]]) -> list[list[Who]]:
    """Each address in the first group it is in only."""
    seen: set[str] = set()
    out: list[list[Who]] = []
    for group in groups:
        kept = []
        for one in group:
            key = one.address.lower()
            if key not in seen:
                seen.add(key)
                kept.append(one)
        out.append(kept)
    return out


class Letters:
    """Letters over one :class:`Mailbox`: composed, written as drafts, discarded and sent."""

    def __init__(self, box: Mailbox, people: People) -> None:
        """Hold the mailbox the drafts are in and the lookup that names recipients."""
        self._box = box
        self._people = people

    def compose(
        self, given: Mapping[str, Any], replied: str, now: datetime, zone: tzinfo, action: str,
    ) -> Composed:
        """The letter ``given`` describes: recipients named or found, a reply's thread and subject.

        ``replied`` is the id of the mail it answers, or empty. Failures say what to ask him.
        """
        own = self._box.own(now) if replied else frozenset[str]()
        to = self._people.resolve(given.get("to"), now, zone)
        cc = self._people.resolve(given.get("cc"), now, zone)
        bcc = self._people.resolve(given.get("bcc"), now, zone)
        subject, thread, answers = str(given.get("subject") or ""), "", ""
        if replied:
            read = self._box.read(replied)
            to, cc = reply_recipients(
                read.mail, own, to, cc, everyone=bool(given.get("reply_all")),
            )
            thread, subject = read.thread, subject or reply_subject(read.mail.subject)
            who = read.mail.sender.shown or "an unknown sender"
            when = f", {speakable.when(read.mail.at, now, zone)}" if read.mail.at else ""
            title = speakable.cut(read.mail.subject, _SUBJECT_CHARS) or "(no subject)"
            answers = f'{who}\'s mail "{title}"{when}'
        elif given.get("reply_all"):
            msg = f"mail: reply_all needs reply_to=<the mail it answers>; {action} has none"
            raise ToolError(msg, code="invalid_arguments")
        body = str(given.get("body") or "")
        problem = self.problem(to, subject, body, action)
        if problem:
            raise ToolError(problem, code="invalid_arguments")
        to, cc, bcc = _once([to, cc, bcc])
        letter = Letter(
            tuple(one.shown for one in to), tuple(one.shown for one in cc),
            tuple(one.shown for one in bcc), subject, body, thread,
        )
        everyone = (*to, *cc, *bcc)
        notes = tuple(
            f"{one.shown} was found in his mail ({one.note})." for one in everyone if one.note
        )
        return Composed(letter, everyone, replied, answers, notes)

    @staticmethod
    def problem(to: Sequence[Who], subject: str, body: str, action: str) -> str:
        """What is wrong with a letter's recipients, subject and body; empty when nothing."""
        if not to:
            return f"mail: {action} needs to=<who>, or reply_to=<the mail it answers>"
        if not subject.strip():
            return f"mail: a new letter needs a subject; {action} has none"
        if not body.strip():
            return f"mail: {action} needs body=<the letter's text>"
        if len(body) > BODY_MAX:
            return f"mail: the body is {len(body)} characters; a letter here takes up to {BODY_MAX}"
        return ""

    def parse(self, letter: Letter, now: datetime) -> tuple[Who, ...]:
        """The recipients of a letter already made, with his own marked."""
        own = self._box.own(now)
        out = []
        for shown in (*letter.to, *letter.cc, *letter.bcc):
            name, address = split_person(shown)
            out.append(Who(" ".join(name.split()), address, address.lower() in own))
        return tuple(out)

    # --- Gmail ---------------------------------------------------------------------------------

    def _made(self, letter: Letter) -> Draft:
        return self._box.draft(
            to=letter.addresses(letter.to), cc=letter.addresses(letter.cc),
            bcc=letter.addresses(letter.bcc), subject=letter.subject, body=letter.body,
            thread=letter.thread,
        )

    def write(
        self, conn: Connection, composed: Composed, cause: Cause,
        *, replaces: Written | None = None,
    ) -> tuple[Written, bool]:
        """Write the letter as a Gmail draft, then retire the draft it replaces.

        The result is the new draft and whether the old one is still in Drafts (Gmail would not
        move it to Trash); the new draft is written either way.
        """
        made = self._made(composed.letter)
        written = record_written(
            conn, made, composed.letter, cause, reply_to=composed.reply_to,
            replaces="" if replaces is None else replaces.uid,
        )
        left = False
        if replaces is not None:
            left = not self.retire(conn, replaces, "replaced", cause)
        return written, left

    def retire(
        self, conn: Connection, old: Written, how: str, cause: Cause, *, undoes: str = "",
    ) -> bool:
        """Move a draft's message to Trash and record how it ended; False when Gmail refused."""
        moved = self._box.trash(old.message_id)
        record_closed(conn, old, how, cause, in_drafts=not moved, undoes=undoes)
        return moved

    def discard(self, conn: Connection, old: Written, cause: Cause) -> bool:
        """The undo of a draft: the same as retiring it, but a refusal leaves it open.

        Gmail refusing to move it to Trash means it is still a draft he can send, so nothing is
        recorded and the caller says so.
        """
        if not self._box.trash(old.message_id):
            return False
        record_closed(conn, old, "discarded", cause, undoes=old.uid)
        return True

    # --- sending -------------------------------------------------------------------------------

    def _state(self, message: str) -> frozenset[str] | None:
        """The labels a message holds now; None when that cannot be found out."""
        try:
            return self._box.labels([message]).get(message)
        except ToolError:
            return None

    def _deliver(self, draft: str, message: str) -> str:
        """Send a Gmail draft; the id of the message Gmail says it sent.

        Raises :class:`UnsentError` when it did not say so: ``draft_left`` is True when the
        message's labels say it is still a draft, False when it is no longer one, None when that
        cannot be told. A draft whose labels say it was sent after all counts as sent.
        """
        try:
            return self._box.send_draft(draft)
        except ToolError as exc:
            labels = self._state(message)
            if labels is not None and "SENT" in labels and "DRAFT" not in labels:
                return message
            raise UnsentError(
                str(exc), code=exc.code, draft_left=None if labels is None else "DRAFT" in labels,
            ) from exc

    def send(
        self, conn: Connection, letter: Letter, draft: Written | None, cause: Cause,
    ) -> str:
        """Send ``letter``: ``draft`` itself when it is open and unchanged, else a new draft.

        The new draft replaces the old, which moves to Trash after the send. The id of the
        message Gmail says it sent. A send that fails moves the draft this call wrote to Trash
        and raises the failure worded for the model (:class:`UnsentError`'s ``worded``).
        """
        open_one = draft if draft is not None and draft.is_open else None
        if open_one is not None and open_one.letter.same_as(letter):
            try:
                sent = self._deliver(open_one.draft_id, open_one.message_id)
            except UnsentError as exc:
                failure = exc.worded("kept", open_one.short)
                raise failure from exc
            record_closed(conn, open_one, "sent", cause, message_id=sent)
            return sent
        made = self._made(letter)
        written = record_written(
            conn, made, letter, cause, replaces="" if open_one is None else open_one.uid,
        )
        try:
            sent = self._deliver(made.id, made.message)
        except UnsentError as exc:
            if exc.draft_left is True:
                how = "trashed" if self.retire(conn, written, "discarded", cause) else "stuck"
            elif exc.draft_left is False:
                record_closed(conn, written, "discarded", cause)
                how = "gone"
            else:
                how = "unknown"
            failure = exc.worded(how)
            raise failure from exc
        record_closed(conn, written, "sent", cause, message_id=sent)
        if open_one is not None:
            self.retire(conn, open_one, "replaced", cause)
        return sent


class UnsentError(ToolError):
    """Gmail did not answer that the letter was sent."""

    def __init__(self, detail: str, *, code: str, draft_left: bool | None) -> None:
        """Keep Gmail's own wording and where the draft stands."""
        super().__init__(detail, code=code)
        self.detail = detail
        self.draft_left = draft_left

    def worded(self, how: str, short: str = "") -> ToolError:
        """The failure as the model should relay it, and what became of the draft.

        ``how``: ``kept`` (the draft he named is still in Drafts, ``short`` its number),
        ``trashed`` (the draft this call wrote was moved to Trash), ``stuck`` (Gmail would not
        move it), ``gone`` (it is no longer a draft) or ``unknown`` (it may have gone).
        """
        if self.draft_left is None or how == "unknown":
            msg = (
                f"mail: it is not known whether the letter went: {self.detail} Ask him to look in"
                " his Sent folder before sending it again."
            )
            return ToolError(msg, code=self.code, kind="dont_retry")
        fate = {
            "kept": f"; draft {short} is still in his Drafts",
            "trashed": ", and the draft this call wrote was moved to Trash",
            "stuck": (
                ", and the draft this call wrote is still in his Drafts (Gmail would not move it"
                " to Trash)"
            ),
            "gone": "",
        }[how]
        return ToolError(f"mail: the letter was not sent{fate}. {self.detail}", code=self.code)


__all__ = [
    "BODY_MAX",
    "BODY_SHOWN",
    "SHORT",
    "Cause",
    "Composed",
    "Letter",
    "Letters",
    "UnsentError",
    "Written",
    "bare",
    "drafts",
    "how_it_ended",
    "named",
    "open_in",
    "record_closed",
    "record_written",
    "reply_recipients",
    "reply_subject",
]
