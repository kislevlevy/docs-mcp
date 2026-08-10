"""Chunker unit tests. No model or index needed."""

from __future__ import annotations

from docs_mcp.chunk import embedding_text, split_document
from docs_mcp.config import settings


def paths(chunks):
    return [c.heading_path for c in chunks]


def joined(chunks):
    return "\n".join(c.text for c in chunks)


def filler(words: str, n: int = 90) -> str:
    """Section body large enough that the packer keeps it as its own chunk."""
    return (words + " ") * n


def test_markdown_headings_build_breadcrumbs():
    text = (
        f"# Guide\n\n{filler('intro prose')}\n\n"
        f"## Setup\n\n{filler('installation steps')}\n\n"
        f"### Details\n\n{filler('fine print details')}\n"
    )
    title, chunks = split_document(text, ".md")
    assert title == "Guide"
    assert "Guide > Setup > Details" in paths(chunks)


def test_small_adjacent_sections_merge_into_one_chunk():
    # Packing deliberately merges tiny sections; the headings stay in the text,
    # and the chunk carries the first section's breadcrumb.
    _, chunks = split_document("# Guide\n\nintro\n\n## Setup\n\nstep one\n\n### Details\n\nfine print\n", ".md")
    assert len(chunks) == 1
    assert chunks[0].heading_path == "Guide"
    assert "## Setup" in chunks[0].text and "### Details" in chunks[0].text


def test_frontmatter_title_wins_and_is_removed():
    title, chunks = split_document('---\ntitle: "Real Title"\ntype: page\n---\n\nbody text\n', ".md")
    assert title == "Real Title"
    assert "type: page" not in joined(chunks)


def test_heading_anchors_stripped():
    _, chunks = split_document("## Per-Queue TTL {#per-queue-ttl}\n\nbody\n", ".md")
    assert "{#per-queue-ttl}" not in " ".join(paths(chunks))


def test_headings_inside_fences_do_not_split():
    text = "# Real\n\n```sh\n# not a heading\necho hi\n```\n\ntail\n"
    _, chunks = split_document(text, ".md")
    assert len(chunks) == 1
    assert "echo hi" in chunks[0].text


def test_hugo_shortcodes_keep_their_quoted_arguments():
    text = (
        f'# T\n\n{{{{% notice warning "Heads up" %}}}}\n{filler("be careful when configuring")}\n'
        f"{{{{% /notice %}}}}\n\n{{{{% children %}}}}\n"
    )
    _, chunks = split_document(text, ".md")
    body = joined(chunks)
    assert "Heads up" in body and "be careful" in body
    assert "notice" not in body and "children" not in body


def test_shortcodes_inside_fences_survive():
    text = f"# T\n\n{filler('prose about the snippet')}\n\n```go\n{{{{% notice %}}}}\n```\n"
    _, chunks = split_document(text, ".md")
    assert "{{% notice %}}" in joined(chunks)


def test_jsx_and_imports_stripped_from_plain_md():
    # Docusaurus ships JSX inside .md, which rabbitmq and fastapi both do.
    text = (
        "import Tabs from '@theme/Tabs';\n\n# T\n\n"
        f'<Tabs groupId="x">\n<TabItem value="a">\n{filler("real text")}\n</TabItem>\n</Tabs>\n'
    )
    _, chunks = split_document(text, ".md")
    body = joined(chunks)
    assert "real text" in body
    assert "@theme/Tabs" not in body and "<Tabs" not in body and "<TabItem" not in body


def test_html_comments_stripped_across_lines():
    _, chunks = split_document(f"# T\n\n<!--\nlicense\nblock\n-->\n\n{filler('keep me')}\n", ".md")
    body = joined(chunks)
    assert "keep me" in body and "license" not in body


def test_rst_sections_and_levels():
    text = (
        f"Periodic Tasks\n==============\n\n{filler('intro prose')}\n\n"
        f"Entries\n-------\n\n{filler('entry explanation')}\n\n"
        f"Available Fields\n~~~~~~~~~~~~~~~~\n\n{filler('field descriptions')}\n"
    )
    title, chunks = split_document(text, ".rst")
    assert title == "Periodic Tasks"
    assert any("Entries > Available Fields" in p for p in paths(chunks))


def test_rst_toctree_and_labels_dropped():
    text = (
        "Index\n=====\n\n.. _some-label:\n\n.. toctree::\n    :maxdepth: 2\n\n    getting-started\n    faq\n\n"
        + filler("real content here")
    )
    _, chunks = split_document(text, ".rst")
    body = joined(chunks)
    assert "real content here" in body
    assert "toctree" not in body and "maxdepth" not in body and "_some-label" not in body


def test_every_chunk_has_a_locator():
    text = f"{filler('preamble before any heading')}\n\n# Later Heading\n\n{filler('later content')}\n"
    _, chunks = split_document(text, ".md")
    assert all(c.heading_path for c in chunks)


def test_oversized_section_is_split_under_the_cap():
    body = "\n\n".join(f"paragraph {i} " + "filler words " * 60 for i in range(60))
    _, chunks = split_document(f"# Big\n\n{body}\n", ".md")
    assert len(chunks) > 1
    assert all(len(c.text) <= settings.chunk_max for c in chunks)


def test_giant_unbreakable_block_still_capped():
    # A single code block with no blank lines cannot be split on paragraphs.
    huge = "\n".join(f"line {i} " + "x" * 80 for i in range(2000))
    _, chunks = split_document(f"# T\n\n```\n{huge}\n```\n", ".md")
    assert all(len(c.text) <= settings.chunk_max for c in chunks)


def test_stub_documents_produce_nothing():
    # Hugo redirect files are just a target path; they have nothing retrievable.
    _, chunks = split_document('---\ntitle: "YouTube"\ntype: "redirect"\n---\n', ".md")
    assert chunks == []


def test_chunk_ordinals_are_contiguous():
    body = "\n\n".join(f"## S{i}\n\n" + "text " * 400 for i in range(12))
    _, chunks = split_document(f"# T\n\n{body}", ".md")
    assert [c.ord for c in chunks] == list(range(len(chunks)))


def test_embedding_text_leads_with_the_breadcrumb():
    out = embedding_text("rabbitmq", "TTL > Per-Queue", "body")
    assert out.startswith("rabbitmq > TTL > Per-Queue")
    assert out.endswith("body")
