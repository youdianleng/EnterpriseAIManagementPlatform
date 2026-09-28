"""Text extraction and the parent/child split — the parsing half of the pipeline.

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

* **The split is structural first and by length second (D-Q37, ticket 32).** A
  document is read as a sequence of *sections* — cut where the format itself says a
  section begins: a Markdown heading, a DOCX heading style, a numbered clause or an
  all-caps title in a PDF, an XLSX sheet name — and each section is then packed into a
  **parent** of about 1500 tokens, with **children** of about 400 tokens and 15%
  overlap inside it. A child never crosses a section boundary, and length is only
  consulted inside one: without the first cut, a 400-token window would divide a
  clause from its heading and hand retrieval a fragment whose subject is on the
  previous page.

* **A boundary is a sentence boundary.** `_sentences` decides where a sentence ends,
  and §"Sentence boundaries" below states the rule for Spanish and English. The only
  place a character count can cut a sentence is a sentence longer than
  `MAX_CHILD_TOKENS` on its own (`_split_oversized`), which is a fallback, is
  documented as one, and is asserted by a test.

* **The page is carried, not inferred.** A PDF is split into page blocks before
  anything else, every section keeps the block it begins in, and every fragment keeps
  the character span it came from. A child's `page_from`/`page_to` is therefore read
  out of the parse — which is what `《文件名》第 N 页` needs — and a format with no
  pages answers `None` rather than guessing a 1.

## Sentence boundaries

A sentence ends at a terminator — `.`, `!`, `?` (and `…`) — or at a closing mark that
follows one (`.)`, `."`, `.»`, `.”`). It does **not** end at a terminator that is
followed by a lower-case letter, which is what keeps Spanish's and English's shared
abbreviations (`art. 5`, `p. ej.`, `etc.`, `Dr.`) inside one sentence; a numbered
clause heading (`3.2 Alcance`) is likewise never a boundary, and a blank line always
is one. That is deliberately the same rule for both languages rather than two: the
abbreviation list that would separate them is unbounded, the two languages share the
punctuation, and a rule that is wrong occasionally on `art. 5` on both sides is
worth more than a rule that is right on abbreviations and wrong on `¿…?` — Spanish's
inverted marks open a sentence rather than close one, so a splitter keyed on `?`
alone loses the opening mark and the sentence with it.

## What ticket 31 wrote, and what this file writes now

`data/cl100k_base.tiktoken` and `tokenizer.py` are new. The page offsets were
relative to one page and are absolute now (the bug that made `page_to` name the first
page), the heading path is a stack of levels rather than the last line seen, and the
chunk rows are a parent/child pair per section rather than one row per block — which
is why `CHUNKING_VERSION` changed. A re-index of an existing corpus is therefore
`WHERE chunking_version <> 'parent-child-v1'`, which is the query the column exists
for.
"""

import io
import re
from dataclasses import dataclass

import docx
import openpyxl
from pypdf import PdfReader

from app.domain.document.tokenizer import count_tokens

#: Bumped when the split changes, so a re-index is a query rather than a guess. The
#: column is on the chunk row for exactly this.
CHUNKING_VERSION = "parent-child-v1"

#: What a child chunk aims to be: the unit retrieval matches on. ~400 tokens is the
#: design's number — small enough that a hit is about one thing, large enough to carry
#: the argument around it.
CHILD_TARGET_TOKENS = 400

#: The hard ceiling for a child. Above the target because the packer closes a chunk at
#: a *sentence* boundary: the chunk that straddles the target is allowed to finish its
#: sentence, and this is how far past it may go.
MAX_CHILD_TOKENS = 500

#: How much of a child is repeated at the head of the next one, as a fraction of the
#: target — the design's ~15%. Measured on whole sentences from the tail of the
#: previous child, so the overlap is a sentence the reader can see rather than a
#: fragment that starts mid-clause.
CHILD_OVERLAP_RATIO = 0.15

#: What a parent chunk aims to be: the block handed to the model as context. Not split
#: at a sentence boundary — a parent is never a retrieval result, so there is nothing
#: to gain from cutting it short, and the ceiling exists only so one pathological
#: section cannot produce a parent no prompt window holds.
PARENT_TARGET_TOKENS = 1500
MAX_PARENT_TOKENS = 2000

