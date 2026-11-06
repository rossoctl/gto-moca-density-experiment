# CPU vs RAM — warm and per-exec firecracker pressure map (v2)
# Variant: shaded areas + iso-duty (slanted) lines + dots at N points.
# No horizontal iso-N lines.
#
# Data: cpu-ram-pressure-v2.dat
#
# Usage:
#   gnuplot cpu-ram-pressure-v2.gp                       # → .png
#   gnuplot -e "TERM='pdf'" cpu-ram-pressure-v2.gp       # → .pdf

if (!exists("TERM")) TERM = "png"

if (TERM eq "pdf") {
    set terminal pdfcairo enhanced font "sans,11" size 7in,4.5in
    set output "cpu-ram-pressure-v2.pdf"
} else {
    set terminal pngcairo enhanced font "sans,11" size 1050,675
    set output "cpu-ram-pressure-v2.png"
}

# --- palette ---
warm     = "#e63946"
stut     = "#2a78d6"
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
set style line 12 lc rgb warm lw 1.2                 # warm iso-D lines
set style line 13 lc rgb warm lw 0 pt 7 ps 1.2       # warm dots (filled circle)

set style line 22 lc rgb stut lw 1.2                  # perexec iso-D lines
set style line 23 lc rgb stut lw 0 pt 7 ps 1.2       # perexec dots

set style line 30 lc rgb excl lw 2.5                  # N=200 line
set style line 31 lc rgb excl lw 1.0                  # excl iso-D lines
set style line 32 lc rgb excl lw 0 pt 7 ps 1.2       # excl dots

set style line 40 lc rgb bisect lw 1.5 dt 4           # bisector

# --- annotations ---

set label 2 at 11,10 "1 vCPU : 1 GiB" font ",9" tc rgb bisect rotate by 42

# D% labels (warm, at top of each duty line near N=100)
set label 30 at  6.3,31.5 "3%"  font ",9" tc rgb warm center
set label 31 at 12.7,31.5 "6%"  font ",9" tc rgb warm center
set label 32 at 19.0,31.5 "9%"  font ",9" tc rgb warm center

# D% labels (per-exec)
set label 33 at  9.0,16.5  "3%"  font ",9" tc rgb stut center
set label 34 at 17.0,12.5  "6%"  font ",9" tc rgb stut center

# N=200 label
set label 40 at 32.5,22 "N=200" font ",9" tc rgb excl

# N labels along the warm 3% duty line (offset from dot)
set label 50 at  1.5,5.5   "20"  font ",8" tc rgb warm
set label 51 at  2.8,11.6  "40"  font ",8" tc rgb warm
set label 52 at  4.1,17.8  "60"  font ",8" tc rgb warm
set label 53 at  5.3,23.9  "80"  font ",8" tc rgb warm
set label 54 at  6.6,30.0  "100" font ",8" tc rgb warm

# N labels along the per-exec 3% duty line
set label 60 at  3.1,1.5   "20"  font ",8" tc rgb stut
set label 61 at  5.9,3.6   "40"  font ",8" tc rgb stut
set label 62 at  8.7,5.7   "60"  font ",8" tc rgb stut
set label 63 at 11.5,7.8   "80"  font ",8" tc rgb stut
set label 64 at 14.3,9.9   "100" font ",8" tc rgb stut

# --- plot ---
plot \
    "cpu-ram-pressure-v2.dat" index 0 using 1:2 with filledcurves closed \
        fc rgb warm fs transparent solid 0.10 noborder title "Warm (resident) fc", \
    "" index 3 using 1:2 with filledcurves closed \
        fc rgb stut fs transparent solid 0.10 noborder title "Per-exec (start/stop) fc", \
    "" index 6 using 1:2 with filledcurves closed \
        fc rgb excl fs transparent solid 0.12 noborder title "Moca opportunity", \
    "" index 1 using 1:2 with lines ls 12 notitle, \
    "" index 2 using 1:2 with points ls 13 notitle, \
    "" index 4 using 1:2 with lines ls 22 notitle, \
    "" index 5 using 1:2 with points ls 23 notitle, \
    "" index 7 using 1:2 with lines ls 31 notitle, \
    "" index 8 using 1:2 with points ls 32 notitle, \
    "" index 9 using 1:2 with lines ls 30 notitle, \
    "" index 10 using 1:2 with lines ls 40 notitle
