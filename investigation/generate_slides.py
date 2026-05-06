"""Generate PPT slides for Qiskit transpiler optimization investigations.

Creates slides covering three completed investigations:
1. Optimization Loop — Opportunity-driven convergence (zero regression, fewer iterations)
2. Compute-Then-Apply — Parallel DAG access refactoring, 1.5-1.9x speedup (release build)
3. 3-Qubit Block Synthesis — Separability splitting, -5% CX gates

Usage:
    source ~/.venv/bin/activate
    python investigation/generate_slides.py
    # Output: investigation/qiskit_transpiler_optimizations.pptx
"""

from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE

# Colors
IBM_BLUE = RGBColor(0x05, 0x30, 0xAD)
DARK_GRAY = RGBColor(0x33, 0x33, 0x33)
MED_GRAY = RGBColor(0x66, 0x66, 0x66)
LIGHT_GRAY = RGBColor(0xE0, 0xE0, 0xE0)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
GREEN = RGBColor(0x1A, 0x80, 0x3E)
RED = RGBColor(0xCC, 0x33, 0x33)
ACCENT = RGBColor(0x00, 0x62, 0xFF)


def set_slide_bg(slide, color):
    bg = slide.background
    fill = bg.fill
    fill.solid()
    fill.fore_color.rgb = color


def add_textbox(slide, left, top, width, height, text, font_size=14,
                bold=False, color=DARK_GRAY, alignment=PP_ALIGN.LEFT,
                font_name="Calibri"):
    txBox = slide.shapes.add_textbox(left, top, width, height)
    tf = txBox.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = text
    p.font.size = Pt(font_size)
    p.font.bold = bold
    p.font.color.rgb = color
    p.font.name = font_name
    p.alignment = alignment
    return tf


def add_bullet_frame(slide, left, top, width, height, bullets, font_size=13,
                     color=DARK_GRAY, spacing=Pt(4)):
    txBox = slide.shapes.add_textbox(left, top, width, height)
    tf = txBox.text_frame
    tf.word_wrap = True
    for i, (text, bold, indent) in enumerate(bullets):
        if i == 0:
            p = tf.paragraphs[0]
        else:
            p = tf.add_paragraph()
        p.text = text
        p.font.size = Pt(font_size)
        p.font.bold = bold
        p.font.color.rgb = color
        p.font.name = "Calibri"
        p.level = indent
        p.space_after = spacing
    return tf


def add_table(slide, left, top, width, height, rows, col_widths=None):
    table_shape = slide.shapes.add_table(len(rows), len(rows[0]), left, top, width, height)
    table = table_shape.table

    if col_widths:
        for i, w in enumerate(col_widths):
            table.columns[i].width = w

    for r, row_data in enumerate(rows):
        for c, cell_text in enumerate(row_data):
            cell = table.cell(r, c)
            cell.text = str(cell_text)
            for paragraph in cell.text_frame.paragraphs:
                paragraph.font.size = Pt(11)
                paragraph.font.name = "Calibri"
                if r == 0:
                    paragraph.font.bold = True
                    paragraph.font.color.rgb = WHITE
                else:
                    paragraph.font.color.rgb = DARK_GRAY
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE

            if r == 0:
                cell.fill.solid()
                cell.fill.fore_color.rgb = IBM_BLUE
            elif r % 2 == 0:
                cell.fill.solid()
                cell.fill.fore_color.rgb = RGBColor(0xF5, 0xF5, 0xF5)

    return table


def add_code_box(slide, left, top, width, height, code, label=None):
    # Background box
    shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, left, top, width, height)
    shape.fill.solid()
    shape.fill.fore_color.rgb = RGBColor(0xF8, 0xF8, 0xF8)
    shape.line.color.rgb = LIGHT_GRAY
    shape.line.width = Pt(1)

    # Label
    if label:
        add_textbox(slide, left + Inches(0.1), top + Inches(0.05),
                    width - Inches(0.2), Inches(0.25),
                    label, font_size=9, bold=True, color=MED_GRAY)
        code_top = top + Inches(0.28)
    else:
        code_top = top + Inches(0.05)

    # Code text
    txBox = slide.shapes.add_textbox(left + Inches(0.15), code_top,
                                      width - Inches(0.3), height - Inches(0.35))
    tf = txBox.text_frame
    tf.word_wrap = True
    for i, line in enumerate(code.strip().split("\n")):
        if i == 0:
            p = tf.paragraphs[0]
        else:
            p = tf.add_paragraph()
        p.text = line
        p.font.size = Pt(9)
        p.font.name = "Courier New"
        p.font.color.rgb = DARK_GRAY
        p.space_after = Pt(1)


