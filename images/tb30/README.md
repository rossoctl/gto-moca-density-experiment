# The microVM images for the TB-30 trajectories

One folder per Terminal-Bench task in `../runs/` (30). Each holds the exact recipe of the image the task's
**microVM** commands ran in, `microvm.Dockerfile` (image-factory's tb-microvm image, emitted by its `factory/tb.py`; the
recipe's sha256 is checked against image-factory's record), its build context `hosts`, and `environment.txt`: the task's
public Terminal-Bench environment image by digest, which both hands start from.

The recipe moves the environment's `/app` to `/opt/testbed-src` and links `/app` to the per-session `/workspace` (MOCA
seeds each workspace from `/opt/testbed-src`), adds the tools MOCA's rootfs builder needs where missing (git, file,
python3), and a CA bundle where the environment has none.

The upstream image-factory recipe also installs a pinned ripgrep into the image for MOCA's remote-worker Glob/Grep
tools; that was removed on import -- this repo's run environment ships its own moca runners, and no recorded
trajectory invokes `rg` (verified across all 527).

Build one with Docker or Podman, from the folder (the context is `hosts`):

```bash
docker build --load --platform linux/amd64 -t tb-microvm-regex-log -f regex-log/microvm.Dockerfile regex-log
```

The **local-hands** runs used image-factory's tb-testbed image: the same environment image with the agents copied in.
To replay a local run's commands, the environment image alone is enough (`environment.txt`); the agents are not needed.

| Task | Class | Environment image | microvm.Dockerfile sha256 |
|---|---|---|---|
| `chess-best-move` | medium | `alexgshaw/chess-best-move` | `17a068f8f5fd2ad8…` |
| `cobol-modernization` | short | `alexgshaw/cobol-modernization` | `c66a1fb290f3b452…` |
| `constraints-scheduling` | short | `alexgshaw/constraints-scheduling` | `ddc287f156c8b892…` |
| `crack-7z-hash` | medium | `alexgshaw/crack-7z-hash` | `bb6fed68b7c0bade…` |
| `db-wal-recovery` | short | `alexgshaw/db-wal-recovery` | `3e63aebd1f5898b6…` |
| `distribution-search` | short | `alexgshaw/distribution-search` | `a21d62572655061e…` |
| `extract-elf` | short | `alexgshaw/extract-elf` | `28225e8544a2c223…` |
| `feal-differential-cryptanalysis` | medium | `alexgshaw/feal-differential-cryptanalysis` | `722edc27e7c18559…` |
| `feal-linear-cryptanalysis` | short | `alexgshaw/feal-linear-cryptanalysis` | `54b1f8c77236e733…` |
| `financial-document-processor` | medium | `alexgshaw/financial-document-processor` | `a93a8da456f03c1f…` |
| `fix-git` | short | `alexgshaw/fix-git` | `7112ee7691f071ac…` |
| `gcode-to-text` | medium | `alexgshaw/gcode-to-text` | `c4e70edf19937304…` |
| `large-scale-text-editing` | long | `alexgshaw/large-scale-text-editing` | `dd66f76f12d3ee73…` |
| `largest-eigenval` | medium | `alexgshaw/largest-eigenval` | `79452cf8887b1db7…` |
| `llm-inference-batching-scheduler` | medium | `alexgshaw/llm-inference-batching-scheduler` | `5b1647e9ac8c09e6…` |
| `log-summary-date-ranges` | short | `alexgshaw/log-summary-date-ranges` | `c466476860e9451c…` |
| `model-extraction-relu-logits` | short | `alexgshaw/model-extraction-relu-logits` | `d99a5f5f5e9198b7…` |
| `modernize-scientific-stack` | short | `alexgshaw/modernize-scientific-stack` | `e856c3785857b51e…` |
| `multi-source-data-merger` | short | `alexgshaw/multi-source-data-merger` | `89bd3e1e8cb2de44…` |
| `nginx-request-logging` | medium | `alexgshaw/nginx-request-logging` | `9a812513e05417b8…` |
| `openssl-selfsigned-cert` | short | `alexgshaw/openssl-selfsigned-cert` | `97c996532587e08e…` |
| `portfolio-optimization` | long | `alexgshaw/portfolio-optimization` | `b9a86b8bf7e4c1d4…` |
| `pytorch-model-recovery` | medium | `alexgshaw/pytorch-model-recovery` | `bb9ece18d153aa52…` |
| `qemu-alpine-ssh` | long | `alexgshaw/qemu-alpine-ssh` | `35d68c84da0956ad…` |
| `query-optimize` | long | `alexgshaw/query-optimize` | `4fed71fae92c09b7…` |
| `raman-fitting` | medium | `alexgshaw/raman-fitting` | `34b2999ca930bde3…` |
| `regex-log` | short | `alexgshaw/regex-log` | `6153db2423ac1e6d…` |
| `sparql-university` | medium | `alexgshaw/sparql-university` | `98576f49e3276408…` |
| `sqlite-db-truncate` | short | `alexgshaw/sqlite-db-truncate` | `b0b72cf6953fe094…` |
| `video-processing` | medium | `alexgshaw/video-processing` | `1d2dea7b2e5df34d…` |
