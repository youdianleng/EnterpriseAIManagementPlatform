"""The morning digest: what one recipient is told, and in which language.

The one place in this backend that writes a *sentence*. Everywhere else a
notification stores a bilingual key and the client renders it (D2), because the
reader is a browser that has dictionaries. The reader here is a mail client, which
has none — so the digest carries the rendered copy, and the copy therefore lives
in a catalogue next to the renderer, in both languages, within one file that
cannot half-exist in one of them.

**The language is the recipient's, and there is now somewhere to put it.**
DESIGN §10.4 has the interface read `Accept-Language` and persist a manual choice
to `users.locale`; that column exists as of migration 0017, so the digest reads the
*stored preference* and falls back to `DIGEST_DEFAULT_LANGUAGE` when the account has
never chosen (NULL). No profile screen writes it yet — that is the screen this
mechanism is waiting for — and a test sets it directly to prove the preference is
what decides. A stored value nobody recognises falls back rather than raising: one
account with odd data must not stop the other ninety-nine people's morning.

**HTML and text are two renderings of one `DigestContent`, never two messages.**
Both come from the same value object and the same catalogue, which is what makes
"the text part says the same thing without markup" a property rather than a promise
to keep two templates in step. The HTML is inline-styled with the design system's
own values (`web/app/globals.css` tokens, copied as literals because a mail client
loads no stylesheet), and every piece of user data is escaped: a name is data, and
a name that happens to contain `<` must not become markup in somebody's inbox.

**The subject carries the date and nothing else.** "Do not put user data in the
subject line" is both a privacy rule — a subject is visible on a lock screen and in
every server log — and a noise rule: the recipient already knows their own name,
and the summary is what the body is for.
"""

import html
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any
from uuid import UUID

from app.domain.attendance.anomalies import AnomalyType

#: Where the link in a digest points, below the configured web base URL and the
#: language. The notification centre is deliberate: it exists today, it is where
#: the same anomalies are readable in-app, and it is in the reader's own language
#: because the language is part of the path. Ticket 24's correction screens are the
#: natural second destination, and adding one will be a second link rather than a
#: changed one.
DIGEST_PATH = "/notifications"

#: How much of a report's anomalies one mail lists. A hundred people with a
#: hundred anomalies is a report, not a digest; past this the mail says how many
#: were left out and the link is where the rest are read.
MAX_ANOMALIES_PER_REPORT = 20


class DigestLanguage(StrEnum):
    """The two languages the mail is written in (D2). Closed, like every catalogue."""

    ES = "es"
    EN = "en"


#: The copy, one entry per language, keyed the same in both. Anomaly labels are
#: keyed by the anomaly catalogue's own members, so a sixth kind of anomaly fails
#: the test that walks `AnomalyType` rather than reaching a reader as a blank.
DIGEST_COPY: dict[DigestLanguage, dict[str, str]] = {
    DigestLanguage.ES: {
        "subject": "Resumen diario de anomalías — {date}",
        "heading": "Resumen diario de anomalías",
        "intro": "Fichajes del {date} que requieren atención:",
        "section.own": "Tus fichajes pendientes",
        "section.team": "Fichajes pendientes de tu equipo",
        "link": "Abrir en la plataforma",
        "footer": "Mensaje automático del sistema de gestión. No responda a este correo.",
        "more": "y {count} más en la plataforma",
        # Day first, as Spanish writes it. Numbers only, so no month name and no
        # locale-dependent formatting.
        "date_format": "%d/%m/%Y",
        "anomaly.no_punches": "Sin fichajes",
        "anomaly.missing_clock_in": "Falta el fichaje de entrada",
        "anomaly.missing_clock_out": "Falta el fichaje de salida",
        "anomaly.late": "Retraso",
        "anomaly.early_leave": "Salida anticipada",
    },
    DigestLanguage.EN: {
        "subject": "Daily anomaly digest — {date}",
        "heading": "Daily anomaly digest",
        "intro": "Punches on {date} that need attention:",
        "section.own": "Your own outstanding punches",
        "section.team": "Your team's outstanding punches",
        "link": "Open in the platform",
        "footer": "Automatic message from the management system. Do not reply.",
        "more": "and {count} more in the platform",
        # `%B` reads the C locale's month names, which is English: the process
        # never calls `setlocale`, so this is stable wherever it runs.
        "date_format": "%d %B %Y",
        "anomaly.no_punches": "No punches",
        "anomaly.missing_clock_in": "Missing clock-in",
        "anomaly.missing_clock_out": "Missing clock-out",
        "anomaly.late": "Late arrival",
        "anomaly.early_leave": "Early leave",
    },
}


