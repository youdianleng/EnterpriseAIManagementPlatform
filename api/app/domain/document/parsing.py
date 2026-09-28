"""Text extraction and structural chunking — the parsing half of the pipeline.

One entry point, `extract`, and one decision that shapes everything under it:

* **The parser is chosen from the media type, and the media type comes from
  `files.SUPPORTED`, never from the request.** A caller cannot ask for a reader; it
  can only name a file, and an extension outside the table is refused before this
  module is reached.

* **Reading is done from bytes, not from a path.** The storage adapter hands the
  original back through its own seam, so the parsers never learn where files live,
  and a test can exercise every one of them with a fixture built in memory rather
  than a binary committed to the repository.

* **"No text" is a refusal, never an empty document.** A scanned PDF has pages and no
  extractable characters; a workbook can hold one empty sheet. Both come out of here
  as `ParseRefused`, and the service turns that into `failed` with the ticket's
  message. There is deliberately no OCR anywhere in this module: adding one later is
  a parser adapter *behind this same interface*, which is the judgement
  `docs/architecture/codebase-design.md` §2.5 records.

**Chunking is structural, and it is the parent/child split the design asks for
(D-Q37).** Sections are separated at blank lines, consecutive sections are packed up
to a target size, and a section larger than the maximum is split on whitespace with a
small overlap so a sentence that straddles a boundary is still retrievable from
either side. Ticket 31 stores content and locations; ticket 32 fills the `embedding`
column, which is why `parse` writes rows with no vector and why nothing here imports
the embedding model.
"""

import io
import re
from dataclasses import dataclass

import docx
import openpyxl
from pypdf import PdfReader

#: Bumped when the split changes, so a re-index is a query rather than a guess. The
#: column is on the chunk row for exactly this.
CHUNKING_VERSION = "structural-v1"

#: How big a chunk aims to be, and the largest one may get. The design names ~1500
#: and ~400 tokens for parent and child blocks; this first cut writes one row per
#: structural block, so the target sits between them and ticket 32's retrieval work
#: is what will split parents from children if the measurements ask for it.
CHUNK_TARGET_CHARS = 1500
CHUNK_MAX_CHARS = 2000

#: Characters carried over between two halves of one oversized section, so a phrase
#: on the boundary is not lost from both.
CHUNK_OVERLAP_CHARS = 200

#: Tokens are counted as characters over this. An approximation, stated rather than
#: hidden: an exact count needs the embedding model's tokenizer, and a tokenizer that
#: disagrees with the model would be a worse number than an honest estimate.
CHARS_PER_TOKEN = 4

#: What the ticket's refusal says. One constant because the *message* is the
#: acceptance criterion, and a sentence assembled at two call sites is two sentences.
NO_TEXT_MESSAGE = "no text extracted; upload a text version"

#: A line that is a heading rather than prose: a Markdown heading, or a numbered
#: clause such as `3.2 Alcance`. Read as structure, never stripped from the chunk —
#: the heading is part of what the chunk says.
_HEADING = re.compile(r"^(?:#{1,6}\s+\S|\d+(?:\.\d+)*[.)]?\s+\S)")

#: Runs of horizontal whitespace, and runs of blank lines. Both collapse, because
#: a PDF's line breaks are a layout artefact: the same sentence comes back with
#: different spacing from a different producer.
_SPACES = re.compile(r"[ \t\u00a0]+")
_BLANK_LINES = re.compile(r"\n{3,}")


class ParseRefused(Exception):
    """The file holds no extractable text.

    Not a `DomainError`: this is raised inside the parsing adapters, which know
    nothing about error catalogues, and the service maps it to the catalogued refusal
    the client sees. The message is the ticket's own sentence.
    """


@dataclass(frozen=True, slots=True)
class ParsedText:
    """What a reader got out of one file.

    `char_count` is measured on the written text and is what `documents
    .extracted_chars` stores: the question that column answers is "did this document
    produce anything", and a byte count of the stored file cannot answer it.

    `pages` is the per-page text a PDF reader produced, and it is what makes a page
    number locatable after the pages have been joined into one string. It is private
    to the split: a caller that wants the document's text wants `text`.
    """

    text: str
    char_count: int
    page_count: int | None = None
    pages: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.pages and self.text:
            object.__setattr__(self, "pages", (self.text,))


@dataclass(frozen=True, slots=True)
class Chunk:
    """One row of `document_chunks`, before it is written."""

    chunk_index: int
    content: str
    token_count: int
    page_from: int | None = None
    page_to: int | None = None
    heading_path: str | None = None


#: Media type → reader. The table mirrors `files.SUPPORTED`, and a media type that
#: reaches `extract` without an entry here is a bug in this module rather than a
#: request problem.
_READERS = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "text/plain": "text",
    "text/markdown": "text",
}


