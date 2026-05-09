#!/usr/bin/env python3
"""Render Laws of Mystery diagram as a nicely formatted 1-page PDF."""

import asyncio
import tempfile
import os
from pathlib import Path

from playwright.async_api import async_playwright
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor
from reportlab.platypus import SimpleDocTemplate, Spacer, Image, Paragraph, Table, TableStyle
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER

MERMAID_CODE = r"""
graph TD
    subgraph "FUNDAMENTAL AXIOMS"
        E["<b>Law of Everything</b><br/>EXPERIENCE IS EXPERIENCE"]
        N["<b>Law of Nothing</b><br/>NOT EXPERIENCE IS NOT EXPERIENCE"]
    end

    %% === THE PRESENT MOMENT ===
    E --> TOT["<b>Law of Totality</b><br/>ALL EXPERIENCE IS EXPERIENCE"]
    TOT --> HN["<b>Eternal Here & Now</b><br/>THIS EXPERIENCE IS ALL EXPERIENCE"]
    HN --> NOESC["<b>No Escape / No Negation</b><br/>NOT THIS EXPERIENCE IS NOT EXPERIENCE"]

    %% === UNITY OF THE ONE ===
    HN --> ONE["<b>Law of One</b><br/>All experience is ONE experience"]
    ONE --> UNITY["<b>Law of Unity</b><br/>ALL EXPERIENCE IS THIS EXPERIENCE<br/>Immediacy, no distance"]

    %% === TIME AND CHANGE ===
    UNITY --> CHANGE["<b>Eternal Change</b><br/>THIS EXPERIENCE IS THIS EXPERIENCE<br/>Experience always changes"]
    CHANGE --> VERBS["<b>Law of Verbs</b><br/>ALL EXPERIENCE IS EXPERIENCE OF CHANGE<br/>Everything is a verb, no nouns"]
    VERBS --> P_CHANGE["<b>Paradox of Change</b><br/>Everything changes; Nothing is immutable"]
    P_CHANGE --> IMMUT["<b>Law of Immutability</b><br/>Subjective time is illusion<br/>Memory = experience of another experience"]

    %% === IMMORTALITY (from Law of Nothing) ===
    N --> IMMORTAL["<b>Law of Eternal Life</b><br/>Death cannot be experienced"]
    IMMORTAL --> PERM["<b>Law of Permanence</b><br/>NOT THIS EXPERIENCE IS NOT EXPERIENCE<br/>The One is immortal and immutable"]
    P_CHANGE -.->|Nothing is immutable| PERM

    %% === WHOLENESS ===
    NOESC --> WHOLE["<b>Law of Wholeness</b><br/>No partial experience, every experience is total"]
    WHOLE --> NOCAUSE["<b>No Outcome / No Cause</b><br/>Experience is self-contained"]
    NOCAUSE --> DIRECT["<b>Law of Direct Experience</b><br/>Just experience it — all else is futile"]

    %% === SELF AND GOD ===
    DIRECT --> SOV["<b>Law of Sovereignty</b><br/>ALL EXPERIENCE IS MY EXPERIENCE"]
    SOV --> P_SELF["<b>Paradox of Self</b><br/>I am everything · I am nothing"]
    PERM -->|The One is immutable| P_SELF
    P_SELF --> TRI["<b>Tri-Paradox</b><br/>THE ONE IS EVERYTHING<br/>THE ONE IS NOTHING"]

    %% === MEANING AND STORY ===
    TRI --> P_MEANING["<b>Paradox of Meaning</b><br/>Experience is meaningful;<br/>meaning is not experienceable"]
    P_MEANING --> MIRROR["<b>The Great Mirror</b><br/>Inside = Outside"]
    MIRROR --> P_STORY["<b>Paradox of Story</b><br/>Story is of everything; story is of nothing"]
    P_STORY --> P_MYSTERY["<b>Paradox of Mystery</b><br/>Nothing is not in the Story"]

    %% === EXISTENCE AND DENSITY ===
    P_MYSTERY --> EXIST["<b>Existence & Density</b><br/>Existence = persistence = resistance to change<br/>Nothing: infinitely dense / Everything: infinitely ephemeral"]

    %% === LIFE AND ANOTHER ===
    EXIST --> LIFE["<b>Law of Life</b><br/>EXPERIENCE OF ANOTHER IS EXPERIENCE OF ANOTHER"]
    LIFE --> NOSOL["<b>Law of No Solipsism</b><br/>I AM EVERYTHING AND EVERYTHING IS I"]
    NOSOL --> GOD["A god: unity of one and many<br/>The God creates · A god co-creates"]

    %% === CREATION ===
    GOD --> P_ACTION["<b>Paradox of Action</b><br/>Everything is an action AND nothing acts"]
    VERBS -.->|Everything is an action<br/>by the Law of Verbs| P_ACTION
    P_ACTION --> P_CREATE["<b>Paradox of Creation</b><br/>Nothing creates everything AND<br/>everything creates nothing"]
    P_CREATE --> CREATE["<b>Fundamental Law of Creation</b><br/>THIS EXPERIENCE CREATES THIS EXPERIENCE<br/>To create is to experience"]
    TRI -.->|Unity = substance of creation| CREATE

    %% === INTENTION AND CAUSALITY ===
    CREATE --> THEATRE["<b>Law of Cosmic Theatre</b><br/>Nothing acts · All creation is acting"]
    THEATRE --> INTENT["<b>Law of Intention</b><br/>Substance of action = its meaning"]
    INTENT --> CAUSAL["<b>Law of Causality</b><br/>What I put out is what I get back"]
    CAUSAL --> P_CAUSAL["<b>Paradox of Causality</b><br/>THE OBJECT IS THE SUBJECT<br/>Causality folds back on itself"]
    P_CAUSAL --> RESP["<b>Responsibility</b><br/>Conscious self-creation"]

    %% === LOVE AND TRUST ===
    RESP --> LOVE_LAW["<b>Law of Universal Love</b><br/>TRUST OF LOVE IS EXPERIENCE OF LOVE<br/>Love = relaxation of separation"]
    LOVE_LAW --> P_UNITY["<b>Paradox of Unity</b><br/>SEPARATION FROM UNITY IS UNITY WITH SEPARATION"]
    P_UNITY --> FINAL["<b>EVERYTHING IS CREATED WITH LOVE</b>"]

    %% === CONCLUSION ===
    FINAL --> DANCE["<b>The Dance</b><br/>Mystery ↔ Structure<br/>Subjective ↔ Objective"]

    %% === STYLES ===
    style E fill:#4a90d9,color:#fff,stroke:#2563eb
    style N fill:#444,color:#fff,stroke:#222
    style TRI fill:#7c3aed,color:#fff,stroke:#5b21b6
    style P_SELF fill:#7c3aed,color:#fff,stroke:#5b21b6
    style P_CHANGE fill:#7c3aed,color:#fff,stroke:#5b21b6
    style P_MEANING fill:#7c3aed,color:#fff,stroke:#5b21b6
    style P_STORY fill:#7c3aed,color:#fff,stroke:#5b21b6
    style P_MYSTERY fill:#7c3aed,color:#fff,stroke:#5b21b6
    style P_ACTION fill:#7c3aed,color:#fff,stroke:#5b21b6
    style P_CREATE fill:#7c3aed,color:#fff,stroke:#5b21b6
    style P_CAUSAL fill:#7c3aed,color:#fff,stroke:#5b21b6
    style P_UNITY fill:#7c3aed,color:#fff,stroke:#5b21b6
    style CREATE fill:#6366f1,color:#fff,stroke:#4f46e5
    style FINAL fill:#f59e0b,color:#000,stroke:#d97706
    style DANCE fill:#10b981,color:#fff,stroke:#059669
    style MIRROR fill:#e0e7ff,color:#1e293b,stroke:#6366f1
"""