def language_of(stored: str | None, *, default: str) -> DigestLanguage:
    """The preference the account stored, else the configured default.

    The default is a *setting* rather than a constant because a deployment decides
    which language its people read when nobody has been asked; `es` is this one's
    answer, and DESIGN §10.4's profile screen is what will make the question
    unnecessary.
    """
    for candidate in (stored, default):
        if not candidate:
            continue
        try:
            return DigestLanguage(candidate.strip().lower())
        except ValueError:
            continue
    return DigestLanguage.ES


def format_date(day: date, language: DigestLanguage) -> str:
    """A date as the language writes it. Both formats are fixed-width on purpose."""
    return day.strftime(DIGEST_COPY[language]["date_format"])


@dataclass(slots=True, frozen=True)
class DigestAnomaly:
    """One line of the mail: what was wrong, and the day it was wrong on."""

    type: AnomalyType
    business_date: date


@dataclass(slots=True, frozen=True)
class DigestReport:
    """One person the mail is about, with their anomalies.

    A "report" in the manager's sense, and the recipient themself in the personal
    sense — one shape, because the mail lists the same thing in both cases.
    """

    employee_id: UUID
    name: str
    anomalies: tuple[DigestAnomaly, ...]

    def is_about(self, employee_id: UUID) -> bool:
        """Whether this row is the reader's own, which decides its heading."""
        return self.employee_id == employee_id


@dataclass(slots=True, frozen=True)
class DigestContent:
    """Everything one digest mail says, before it is a mail.

    `reports` is ordered by the caller: the recipient's own row first, then the
    team by name, because the reader's own outstanding punches are the ones they
    can still do something about today.
    """

    recipient_employee_id: UUID
    recipient_email: str
    digest_date: date
    language: DigestLanguage
    link: str
    reports: tuple[DigestReport, ...]

    @property
    def anomaly_count(self) -> int:
        return sum(len(report.anomalies) for report in self.reports)

    def as_payload(self) -> dict[str, Any]:
        """The structured record: ids, kinds and dates — no names, no sentences.

        A name here would be a second copy of a fact the employee record owns, and
        the point of the payload is to say *what was reported*, which the anomaly
        rows already say. `language` is stored because a mail that has left the
        building should be readable as it was written.
        """
        return {
            "language": str(self.language),
            "digest_date": self.digest_date.isoformat(),
            "anomaly_count": self.anomaly_count,
            "reports": [
                {
                    "employee_id": str(report.employee_id),
                    "anomalies": [
                        {
                            "type": str(anomaly.type),
                            "business_date": anomaly.business_date.isoformat(),
                        }
                        for anomaly in report.anomalies
                    ],
                }
                for report in self.reports
            ],
        }


@dataclass(slots=True, frozen=True)
class RenderedDigest:
    """One composed message: a subject and the two parts of its body."""

    subject: str
    text: str
    html: str


def render(content: DigestContent) -> RenderedDigest:
    """The mail, as a subject and its two alternative parts.

    The recipient's own row is separated from the team's here rather than by the
    caller: the two sections have different headings, and which section a report
    belongs to is a question about the reader, not about the report.
    """
    copy = DIGEST_COPY[content.language]
    written = format_date(content.digest_date, content.language)
    own = [report for report in content.reports if report.is_about(content.recipient_employee_id)]
    team = [
        report for report in content.reports if not report.is_about(content.recipient_employee_id)
    ]

    return RenderedDigest(
        subject=copy["subject"].format(date=written),
        text=_render_text(content, own=own, team=team),
        html=_render_html(content, own=own, team=team),
    )


def _render_text(
    content: DigestContent, *, own: list[DigestReport], team: list[DigestReport]
) -> str:
    """The plain-text part: the same facts, no markup, and no dependence on it."""
    copy = DIGEST_COPY[content.language]
    written = format_date(content.digest_date, content.language)
    lines = [
        copy["heading"],
        "",
        copy["intro"].format(date=written),
        "",
    ]
    for heading, reports in (("section.own", own), ("section.team", team)):
        if not reports:
            continue
        lines.extend([copy[heading], ""])
        for report in reports:
            lines.append(f"  {report.name}")
            lines.extend(_anomaly_lines(report, content.language))
            lines.append("")

    lines.append(f"{copy['link']}: {content.link}")
    lines.extend(["", copy["footer"]])
    return "\n".join(lines)


def _anomaly_lines(report: DigestReport, language: DigestLanguage) -> list[str]:
    copy = DIGEST_COPY[language]
    shown = report.anomalies[:MAX_ANOMALIES_PER_REPORT]
    lines = [
        f"    - {copy[f'anomaly.{anomaly.type}']} · {format_date(anomaly.business_date, language)}"
        for anomaly in shown
    ]
    if len(report.anomalies) > len(shown):
        lines.append(f"    - {copy['more'].format(count=len(report.anomalies) - len(shown))}")
    return lines