def extract(content: bytes, media_type: str) -> ParsedText:
    """The file's text, or `ParseRefused` when it holds none.

    Every reader returns the same shape, so the pipeline above never learns which
    format it is holding — which is what makes a future OCR adapter a change to this
    function and nothing else.

    **The refusal is decided here, once, on the normalised text.** A reader that
    found nothing and a reader that found only whitespace are the same answer, and a
    check at each call site is a check that gets forgotten at one of them — which is
    exactly how a scanned file would reach `ready` with an empty document behind it.
    """
    reader = _READERS.get(media_type)
    if reader is None:  # pragma: no cover - files.accept is the gate
        raise ValueError(f"no reader for media type {media_type!r}")
    if reader == "pdf":
        parsed = _pdf(content)
    elif reader == "docx":
        parsed = _docx(content)
    elif reader == "xlsx":
        parsed = _xlsx(content)
    else:
        parsed = _plain_text(content)
    if not parsed.text.strip():
        raise ParseRefused(NO_TEXT_MESSAGE)
    return parsed


def parse(content: bytes, media_type: str) -> tuple[ParsedText, list[Chunk]]:
    """`extract`, then split. The whole parsing half in one call."""
    parsed = extract(content, media_type)
    return parsed, chunk(parsed)


def chunk(parsed: ParsedText) -> list[Chunk]:
    """The structural split: sections packed to a target, oversized ones divided.

    Pages travel with the text, so a PDF's chunk can say which pages it came from —
    which is what the design's citation format (`《文件名》第 N 页`) needs, and what
    ticket 32 will read out.
    """
    if not parsed.text:
        return []

    blocks = _blocks(parsed)
    sections = _sections(blocks)

    chunks: list[Chunk] = []
    pending: list[dict] = []
    pending_length = 0

    def flush() -> None:
        nonlocal pending, pending_length
        if not pending:
            return
        section = dict(pending[0])
        section["text"] = "\n\n".join(part["text"] for part in pending)
        section["end"] = pending[-1]["end"]
        section["page_to"] = pending[-1]["page"]
        chunks.append(_chunk(len(chunks), section, blocks))
        pending = []
        pending_length = 0

    for section in sections:
        body = section["text"]
        if len(body) > CHUNK_MAX_CHARS:
            flush()
            for piece in _divide(body):
                start = parsed.text.find(piece)
                divided = dict(section)
                divided["text"] = piece
                if start >= 0:
                    divided["start"] = start
                    divided["end"] = start + len(piece)
                divided["page_to"] = _page_at(divided["end"], blocks)
                chunks.append(_chunk(len(chunks), divided, blocks))
            continue
        if pending and pending_length + len(body) > CHUNK_TARGET_CHARS:
            flush()
        pending.append(section)
        pending_length += len(body) + 2
    flush()
    return chunks