#: What the ticket's refusal says. One constant because the *message* is the
#: acceptance criterion, and a sentence assembled at two call sites is two sentences.
NO_TEXT_MESSAGE = "no text extracted; upload a text version"

#: A line that is a heading rather than prose: a Markdown heading, or a numbered
#: clause such as `3.2 Alcance`. Read as structure, never stripped from the chunk —
#: the heading is part of what the chunk says.
_HEADING = re.compile(r"^(#{1,6}\s+\S|\d+(?:\.\d+)*[.)]?\s+\S)")

#: A heading with its Markdown level, for the path stack. A numbered clause is level 1
#: unless the format said otherwise, because the numbering's own depth (`3.2` is
#: deeper than `3`) is a tree this module deliberately does not rebuild — the path is
#: for a citation, and "3.2 Alcance" answers that on its own.
_MARKDOWN_HEADING = re.compile(r"^(#{1,6})\s+(\S.*)$")

#: A PDF's section title, where the format has no styles to consult: a short line in
#: capitals. The word ceiling is what keeps a shouted sentence ("NO SE ADMITE
#: DEVOLUCIONES DESPUES DE TREINTA DIAS") from becoming one — a title is short by
#: nature, and the cost of missing one is a section that is packed as prose.
_ALLCAPS_HEADING = re.compile(r"(?=.*[A-ZÁÉÍÓÚÜÑ])[^a-záéíóúüñ]{4,80}$")
_ALLCAPS_MAX_WORDS = 12

#: Sentence terminators, and the closing marks that may follow one. See the module
#: docstring for the languages this covers and why it is one rule rather than two.
_TERMINATORS = ".!?…"
_CLOSERS = ')"\'»”’]'

#: Runs of horizontal whitespace, and runs of blank lines. Both collapse, because a
#: PDF's line breaks are a layout artefact: the same sentence comes back with
#: different spacing from a different producer.
_SPACES = re.compile(r"[ \t\u00a0]+")
_BLANK_LINES = re.compile(r"\n{3,}")

#: Where a paragraph begins inside a page's text. `re.split` keeps the delimiters, so
#: the offsets stay aligned with the text that was joined.
_PARAGRAPH = re.compile(r"\n\s*\n")


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
    """One row of `document_chunks`, before it is written.

    `parent` is the index of the parent row this chunk belongs to, and it is set on
    children only — a parent's own `parent` is `None`, because the self-reference in
    the schema means "this fragment's context block", and a parent has none.
    """

    chunk_index: int
    content: str
    token_count: int
    page_from: int | None = None
    page_to: int | None = None
    heading_path: str | None = None
    parent: int | None = None


@dataclass(frozen=True, slots=True)
class ParentGroup:
    """One parent and the children under it, by index in `Chunking.chunks`.

    The link by *index* rather than by an id is what makes the split testable without
    a database: the repository resolves the indices to the uuids it generates, and
    this module never learns what a uuid is.
    """

    index: int
    child_indices: tuple[int, ...]
    heading_path: str | None


@dataclass(frozen=True, slots=True)
class Chunking:
    """The split: rows in write order, and the links between them.

    `chunks` is ordered parents-before-children within a section only in the sense
    that the indices are: **children are written first, parents after**, because a
    child names its parent by uuid and that uuid has to exist. The index assignment
    is therefore "0..n-1 are the children in reading order, n.. are the parents",
    which is also the order the unique `(document_id, chunk_index)` makes
    reproducible across runs.
    """

    chunks: list[Chunk]
    parents: list[ParentGroup]

    @property
    def child_count(self) -> int:
        return sum(len(group.child_indices) for group in self.parents)


@dataclass(frozen=True, slots=True)
class _Block:
    """One page's text, or the whole text when the format has no pages.

    Offsets are absolute in `ParsedText.text`: `_blocks` walks the pages the same way
    `_pdf` joined them, so `text[start:end] == body` for every block. Ticket 31 built
    these relative to the page, which is why its `page_to` named the first page of
    every multi-page chunk.
    """

    start: int
    end: int
    body: str
    page: int | None