def make_title_slide(prs):
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank
    set_slide_bg(slide, WHITE)

    add_textbox(slide, Inches(0.8), Inches(1.5), Inches(8.4), Inches(1.0),
                "Qiskit Transpiler Optimization Passes",
                font_size=32, bold=True, color=IBM_BLUE, alignment=PP_ALIGN.LEFT)

    add_textbox(slide, Inches(0.8), Inches(2.5), Inches(8.4), Inches(0.5),
                "Three Investigations into Level 2 Pipeline Efficiency",
                font_size=18, color=MED_GRAY)

    bullets = [
        ("1.  Optimization Loop: opportunity-driven convergence for L2 (zero regression)", False, 0),
        ("2.  Compute-Then-Apply: parallel DAG access refactoring (1.5-1.9x speedup)", False, 0),
        ("3.  3-Qubit Block Synthesis: separability splitting (-5% CX gates)", False, 0),
    ]
    add_bullet_frame(slide, Inches(0.8), Inches(3.5), Inches(8.4), Inches(2.0),
                     bullets, font_size=14, color=DARK_GRAY)

    add_textbox(slide, Inches(0.8), Inches(6.2), Inches(8.4), Inches(0.4),
                "Sophia Wen  |  IBM Quantum  |  Benchpress + Qiskit fork",
                font_size=12, color=MED_GRAY)


# ── Investigation 1: Optimization Loop ──

