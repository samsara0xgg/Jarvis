"""ADR 0363: a mail read from its raw message.

Invented mail goes through ``parse`` (the real ``email`` package and the real reader) and the
tests assert what the text holds: what was dropped, what stayed, what was said to be left out.
"""

from __future__ import annotations

import pytest

from jarvis.state.mail_message import Attachment, attachment_text, parse
from jarvis.state.mail_text import clean, is_forward, read_html, read_plain
from tests.support.mail_raw import PADDING, pdf, raw, scanned_pdf

MARKETING = f"""<html><head><style>.x {{ color: red }}</style><title>Hi</title></head><body>
<!--[if mso]><table><tr><td>MSO ONLY COPY</td></tr></table><![endif]-->
<div style="display:none;font-size:1px;max-height:0px;opacity:0;mso-hide:all">Hidden preheader</div>
<div style="font-size:0">&zwnj;&nbsp;{PADDING}</div>
<table width="600"><tr><td><h1>Your statement is ready</h1></td></tr>
<tr><td><table><tr><td>Statement balance</td><td>$1,234.56</td></tr>
<tr><td>Minimum payment due</td><td>$35.00</td></tr>
<tr><td>Payment due date</td><td>Nov 3, 2026</td></tr></table></td></tr>
<tr><td><a href="https://click.mail.example.com/t/abc?u=123&amp;x=y">Pay now</a><a
 href="https://www.example.com/help?utm=1">Help</a></td></tr>
<tr><td>Questions? Visit https://www.example.com/support/faq?utm_source=mail&amp;id=9,
 or call.</td></tr>
</table>
<img src="https://t.example.com/pixel.gif" width="1" height="1">
<img src="https://t.example.com/logo.png" alt="Logo">
<p>Reply to <a href="mailto:help@example.com">help@example.com</a> any time.</p>
<ul><li>One<li>Two<ul><li>Two a</li></ul></li><li>Three</li></ul>
<!--[if !mso]><!--><p>Shown once</p><!--<![endif]-->
</body></html>"""

GMAIL_REPLY = (
    '<div dir="ltr">Thursday works for me.<div><br></div><div>-- <br><div dir="ltr">Sam</div>'
    "</div></div><br>"
    '<div class="gmail_quote gmail_quote_container"><div dir="ltr" class="gmail_attr">On Mon, '
    'Oct 5, 2026 at 3:04 PM Alex Wu &lt;alex@example.com&gt; wrote:<br></div>'
    '<blockquote class="gmail_quote" style="margin:0px 0px 0px 0.8ex">'
    '<div dir="ltr">Can you do Thursday or Friday?</div></blockquote></div>'
)
GMAIL_FORWARD = (
    '<div dir="ltr">FYI, see below.<br><br><div class="gmail_quote gmail_quote_container">'
    '<div dir="ltr" class="gmail_attr">---------- Forwarded message ---------<br>From: '
    '<strong>Alex Wu</strong> &lt;<a href="mailto:alex@example.com">alex@example.com</a>&gt;<br>'
    "Date: Mon, Oct 5, 2026 at 3:04 PM<br>Subject: Contract<br></div><br><br>"
    "<div>The contract is attached. Please sign by Friday.</div>"
    '<div class="gmail_quote"><blockquote class="gmail_quote"><div>History inside the forward'
    "</div></blockquote></div></div></div>"
)
OUTLOOK_REPLY = (
    "<html><body><div>Sounds good, see you then.</div>"
    '<div id="appendonsend"></div><hr style="display:inline-block;width:98%" tabindex="-1">'
    '<div id="divRplyFwdMsg" dir="ltr"><font face="Calibri"><b>From:</b> Alex Wu '
    "&lt;alex@example.com&gt;<br><b>Sent:</b> Monday, October 5, 2026 3:04 PM<br>"
    "<b>Subject:</b> Lunch</font><div>&nbsp;</div></div><div>Lunch on Thursday?</div>"
    "</body></html>"
)
APPLE_REPLY = (
    "<html><body><div>Yes, ten works.</div><br>"
    '<blockquote type="cite"><div>On Oct 5, 2026, at 3:04 PM, Alex Wu &lt;alex@example.com&gt; '
    'wrote:</div><br class="Apple-interchange-newline"><div>Is ten ok?</div></blockquote>'
    "</body></html>"
)
APPLE_FORWARD = (
    "<html><body><div>Look at this.</div><div>Begin forwarded message:</div>"
    '<blockquote type="cite"><div><b>From:</b> Alex Wu</div><div>The key is under the mat.</div>'
    "</blockquote></body></html>"
)
THUNDERBIRD = (
    '<html><body><p>Ten is fine.</p><div class="moz-cite-prefix">On 10/5/26 3:04 PM, Alex Wu '
    'wrote:<br></div><blockquote type="cite" cite="mid:abc@example.com"><p>Is ten ok?</p>'
    "</blockquote></body></html>"
)
YAHOO = (
    '<div>Thanks!</div><div class="yahoo_quoted"><div>On Monday, October 5, 2026, 3:04 PM, '
    "Alex Wu wrote:</div><div>Done.</div></div>"
)
NEWSLETTER = (
    "<html><body><p>On Wednesday, an analyst wrote: shares will rise.</p>"
    "<p>Second paragraph.</p></body></html>"
)


