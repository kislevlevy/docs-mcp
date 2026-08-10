"""Structure-aware splitting for Markdown, MDX, reStructuredText and plain text.

Splitting mid-example is the single biggest quality killer for code-heavy docs, so
sections are cut on headings and never inside a fenced code block. Each chunk keeps
the heading breadcrumb that led to it, which is what makes an isolated chunk
interpretable once it is pulled out of its document.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import settings

# ---------------------------------------------------------------- data

@dataclass(frozen=True, slots=True)
class Chunk:
    ord: int
    heading_path: str
    text: str


@dataclass(slots=True)
class _Section:
    level: int
    title: str
    path: list[str]
    body: str


# ---------------------------------------------------------------- shared helpers

_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def _fence_mask(lines: list[str]) -> list[bool]:
    """True for every line that sits inside a fenced code block (fence lines included)."""
    inside: list[bool] = []
    marker: str | None = None
    for line in lines:
        match = _FENCE_RE.match(line)
        if marker is None:
            if match:
                marker = match.group(1)[0] * 3
                inside.append(True)
                continue
            inside.append(False)
        else:
            inside.append(True)
            if match and match.group(1)[0] * 3 == marker:
                marker = None
    return inside


def _breadcrumb(stack: list[str]) -> str:
    return " > ".join(stack)


def _push(stack: list[tuple[int, str]], level: int, title: str) -> list[str]:
    while stack and stack[-1][0] >= level:
        stack.pop()
    stack.append((level, title))
    return [t for _, t in stack]


# ---------------------------------------------------------------- markdown / mdx

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)
_ATX_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
# Whole-line JSX component tags: <Tip>, </Tip>, <Frame caption="x">, <Card ... />
_JSX_LINE_RE = re.compile(r"^\s*</?[A-Z][\w.]*(?:\s[^<>]*?)?/?>\s*$")
_MDX_IMPORT_RE = re.compile(r"^\s*(?:import|export)\s+.*$")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
# Trailing heading anchors: `## Title {#slug}` / `## Title { #slug }`
_HEADING_ANCHOR_RE = re.compile(r"\s*\{\s*#[^}]*\}\s*$")
# Hugo shortcodes: {{% notice warning "Heads up" %}}, {{< ref "vql" >}}, {{% /notice %}}
_SHORTCODE_RE = re.compile(r"\{\{[<%]-?\s*(.*?)\s*-?[<%>]*[%>]\}\}", re.DOTALL)
_QUOTED_RE = re.compile(r'"([^"]*)"')


def _clean_heading(title: str) -> str:
    return _HEADING_ANCHOR_RE.sub("", title).strip()


def _strip_shortcodes(text: str) -> str:
    """Replace each Hugo shortcode with its quoted arguments, drop it when it has none.

    `{{% notice warning "Heads up" %}}` keeps "Heads up"; `{{% tab name="Linux" %}}`
    keeps "Linux"; `{{% /notice %}}` and `{{% children %}}` disappear.
    """
    return _SHORTCODE_RE.sub(lambda m: " ".join(_QUOTED_RE.findall(m.group(1))), text)


def _clean_outside_fences(lines: list[str]) -> list[str]:
    """Strip HTML comments and Hugo shortcodes, leaving fenced code untouched.

    Both constructs span multiple lines, so non-fence runs are joined and cleaned
    as whole blocks rather than line by line.
    """
    mask = _fence_mask(lines)
    out: list[str] = []
    run: list[str] = []

    def flush_run() -> None:
        if run:
            cleaned = _strip_shortcodes(_HTML_COMMENT_RE.sub("", "\n".join(run)))
            out.extend(cleaned.splitlines())
            run.clear()

    for line, in_fence in zip(lines, mask, strict=True):
        if in_fence:
            flush_run()
            out.append(line)
        else:
            run.append(line)
    flush_run()
    return out


def _strip_frontmatter(text: str) -> tuple[str, str | None]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return text, None
    title = None
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        if key.strip().lower() == "title":
            title = value.strip().strip("\"'") or None
            break
    return text[match.end() :], title


def _strip_jsx(lines: list[str], mask: list[bool]) -> list[str]:
    """Drop whole-line MDX component tags and imports, keeping their inner prose."""
    kept = []
    for line, in_fence in zip(lines, mask, strict=True):
        if not in_fence and (_JSX_LINE_RE.match(line) or _MDX_IMPORT_RE.match(line)):
            continue
        kept.append(line)
    return kept


def _markdown_sections(text: str) -> tuple[list[_Section], str | None]:
    body, fm_title = _strip_frontmatter(text)
    lines = body.splitlines()

    # Shortcodes and HTML comments are rewritten outside fences only, so code
    # samples that legitimately contain them survive intact.
    lines = _clean_outside_fences(lines)

    # Docusaurus ships JSX inside plain `.md` too (rabbitmq and fastapi both do),
    # so component tags are stripped regardless of extension.
    lines = _strip_jsx(lines, _fence_mask(lines))
    mask = _fence_mask(lines)

    sections: list[_Section] = []
    stack: list[tuple[int, str]] = []
    current = _Section(0, "", [], "")
    buf: list[str] = []
    doc_title = fm_title

    def flush() -> None:
        current.body = "\n".join(buf).strip()
        if current.body or current.title:
            sections.append(_Section(current.level, current.title, list(current.path), current.body))
        buf.clear()

    for line, in_fence in zip(lines, mask, strict=True):
        match = None if in_fence else _ATX_RE.match(line)
        if match:
            flush()
            level = len(match.group(1))
            title = _clean_heading(match.group(2))
            if doc_title is None:
                doc_title = title
            current = _Section(level, title, _push(stack, level, title), "")
        else:
            buf.append(line)
    flush()
    return sections, doc_title


# ---------------------------------------------------------------- rst

_RST_UNDERLINE_CHARS = "=-~^\"'#*+<>`:._"
_RST_RULE_RE = re.compile(rf"^([{re.escape(_RST_UNDERLINE_CHARS)}])\1{{1,}}\s*$")
# Navigation-only directives that add noise and no retrievable content.
_RST_NOISE_DIRECTIVES = ("toctree", "contents", "sectionauthor", "moduleauthor")


def _is_rule(line: str) -> bool:
    return bool(line.strip()) and bool(_RST_RULE_RE.match(line.rstrip()))


_RST_TARGET_RE = re.compile(r"^\s*\.\.\s+_[^:]+:\s*$")


def _drop_rst_noise(lines: list[str]) -> list[str]:
    """Remove `.. toctree::` style directives (with their indented body) and
    standalone `.. _label:` hyperlink targets, which carry no retrievable text."""
    out: list[str] = []
    skip_indent: int | None = None
    for line in lines:
        if skip_indent is not None:
            if not line.strip() or (len(line) - len(line.lstrip())) > skip_indent:
                continue
            skip_indent = None
        match = re.match(r"^(\s*)\.\.\s+(\w+)::", line)
        if match and match.group(2).lower() in _RST_NOISE_DIRECTIVES:
            skip_indent = len(match.group(1))
            continue
        if _RST_TARGET_RE.match(line):
            continue
        out.append(line)
    return out


def _rst_sections(text: str) -> tuple[list[_Section], str | None]:
    body, fm_title = _strip_frontmatter(text)
    lines = _drop_rst_noise(body.splitlines())
    sections: list[_Section] = []
    stack: list[tuple[int, str]] = []
    char_levels: dict[str, int] = {}
    current = _Section(0, "", [], "")
    buf: list[str] = []
    doc_title: str | None = fm_title

    def flush() -> None:
        current.body = "\n".join(buf).strip()
        if current.body or current.title:
            sections.append(_Section(current.level, current.title, list(current.path), current.body))
        buf.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        title = None
        consumed = 0

        # Overline form: rule / title / rule
        if (
            _is_rule(line)
            and i + 2 < len(lines)
            and lines[i + 1].strip()
            and _is_rule(lines[i + 2])
            and line.strip()[0] == lines[i + 2].strip()[0]
        ):
            title, char, consumed = lines[i + 1].strip(), line.strip()[0], 3
        # Underline form: title / rule
        elif (
            line.strip()
            and not _is_rule(line)
            and i + 1 < len(lines)
            and _is_rule(lines[i + 1])
            and len(lines[i + 1].strip()) >= len(line.strip()) - 2
        ):
            title, char, consumed = line.strip(), lines[i + 1].strip()[0], 2

        if title is not None:
            flush()
            level = char_levels.setdefault(char, len(char_levels) + 1)
            if doc_title is None:
                doc_title = title
            current = _Section(level, title, _push(stack, level, title), "")
            i += consumed
            continue

        buf.append(line)
        i += 1
    flush()
    return sections, doc_title


# ---------------------------------------------------------------- packing

def _split_oversized(block: str) -> list[str]:
    """Break a too-large block on paragraph boundaries, never inside a fence."""
    lines = block.splitlines()
    mask = _fence_mask(lines)
    paragraphs: list[str] = []
    buf: list[str] = []
    for line, in_fence in zip(lines, mask, strict=True):
        if not line.strip() and not in_fence:
            if buf:
                paragraphs.append("\n".join(buf))
                buf = []
            continue
        buf.append(line)
    if buf:
        paragraphs.append("\n".join(buf))

    pieces: list[str] = []
    acc: list[str] = []
    size = 0
    for para in paragraphs:
        # A single paragraph over the hard cap (giant table or code block) is cut on lines.
        if len(para) > settings.chunk_max:
            if acc:
                pieces.append("\n\n".join(acc))
                acc, size = [], 0
            para_lines = para.splitlines()
            step = max(1, len(para_lines) * settings.chunk_target // max(len(para), 1))
            for start in range(0, len(para_lines), step):
                pieces.append("\n".join(para_lines[start : start + step]))
            continue
        if size + len(para) > settings.chunk_target and acc:
            pieces.append("\n\n".join(acc))
            acc, size = [], 0
        acc.append(para)
        size += len(para) + 2
    if acc:
        pieces.append("\n\n".join(acc))
    return [p for p in pieces if p.strip()]


def _pack(sections: list[_Section]) -> list[Chunk]:
    chunks: list[Chunk] = []
    buf: list[str] = []
    buf_path = ""
    size = 0

    def emit(path: str, text: str) -> None:
        text = text.strip()
        if text:
            chunks.append(Chunk(ord=len(chunks), heading_path=path, text=text))

    def flush() -> None:
        nonlocal buf, buf_path, size
        if buf:
            emit(buf_path, "\n\n".join(buf))
        buf, buf_path, size = [], "", 0

    for section in sections:
        path = _breadcrumb(section.path)
        heading = f"{'#' * max(section.level, 1)} {section.title}".strip() if section.title else ""
        block = f"{heading}\n\n{section.body}".strip() if heading else section.body.strip()
        if not block:
            continue

        if len(block) > settings.chunk_max:
            flush()
            for piece in _split_oversized(block):
                emit(path, piece)
            continue

        # Start a new chunk once the buffer is big enough to stand on its own.
        if size + len(block) > settings.chunk_target and size >= settings.chunk_min:
            flush()
        # Prefer the first *non-empty* breadcrumb in the buffer: a document that opens
        # with a preamble before its first heading would otherwise lose its locator.
        if not buf or (not buf_path and path):
            buf_path = path
        buf.append(block)
        size += len(block) + 2
    flush()
    return _absorb_stubs(chunks)


def _absorb_stubs(chunks: list[Chunk]) -> list[Chunk]:
    """Fold undersized chunks into their predecessor.

    Heading-only stubs ("### Docs" in a changelog, a bare redirect path) retrieve
    as noise on their own. Merging rather than dropping keeps the text searchable.
    """
    merged: list[Chunk] = []
    for chunk in chunks:
        if (
            merged
            and len(chunk.text) < settings.chunk_min
            and len(merged[-1].text) + len(chunk.text) + 2 <= settings.chunk_max
        ):
            previous = merged[-1]
            merged[-1] = Chunk(
                ord=previous.ord,
                heading_path=previous.heading_path,
                text=f"{previous.text}\n\n{chunk.text}",
            )
            continue
        merged.append(Chunk(ord=len(merged), heading_path=chunk.heading_path, text=chunk.text))
    return merged


# ---------------------------------------------------------------- entry point

def split_document(text: str, suffix: str) -> tuple[str | None, list[Chunk]]:
    """Split `text` into chunks. Returns (document title, chunks)."""
    suffix = suffix.lower()
    if suffix == ".rst":
        sections, title = _rst_sections(text)
    elif suffix in {".md", ".mdx"}:
        sections, title = _markdown_sections(text)
    else:
        sections, title = [_Section(0, "", [], text)], None

    chunks = _pack(sections)
    # A document whose entire content is a stub (Hugo redirect files are just a
    # target path) has nothing retrievable in it; keep it out of the index.
    if len(chunks) == 1 and len(chunks[0].text) < settings.stub_max:
        return title, []
    # Every chunk should carry some locator; fall back to the document title.
    if title:
        chunks = [
            c if c.heading_path else Chunk(ord=c.ord, heading_path=title, text=c.text) for c in chunks
        ]
    return title, chunks


def embedding_text(source: str, heading_path: str, text: str) -> str:
    """What actually gets embedded: breadcrumb first, so a lone chunk keeps its context."""
    prefix = f"{source} > {heading_path}" if heading_path else source
    return f"{prefix}\n\n{text}"