def make_opt_loop_slide1(prs):
    """Slide: Problem + Solution"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_slide_bg(slide, WHITE)

    add_textbox(slide, Inches(0.5), Inches(0.3), Inches(9.0), Inches(0.5),
                "1. Optimization Loop: Opportunity-Driven Convergence for Level 2",
                font_size=22, bold=True, color=IBM_BLUE)

    # Problem
    add_textbox(slide, Inches(0.5), Inches(1.0), Inches(4.2), Inches(0.3),
                "Problem", font_size=16, bold=True, color=DARK_GRAY)

    problem_bullets = [
        ("Level 2 uses FixedPoint(size) AND FixedPoint(depth)", False, 0),
        ("Always requires a redundant \"confirmation\" iteration", False, 0),
        ("that re-runs all passes just to verify nothing changed", False, 0),
        ("", False, 0),
        ("Challenge: Optimize1qGatesDecomposition mutates DAG", False, 0),
        ("unconditionally (replaces 1Q runs even when result is", False, 0),
        ("identical). A naive \"changed?\" flag never converges.", False, 0),
    ]
    add_bullet_frame(slide, Inches(0.5), Inches(1.35), Inches(4.2), Inches(2.2),
                     problem_bullets, font_size=12)

    # Solution
    add_textbox(slide, Inches(5.2), Inches(1.0), Inches(4.5), Inches(0.3),
                "Solution: Track Opportunity Producers", font_size=16, bold=True, color=GREEN)

    solution_bullets = [
        ("Initial attempt: track only 2Q changes", True, 0),
        ("  \u2192 0.18% 1Q regression on QFT (69 gates)", False, 0),
        ("  \u2192 rotation merges missed across iterations", False, 0),
        ("", False, 0),
        ("Fix: also track rotation consolidation", True, 0),
        ("  _opt_pass_changed: 2Q gate removed", False, 0),
        ("  _opt_1q_consolidated: rotations merged", False, 0),
        ("  \u2192 zero regression (A/B verified)", False, 0),
    ]
    add_bullet_frame(slide, Inches(5.2), Inches(1.35), Inches(4.5), Inches(2.5),
                     solution_bullets, font_size=12)

    # Results table
    add_textbox(slide, Inches(0.5), Inches(3.7), Inches(9.0), Inches(0.3),
                "Results: Zero Regression vs FixedPoint (A/B tested)",
                font_size=14, bold=True, color=DARK_GRAY)

    rows = [
        ["Circuit", "FixedPoint Iters", "Changed-Flag Iters", "Total Gates", "Depth", "Regressed?"],
        ["QFT_100", "3", "2", "37,687", "5,404", "No (0 delta)"],
        ["QV_100", "2", "1", "96,474", "—", "No"],
        ["EfficientSU2_100", "2", "1", "1,494", "330", "No (0 delta)"],
        ["QAOA_100", "3", "1", "2,137", "323", "No (0 delta)"],
        ["BV_100", "2", "1", "196", "—", "No"],
        ["Heisenberg_100", "2", "1", "891", "—", "No"],
    ]
    add_table(slide, Inches(0.5), Inches(4.05), Inches(9.0), Inches(2.3), rows,
              col_widths=[Inches(1.8), Inches(1.3), Inches(1.5), Inches(1.3), Inches(1.0), Inches(1.6)])

    # Bottom summary
    add_textbox(slide, Inches(0.5), Inches(6.5), Inches(9.0), Inches(0.5),
                "Tracking rotation consolidation captures the CommutativeCancellation \u2192 Optimize1qGatesDecomposition "
                "cross-iteration benefit. Without it, skipping the extra iteration loses 1Q optimization on rotation-heavy "
                "circuits. With it: zero regression, fewer iterations, no analysis passes needed.",
                font_size=11, color=MED_GRAY)


# ── Investigation 2: Compute-Then-Apply ──

def make_cta_slide1(prs):
    """Slide: Problem + Approach with side-by-side code"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_slide_bg(slide, WHITE)

    add_textbox(slide, Inches(0.5), Inches(0.3), Inches(9.0), Inches(0.5),
                "2. Compute-Then-Apply: Batching DAG Access for Cache Efficiency",
                font_size=22, bold=True, color=IBM_BLUE)

    add_textbox(slide, Inches(0.5), Inches(0.85), Inches(9.0), Inches(0.4),
                "Three most expensive optimization passes: ConsolidateBlocks (51-75%), "
                "Optimize1qGatesDecomposition (6-15%), CommutativeCancellation (6-15%)",
                font_size=12, color=MED_GRAY)

    # Before code
    before_code = """\
for item in items:
    result = read_dag(item)   # READ
    compute(result)           # COMPUTE
    mutate_dag(result)        # WRITE
    # next read hits cold cache
    # (edges/nodes just modified)"""
    add_code_box(slide, Inches(0.4), Inches(1.5), Inches(4.3), Inches(2.2),
                 before_code, label="BEFORE: Interleaved read-compute-write")

    # After code
    after_code = """\
# Phase 1: compute (DAG stable, cache warm)
results = []
for item in items:
    results.push(read_dag(item) + compute)

# Phase 2: apply (batch all mutations)
for result in results:
    mutate_dag(result)"""
    add_code_box(slide, Inches(5.1), Inches(1.5), Inches(4.5), Inches(2.2),
                 after_code, label="AFTER: Batched compute-then-apply")

    # Key insight box
    shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                                    Inches(0.4), Inches(3.95), Inches(9.2), Inches(0.7))
    shape.fill.solid()
    shape.fill.fore_color.rgb = RGBColor(0xE8, 0xF0, 0xFE)
    shape.line.color.rgb = ACCENT
    shape.line.width = Pt(1.5)
    tf = shape.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = ("Key insight: Separating compute from mutation enables both cache-friendly access AND rayon parallelism. "
              "Serial mode shows 1.85x from batching alone (cache-warm reads). "
              "Rayon adds further speedup on release builds (4.7-6.5x within these passes). "
              "Combined: 1.5-1.6x end-to-end on QFT circuits.")
    p.font.size = Pt(11)
    p.font.name = "Calibri"
    p.font.color.rgb = DARK_GRAY

    # Passes modified
    add_textbox(slide, Inches(0.5), Inches(4.9), Inches(9.0), Inches(0.3),
                "Passes Refactored (all Rust, ~470 lines changed):",
                font_size=13, bold=True, color=DARK_GRAY)

    pass_bullets = [
        ("Optimize1qGatesDecomposition: extract process_run(), pre-compute basis data, parallel threshold 500 runs", False, 0),
        ("CommutationAnalysis: extract per-wire function, &mut self -> &self on CommutationChecker, parallel threshold 100 qubits", False, 0),
        ("ConsolidateBlocks: TwoQBlockAction enum, 3-phase (classify/compute/apply), parallel threshold 200 blocks", False, 0),
    ]
    add_bullet_frame(slide, Inches(0.5), Inches(5.25), Inches(9.0), Inches(1.5),
                     pass_bullets, font_size=11, color=MED_GRAY, spacing=Pt(2))

    add_textbox(slide, Inches(0.5), Inches(6.5), Inches(9.0), Inches(0.3),
                "Branch: parallel-optimization-passes  |  Base: 03c640f73  |  Full diff: 03c640f73...ea4abc77c",
                font_size=10, color=MED_GRAY)


