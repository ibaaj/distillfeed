from rss_reader.presentation import render_summary_markdown


def test_summary_markdown_formats_common_model_output_and_escapes_html():
    rendered = str(render_summary_markdown(
        "## Main result\n\n**Important** finding with `code`.\n\n"
        "- First point\n- Second point\n\n<script>alert(1)</script>"
    ))
    assert "<h4>Main result</h4>" in rendered
    assert "<strong>Important</strong>" in rendered
    assert "<code>code</code>" in rendered
    assert "<ul>" in rendered and rendered.count("<li>") == 2
    assert "<script>" not in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered


def test_summary_markdown_handles_plain_prose_without_artifacts():
    assert str(render_summary_markdown("A plain sentence.")) == "<p>A plain sentence.</p>"
    assert str(render_summary_markdown("")) == ""
