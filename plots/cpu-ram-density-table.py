#!/usr/bin/env python3
"""Generate a PowerPoint slide with the max-density table."""

from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.oxml.ns import qn
from lxml import etree

# ── model parameters ──
WARM_CPU = 2.112;  WARM_MEM_BASE = 0.307; WARM_MEM_DUTY = 0
STUT_CPU = 4.65;   STUT_MEM_BASE = 0.07;  STUT_MEM_DUTY = 1.16

AWS = [
    ("c7g.medium",    1,    2),
    ("c7g.large",     2,    4),
    ("c7g.xlarge",    4,    8),
    ("c7g.2xlarge",   8,   16),
    ("c7g.4xlarge",  16,   32),
    ("c7g.8xlarge",  32,   64),
    ("c7g.12xlarge", 48,   96),
    ("c7g.16xlarge", 64,  128),
]

DUTIES = [0.01, 0.03, 0.10]

# colors
HDR_BG    = RGBColor(0x2C, 0x2C, 0x2C)
WARM_HDR  = RGBColor(0xE6, 0x39, 0x46)
STUT_HDR  = RGBColor(0x2A, 0x78, 0xD6)
GREEN     = RGBColor(0x1B, 0xAF, 0x7A)
GREEN_BG  = RGBColor(0xE6, 0xF7, 0xF0)
WHITE     = RGBColor(0xFF, 0xFF, 0xFF)
BLACK     = RGBColor(0x0B, 0x0B, 0x0B)
GRAY      = RGBColor(0xF5, 0xF5, 0xF3)


def max_n(cpu_rate, mem_base, mem_duty, vcpu, gib, d):
    n_cpu = vcpu / (d * cpu_rate) if d > 0 else 999999
    n_ram = gib / (mem_base + d * mem_duty) if (mem_base + d * mem_duty) > 0 else 999999
    n = min(n_cpu, n_ram)
    limit = "CPU" if n_cpu < n_ram else "RAM"
    return int(n), limit


def set_cell(cell, text, font_size=10, bold=False, align=PP_ALIGN.CENTER,
             fg=BLACK, bg=None):
    cell.text = ""
    p = cell.text_frame.paragraphs[0]
    p.alignment = align
    run = p.add_run()
    run.text = str(text)
    run.font.size = Pt(font_size)
    run.font.bold = bold
    run.font.color.rgb = fg
    run.font.name = "Calibri"
    cell.vertical_anchor = MSO_ANCHOR.MIDDLE
    if bg:
        cell.fill.solid()
        cell.fill.fore_color.rgb = bg
    cell.margin_left = Emu(45720)
    cell.margin_right = Emu(45720)
    cell.margin_top = Emu(18288)
    cell.margin_bottom = Emu(18288)


def set_cell_border(cell, color_hex, width_pt=2.0):
    """Set all four borders of a cell to a solid line."""
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    w = int(width_pt * 12700)  # EMU
    for edge in ("lnL", "lnR", "lnT", "lnB"):
        ln = etree.SubElement(tcPr, qn(f"a:{edge}"))
        ln.set("w", str(w))
        ln.set("cap", "flat")
        ln.set("cmpd", "sng")
        solidFill = etree.SubElement(ln, qn("a:solidFill"))
        srgb = etree.SubElement(solidFill, qn("a:srgbClr"))
        srgb.set("val", color_hex)