def _text(html: str, subject: str = "Re: Lunch") -> str:
    return parse(raw(subject, html=html)).text


# --- html: what the mail hides, cells, links ---------------------------------------------------


def test_hidden_text_padding_and_mso_copies_are_dropped_before_anything_is_read() -> None:
    """A preheader, padding characters and an MSO-only copy are not in the text."""
    text = parse(raw("Your statement", html=MARKETING)).text
    assert "Hidden preheader" not in text
    assert "MSO ONLY COPY" not in text
    assert not set(PADDING) & set(text)
    assert text.count("Shown once") == 1  # what only other clients get is kept, once
    assert "Logo" not in text  # a picture's alt text is not read, and a pixel is no text


def test_table_cells_stay_apart_a_line_per_row() -> None:
    """A label and its amount are not glued: cells join with a bar, rows are lines."""
    lines = parse(raw("Your statement", html=MARKETING)).text.split("\n")
    assert "Statement balance | $1,234.56" in lines
    assert "Minimum payment due | $35.00" in lines
    assert "Payment due date | Nov 3, 2026" in lines


def test_a_link_reads_as_its_text_and_its_site_and_a_bare_address_without_its_query() -> None:
    """Links name where they go; tracking queries are not text."""
    text = parse(raw("Your statement", html=MARKETING)).text
    assert "Pay now (click.mail.example.com) Help (example.com)" in text
    assert "Visit example.com/support/faq, or call." in text
    assert "help@example.com" in text
    assert "utm_source" not in text
    assert "u=123" not in text


def test_lists_and_blocks_keep_their_own_lines() -> None:
    """List items are lines with a dash; headings and paragraphs are apart."""
    text = parse(raw("Your statement", html=MARKETING)).text
    assert "- One\n- Two\n- Two a\n- Three" in text
    assert text.startswith("Your statement is ready\nStatement balance | $1,234.56\n")


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("<p>a<br>b</p>", "a\nb"),
        ("<p>a</p><p>b</p>", "a\n\nb"),
        ("<div>a</div><div>b</div>", "a\nb"),
        ("<p>one<p>two", "one\n\ntwo"),  # an unclosed paragraph ends where the next begins
        ("<table><tr><td>a<td>b<tr><td>c<td>d</table>", "a | b\nc | d"),
        ("<b>bold</b>text <i>and</i> more", "boldtext and more"),
        ("<table><tr><td>only one</td></tr><tr><td>cell per row</td></tr></table>",
         "only one\ncell per row"),
        ("<pre>line one\n  line two</pre>", "line one\n  line two".replace("  ", "")),
        ("<div style='font-size:0'><span style='font-size:14px'>kept</span></div>", "kept"),
        ("<span style='font-size:0'>hidden words</span>shown", "shown"),
        ("<p hidden>no</p><p>yes</p>", "yes"),
        ("<p style='visibility:hidden'>no</p><p>yes</p>", "yes"),
        ("<p style='opacity:0'>no</p><p>yes</p>", "yes"),
        ("<img src='x' width='0' height='0'>text", "text"),
        ("<a href='https://example.com/path/?q=1'>https://example.com/path/?q=1</a>",
         "example.com/path"),
        ("<a href='https://example.com/x'><img alt='Open the file' src='y'></a>",
         "Open the file (example.com)"),
        ("<p>a&nbsp;&nbsp;&nbsp;b</p>", "a b"),
    ],
)
def test_the_html_reads_like_this(html: str, expected: str) -> None:
    """One construct at a time, as a mail program would write it."""
    assert read_html(html).text == expected