@dataclass(frozen=True, slots=True)
class _Source:
    """The text and the page map, threaded through the split.

    One object rather than two parameters through five functions, and the reason is
    the one thing the split must never do: invent a page. Every page number written
    below comes from `page_of`.
    """

    text: str
    blocks: tuple[_Block, ...]

    def page_of(self, offset: int) -> int | None:
        """Which page an offset falls on, or `None` for a format without pages.

        The block whose span *contains* the offset, and the last block starting at or
        before it as the fallback for an offset that is a separator's. Never the page
        a chunk begins on: ticket 31 answered that for both ends, which is what made
        `page_to == page_from` for every chunk it wrote.
        """
        found: int | None = None
        for block in self.blocks:
            if block.page is None:
                return None
            if block.start <= offset <= block.end:
                return block.page
            if block.start > offset:
                break
            found = block.page
        return found


@dataclass(frozen=True, slots=True)
class _Unit:
    """A sentence-sized piece of one section, with the span it came from.

    The span travels with the text so a child's page range is a reading of the
    original rather than a search for the fragment's text in the document — which
    would find the first occurrence and be wrong for every repeated sentence.

    `page`/`end_page` are the pages the span *begins* and *ends* on, and they differ
    for a sentence that crosses a page break — which is the whole reason ticket 31's
    chunks claimed `page_to == page_from`.
    """

    text: str
    start: int | None
    end: int | None
    page: int | None
    end_page: int | None


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


def parse(content: bytes, media_type: str) -> tuple[ParsedText, Chunking]:
    """`extract`, then split. The whole parsing half in one call."""
    parsed = extract(content, media_type)
    return parsed, chunk(parsed)


def chunk(parsed: ParsedText) -> Chunking:
    """The structural split: sections first, then length inside each one.

    Four steps, and each is a decision rather than a pass:

    1. `_sections` cuts the document where the format says a section begins and keeps
       the heading path and the page each section starts on.
    2. Each section is split into sentences (`_sentences`) — the units a child may
       end on.
    3. `_pack` fills children to `CHILD_TARGET_TOKENS`, closing on a unit boundary and
       repeating the tail for `CHILD_OVERLAP_RATIO`.
    4. Children are grouped into parents under `PARENT_TARGET_TOKENS`, and the indices
       are assigned children-first so a child's parent index always resolves.
    """
    if not parsed.text:
        return Chunking(chunks=[], parents=[])

    source = _Source(text=parsed.text, blocks=tuple(_blocks(parsed)))

    units: list[_Unit] = []
    section_units: list[tuple[int, int, str | None]] = []
    for section, offset, heading in _sections(source):
        first = len(units)
        units.extend(_sentences(section, offset, source))
        section_units.append((first, len(units), heading))

    children: list[_Unit] = []
    groups: list[tuple[int, int, str | None]] = []
    for first, last, heading in section_units:
        if first == last:
            continue
        begin = len(children)
        children.extend(_pack(units[first:last]))
        groups.append((begin, len(children), heading))

    chunks: list[Chunk] = []
    parents: list[ParentGroup] = []
    owner: dict[int, int] = {}
    for begin, end, heading in groups:
        for start, stop in _parent_spans(children[begin:end]):
            parent_index = len(children) + len(parents)
            child_indices = list(range(begin + start, begin + stop))
            for child_index in child_indices:
                owner[child_index] = parent_index
            parents.append(
                ParentGroup(
                    index=parent_index,
                    child_indices=tuple(child_indices),
                    heading_path=heading,
                )
            )
            spent = children[begin + start : begin + stop]
            chunks.append(_parent_row(parent_index, spent, heading))

    for position, unit in enumerate(children):
        chunks.append(
            _child_row(
                position,
                unit,
                parent=owner[position],
                heading=next(
                    path for begin, end, path in groups if begin <= position < end
                ),
            )
        )
    return Chunking(chunks=chunks, parents=parents)


def _blocks(parsed: ParsedText) -> list[_Block]:
    """The text as blocks, each knowing the span it occupies.

    A PDF is cut into page blocks first so every later chunk can name the page it came
    from; the other formats produce one block covering the whole text, because `None`
    pages are the truthful answer for a file that has no pages.
    """
    if parsed.page_count is None:
        return [_Block(start=0, end=len(parsed.text), body=parsed.text, page=None)]

    blocks: list[_Block] = []
    offset = 0
    for page, page_text in enumerate(parsed.pages, start=1):
        blocks.append(
            _Block(start=offset, end=offset + len(page_text), body=page_text, page=page)
        )
        # The two characters `_pdf` joined the pages with: counting them is what keeps
        # every later offset absolute, and forgetting them is how a chunk's end drifts
        # one page early as a document grows.
        offset += len(page_text) + 2
    return blocks


