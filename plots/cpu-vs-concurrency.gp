# CPU use (mean % of 32 cores) vs concurrency (pressure)
# Warm firecrackers (lines) vs stuttered (bands: no-op floor to real ceiling)
# at 10% measured and 2% predicted duty.
#
# The D=10% real-workload ceiling is a smooth queueing-model fit
# (CPU = a·N / (1 − a·N/K), a=1.215, K=128.6) through the two measured
# points, not a straight-line interpolation.  Illustrative, not a fit claim.
#
# Data: docs/reports/moca-side-by-side-report.md
# Prediction: see derivation in cpu-vs-concurrency.dat
#
# Usage:
#   gnuplot cpu-vs-concurrency.gp                       # → .png
#   gnuplot -e "TERM='pdf'" cpu-vs-concurrency.gp       # → .pdf

if (!exists("TERM")) TERM = "png"

if (TERM eq "pdf") {
    set terminal pdfcairo enhanced font "sans,11" size 7in,4.5in
    set output "cpu-vs-concurrency.pdf"
} else {
    set terminal pngcairo enhanced font "sans,11" size 1050,675
    set output "cpu-vs-concurrency.png"
}

# --- style ---
set style line 1 lc rgb "#e63946" lw 2.5 pt 7 ps 1.8         # fc warm 10% measured
set style line 2 lc rgb "#e63946" lw 2.0 dt 4 pt 6 ps 1.8    # fc warm 2% predicted
set style line 3 lc rgb "#457b9d" lw 1.5                      # perexec 10% band edges
set style line 4 lc rgb "#457b9d" lw 1.2 dt 4                 # perexec 2% band edges
set style line 5 lc rgb "#457b9d" lw 0 pt 5 ps 1.8            # perexec 10% measured markers

set key top left Left reverse spacing 1.3 box lw 0.5
set grid xtics ytics lt 0 lw 0.5 lc rgb "#cccccc"

set xlabel "Mean concurrent sandboxes (pressure)" font ",12"
set ylabel "Host CPU (mean % of 32 cores)" font ",12"
set title "CPU vs concurrency — warm vs stuttered firecrackers" font ",13"

set xrange [0:90]
set yrange [0:100]
set xtics 10
set ytics 10

# saturation line
set arrow 10 from 0,100 to 90,100 nohead lw 1.5 dt 2 lc rgb "#cccccc"
set label 10 at 72,96 "32 cores (100%)" font ",9" tc rgb "#999999"

# fill style: hatched patterns
set style fill transparent pattern 6

# annotations
set label 1 at 44,33 \
    "warm D=10% (measured)" \
    font ",9" tc rgb "#e63946"
set arrow 1 from 43.5,32 to 41,27 lw 1 lc rgb "#e63946" head size 0.5,15

set label 2 at 44,18 \
    "{/*0.85 predicted}  warm D=2%" \
    font ",9" tc rgb "#e63946"
set arrow 2 from 62,17.5 to 73,13.5 lw 1 lc rgb "#e63946" dt 4 head size 0.5,15

set label 3 at 44,58 \
    "stuttered D=10%:\nband = workload-dependent\nCPU (cold-rootfs reads)" \
    font ",9" tc rgb "#457b9d"
set arrow 3 from 43.5,56 to 30,45 lw 1 lc rgb "#457b9d" head size 0.5,15

set label 4 at 55,40 \
    "{/*0.85 predicted}  stuttered D=2%:\n{/*0.85 predicted}  narrow band (low duty)" \
    font ",9" tc rgb "#457b9d"
set arrow 4 from 68,39 to 78,24 lw 1 lc rgb "#457b9d" dt 4 head size 0.5,15

# Data blocks: 0=warm10, 1=warm2, 2=perexec10 smooth band,
#              3=perexec10 measured points, 4=perexec2 band.

plot "cpu-vs-concurrency.dat" index 2 \
        using 1:2:3 with filledcurves fc rgb "#457b9d" fs transparent pattern 6 \
        title "fc perexec (D=10%, noop→real)", \
     "" index 2 using 1:2 with lines ls 3 notitle, \
     "" index 2 using 1:3 with lines ls 3 notitle, \
     "" index 3 using 1:2 with points ls 5 notitle, \
     "" index 3 using 1:3 with points ls 5 notitle, \
     "" index 4 \
        using 1:2:3 with filledcurves fc rgb "#457b9d" fs transparent pattern 4 \
        title "fc perexec (D=2%, predicted)", \
     "" index 4 using 1:2 with lines ls 4 notitle, \
     "" index 4 using 1:3 with lines ls 4 notitle, \
     "" index 0 using 1:2 with linespoints ls 1 title "fc warm (D=10%, measured)", \
     "" index 1 using 1:2 with linespoints ls 2 title "fc warm (D=2%, predicted)"