def test_a_page_nested_far_deeper_than_any_mail_is_read_not_crashed() -> None:
    """A message built to nest thousands of elements still gives its text."""
    depth = 5000
    html = "<div>" * depth + "deep text" + "</div>" * depth
    assert "deep text" in read_html(html).text


# --- quotes, by the mail's own marks -----------------------------------------------------------


@pytest.mark.parametrize(
    ("html", "new", "gone"),
    [
        (GMAIL_REPLY, "Thursday works for me.", "Can you do Thursday"),
        (OUTLOOK_REPLY, "Sounds good, see you then.", "Lunch on Thursday?"),
        (APPLE_REPLY, "Yes, ten works.", "Is ten ok?"),
        (THUNDERBIRD, "Ten is fine.", "Is ten ok?"),
        (YAHOO, "Thanks!", "Done."),
    ],
)
def test_an_older_message_is_cut_where_the_mail_marks_it_and_the_result_says_so(
    html: str, new: str, gone: str,
) -> None:
    """Each client's quote mark cuts its quote, its attribution line and nothing else."""
    mail = parse(raw("Re: Lunch", html=html))
    assert mail.text.split("\n")[0] == new
    assert gone not in mail.text
    assert "quote" in mail.body.cut
    assert "wrote" not in mail.text


def test_prose_that_says_wrote_is_content_not_a_quote() -> None:
    """A newsletter's 'On Wednesday, an analyst wrote:' has no quote mark: all of it stays."""
    mail = parse(raw("Market", html=NEWSLETTER))
    assert mail.text == "On Wednesday, an analyst wrote: shares will rise.\n\nSecond paragraph."
    assert mail.body.cut == ()


def test_a_signature_is_cut_only_at_the_line_that_is_exactly_dash_dash_space() -> None:
    """``-- `` cuts; ``--`` and ``---`` do not."""
    signed = parse(raw("Re: Lunch", html=GMAIL_REPLY))
    assert "Sam" not in signed.text.split("\n")
    assert signed.body.cut == ("quote", "signature")
    dash = "--"
    plain = parse(raw("Thanks", plain=f"Thanks.\n\n{dash}\nSam Lee\nExample Corp\n"))
    assert plain.text == f"Thanks.\n\n{dash}\nSam Lee\nExample Corp"
    assert plain.body.cut == ()
    spaced = parse(raw("Thanks", plain="Thanks.\n\n-- \nSam Lee\nExample Corp\n"))
    assert spaced.text == "Thanks."
    assert spaced.body.cut == ("signature",)


@pytest.mark.parametrize(
    ("html", "subject", "kept"),
    [
        (GMAIL_FORWARD, "Fwd: Contract", "The contract is attached. Please sign by Friday."),
        (OUTLOOK_REPLY, "FW: Lunch", "Lunch on Thursday?"),
        (APPLE_FORWARD, "Fwd: Key", "The key is under the mat."),
    ],
)
def test_a_forwarded_message_is_content_and_stays(html: str, subject: str, kept: str) -> None:
    """A forward's header and body are kept; only what is quoted inside the forward goes."""
    text = parse(raw(subject, html=html)).text
    assert kept in text
    assert "History inside the forward" not in text


def test_the_same_apple_quote_is_cut_when_the_subject_is_no_forward() -> None:
    """The subject tells a forward from a reply when the markup is the same."""
    assert "The key is under the mat." not in _text(APPLE_FORWARD, "Re: Key")


def test_a_note_above_sent_from_my_iphone_keeps_the_forwarded_statement() -> None:
    """The phone's line is text, not a mark: nothing under it is dropped."""
    body = (
        "Can you check this?\n\nSent from my iPhone\n\n"
        "Begin forwarded message:\n\nFrom: Bank <alerts@example.com>\nYour balance is $52.00"
    )
    mail = parse(raw("Fwd: Statement", plain=body))
    assert "Your balance is $52.00" in mail.text
    assert mail.body.cut == ()