HTML_TEMPLATE = """
<!DOCTYPE html>
<html><head>
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
<style>
  body {{ margin: 0; padding: 20px; background: white; }}
  .mermaid {{ display: flex; justify-content: center; }}
</style>
</head><body>
<pre class="mermaid">
{code}
</pre>
<script>
  mermaid.initialize({{
    startOnLoad: true,
    theme: 'base',
    themeVariables: {{
      fontSize: '11px',
      fontFamily: 'Helvetica, Arial, sans-serif',
      primaryColor: '#e0e7ff',
      primaryBorderColor: '#6366f1',
      lineColor: '#94a3b8',
      primaryTextColor: '#1e293b'
    }},
    flowchart: {{ curve: 'basis', padding: 8, nodeSpacing: 20, rankSpacing: 25 }}
  }});
</script>
</body></html>
"""


async def render_mermaid_to_png(output_path: str) -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1400, "height": 3500})
        html = HTML_TEMPLATE.format(code=MERMAID_CODE)
        await page.set_content(html)
        await page.wait_for_selector(".mermaid svg", timeout=15000)
        await asyncio.sleep(1)
        svg_el = await page.query_selector(".mermaid svg")
        bbox = await svg_el.bounding_box()
        await page.set_viewport_size({
            "width": int(bbox["width"]) + 40,
            "height": int(bbox["height"]) + 40,
        })
        await svg_el.screenshot(path=output_path, type="png")
        await browser.close()