#: The design system's values, as literals. A mail client loads no stylesheet, so
#: `web/app/globals.css`'s tokens are copied here deliberately and must be changed
#: in both places; the names are kept so the copy is checkable.
INK = "#14181f"
INK_MUTED = "#5b6472"
SURFACE = "#ffffff"
CANVAS = "#f7f8fa"
BORDER = "#dfe3e8"
PRIMARY = "#1d4ed8"
WARNING = "#9a5b00"
WARNING_BG = "#fdf3e2"
FONT = 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif'


def _render_html(
    content: DigestContent, *, own: list[DigestReport], team: list[DigestReport]
) -> str:
    """The HTML part: one column, inline styles, no images and no stylesheet.

    `role="presentation"` on the tables and no fixed pixel widths: Spanish is a
    fifth longer than English (design system §3.1), and a layout that only holds
    in one of them is the defect that rule exists to prevent.
    """
    copy = DIGEST_COPY[content.language]
    written = format_date(content.digest_date, content.language)
    body = [
        f'<div style="max-width:640px;margin:0 auto;background:{SURFACE};'
        f'border:1px solid {BORDER};border-radius:8px;padding:24px;">',
        f'<h1 style="margin:0 0 4px;font-size:20px;line-height:1.3;color:{INK};">'
        f"{_escape(copy['heading'])}</h1>",
        f'<p style="margin:0 0 24px;font-size:13px;line-height:1.45;color:{INK_MUTED};">'
        f"{_escape(copy['intro'].format(date=written))}</p>",
    ]
    for heading, reports in (("section.own", own), ("section.team", team)):
        if not reports:
            continue
        body.append(
            f'<h2 style="margin:0 0 8px;font-size:16px;line-height:1.4;color:{INK};">'
            f"{_escape(copy[heading])}</h2>"
        )
        body.append(_html_reports(reports, content.language))
    body.append(
        f'<p style="margin:8px 0 0;font-size:14px;line-height:1.5;">'
        f'<a href="{_escape(content.link)}" style="color:{PRIMARY};">'
        f"{_escape(copy['link'])}</a></p>"
    )
    body.append("</div>")
    body.append(
        f'<p style="max-width:640px;margin:16px auto 0;font-size:13px;line-height:1.45;'
        f'color:#6b7280;">{_escape(copy["footer"])}</p>'
    )
    return (
        f'<!doctype html><html lang="{content.language}"><head>'
        f'<meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        f"<title>{_escape(copy['heading'])}</title></head>"
        f'<body style="margin:0;padding:24px;background:{CANVAS};font-family:{FONT};'
        f'font-size:14px;line-height:1.5;color:{INK};">'
        + "".join(body)
        + "</body></html>"
    )


def _html_reports(reports: list[DigestReport], language: DigestLanguage) -> str:
    """Each report's name once, with their anomalies as tags beneath it."""
    copy = DIGEST_COPY[language]
    blocks = []
    for report in reports:
        chips = "".join(
            f'<span style="display:inline-block;margin:0 4px 4px 0;padding:2px 8px;'
            f"border-radius:4px;background:{WARNING_BG};color:{WARNING};font-size:13px;"
            f'line-height:1.45;font-variant-numeric:tabular-nums;">'
            f"{_escape(copy[f'anomaly.{anomaly.type}'])} · "
            f"{_escape(format_date(anomaly.business_date, language))}</span>"
            for anomaly in report.anomalies[:MAX_ANOMALIES_PER_REPORT]
        )
        if len(report.anomalies) > MAX_ANOMALIES_PER_REPORT:
            left_out = copy["more"].format(
                count=len(report.anomalies) - MAX_ANOMALIES_PER_REPORT
            )
            chips += (
                f'<span style="display:inline-block;padding:2px 0;color:{INK_MUTED};'
                f'font-size:13px;">{_escape(left_out)}</span>'
            )
        blocks.append(
            '<table role="presentation" style="width:100%;border-collapse:collapse;'
            'margin:0 0 16px;"><tr><td style="padding:0 0 6px;font-size:14px;'
            f'line-height:1.5;color:{INK};font-weight:600;">{_escape(report.name)}</td></tr>'
            f'<tr><td style="padding:0;">{chips}</td></tr></table>'
        )
    return "".join(blocks)


def _escape(value: str) -> str:
    """Every value that reached here came from a row somebody typed."""
    return html.escape(value, quote=True)


__all__ = [
    "DIGEST_COPY",
    "DIGEST_PATH",
    "MAX_ANOMALIES_PER_REPORT",
    "DigestAnomaly",
    "DigestContent",
    "DigestLanguage",
    "DigestReport",
    "RenderedDigest",
    "format_date",
    "language_of",
    "render",
]
