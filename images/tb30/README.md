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


Removed on import: 17 tasks whose trajectories require external connectivity (pip/apt/npm installs, curl/wget fetches) -- the replay sandbox runs network-less. Their images and runs can be re-added from the TB source if that changes.
| Task | Class | Environment image | microvm.Dockerfile sha256 |
|---|---|---|---|
| `constraints-scheduling` | short | `alexgshaw/constraints-scheduling` | `ddc287f156c8b892…` |
| `distribution-search` | short | `alexgshaw/distribution-search` | `a21d62572655061e…` |
| `feal-differential-cryptanalysis` | medium | `alexgshaw/feal-differential-cryptanalysis` | `722edc27e7c18559…` |
| `fix-git` | short | `alexgshaw/fix-git` | `7112ee7691f071ac…` |
| `large-scale-text-editing` | long | `alexgshaw/large-scale-text-editing` | `dd66f76f12d3ee73…` |
| `log-summary-date-ranges` | short | `alexgshaw/log-summary-date-ranges` | `c466476860e9451c…` |
| `model-extraction-relu-logits` | short | `alexgshaw/model-extraction-relu-logits` | `d99a5f5f5e9198b7…` |
| `multi-source-data-merger` | short | `alexgshaw/multi-source-data-merger` | `89bd3e1e8cb2de44…` |
| `openssl-selfsigned-cert` | short | `alexgshaw/openssl-selfsigned-cert` | `97c996532587e08e…` |
| `portfolio-optimization` | long | `alexgshaw/portfolio-optimization` | `b9a86b8bf7e4c1d4…` |
| `pytorch-model-recovery` | medium | `alexgshaw/pytorch-model-recovery` | `bb9ece18d153aa52…` |
| `query-optimize` | long | `alexgshaw/query-optimize` | `4fed71fae92c09b7…` |
| `regex-log` | short | `alexgshaw/regex-log` | `6153db2423ac1e6d…` |
