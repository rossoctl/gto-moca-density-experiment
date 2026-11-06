# Memory use (host-used delta) vs concurrency (pressure)
# Warm firecrackers vs stuttered (fc-perexec) at 10% and predicted 2% duty
#
# Data: docs/reports/moca-side-by-side-report.md, §load80 completed grid
# Metric: host-used delta (peak − baseline) — the only apples-to-apples
#          across backends (VM RSS is not comparable; see report §memory-metrics)
# Prediction: see derivation in mem-vs-concurrency.dat
#
# Usage:
#   gnuplot mem-vs-concurrency.gp                       # → .png
#   gnuplot -e "TERM='pdf'" mem-vs-concurrency.gp       # → .pdf

if (!exists("TERM")) TERM = "png"

if (TERM eq "pdf") {
    set terminal pdfcairo enhanced font "sans,11" size 7in,4.5in
    set output "mem-vs-concurrency.pdf"
} else {
    set terminal pngcairo enhanced font "sans,11" size 1050,675
    set output "mem-vs-concurrency.png"
}

set datafile missing "?"

# --- style ---
set style line 1 lc rgb "#e63946" lw 2.5 pt 7 ps 1.8         # fc warm — red solid, filled circle
set style line 2 lc rgb "#457b9d" lw 2.5 pt 5 ps 1.8         # fc perexec 10% — blue solid, filled triangle
set style line 3 lc rgb "#457b9d" lw 2.0 dt 4 pt 4 ps 1.8    # fc perexec 2% pred — blue dashed, open triangle

set key top left Left reverse spacing 1.4 box lw 0.5
set grid xtics ytics lt 0 lw 0.5 lc rgb "#cccccc"

set xlabel "Mean concurrent sandboxes (pressure)" font ",12"
set ylabel "Host memory delta (GB)" font ",12"
set title "Physical memory vs concurrency — warm vs stuttered firecrackers" font ",13"

set xrange [0:90]
set yrange [0:16]
set xtics 10
set ytics 2

# annotations
set label 1 at 44,10.5 \
    "warm: duty-invariant\n(resident VMs hold pages)" \
    font ",9" tc rgb "#e63946"
set arrow 1 from 43.5,10.2 to 41,12.2 lw 1 lc rgb "#e63946" head size 0.4,15

set label 2 at 42,15 \
    "stuttered D=10%: non-linear —\nqueueing past the CPU\nsaturation knee inflates\neffective residency" \
    font ",9" tc rgb "#457b9d"
set arrow 2 from 41.5,14.8 to 40.3,13.5 lw 1 lc rgb "#457b9d" head size 0.4,15

set label 3 at 50,5.5 \
    "{/*0.85 predicted}  stuttered D=2%:\n{/*0.85 predicted}  linear (knee at N≈200+)" \
    font ",9" tc rgb "#457b9d"
set arrow 3 from 63,5.5 to 79,7.3 lw 1 lc rgb "#457b9d" dt 4 head size 0.4,15

plot "mem-vs-concurrency.dat" \
        using 1:2 with linespoints ls 1 title "fc warm (duty-invariant)", \
     "" using 1:3 with linespoints ls 2 title "fc perexec (D=10%, measured)", \
     "" using 1:4 with linespoints ls 3 title "fc perexec (D=2%, predicted)"