@pytest.mark.parametrize(
    ("plain", "kept"),
    [
        (
            "Sounds good.\n\nOn Mon, Oct 5, 2026 at 3:04 PM Alex Wu <alex@example.com> wrote:"
            "\n> Hi\n> x",
            "Sounds good.",
        ),
        (
            "Sounds good.\n\nOn Mon, Oct 5, 2026 at 3:04 PM Alex Wu <alex@example.com>\nwrote:"
            "\n> Hi",
            "Sounds good.",
        ),
        (
            "周四可以。\n\n在 2026年10月5日，15:04，Alex Wu <alex@example.com> 写道：\n\n"  # noqa: RUF001
            "> 周四还是周五？",  # noqa: RUF001
            "周四可以。",
        ),
        ("Yes.\n> an old line\nThanks", "Yes.\nThanks"),
        (
            "Oui.\n\nLe lun. 5 oct. 2026 à 15:04, Alex Wu <alex@example.com> a écrit :\n> Salut",
            "Oui.",
        ),
    ],
)
def test_quoted_lines_are_cut_with_the_attribution_line_just_above_them(
    plain: str, kept: str,
) -> None:
    """A block of ``>`` lines goes with its attribution line, wherever the program wrapped it."""
    mail = parse(raw("Re: Lunch", plain=plain))
    assert mail.text == kept
    assert mail.body.cut == ("quote",)


def test_a_body_that_is_nothing_but_a_quote_is_read_whole() -> None:
    """Cutting everything would read as an empty mail: it is kept, and nothing is claimed cut."""
    plain = parse(raw("Fwd", plain="> only a quote\n> and another\n"))
    assert plain.text == "> only a quote\n> and another"
    assert plain.body.cut == ()
    html = parse(raw("Fwd", html='<blockquote type="cite"><div>just this</div></blockquote>'))
    assert html.text == "just this"
    assert html.body.cut == ()


def test_format_flowed_text_is_joined_back_into_paragraphs() -> None:
    """A soft-wrapped line (ending in a space) continues; a hard break stays."""
    wrapped = "Hello Sam, this line was soft wrapped by the sender \nso it joins.\n\nNew."
    mail = parse(raw("flowed", plain=wrapped, flowed=True))
    assert mail.text == "Hello Sam, this line was soft wrapped by the sender so it joins.\n\nNew."


def test_padding_characters_in_plain_text_are_dropped_too() -> None:
    """A plain part's preheader padding goes the way an HTML one does."""
    assert read_plain(f"Hello{PADDING}\n\nWorld").text == "Hello\n\nWorld"


def test_a_body_the_server_chose_is_cleaned_the_same_way() -> None:
    """``clean`` takes the plain part or the HTML the server chose and reads either."""
    assert clean("<p>Hi<br>there</p>").text == "Hi\nthere"
    assert clean("Hi\n> old\nthere").text == "Hi\nthere"
    assert [is_forward(one) for one in ("Fwd: x", "FW: x", "Re: Fwd: x")] == [True, True, False]


# --- the message: headers, parts, attachments --------------------------------------------------


def test_the_headers_name_who_sent_it_to_whom_and_when() -> None:
    """From, To, Cc, Reply-To, Date and the subject come off the headers, decoded."""
    mail = parse(
        raw(
            "=?utf-8?q?Caf=C3=A9_menu?=", sender='"Wu, Alex" <alex@example.com>',
            to="Sam Lee <sam@example.com>, kim@example.com", cc="Pat <pat@example.com>",
            headers={"Reply-To": "Billing <bills@example.com>"}, plain="x",
        ),
    )
    assert (mail.sender.name, mail.sender.address) == ("Wu, Alex", "alex@example.com")
    assert [one.address for one in mail.to] == ["sam@example.com", "kim@example.com"]
    assert [one.address for one in mail.cc] == ["pat@example.com"]
    assert [one.address for one in mail.reply_to] == ["bills@example.com"]
    assert mail.subject == "Café menu"
    assert mail.at is not None
    assert mail.at.isoformat() == "2026-10-05T15:04:00-07:00"
    assert mail.addressed_to({"sam@example.com"})
    assert not mail.addressed_to({"pat@example.com"})  # Cc is not To


@pytest.mark.parametrize(
    ("headers", "flags"),
    [
        ({}, (False, False)),
        ({"List-Unsubscribe": "<mailto:u@example.com>"}, (True, False)),
        ({"List-Id": "<news.example.com>"}, (True, False)),
        ({"Precedence": "bulk"}, (True, False)),
        ({"Precedence": "list"}, (True, False)),
        ({"Precedence": "junk"}, (True, False)),
        ({"Precedence": "first-class"}, (False, False)),
        ({"Auto-Submitted": "auto-replied"}, (False, True)),
        ({"Auto-Submitted": "auto-generated"}, (False, True)),
        ({"Auto-Submitted": "no"}, (False, False)),
    ],
)
def test_bulk_and_automated_are_read_from_the_headers(
    headers: dict[str, str], flags: tuple[bool, bool],
) -> None:
    """List-Unsubscribe, List-Id, Precedence say bulk; Auto-Submitted other than no, automated."""
    mail = parse(raw("x", plain="hello", headers=headers))
    assert (mail.bulk, mail.automated) == flags