def _sections(source: _Source) -> list[tuple[str, str | None]]:
    """The document cut into sections, each with the heading path above it.

    A section is one heading and the prose under it, so the split is the document's
    own structure rather than a blank line: `# Vacaciones` and the paragraph that
    answers a question about vacations are one unit, and a citation can name the
    heading it came from.

    The path is a stack of levels — "3.2 Alcance" under "3. Personal" reads as
    `3. Personal > 3.2 Alcance` — because the ticket asks for a heading *hierarchy*
    and a path of one line is a document with no hierarchy at all. An XLSX sheet and a
    DOCX Heading 1 both arrive here as Markdown headings, which is what lets one
    reader-agnostic walk serve four formats.

    A section's first child *is* prefixed with its heading text, because the packer
    hands the heading to it as the opening unit: "1.2 Vacaciones" followed by the prose
    that answers a question about vacations is one fragment a reader can understand,
    and the heading is the most compact statement of what the fragment is about. It is
    also recorded in `heading_path` separately, which is what retrieval filters on —
    the duplication is the price of the fragment standing on its own.
    """
    sections: list[tuple[str, int, str | None]] = []
    stack: list[tuple[int, str]] = []
    body: list[str] = []
    heading_path: str | None = None
    start = 0
    cursor = 0

    def flush() -> None:
        nonlocal body, heading_path
        text = "\n\n".join(body).strip()
        if text:
            sections.append((text, start, heading_path))
        body = []

    for block in source.blocks:
        cursor = block.start
        for text in _paragraphs(block):
            level = _heading_level(text)
            if level is not None:
                flush()
                # The next section begins where this heading does, and the offset is
                # tracked as the document is walked rather than searched for later: a
                # heading that occurs twice would otherwise cut both sections at the
                # first occurrence.
                start = cursor
                # A level replaces everything at or below it: `## 1.2` closes `## 1.1`
                # but not `# 1`, which is what makes the path the chain of headings a
                # reader sees rather than a list of every heading so far.
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, text))
                heading_path = " > ".join(entry[1] for entry in stack)
            body.append(text)
            # Paragraphs inside one block were joined with a blank line, for the reason
            # `_pdf` joins pages with one, so the length to add back is two.
            cursor += len(text) + 2
    flush()
    return sections


def _paragraphs(block: _Block) -> list[str]:
    """One block's paragraphs, stripped and in order.

    No offsets: pages are resolved from each *sentence's* span a level down, and a
    paragraph that carried a span of its own would be a second answer to "where is
    this text" waiting to disagree with the first.
    """
    return [body for paragraph in _PARAGRAPH.split(block.body) if (body := paragraph.strip())]


def _heading_level(text: str) -> int | None:
    """The heading depth of a line, or `None` when it is prose.

    Three shapes, because the formats disagree about how a heading is marked and the
    split must not: a Markdown `#` run (which is also how the DOCX and XLSX readers
    report their headings), a numbered clause such as `3.2 Alcance`, and — for a PDF,
    which has no structure at all — a short line in capitals that does not read as a
    sentence.
    """
    markdown = _MARKDOWN_HEADING.match(text)
    if markdown:
        return len(markdown.group(1))
    if _HEADING.match(text):
        return 1
    first_line = text.splitlines()[0].strip()
    if (
        "\n" not in text
        and len(first_line.split()) <= _ALLCAPS_MAX_WORDS
        and not first_line.endswith(tuple(_TERMINATORS))
        and _ALLCAPS_HEADING.match(first_line)
    ):
        return 1
    return None