def make_cta_slide2(prs):
    """Slide: Benchmark results (release build)"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_slide_bg(slide, WHITE)

    add_textbox(slide, Inches(0.5), Inches(0.3), Inches(9.0), Inches(0.5),
                "2. Compute-Then-Apply: Benchmark Results (Release Build)",
                font_size=22, bold=True, color=IBM_BLUE)

    add_textbox(slide, Inches(0.5), Inches(0.85), Inches(9.0), Inches(0.3),
                "FakeTorino (133Q heavy-hex), optimization level 2, QISKIT_BUILD_PROFILE=release, 5 runs mean",
                font_size=12, color=MED_GRAY)

    # Default mode (rayon ON)
    add_textbox(slide, Inches(0.3), Inches(1.3), Inches(4.5), Inches(0.3),
                "Default Mode (Rayon ON) -- Remote Server",
                font_size=12, bold=True, color=DARK_GRAY)

    rows_default = [
        ["Circuit", "Main (s)", "Ours (s)", "Speedup"],
        ["QFT-50", "0.196", "0.129", "1.52x"],
        ["QFT-100", "0.456", "0.288", "1.58x"],
        ["ESU2-50", "0.012", "0.013", "~same"],
        ["ESU2-100", "0.019", "0.018", "~same"],
        ["QV-50", "0.060", "0.054", "1.11x"],
        ["QV-100", "0.164", "0.154", "1.06x"],
    ]
    t1 = add_table(slide, Inches(0.3), Inches(1.65), Inches(4.5), Inches(2.4), rows_default,
              col_widths=[Inches(1.2), Inches(1.0), Inches(1.0), Inches(1.0)])
    # Bold the best speedups
    for r in [1, 2]:
        cell = t1.cell(r, 3)
        for p in cell.text_frame.paragraphs:
            p.font.bold = True
            p.font.color.rgb = GREEN

    # Serial mode (rayon OFF)
    add_textbox(slide, Inches(5.2), Inches(1.3), Inches(4.5), Inches(0.3),
                "Serial Mode (Rayon OFF) -- Remote Server",
                font_size=12, bold=True, color=DARK_GRAY)

    rows_serial = [
        ["Circuit", "Main (s)", "Ours (s)", "Speedup"],
        ["QFT-50", "1.128", "0.609", "1.85x"],
        ["QFT-100", "2.648", "1.407", "1.88x"],
        ["ESU2-50", "0.012", "0.012", "~same"],
        ["ESU2-100", "0.019", "0.019", "~same"],
        ["QV-50", "0.305", "0.264", "1.16x"],
        ["QV-100", "1.040", "0.999", "1.04x"],
    ]
    t2 = add_table(slide, Inches(5.2), Inches(1.65), Inches(4.5), Inches(2.4), rows_serial,
              col_widths=[Inches(1.2), Inches(1.0), Inches(1.0), Inches(1.0)])
    for r in [1, 2]:
        cell = t2.cell(r, 3)
        for p in cell.text_frame.paragraphs:
            p.font.bold = True
            p.font.color.rgb = GREEN

    # Rayon speedup comparison
    add_textbox(slide, Inches(0.3), Inches(4.3), Inches(9.4), Inches(0.3),
                "Rayon ON vs OFF -- Isolating Threading Contribution:",
                font_size=12, bold=True, color=DARK_GRAY)

    rows_rayon = [
        ["Circuit", "Main: ON", "Main: OFF", "Main Rayon", "Ours: ON", "Ours: OFF", "Ours Rayon"],
        ["QFT-50", "0.196", "1.128", "5.8x", "0.129", "0.609", "4.7x"],
        ["QFT-100", "0.456", "2.648", "5.8x", "0.288", "1.407", "4.9x"],
        ["QV-100", "0.164", "1.040", "6.3x", "0.154", "0.999", "6.5x"],
    ]
    add_table(slide, Inches(0.3), Inches(4.65), Inches(9.4), Inches(1.2), rows_rayon,
              col_widths=[Inches(1.2), Inches(1.1), Inches(1.1), Inches(1.2), Inches(1.1), Inches(1.1), Inches(1.2)])

    # Takeaway
    shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                                    Inches(0.3), Inches(6.1), Inches(9.4), Inches(0.7))
    shape.fill.solid()
    shape.fill.fore_color.rgb = RGBColor(0xE8, 0xF0, 0xFE)
    shape.line.color.rgb = ACCENT
    shape.line.width = Pt(1.5)
    tf = shape.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = ("Takeaway: 1.5-1.6x end-to-end speedup on QFT circuits (rayon ON). "
              "In serial mode, restructuring alone gives 1.85-1.88x. "
              "Main already has rayon in SABRE (5-6x); our branch adds rayon to 3 optimization passes. "
              "Small circuits show no overhead. Gate counts identical.")
    p.font.size = Pt(11)
    p.font.name = "Calibri"
    p.font.color.rgb = DARK_GRAY


def make_cta_slide3(prs):
    """Slide: Related upstream PRs comparison"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_slide_bg(slide, WHITE)

    add_textbox(slide, Inches(0.5), Inches(0.3), Inches(9.0), Inches(0.5),
                "2. Compute-Then-Apply: Related Upstream PRs",
                font_size=22, bold=True, color=IBM_BLUE)

    add_textbox(slide, Inches(0.5), Inches(0.85), Inches(9.0), Inches(0.3),
                "Three open Qiskit PRs pursue the same goal. All unmerged and on hold (May 2026).",
                font_size=12, color=MED_GRAY)

    # Comparison table
    rows = [
        ["PR", "Pass", "Their Approach", "Blocker", "Our Advantage"],
        ["#16014", "CommutationAnalysis",
         "fold+reduce with rayon",
         "Non-deterministic order",
         "Deterministic merge by qubit index"],
        ["#15567", "Optimize1qGatesDecomp",
         "OnceLock lazy init, 100K threshold",
         "Test failures, never triggers",
         "Simple precompute, threshold=500"],
        ["#13419", "ConsolidateBlocks (rewrite)",
         "Unified pass (86 commits)",
         "Large scope",
         "Minimal focused change, composable"],
    ]
    add_table(slide, Inches(0.3), Inches(1.3), Inches(9.4), Inches(2.0), rows,
              col_widths=[Inches(0.8), Inches(2.0), Inches(2.3), Inches(2.0), Inches(2.3)])

    # Key differences
    add_textbox(slide, Inches(0.5), Inches(3.6), Inches(9.0), Inches(0.3),
                "Key Implementation Differences", font_size=14, bold=True, color=DARK_GRAY)

    diff_bullets = [
        ("#16014: Their fold+reduce merges IndexMaps from threads \u2192 non-deterministic iteration order (flagged by reviewer).", False, 0),
        ("    Ours: collect per-wire results into Vec ordered by qubit index, merge sequentially. Deterministic, zero overhead.", False, 0),
        ("", False, 0),
        ("#15567: OnceLock defers basis computation per-qubit with thread-safe lazy init \u2014 complex, threshold too high to trigger.", False, 0),
        ("    Ours: precompute_basis_data() upfront in one pass, then stateless process_run(). No sync overhead, practical threshold.", False, 0),
        ("", False, 0),
        ("#13419: Full rewrite unifying 3 passes. Our compute-then-apply pattern is orthogonal and can be applied to their new pass.", False, 0),
    ]
    add_bullet_frame(slide, Inches(0.5), Inches(3.9), Inches(9.0), Inches(2.5),
                     diff_bullets, font_size=11, color=MED_GRAY, spacing=Pt(2))

    # Recommendation
    shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                                    Inches(0.3), Inches(6.2), Inches(9.4), Inches(0.6))
    shape.fill.solid()
    shape.fill.fore_color.rgb = RGBColor(0xE8, 0xF0, 0xFE)
    shape.line.color.rgb = ACCENT
    shape.line.width = Pt(1.5)
    tf = shape.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = ("Recommendation: Contribute via code review on the open PRs. "
              "Our determinism fix directly solves #16014's blocker. "
              "Our precompute pattern simplifies #15567. "
              "Benchmark data validates practical thresholds for all three.")
    p.font.size = Pt(11)
    p.font.name = "Calibri"
    p.font.color.rgb = DARK_GRAY


