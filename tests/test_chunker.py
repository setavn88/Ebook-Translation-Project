from booktranslator.chunker import split_html, split_text


def test_split_text_respects_limit_and_keeps_paragraphs():
    paragraphs = [f"Paragraph {i} " + "word " * 40 for i in range(30)]
    text = "\n\n".join(paragraphs)
    chunks = split_text(text, max_chars=1000)
    assert len(chunks) > 1
    assert all(len(c) <= 1000 for c in chunks)
    assert "\n\n".join(chunks) == text


def test_split_text_breaks_long_paragraph_by_sentence():
    paragraph = " ".join(f"Sentence number {i} is here." for i in range(200))
    chunks = split_text(paragraph, max_chars=500)
    assert len(chunks) > 1
    assert all(c.endswith(".") for c in chunks)


def test_split_text_drops_blank_paragraphs():
    assert split_text("a\n\n\n\n  \n\nb", max_chars=100) == ["a\n\nb"]


def test_split_html_roundtrip():
    html = "".join(f"<p class='x'>Line {i} " + "text " * 30 + "</p>\n" for i in range(50))
    html += "<h2>Title</h2><br/>tail"
    chunks = split_html(html, max_chars=800)
    assert len(chunks) > 1
    assert "".join(chunks) == html
    # không cắt giữa một khối <p>
    assert all(c.count("<p") == c.count("</p>") for c in chunks)