def _sentences(text: str, offset: int, source: _Source) -> list[_Unit]:
    """The section as sentences, each with its span and its page.

    `offset` is where the section starts in the document, and the walk below carries it
    forward as the text is consumed — which is what makes a span a position in the
    original rather than a position in this string.

    The rules, and each exists because Spanish or English needs it:

    * **A line ends a sentence.** `_normalise` already stripped each line and squeezed
      blank runs, so a newline left in the text is structure — a heading, a paragraph
      break, a table row — and never a PDF's soft wrap. Cutting there is what keeps a
      heading out of the sentence that follows it.
    * **A terminator followed by a capital, a digit or the end of the text ends a
      sentence.** `.`, `!`, `?` and `…`, plus any closing mark after them (`.)`, `."`,
      `.»`, `.”`, `.]`) — Spanish's `¿` and `¡` *open* a sentence, so the terminator is
      the same character in both languages and this needs no per-language list.
    * **A terminator followed by a lower-case letter does not.** That is the whole of
      the abbreviation rule, and it is deliberately blunt: `art. 5`, `p. ej.`, `etc.`
      and `Dr.` all keep their sentence, at the cost of missing a boundary after a
      sentence that ends in a lower-case initial. The list of Spanish and English
      abbreviations is unbounded; this rule is one condition and it is wrong in the
      direction that loses nothing — an over-long unit is packed as one child, while a
      wrong cut would hand retrieval half a clause.
    """
    units: list[_Unit] = []
    consumed = offset
    index = 0
    length = len(text)
    while index < length:
        if text[index] == "\n":
            units.append(_unit(text[: index + 1], consumed, source))
            consumed += index + 1
            text = text[index + 1 :]
            length = len(text)
            index = 0
            continue
        if text[index] not in _TERMINATORS:
            index += 1
            continue
        end = index + 1
        while end < length and text[end] in _TERMINATORS:
            end += 1
        while end < length and text[end] in _CLOSERS:
            end += 1
        if end < length and text[end].isalpha() and text[end].islower():
            index += 1
            continue
        units.append(_unit(text[:end], consumed, source))
        consumed += end
        text = text[end:]
        length = len(text)
        index = 0

    if text.strip():
        units.append(_unit(text, consumed, source))
    return [unit for unit in units if unit.text.strip()]


def _unit(piece: str, consumed: int, source: _Source) -> _Unit:
    """One raw sentence, with the span it occupies in the original document.

    `consumed` is how much of this section has already been taken, and the span is
    found from there rather than by searching the whole document: a sentence that
    occurs twice — a repeated clause, a table row — would otherwise be located at its
    first occurrence and carry the wrong page for the rest of the document.
    """
    body = piece.strip()
    span_end = consumed + len(piece)
    if not source.blocks or source.blocks[0].page is None:
        # A format without pages. The span is still measured so that two sections'
        # offsets are comparable, but the pages are `None` and stay `None`.
        return _Unit(text=body, start=consumed, end=span_end, page=None, end_page=None)
    absolute = source.text.find(body, consumed, span_end + 1)
    if absolute < 0:  # pragma: no cover - the sentence came from this document
        absolute = consumed
    end = absolute + len(body)
    return _Unit(
        text=body,
        start=absolute,
        end=end,
        page=source.page_of(absolute),
        end_page=source.page_of(end),
    )


def _pack(units: list[_Unit]) -> list[_Unit]:
    """Units packed into children of about `CHILD_TARGET_TOKENS`, on unit boundaries.

    **Length decides *after* structure does.** A child only ever contains units from
    one section (the caller passes one section's units), and the boundary is always
    between two units — the target is a target, and the sentence that crosses it is
    finished rather than cut. A unit that is itself longer than `MAX_CHILD_TOKENS` is
    the one exception, in `_split_oversized`, and it is the only place a sentence can be
    divided at all.
    """
    prepared: list[_Unit] = []
    for unit in units:
        if count_tokens(unit.text) <= MAX_CHILD_TOKENS:
            prepared.append(unit)
        else:
            prepared.extend(_split_oversized(unit))

    children: list[_Unit] = []
    current: list[_Unit] = []
    tokens = 0
    for unit in prepared:
        size = count_tokens(unit.text)
        # Two conditions, and both are needed. `>= target` is what makes ~400 a target
        # rather than a ceiling: without it, packing stopped only at the maximum and a
        # section of 120-token sentences produced 480-token children that emptied at the
        # end. The second is the ceiling, and it is what keeps a piece of a *divided*
        # oversized sentence — which arrives already full — from being merged with a
        # neighbour or growing an overlap.
        if current and (
            tokens >= CHILD_TARGET_TOKENS or tokens + size > MAX_CHILD_TOKENS
        ):
            children.append(_merge(current))
            current = _overlap(current, unit)
            tokens = sum(count_tokens(part.text) for part in current)
        current.append(unit)
        tokens += size
    if current:
        children.append(_merge(current))
    return children