def test_the_html_part_is_preferred_and_the_plain_part_is_the_fallback() -> None:
    """An alternative reads its HTML; an HTML that holds no text falls back to its plain part."""
    both = parse(raw("x", plain="plain words", html="<p>html words</p>"))
    assert both.text == "html words"
    assert both.plain.strip() == "plain words"
    assert both.html == "<p>html words</p>\n"
    empty = parse(raw("x", plain="plain words", html="<p> </p>"))
    assert empty.text == "plain words"


def test_attachments_are_named_with_type_and_size_and_inline_pictures_are_not_files() -> None:
    """Named parts are the files; the body, an embedded logo and the text alternatives are not."""
    data = pdf(["hello"])
    mail = parse(
        raw(
            "Statement", plain="See attached.",
            attachments=[
                ("stmt.pdf", "application/pdf", data), ("rows.csv", "text/csv", b"a,b\n1,2\n"),
                ("photo.png", "image/png", b"\x89PNG...."),
            ],
        ),
    )
    assert [(a.name, a.mime, a.size) for a in mail.attachments] == [
        ("stmt.pdf", "application/pdf", len(data)),
        ("rows.csv", "text/csv", 8),
        ("photo.png", "image/png", 8),
    ]
    assert mail.text == "See attached."


def test_a_message_with_broken_parts_still_reads() -> None:
    """An unknown charset, no body at all and a bare header block do not raise."""
    odd = (
        b"From: a@example.com\nSubject: odd\nContent-Type: text/plain; charset=x-nonsense\n\n"
        b"caf\xe9\n"
    )
    assert parse(odd).text.startswith("caf")
    nothing = parse(b"From: a@example.com\nSubject: empty\n\n")
    assert (nothing.text, nothing.subject, nothing.attachments) == ("", "empty", ())
    assert parse(b"").sender.address == ""


# --- attachments as text -----------------------------------------------------------------------


def _file(name: str, mime: str, data: bytes) -> Attachment:
    return Attachment(name, mime, len(data), data)


def test_a_pdf_with_a_text_layer_is_read_with_its_columns_kept() -> None:
    """A statement's amount and due date come out as lines, page after page."""
    data = pdf(
        ["Statement balance      $1,234.56", "Payment due date      Nov 3, 2026"],
        ["Page two line"],
    )
    read = attachment_text(_file("stmt.pdf", "application/pdf", data))
    assert read.kind == "text"
    assert "Statement balance  $1,234.56" in read.text
    assert "Payment due date  Nov 3, 2026" in read.text
    assert read.text.endswith("Page two line")


def test_a_pdf_with_no_text_layer_is_a_scan_and_a_broken_one_is_unreadable() -> None:
    """The result can say which: a scan has nothing to read, a damaged file cannot be opened."""
    assert attachment_text(_file("scan.pdf", "application/pdf", scanned_pdf())).kind == "scan"
    broken = attachment_text(_file("bad.pdf", "application/pdf", b"%PDF-1.4 nope"))
    assert broken.kind == "unreadable"


@pytest.mark.parametrize(
    ("name", "mime", "data", "text"),
    [
        ("rows.csv", "text/csv", b"a,b\r\n1,2\r\n", "a,b\n1,2"),
        ("notes.txt", "text/plain", "café".encode(), "café"),
        ("invite.ics", "text/calendar", b"BEGIN:VCALENDAR\nSUMMARY:Lunch\nEND:VCALENDAR\n",
         "BEGIN:VCALENDAR\nSUMMARY:Lunch\nEND:VCALENDAR"),
        ("page.html", "text/html", b"<p>Hello <b>there</b></p>", "Hello there"),
        ("legacy.txt", "application/octet-stream", b"caf\xe9", "café"),
    ],
)
def test_text_csv_and_calendar_files_are_read_as_text(
    name: str, mime: str, data: bytes, text: str,
) -> None:
    """Whatever is text is text, in the encoding it came in."""
    read = attachment_text(_file(name, mime, data))
    assert (read.kind, read.text) == ("text", text)


def test_any_other_file_is_only_named() -> None:
    """A picture or an archive has no text: the kind says so and nothing is read."""
    assert attachment_text(_file("photo.png", "image/png", b"\x89PNG")).kind == "named"
    assert attachment_text(_file("a.zip", "application/zip", b"PK")).kind == "named"