def _chunk(index: int, section: dict, blocks: list[dict]) -> Chunk:
    content = section["text"]
    # `section["page"]` is the page the section *begins* on, which the section carries
    # from the block it was cut from; `_page_at` resolves the end, which can be on the
    # next page. A format without pages resolves both to None.
    page_from = section.get("page") or _page_at(section["start"], blocks)
    page_to = section.get("page_to") or _page_at(section["end"], blocks) or page_from
    return Chunk(
        chunk_index=index,
        content=content,
        token_count=max(1, len(content) // CHARS_PER_TOKEN),
        page_from=page_from,
        page_to=page_to,
        heading_path=section.get("heading_path"),
    )


def _blocks(parsed: ParsedText) -> list[dict]:
    """The text as blocks, each knowing the offset it starts at.

    A PDF is cut into page blocks first so every later chunk can name the page it
    came from; the other formats produce one block covering the whole text, because
    `None` pages are the truthful answer for a file that has no pages.
    """
    if parsed.page_count is None:
        return [{"start": 0, "end": len(parsed.text), "text": parsed.text, "page": None}]

    blocks: list[dict] = []
    offset = 0
    for page, page_text in enumerate(parsed.pages, start=1):
        blocks.append(
            {"start": offset, "end": offset + len(page_text), "text": page_text, "page": page}
        )
        offset += len(page_text) + 2
    return blocks


def _page_at(offset: int, blocks: list[dict]) -> int | None:
    """Which page an offset falls on, or `None` for a format without pages.

    The *last* block that starts at or before the offset, rather than the block that
    contains it: a section's own offsets are relative to the page it begins on and its
    end can land inside the next page's block, and "the page this text starts on" is
    the answer a citation needs.
    """
    found: int | None = None
    for block in blocks:
        if block["page"] is None or block["start"] > offset:
            break
        found = block["page"]
    return found


def _sections(blocks: list[dict]) -> list[dict]:
    """Blocks cut at blank lines into sections, each carrying its heading path.

    The heading path is the nearest heading above the section. It is one line rather
    than a tree: what a citation needs is where a fragment sits in the document
    ("3.2 Alcance"), and a tree would be a second structure to keep in step with the
    text for no additional answer.
    """
    sections: list[dict] = []
    heading_path: str | None = None
    for block in blocks:
        paragraphs = re.split(r"\n\s*\n", block["text"])
        offset = block["start"]
        for paragraph in paragraphs:
            body = paragraph.strip()
            if body:
                if _HEADING.match(body):
                    heading_path = body.splitlines()[0].strip()
                sections.append(
                    {
                        "text": body,
                        "heading_path": heading_path,
                        "start": offset,
                        "end": offset + len(body),
                        "page": block["page"],
                    }
                )
            offset += len(paragraph) + 2
    return sections


def _divide(body: str) -> list[str]:
    """One oversized section, split on whitespace with a small overlap."""
    words = body.split(" ")
    pieces: list[str] = []
    current: list[str] = []
    length = 0
    for word in words:
        if current and length + len(word) + 1 > CHUNK_MAX_CHARS:
            pieces.append(" ".join(current))
            current = _tail(current)
            length = sum(len(part) + 1 for part in current)
        current.append(word)
        length += len(word) + 1
    if current:
        pieces.append(" ".join(current))
    return pieces


def _tail(words: list[str]) -> list[str]:
    """The last few words of a piece, to overlap the next one with.

    The budget is `CHUNK_OVERLAP_CHARS`, and the boundary lands on a word so the
    overlap never begins mid-token — a citation that starts with half a word is worse
    than no overlap at all.
    """
    kept: list[str] = []
    length = 0
    for word in reversed(words):
        if length + len(word) + 1 > CHUNK_OVERLAP_CHARS:
            break
        kept.insert(0, word)
        length += len(word) + 1
    return kept


def _normalise(text: str) -> str:
    """Whitespace as this module stores it.

    A PDF's extractor reproduces the layout's line breaks, so the same paragraph
    arrives with different spacing from different producers. Collapsing horizontal
    runs and blank-line runs gives one text per document rather than one per tool,
    which is what makes a chunk's content comparable across uploads.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x0c", "\n")
    text = _SPACES.sub(" ", text)
    text = _BLANK_LINES.sub("\n\n", text)
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def _pdf(content: bytes) -> ParsedText:
    """Per-page text, joined so the page boundaries stay locatable."""
    reader = PdfReader(io.BytesIO(content))
    pages = tuple(_normalise(page.extract_text() or "") for page in reader.pages)
    return ParsedText(
        text="\n\n".join(pages),
        char_count=sum(len(page) for page in pages),
        page_count=len(pages),
        pages=pages,
    )


def _docx(content: bytes) -> ParsedText:
    """Paragraphs in order, then tables — a table cell's text is content too.

    The order is document order as far as the format exposes it, which is why the
    tables are appended rather than interleaved: a docx table's true position needs
    the body's XML, and a chunk that names the right text in the wrong place is worth
    more than dropping it.
    """
    document = docx.Document(io.BytesIO(content))
    parts = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.append("\t".join(cell.text for cell in row.cells))
    text = _normalise("\n\n".join(part for part in parts if part.strip()))
    return ParsedText(text=text, char_count=len(text), page_count=None)


def _xlsx(content: bytes) -> ParsedText:
    """One section per sheet, rows tab-separated and numbers as stored.

    `read_only` so a large workbook is streamed rather than expanded into memory, and
    `data_only` so a formula's last computed value is read instead of the formula —
    the text of a document is what it says, not how it was built.

    A sheet's name becomes a heading, so the chunks carry "which sheet" the way a
    PDF's carry "which page".
    """
    workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    try:
        sections: list[str] = []
        for sheet in workbook.worksheets:
            rows = [
                "\t".join("" if cell is None else str(cell) for cell in row)
                for row in sheet.iter_rows(values_only=True)
            ]
            body = "\n".join(row for row in rows if row.strip())
            if body:
                sections.append(f"# {sheet.title}\n{body}")
    finally:
        workbook.close()
    text = _normalise("\n\n".join(sections))
    return ParsedText(text=text, char_count=len(text), page_count=None)


def _plain_text(content: bytes) -> ParsedText:
    """Decoded as UTF-8, with the byte-order mark removed.

    `errors="replace"` rather than a refusal: a Spanish text file saved as Latin-1
    should produce a document with a few wrong accents, not a failure — and the
    alternative, guessing the encoding, is how a file ends up half in the wrong
    alphabet. A file that is not text at all still decodes to something, so this
    reader refuses only the file that is genuinely empty.
    """
    text = _normalise(content.decode("utf-8-sig", errors="replace"))
    return ParsedText(text=text, char_count=len(text), page_count=None)


__all__ = [
    "CHARS_PER_TOKEN",
    "CHUNKING_VERSION",
    "CHUNK_MAX_CHARS",
    "CHUNK_OVERLAP_CHARS",
    "CHUNK_TARGET_CHARS",
    "NO_TEXT_MESSAGE",
    "Chunk",
    "ParseRefused",
    "ParsedText",
    "chunk",
    "extract",
    "parse",
]