def _overlap(current: list[_Unit], next_unit: _Unit) -> list[_Unit]:
    """The tail of a finished child, repeated at the head of the next one.

    Whole units from the end, up to `CHILD_OVERLAP_RATIO` of the target — a phrase on
    the boundary is then retrievable from either side, which is the design's reason
    for an overlap at all. At least one unit is kept when the child has two, and never
    all of them: an overlap that was the whole child would make the next one a copy of
    it.

    **The overlap may never push the next child over `MAX_CHILD_TOKENS`**, and that is
    enforced rather than hoped for: a child of one enormous sentence followed by a
    normal one would otherwise begin at 500 tokens and end at 520. The budget shrinks
    to whatever leaves room for the unit that has to be packed next, and a unit that
    leaves room for nothing is answered with no overlap — the boundary loses its echo,
    which is a smaller loss than a chunk over the ceiling.
    """
    if len(current) < 2:
        return []
    room = MAX_CHILD_TOKENS - count_tokens(next_unit.text)
    budget = min(int(CHILD_TARGET_TOKENS * CHILD_OVERLAP_RATIO), room)
    if budget <= 0:
        return []
    kept: list[_Unit] = []
    spent = 0
    for unit in reversed(current[:-1]):
        size = count_tokens(unit.text)
        if kept and spent + size > budget:
            break
        if spent + size > budget:
            continue
        kept.insert(0, unit)
        spent += size
    if len(kept) >= len(current):  # pragma: no cover - the slice above prevents it
        kept = kept[1:]
    return kept


def _split_oversized(unit: _Unit) -> list[_Unit]:
    """One sentence longer than a whole child, divided on word boundaries.

    **The only cut that can land inside a sentence, and it is a fallback.** A measured
    fixture — a generated table row, a legal clause with no full stop in two pages —
    would otherwise become a single chunk over the model's window. The division is
    greedy on the ceiling and measured with the real tokenizer rather than estimated
    from a character ratio: a "palabra " repeated three thousand times costs one token
    per word while an accented clause costs three, so a character-estimated cut would
    land over the ceiling on exactly the text this fallback exists for. The boundary is
    a space, so the damage is a cut between two words rather than inside one.
    """
    words = unit.text.split(" ")
    pieces: list[_Unit] = []
    current: list[str] = []
    consumed = 0
    for word in words:
        candidate = " ".join([*current, word])
        if current and count_tokens(candidate) > MAX_CHILD_TOKENS:
            pieces.append(_piece(" ".join(current), unit, consumed))
            consumed += len(" ".join(current)) + 1
            current = [word]
            continue
        current.append(word)
    if current:
        pieces.append(_piece(" ".join(current), unit, consumed))
    return pieces


def _piece(text: str, unit: _Unit, consumed: int) -> _Unit:
    """One piece of a divided sentence, carrying the parent sentence's page."""
    body = text.strip()
    start = None if unit.start is None else unit.start + consumed
    return _Unit(
        text=body,
        start=start,
        end=None if start is None else start + len(body),
        page=unit.page,
        end_page=unit.end_page,
    )


def _merge(units: list[_Unit]) -> _Unit:
    """Several units as one child, keeping the span and the page range.

    `page` comes from the first unit and `end_page` from the last, which is how a
    child that ran off the bottom of one page answers "pages 1–2" rather than naming
    the page it started on twice — the shape ticket 31's chunks had.
    """
    first, last = units[0], units[-1]
    return _Unit(
        text=" ".join(unit.text.strip() for unit in units).strip(),
        start=first.start,
        end=last.end,
        page=first.page,
        end_page=last.end_page if last.end_page is not None else last.page,
    )


