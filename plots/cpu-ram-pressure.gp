# CPU vs RAM — warm and per-exec firecracker pressure map
#
# Red wedge:  warm (resident) fc.  Blue wedge: per-exec (start/stop) fc.
# Green zone: per-exec only — warm fc cannot reach (N=100..200).
# Iso-pressure lines (constant N, every 5).  Iso-duty lines (constant D, every 3%).
#
# Model parameters (from moca-side-by-side-report.md):
#   Warm:     CPU = N × D × 2.112 vCPU;  RAM = N × 0.307 GiB
#   Per-exec: CPU = N × D × 4.65  vCPU;  RAM = N × 0.07 + N × D × 1.16 GiB
#
# Data: cpu-ram-pressure.dat (generated)
#
# Usage:
#   gnuplot cpu-ram-pressure.gp                       # → .png
#   gnuplot -e "TERM='pdf'" cpu-ram-pressure.gp       # → .pdf

if (!exists("TERM")) TERM = "png"

if (TERM eq "pdf") {
    set terminal pdfcairo enhanced font "sans,11" size 7in,4.5in
    set output "cpu-ram-pressure.pdf"
} else {
    set terminal pngcairo enhanced font "sans,11" size 1050,675
    set output "cpu-ram-pressure.png"
}

# --- palette ---
warm     = "#e63946"
warm_lt  = "#f0a0a6"
stut     = "#2a78d6"
stut_lt  = "#86b6ef"
excl     = "#1baf7a"
bisect   = "#c3c2b7"
gridcol  = "#e1e0d9"

# --- axes ---
set xlabel "CPU (vCPU)" font ",12"
set ylabel "RAM (GiB)" font ",12"
set title  "CPU vs RAM — warm and per-exec firecracker" font ",13"

set xrange [0:32]
set yrange [0:35]
set xtics 4
set ytics 5

set grid xtics ytics lt 0 lw 0.5 lc rgb gridcol
set key top left Left reverse spacing 1.3 box lw 0.5

set clip two
set rmargin 8

# --- line styles ---
set style line 10 lc rgb warm    lw 1.2              # warm iso-N major
set style line 11 lc rgb warm_lt lw 0.6              # warm iso-N minor
set style line 12 lc rgb warm    lw 0.9              # warm iso-D

set style line 20 lc rgb stut    lw 1.2              # perexec iso-N major
set style line 21 lc rgb stut_lt lw 0.6              # perexec iso-N minor
set style line 22 lc rgb stut    lw 0.9              # perexec iso-D

set style line 30 lc rgb excl    lw 2.5              # N=200 line
set style line 31 lc rgb excl    lw 0.9              # excl iso-D

set style line 40 lc rgb bisect  lw 1.5 dt 4         # bisector

# --- annotations ---


set label 2 at 11,10 "1 vCPU : 1 GiB" font ",9" tc rgb bisect rotate by 42

# N= labels for warm (at D=MAX end)
set label 10 at  1.1,1.535 "N=5"   font ",8" tc rgb warm
set label 11 at  4.0,6.14  "N=20"  font ",8" tc rgb warm
set label 12 at  7.8,12.28 "N=40"  font ",8" tc rgb warm
set label 13 at 11.6,18.42 "N=60"  font ",8" tc rgb warm
set label 14 at 15.4,24.56 "N=80"  font ",8" tc rgb warm
set label 15 at 19.2,30.7  "N=100" font ",8" tc rgb warm

# N= labels for per-exec (high-N on D=0 edge)
set label 20 at  2.3,0.87  "N=5"   font ",8" tc rgb stut
set label 21 at  8.6,3.49  "N=20"  font ",8" tc rgb stut
set label 22 at 16.9,6.98  "N=40"  font ",8" tc rgb stut
# N=60: D=MAX endpoint (25.11, 10.46) is on-chart → label at D=MAX end
set label 23 at 25.3,10.46 "N=60"  font ",8" tc rgb stut
# N=80,100: cross x=32 before D=MAX → label outside right axis at crossing RAM
set label 24 at 32.5,13.6  "N=80"  font ",8" tc rgb stut
set label 25 at 32.5,15.0  "N=100" font ",8" tc rgb stut

# D% labels (warm, at N=100)
set label 30 at  6.3,31.5 "3%"  font ",8" tc rgb warm center
set label 31 at 12.7,31.5 "6%"  font ",8" tc rgb warm center
set label 32 at 19.0,31.5 "9%"  font ",8" tc rgb warm center

# D% labels (per-exec, at highest on-chart N)
set label 33 at  9.0,16.5  "3%"  font ",8" tc rgb stut center
set label 34 at 17.0,12.5  "6%"  font ",8" tc rgb stut center

# N=200 label (outside plot right edge)
set label 40 at 32.5,22 "N=200" font ",9" tc rgb excl

# --- plot ---
# Gnuplot breaks lines at blank lines within an index, so each
# iso-N / iso-D segment is drawn separately automatically.

plot \
    "cpu-ram-pressure.dat" index 0 using 1:2 with filledcurves closed \
        fc rgb warm fs transparent solid 0.10 noborder title "Warm (resident) fc", \
    "" index 3 using 1:2 with filledcurves closed \
        fc rgb stut fs transparent solid 0.10 noborder title "Per-exec (start/stop) fc", \
    "" index 6 using 1:2 with filledcurves closed \
        fc rgb excl fs transparent solid 0.12 noborder title "Moca opportunity", \
    "" index 1 using 1:2 with lines ls 11 notitle, \
    "" index 2 using 1:2 with lines ls 12 notitle, \
    "" index 4 using 1:2 with lines ls 21 notitle, \
    "" index 5 using 1:2 with lines ls 22 notitle, \
    "" index 7 using 1:2 with lines ls 31 notitle, \
    "" index 8 using 1:2 with lines ls 30 notitle, \
    "" index 9 using 1:2 with lines ls 40 notitle
