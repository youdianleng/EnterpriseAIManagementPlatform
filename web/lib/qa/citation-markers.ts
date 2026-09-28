/**
 * `[1]`, `[2]` in an answer become real links a client can intercept.
 *
 * §5.2's citation format is a marker in the prose that points at an entry of the answer's
 * citation list, and the ticket asks for those markers to be clickable badges. The
 * question is *where* to turn them into something clickable, and the answer is the parsed
 * Markdown tree rather than the string:
 *
 * * **A regular expression over the raw Markdown would rewrite code.** `array[1]` inside a
 *   fenced block, or `[1]` in a snippet of a policy document, is text the reader is meant
 *   to see verbatim; a string pass would replace it and silently corrupt a quoted excerpt.
 *   Walking the tree visits only `text` nodes, so code spans and code blocks — which are
 *   `inlineCode` and `code` nodes — can never be touched.
 * * **Only markers that *are* citations are rewritten.** A `[7]` in an answer that carries
 *   three citations is a footnote the model invented or a bracket in a quotation; linking
 *   it would open a panel that does not exist. `max` is the citation count, and anything
 *   above it is left exactly as it was written.
 *
 * The marker becomes a link with a `#cite-N` href rather than a custom node, because the
 * renderer already has to supply an `a` component (see `answer-view.tsx`) and a link is
 * what the marker *is*: it survives a client that renders links and nothing else, and the
 * href is the one fragment scheme that cannot be mistaken for a document URL.
 */

/** The subset of an mdast node this plugin reads and writes. */
type Node = {
  type: string;
  value?: string;
  url?: string;
  children?: Node[];
};

export type CitationMarkerOptions = {
  /** The number of citations the answer carries; higher numbers are left alone. */
  max: number;
};

export function remarkCitationMarkers(options: CitationMarkerOptions) {
  const max = Number.isFinite(options?.max) ? Math.floor(options.max) : 0;
  return (tree: unknown) => {
    if (max < 1) return;
    walk(tree as Node, max);
  };
}

function walk(node: Node, max: number): void {
  if (!node.children) return;
  const next: Node[] = [];
  for (const child of node.children) {
    if (child.type === "text" && typeof child.value === "string") {
      next.push(...markers(child.value, max));
    } else {
      walk(child, max);
      next.push(child);
    }
  }
  node.children = next;
}

/** One text node's value, split around the citation markers it contains. */
function markers(value: string, max: number): Node[] {
  const pattern = /\[(\d{1,3})\]/g;
  const parts: Node[] = [];
  let last = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(value)) !== null) {
    const index = Number(match[1]);
    // Above the list: a bracket that is not a citation, left as text.
    if (index < 1 || index > max) continue;
    if (match.index > last) {
      parts.push({ type: "text", value: value.slice(last, match.index) });
    }
    parts.push({
      type: "link",
      url: `#cite-${index}`,
      children: [{ type: "text", value: match[1] }],
    });
    last = match.index + match[0].length;
  }
  if (parts.length === 0) return [{ type: "text", value }];
  if (last < value.length) parts.push({ type: "text", value: value.slice(last) });
  return parts;
}
