"""Documents built in memory, and an in-memory file store.

The ticket's constraint is explicit: tests build small PDFs/DOCX/XLSX rather than
committing binaries. So every format this module accepts has a builder here, and the
builders are real files — `pypdf`, `python-docx` and `openpyxl` read back what these
produce, which is what makes a test of the *parsers* a test of the parsers rather than
of a stub.

**The PDF builder writes the file by hand, and that is deliberate.** A text PDF needs a
font dictionary, a content stream and a cross-reference table; that is thirty lines of
bytes here, against a dependency (`reportlab`) that would exist for nothing but test
fixtures. The two shapes the tests need are a PDF whose text is extractable and a PDF
whose pages are empty — the scanned file the ticket refuses — and the second one is
`add_blank_page` from `pypdf` itself, so the "scanned" fixture is produced by the same
library that would have to read it.

`RecordingFileStore` is the second adapter the storage seam needs to be a seam
(`docs/architecture/codebase-design.md` §4). It records every call as well as the bytes,
so a test can assert *what an operation did to storage* — which is how "a retry does not
write a second copy" is checked without inspecting a volume.
"""

import io
from dataclasses import dataclass, field

# --- the five accepted formats ----------------------------------------------


def text_bytes(body: str = "Politica de vacaciones: 23 dias laborables.") -> bytes:
    """A `.txt` file."""
    return body.encode("utf-8")


def markdown_bytes(
    body: str = "# Politica de vacaciones\n\n## 1. Ambito\n\nTodo el personal.\n",
) -> bytes:
    """A `.md` file, with the headings the chunker reads as structure."""
    return body.encode("utf-8")


def pdf_bytes(*pages: str) -> bytes:
    """A PDF whose text `pypdf` extracts, built the long way round.

    One page per string. The content is written as an ASCII `Tj` string, so a test
    cannot put an accent through it — the font dictionary declares Helvetica without an
    encoding, and a byte outside ASCII would be read back as mojibake rather than as
    the text that went in. Spanish bodies belong in the DOCX, XLSX and text fixtures,
    which carry UTF-8 properly; what this builder is for is the *pipeline*.
    """
    objects: list[bytes] = []
    kids = " ".join(f"{4 + index * 2} 0 R" for index in range(len(pages)))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for index, page in enumerate(pages):
        content = f"BT /F1 12 Tf 72 720 Td ({page}) Tj ET".encode("ascii")
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + index * 2} 0 R >>"
            ).encode()
        )
        objects.append(
            f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    start_xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{start_xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


def scanned_pdf_bytes(pages: int = 1) -> bytes:
    """A PDF with the right number of pages and no text at all.

    The ticket's scanned file, as a fixture. `add_blank_page` writes a page with an
    empty content stream — exactly what a scanner's image-only page looks like to a text
    extractor that does no OCR — so this is not a stand-in for the case, it *is* the
    case.
    """
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def docx_bytes(
    paragraphs: tuple[str, ...] = ("Politica de vacaciones", "Veintitres dias laborables."),
    table: tuple[tuple[str, ...], ...] = (),
) -> bytes:
    """A `.docx`, paragraphs and optionally a table."""
    import docx

    document = docx.Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    if table:
        added = document.add_table(rows=len(table), cols=len(table[0]))
        for row_index, row in enumerate(table):
            for column_index, value in enumerate(row):
                # `str`, because a docx cell holds *text* and python-docx refuses an
                # int outright — and a fixture that cannot express the number a real
                # policy table contains would not be testing the reader.
                added.cell(row_index, column_index).text = str(value)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def xlsx_bytes(sheets: dict[str, tuple[tuple[object, ...], ...]] | None = None) -> bytes:
    """An `.xlsx` with one named sheet by default, and a second when asked."""
    import openpyxl

    workbook = openpyxl.Workbook()
    content = sheets or {"Vacaciones": (("Concepto", "Dias"), ("Anual", 23))}
    for index, (name, rows) in enumerate(content.items()):
        sheet = workbook.active if index == 0 else workbook.create_sheet()
        sheet.title = name
        for row in rows:
            sheet.append(list(row))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def empty_xlsx_bytes() -> bytes:
    """A workbook with a sheet and no values: a spreadsheet that says nothing."""
    import openpyxl

    workbook = openpyxl.Workbook()
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# --- the recording storage double -------------------------------------------


@dataclass
class RecordingFileStore:
    """The in-memory adapter, and a record of what was asked of it.

    Every method is recorded, because the interesting assertions about storage are
    about *operations* rather than about bytes: "a retry did not write a second copy",
    "the download read exactly the key the row names", "nothing was deleted".
    """

    #: key → bytes.
    objects: dict[str, bytes] = field(default_factory=dict)
    puts: list[str] = field(default_factory=list)
    gets: list[str] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)

    def put(self, content: bytes, *, extension: str) -> str:
        from app.domain.document.storage import storage_key

        key = storage_key(content, extension)
        self.puts.append(key)
        self.objects.setdefault(key, content)
        return key

    def get(self, key: str) -> bytes:
        self.gets.append(key)
        if key not in self.objects:
            raise FileNotFoundError(f"no stored document at {key!r}")
        return self.objects[key]

    def exists(self, key: str) -> bool:
        return key in self.objects

    def drop(self, key: str) -> None:
        """Forget one object, so a test can simulate a lost original."""
        self.objects.pop(key, None)


__all__ = [
    "RecordingFileStore",
    "docx_bytes",
    "empty_xlsx_bytes",
    "markdown_bytes",
    "pdf_bytes",
    "scanned_pdf_bytes",
    "text_bytes",
    "xlsx_bytes",
]