# ── Investigation 3: 3-Qubit Block Synthesis ──

def make_3q_slide1(prs):
    """Slide: Approach + Results"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_slide_bg(slide, WHITE)

    add_textbox(slide, Inches(0.5), Inches(0.3), Inches(9.0), Inches(0.5),
                "3. 3-Qubit Block Synthesis: Separability Splitting for -5% CX Gates",
                font_size=22, bold=True, color=IBM_BLUE)

    # Problem + Approach (left)
    add_textbox(slide, Inches(0.5), Inches(1.0), Inches(4.3), Inches(0.3),
                "Problem & Approach", font_size=16, bold=True, color=DARK_GRAY)

    approach_bullets = [
        ("Qiskit has excellent 2Q synthesis (KAK/Weyl: 0-3 CX)", False, 0),
        ("but no 3Q block optimization", False, 0),
        ("Many 3Q blocks contain \"bystander\" qubits that don't", False, 0),
        ("interact -- block is really 2Q + 1Q (tensor product)", False, 0),
        ("", False, 0),
        ("Sequential strategy:", True, 0),
        ("1. Run standard 2Q optimization (existing Level 2)", False, 0),
        ("2. Detect separable 3Q blocks (identity coefficient test)", False, 0),
        ("3. Split into 2Q + 1Q, route 2Q through KAK", False, 0),
        ("4. Guard QSD with >14 CX threshold for remainder", False, 0),
    ]
    add_bullet_frame(slide, Inches(0.5), Inches(1.35), Inches(4.3), Inches(3.0),
                     approach_bullets, font_size=12, spacing=Pt(2))

    # Key finding (right)
    add_textbox(slide, Inches(5.2), Inches(1.0), Inches(4.5), Inches(0.3),
                "Key Finding", font_size=16, bold=True, color=GREEN)

    finding_bullets = [
        ("Separability accounts for 90% of all gains", True, 0),
        ("(8,966 of 9,964 CX gates saved)", False, 0),
        ("", False, 0),
        ("After 2Q KAK optimization, separability becomes", False, 0),
        ("more prevalent: 1,498 blocks vs ~500 before", False, 0),
        ("(KAK consolidation creates tensor-product structures)", False, 0),
        ("", False, 0),
        ("QSD contributes only 10% of savings, and only on", False, 0),
        ("circuits with dense 3Q blocks (e.g., multiplier)", False, 0),
    ]
    add_bullet_frame(slide, Inches(5.2), Inches(1.35), Inches(4.5), Inches(2.8),
                     finding_bullets, font_size=12, spacing=Pt(2))

    # Results table
    add_textbox(slide, Inches(0.5), Inches(4.3), Inches(9.0), Inches(0.3),
                "Results: 8 Benchmark Circuits on FakeTorino, Level 2",
                font_size=14, bold=True, color=DARK_GRAY)

    rows = [
        ["Circuit", "Baseline CX", "After 3Q Opt", "CX Reduction", "Separable Blocks"],
        ["QFT_100", "9,528", "9,279", "-2.6%", "312"],
        ["QV_100", "96,474", "91,932", "-4.7%", "408"],
        ["QAOA_100", "186", "176", "-5.7%", "52"],
        ["Random_100", "12,845", "12,137", "-5.5%", "326"],
        ["Multiplier_10", "4,120", "3,861", "-6.3%", "181 (QSD)"],
    ]
    t = add_table(slide, Inches(0.5), Inches(4.65), Inches(9.0), Inches(2.0), rows,
              col_widths=[Inches(1.8), Inches(1.5), Inches(1.7), Inches(1.5), Inches(2.0)])
    # Highlight reduction column
    for r in range(1, len(rows)):
        cell = t.cell(r, 3)
        for p in cell.text_frame.paragraphs:
            p.font.bold = True
            p.font.color.rgb = GREEN

    add_textbox(slide, Inches(0.5), Inches(6.5), Inches(9.0), Inches(0.3),
                "Recommendation: Implement separability splitting as a Python pass (low effort, high impact). "
                "Guard QSD with >14 CX threshold for targeted gains on dense circuits.",
                font_size=11, color=MED_GRAY)


# ── Summary Slide ──

def make_summary_slide(prs):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_slide_bg(slide, WHITE)

    add_textbox(slide, Inches(0.5), Inches(0.3), Inches(9.0), Inches(0.5),
                "Summary & Next Steps",
                font_size=26, bold=True, color=IBM_BLUE)

    # Summary table
    rows = [
        ["Investigation", "Status", "Impact", "Effort"],
        ["Optimization Loop", "Implemented + verified", "Skip redundant iterations, 0 regression", "Low (flag change)"],
        ["Compute-Then-Apply", "Implemented (fork)", "1.5-1.9x speedup (release build)", "Medium (Rust refactor)"],
        ["3Q Block Synthesis", "Prototype done", "-5% CX gates across benchmarks", "Low (Python pass)"],
    ]
    add_table(slide, Inches(0.3), Inches(1.1), Inches(9.4), Inches(1.5), rows,
              col_widths=[Inches(2.3), Inches(1.8), Inches(3.5), Inches(1.8)])

    add_textbox(slide, Inches(0.5), Inches(3.0), Inches(9.0), Inches(0.3),
                "Key Insights", font_size=18, bold=True, color=DARK_GRAY)

    insight_bullets = [
        ("All three optimizations are orthogonal -- they compose without interference", False, 0),
        ("Optimization loop: track opportunity-producing passes, not consumers (solves idempotency problem)", False, 0),
        ("Compute-then-apply: separating reads from writes enables both cache efficiency AND rayon parallelism", False, 0),
        ("3Q synthesis gains are dominated by separability (90%), not decomposition algorithms", False, 0),
    ]
    add_bullet_frame(slide, Inches(0.5), Inches(3.4), Inches(9.0), Inches(1.8),
                     insight_bullets, font_size=13, spacing=Pt(6))

    add_textbox(slide, Inches(0.5), Inches(5.0), Inches(9.0), Inches(0.3),
                "Next Steps", font_size=18, bold=True, color=DARK_GRAY)

    next_bullets = [
        ("Seek feedback from Qiskit transpiler team on all three proposals", False, 0),
        ("Optimization loop: upstream PR with opportunity-driven flag approach", False, 0),
        ("Compute-then-apply: submit PR to Qiskit (branch: parallel-optimization-passes)", False, 0),
        ("3Q synthesis: implement separability pass, integrate into Level 2 pipeline", False, 0),
    ]
    add_bullet_frame(slide, Inches(0.5), Inches(5.4), Inches(9.0), Inches(1.5),
                     next_bullets, font_size=13, spacing=Pt(6))

    add_textbox(slide, Inches(0.5), Inches(6.7), Inches(9.0), Inches(0.3),
                "All code, benchmarks, and investigation docs: github.com/hfwen0502/qiskit",
                font_size=11, color=MED_GRAY)


def main():
    prs = Presentation()
    prs.slide_width = Inches(10)
    prs.slide_height = Inches(7.5)

    make_title_slide(prs)
    make_opt_loop_slide1(prs)       # Investigation 1: 1 slide
    make_cta_slide1(prs)            # Investigation 2: slide 1 (approach + code)
    make_cta_slide2(prs)            # Investigation 2: slide 2 (benchmarks)
    make_cta_slide3(prs)            # Investigation 2: slide 3 (upstream PRs)
    make_3q_slide1(prs)             # Investigation 3: 1 slide
    make_summary_slide(prs)         # Summary: 1 slide

    output = "investigation/qiskit_transpiler_optimizations.pptx"
    prs.save(output)
    print(f"Saved: {output}")
    print(f"Total slides: {len(prs.slides)}")


if __name__ == "__main__":
    main()
