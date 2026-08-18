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


def test_summary_markdown_recovers_model_compressed_arxiv_bullets_and_section_heading():
    rendered = str(render_summary_markdown(
        "Introduction.\n\n"
        "Neuro-symbolique & raisonnement vérifiable\n\n"
        "- 2608.15382 — *Grounding Healthcare LLMs*: First. "
        "- 2608.16224 — *STAIR*: Second."
    ))
    assert "<h3>Neuro-symbolique &amp; raisonnement vérifiable</h3>" in rendered
    assert rendered.count("<li>") == 2
    assert "<em>Grounding Healthcare LLMs</em>" in rendered
    assert "<em>STAIR</em>" in rendered


def test_summary_markdown_recovers_multiple_compressed_daily_brief_sections():
    rendered = str(render_summary_markdown(
        "Aujourd’hui, le corpus se répartit entre plusieurs thèmes.\n\n"
        "Neuro-symbolique & raisonnement vérifiable\n\n"
        "- 2608.15382 — *Grounding Healthcare LLMs*: First. "
        "- 2608.16224 — *STAIR*: Second.\n"
        "Incertitude et calibration\n\n"
        "- 2608.14617 — *Calibrated Trust*: Third. "
        "- 2608.16002 — *RUPA*: Fourth."
    ))
    assert rendered.count("<h3>") == 2
    assert rendered.count("<ul>") == 2
    assert rendered.count("<li>") == 4
    assert "<em>Calibrated Trust</em>" in rendered
