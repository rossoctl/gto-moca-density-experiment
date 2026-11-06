#!/usr/bin/env python3
"""Generate a PowerPoint slide with the density-vs-duty chart."""

from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN

BLACK = RGBColor(0x0B, 0x0B, 0x0B)
MUTED = RGBColor(0x89, 0x87, 0x81)


def main():
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank

    # title
    txBox = slide.shapes.add_textbox(Inches(0.5), Inches(0.25), Inches(12), Inches(0.6))
    tf = txBox.text_frame
    p = tf.paragraphs[0]
    run = p.add_run()
    run.text = "Max Sandbox Density vs Duty Cycle — c7g.16xlarge (64 vCPU, 128 GiB)"
    run.font.size = Pt(22)
    run.font.bold = True
    run.font.color.rgb = BLACK
    run.font.name = "Calibri"

    # chart image — centered
    img_w = Inches(10.5)
    img_h = Inches(10.5 * 675 / 1050)  # preserve aspect ratio
    img_left = (prs.slide_width - img_w) / 2
    img_top = Inches(1.0)
    slide.shapes.add_picture("density-vs-duty.png", int(img_left), int(img_top),
                             int(img_w), int(img_h))

    # footnote
    fn_top = int(img_top + img_h + Inches(0.15))
    txBox2 = slide.shapes.add_textbox(Inches(0.5), fn_top, Inches(12), Inches(0.8))
    tf2 = txBox2.text_frame
    tf2.word_wrap = True
    p2 = tf2.paragraphs[0]
    run2 = p2.add_run()
    run2.text = (
        "Warm: CPU = N × D × 2.112 vCPU;  RAM = N × 0.307 GiB (duty-invariant).  "
        "Moca: CPU = N × D × 4.65 vCPU;  RAM = N × 0.07 + N × D × 1.16 GiB.  "
        "Crossover at D ≈ 3.5 %: below this duty, Moca packs more sandboxes; above, warm wins.  "
        "Source: moca-side-by-side-report.md"
    )
    run2.font.size = Pt(10)
    run2.font.color.rgb = MUTED
    run2.font.name = "Calibri"

    out = "density-vs-duty-slide.pptx"
    prs.save(out)
    print(f"Saved {out}")


if __name__ == "__main__":
    main()
