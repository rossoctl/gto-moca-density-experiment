# Max sandbox density vs duty cycle — c7g.16xlarge (64 vCPU, 128 GiB)
#
# Two curves: warm (resident) fc vs per-exec (start/stop) fc.
# Each point is the max concurrent sandboxes at that duty cycle,
# limited by whichever of CPU or RAM is exhausted first.
#
# Data: density-vs-duty.dat
#
# Usage:
#   gnuplot density-vs-duty.gp                       # → .png
#   gnuplot -e "TERM='pdf'" density-vs-duty.gp       # → .pdf

if (!exists("TERM")) TERM = "png"

if (TERM eq "pdf") {
    set terminal pdfcairo enhanced font "sans,11" size 7in,4.5in
    set output "density-vs-duty.pdf"
} else {
    set terminal pngcairo enhanced font "sans,11" size 1050,675
    set output "density-vs-duty.png"
}

# --- palette ---
warm    = "#e63946"
stut    = "#2a78d6"
excl    = "#1baf7a"
gridcol = "#e1e0d9"

# --- axes ---
set xlabel "Duty cycle (%)" font ",12"
set ylabel "Max concurrent sandboxes (N)" font ",12"
set title  "Max density vs duty cycle — c7g.16xlarge (64 vCPU, 128 GiB)" font ",13"

set xrange [0.5:10]
set yrange [0:1500]
set xtics 0.5
set ytics 100
set mxtics 5

set grid xtics ytics lt 0 lw 0.5 lc rgb gridcol
set key top right Right spacing 1.3 box lw 0.5

# --- line styles ---
set style line 1 lc rgb warm lw 2.5                  # warm
set style line 2 lc rgb stut lw 2.5                  # per-exec

loss    = "#e6394640"

# --- crossover annotation ---
set arrow 1 from 3.5,500 to 3.5,417 lw 1 lc rgb "#898781" head size 0.3,15
set label 1 at 3.5,530 "crossover ≈ 3.5%" font ",9" tc rgb "#898781" center

# warm RAM-limited plateau
set label 2 at 7,445 "warm: RAM-limited plateau (417)" font ",9" tc rgb warm

# CPU-limited annotations (along the hyperbolic segments)
set label 3 at 6.5,175 "{/*0.8 CPU or I/O limited}" font ",8" tc rgb stut rotate by -12
set label 4 at 8.5,330 "{/*0.8 CPU-limited}" font ",8" tc rgb warm rotate by -10

# --- plot ---
plot \
    "density-vs-duty.dat" using 1:($3 > $2 ? $2 : 1/0):($3) with filledcurves \
        fc rgb excl fs transparent solid 0.12 noborder \
        title "Moca advantage", \
    "" using 1:($2 > $3 ? $3 : 1/0):($2) with filledcurves \
        fc rgb warm fs transparent solid 0.08 noborder \
        title "Moca loss", \
    "" using 1:2 with lines ls 1 title "Warm (resident) fc", \
    "" using 1:3 with lines ls 2 title "Moca (start/stop) fc"