def _parent_spans(children: list[_Unit]) -> list[tuple[int, int]]:
    """Where one section's children divide into parents.

    A parent is filled to `PARENT_TARGET_TOKENS` and **closed before the child that
    would cross it**, which is what makes ~1500 a target rather than a description: a
    rule that only closed at `MAX_PARENT_TOKENS` would produce parents of 1990 tokens
    for a section built out of 500-token children, and the number the design names
    would be one nothing implements. The ceiling then covers the case the target
    cannot: a single child larger than the whole target becomes a parent of its own.

    A parent never spans two sections, because the caller passes one section's
    children: a context block that began in `Vacaciones` and ended in `Permisos` would
    answer a question about neither.
    """
    spans: list[tuple[int, int]] = []
    start = 0
    tokens = 0
    for position, child in enumerate(children):
        size = count_tokens(child.text)
        if position > start and tokens + size > PARENT_TARGET_TOKENS:
            spans.append((start, position))
            start, tokens = position, 0
        tokens += size
    if start < len(children):
        spans.append((start, len(children)))
    return spans


def _parent_row(index: int, children: list[_Unit], heading: str | None) -> Chunk:
    """The context block: its children's text, in order, as one row."""
    content = "\n\n".join(child.text for child in children)
    return Chunk(
        chunk_index=index,
        content=content,
        token_count=max(1, count_tokens(content)),
        page_from=children[0].page,
        page_to=children[-1].end_page if children[-1].end_page is not None else children[-1].page,
        heading_path=heading,
        parent=None,
    )


def _child_row(
    index: int, unit: _Unit, *, parent: int, heading: str | None
) -> Chunk:
    """A child row: the sentence-sized unit retrieval matches, and where it came from."""
    return Chunk(
        chunk_index=index,
        content=unit.text,
        token_count=max(1, count_tokens(unit.text)),
        page_from=unit.page,
        page_to=unit.end_page if unit.end_page is not None else unit.page,
        heading_path=heading,
        parent=parent,
    )


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

    **A heading style becomes a Markdown heading**, which is the whole of what this
    reader does about structure: the format records the level in the paragraph's style
    and nothing downstream can see a style, so the level is rendered into the text
    where `_sections` can read it. Without it every DOCX would be one section and the
    structural half of the split would be a no-op for the format a company's policies
    are most likely to arrive in.

    The order of paragraphs and tables is document order as far as the format exposes
    it, which is why the tables are appended rather than interleaved: a docx table's
    true position needs the body's XML, and a chunk that names the right text in the
    wrong place is worth more than dropping it.
    """
    document = docx.Document(io.BytesIO(content))
    parts = [_docx_paragraph(paragraph) for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.append("\t".join(cell.text for cell in row.cells))
    text = _normalise("\n\n".join(part for part in parts if part.strip()))
    return ParsedText(text=text, char_count=len(text), page_count=None)


def _docx_paragraph(paragraph) -> str:  # noqa: ANN001 - python-docx's Paragraph
    """A paragraph, with its heading level rendered in when it has one."""
    style = getattr(paragraph.style, "name", "") or ""
    body = paragraph.text
    if style.startswith("Heading "):
        try:
            level = int(style.split()[1])
        except (IndexError, ValueError):  # pragma: no cover - python-docx names them
            level = 1
        return f"{'#' * min(max(level, 1), 6)} {body}"
    return body


def _xlsx(content: bytes) -> ParsedText:
    """One section per sheet, rows tab-separated and numbers as stored.

    `read_only` so a large workbook is streamed rather than expanded into memory, and
    `data_only` so a formula's last computed value is read instead of the formula —
    the text of a document is what it says, not how it was built.

    A sheet's name becomes a heading, so the chunks carry "which sheet" the way a
    PDF's carry "which page" — and so the split cuts at the sheet boundary, which is
    the only structure a workbook has. The name is written **on its own line above the
    rows**, because a heading and the data under it are two paragraphs: `_sections`
    reads headings one paragraph at a time, and a name glued to the first row would be
    a heading nobody could recognise — which is exactly how the sheet path went
    missing the first time.
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
                sections.append(f"# {sheet.title}\n\n{body}")
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
    "CHILD_OVERLAP_RATIO",
    "CHILD_TARGET_TOKENS",
    "CHUNKING_VERSION",
    "MAX_CHILD_TOKENS",
    "MAX_PARENT_TOKENS",
    "NO_TEXT_MESSAGE",
    "PARENT_TARGET_TOKENS",
    "Chunk",
    "Chunking",
    "ParentGroup",
    "ParseRefused",
    "ParsedText",
    "chunk",
    "extract",
    "parse",
]