def main():
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank

    # title
    txBox = slide.shapes.add_textbox(Inches(0.5), Inches(0.3), Inches(12), Inches(0.6))
    tf = txBox.text_frame
    p = tf.paragraphs[0]
    run = p.add_run()
    run.text = "Firecracker Sandbox Density — Max Concurrency by Instance Type"
    run.font.size = Pt(20)
    run.font.bold = True
    run.font.color.rgb = BLACK
    run.font.name = "Calibri"

    # subtitle
    txBox2 = slide.shapes.add_textbox(Inches(0.5), Inches(0.85), Inches(12), Inches(0.4))
    tf2 = txBox2.text_frame
    p2 = tf2.paragraphs[0]
    r1 = p2.add_run()
    r1.text = "Green outline: per-exec wins (higher concurrency than warm at the same duty cycle)"
    r1.font.size = Pt(11)
    r1.font.color.rgb = GREEN
    r1.font.bold = True
    r1.font.name = "Calibri"

    # table: 3 header rows + data rows
    n_rows = 3 + len(AWS)
    n_cols = 3 + 3 + 3
    left = Inches(0.5)
    top = Inches(1.4)
    width = Inches(12.3)
    height = Inches(0.38 * n_rows)

    table_shape = slide.shapes.add_table(n_rows, n_cols, left, top, width, height)
    table = table_shape.table

    col_widths = [Inches(1.6), Inches(0.7), Inches(0.7)] + [Inches(1.35)] * 6
    for i, w in enumerate(col_widths):
        table.columns[i].width = w

    # ── Row 0: merged group headers ──
    set_cell(table.cell(0, 0), "", bg=HDR_BG)
    set_cell(table.cell(0, 1), "", bg=HDR_BG)
    set_cell(table.cell(0, 2), "", bg=HDR_BG)
    table.cell(0, 0).merge(table.cell(0, 2))
    set_cell(table.cell(0, 0), "Instance", font_size=11, bold=True, fg=WHITE, bg=HDR_BG)

    set_cell(table.cell(0, 3), "", bg=WARM_HDR)
    set_cell(table.cell(0, 4), "", bg=WARM_HDR)
    set_cell(table.cell(0, 5), "", bg=WARM_HDR)
    table.cell(0, 3).merge(table.cell(0, 5))
    set_cell(table.cell(0, 3), "Warm (resident) fc", font_size=11, bold=True,
             fg=WHITE, bg=WARM_HDR)

    set_cell(table.cell(0, 6), "", bg=STUT_HDR)
    set_cell(table.cell(0, 7), "", bg=STUT_HDR)
    set_cell(table.cell(0, 8), "", bg=STUT_HDR)
    table.cell(0, 6).merge(table.cell(0, 8))
    set_cell(table.cell(0, 6), "Per-exec (start/stop) fc", font_size=11, bold=True,
             fg=WHITE, bg=STUT_HDR)

    # ── Row 1: sub-headers ──
    sub_hdrs = ["", "vCPU", "GiB", "D=1%", "D=3%", "D=10%", "D=1%", "D=3%", "D=10%"]
    for c, txt in enumerate(sub_hdrs):
        set_cell(table.cell(1, c), txt, font_size=10, bold=True, fg=WHITE, bg=HDR_BG)

    # ── Row 2: units row ──
    units = ["", "", "", "max N", "max N", "max N", "max N", "max N", "max N"]
    for c, txt in enumerate(units):
        set_cell(table.cell(2, c), txt, font_size=9, bold=False,
                 fg=RGBColor(0x99, 0x99, 0x99), bg=RGBColor(0x3C, 0x3C, 0x3C))

    # ── Data rows ──
    models = [
        (WARM_CPU, WARM_MEM_BASE, WARM_MEM_DUTY),
        (STUT_CPU, STUT_MEM_BASE, STUT_MEM_DUTY),
    ]

    for ri, (name, vcpu, gib) in enumerate(AWS):
        row = ri + 3
        stripe = GRAY if ri % 2 == 0 else WHITE

        set_cell(table.cell(row, 0), name, font_size=10, bold=True,
                 align=PP_ALIGN.LEFT, fg=BLACK, bg=stripe)
        set_cell(table.cell(row, 1), str(vcpu), font_size=10, fg=BLACK, bg=stripe)
        set_cell(table.cell(row, 2), str(gib), font_size=10, fg=BLACK, bg=stripe)

        # compute all values first so we can compare warm vs per-exec
        warm_vals = []
        stut_vals = []
        for di, d in enumerate(DUTIES):
            wn, wlim = max_n(*models[0], vcpu, gib, d)
            warm_vals.append((wn, wlim))
            sn, slim = max_n(*models[1], vcpu, gib, d)
            stut_vals.append((sn, slim))

        # warm columns
        for di, (n, lim) in enumerate(warm_vals):
            col = 3 + di
            text = f"{n} ({lim})"
            set_cell(table.cell(row, col), text, font_size=10, bold=True,
                     fg=BLACK, bg=stripe)

        # per-exec columns
        for di, (n, lim) in enumerate(stut_vals):
            col = 6 + di
            text = f"{n} ({lim})"
            wins = n > warm_vals[di][0]
            bg = GREEN_BG if wins else stripe
            set_cell(table.cell(row, col), text, font_size=10, bold=True,
                     fg=BLACK, bg=bg)
            if wins:
                set_cell_border(table.cell(row, col), "1BAF7A", width_pt=2.0)

    # footnote
    txBox3 = slide.shapes.add_textbox(Inches(0.5), top + height + Inches(0.2),
                                       Inches(12), Inches(0.8))
    tf3 = txBox3.text_frame
    tf3.word_wrap = True
    p3 = tf3.paragraphs[0]
    run3 = p3.add_run()
    run3.text = (
        "Warm: CPU = N × D × 2.112 vCPU;  RAM = N × 0.307 GiB (duty-invariant).  "
        "Per-exec: CPU = N × D × 4.65 vCPU;  RAM = N × 0.07 + N × D × 1.16 GiB.  "
        "AWS c7g (compute-optimized): 1 vCPU : 2 GiB.  "
        "Source: moca-side-by-side-report.md"
    )
    run3.font.size = Pt(9)
    run3.font.color.rgb = RGBColor(0x89, 0x87, 0x81)
    run3.font.name = "Calibri"

    out = "cpu-ram-density-table.pptx"
    prs.save(out)
    print(f"Saved {out}")


if __name__ == "__main__":
    main()