def build_pdf(diagram_path: str, output_path: str) -> None:
    w, h = A4
    margin = 15 * mm

    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=12 * mm,
    )

    title_style = ParagraphStyle(
        "Title",
        fontName="Helvetica-Bold",
        fontSize=20,
        leading=24,
        alignment=TA_CENTER,
        textColor=HexColor("#1e293b"),
    )
    subtitle_style = ParagraphStyle(
        "Subtitle",
        fontName="Helvetica",
        fontSize=10,
        leading=13,
        alignment=TA_CENTER,
        textColor=HexColor("#64748b"),
    )
    footer_style = ParagraphStyle(
        "Footer",
        fontName="Helvetica-Oblique",
        fontSize=7.5,
        leading=10,
        alignment=TA_CENTER,
        textColor=HexColor("#94a3b8"),
    )

    # Calculate available space for the diagram
    available_width = w - 2 * margin
    available_height = h - 2 * margin - 70 * mm  # room for title, subtitle, footer

    # Scale diagram to fit
    from reportlab.lib.utils import ImageReader
    img_reader = ImageReader(diagram_path)
    iw, ih = img_reader.getSize()
    scale = min(available_width / iw, available_height / ih, 1.0)
    img_w = iw * scale
    img_h = ih * scale

    elements = []
    elements.append(Paragraph("Laws of Mystery", title_style))
    elements.append(Spacer(1, 2 * mm))
    elements.append(Paragraph("Aleksandr Bulkin — Logical Structure of the Book", subtitle_style))
    elements.append(Spacer(1, 1 * mm))

    # Thin decorative line
    line_data = [["" ]]
    line_table = Table(line_data, colWidths=[available_width * 0.4])
    line_table.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, HexColor("#c7d2fe")),
    ]))
    elements.append(line_table)
    elements.append(Spacer(1, 4 * mm))

    # Diagram image centered
    img = Image(diagram_path, width=img_w, height=img_h)
    img.hAlign = "CENTER"
    elements.append(img)

    elements.append(Spacer(1, 4 * mm))

    legend_style = ParagraphStyle(
        "Legend", fontName="Helvetica", fontSize=7, leading=9,
        alignment=TA_CENTER, textColor=HexColor("#64748b"),
    )
    elements.append(Paragraph(
        '<font color="#4a90d9">&#9632;</font> Axiom &nbsp;&nbsp; '
        '<font color="#7c3aed">&#9632;</font> Paradox &nbsp;&nbsp; '
        '<font color="#6366f1">&#9632;</font> Creation &nbsp;&nbsp; '
        '<font color="#e0e7ff">&#9632;</font> Mirror &nbsp;&nbsp; '
        '<font color="#f59e0b">&#9632;</font> Universal Love &nbsp;&nbsp; '
        '<font color="#10b981">&#9632;</font> The Dance &nbsp;&nbsp; '
        '--- = derivation &nbsp;&nbsp; '
        '-·- = cross-reference',
        legend_style,
    ))

    elements.append(Spacer(1, 3 * mm))
    elements.append(Paragraph(
        '"Dedicated to those whose mind can hold two or more incompatible notions without flinching."',
        footer_style,
    ))

    doc.build(elements)


async def main():
    out_dir = Path(__file__).parent
    png_path = str(out_dir / "laws_of_mystery_diagram.png")
    pdf_path = str(out_dir / "laws-of-mystery.pdf")

    print("Rendering mermaid diagram...")
    await render_mermaid_to_png(png_path)
    print(f"Diagram saved: {png_path}")

    print("Composing PDF...")
    build_pdf(png_path, pdf_path)
    print(f"PDF saved: {pdf_path}")


if __name__ == "__main__":
    asyncio.run(main())
