# image-factory testbed for pydata__xarray-4094, emitted by factory/emit_oci.py (oci/1).
# Generated: change factory/testbed.py, never this file.
FROM docker.io/swebench/sweb.eval.x86_64.pydata_1776_xarray-4094@sha256:6bcd1fe581af0796691ac3527b940d43e5e1bfd6539adeecebb12f3e4b25dc2c
LABEL io.rossoctl.image-factory.instance="pydata__xarray-4094"
USER root
SHELL ["/bin/bash", "-c"]
# step 1: destroy the clone that reaches the fix, after proving the image
# step 2: one baseline commit tracking exactly what the clone tracked
# step 3: an agent commit must fail loudly, not empty the patch
# step 4: hand the tree to the exec uid, numerically
RUN ( set -eu; head=$(git -C /testbed rev-parse HEAD); tree=$(git -C /testbed rev-parse 'HEAD^{tree}'); git -C /testbed merge-base --is-ancestor a64cf2d5476e7bbda099b34c40b7be1880dbd39a HEAD || { echo "FATAL base_commit a64cf2d5476e is not an ancestor of /testbed HEAD ($head) -- this is not this instance's image" >&2; echo '      FIX: re-run `scripts/swebench-fetch.py --instances <id> --force`. If the published image really moved, the new head is a NEW INSTRUMENT: every template built on the old one must be rebuilt, not mixed with it' >&2; exit 1; }; if [ "$head" != 58761b01d75edf12556fbc9d0056af31e38c86b2 ] || [ "$tree" != 3bd6685d9721a42ec9cd0455d11a15a4637b7577 ]; then   echo "FATAL /testbed is at head=$head tree=$tree; instance.json recorded head=58761b01d75edf12556fbc9d0056af31e38c86b2 tree=3bd6685d9721a42ec9cd0455d11a15a4637b7577" >&2;   echo '      FIX: re-run `scripts/swebench-fetch.py --instances <id> --force`. If the published image really moved, the new head is a NEW INSTRUMENT: every template built on the old one must be rebuilt, not mixed with it' >&2; exit 1; fi; git -C /testbed ls-files -z > /etc/autobench-tracked; test -s /etc/autobench-tracked || { echo 'FATAL the clone tracks no files' >&2; exit 1; }; { echo instance_id=pydata__xarray-4094; echo base_commit=a64cf2d5476e7bbda099b34c40b7be1880dbd39a; echo image_digest=swebench/sweb.eval.x86_64.pydata_1776_xarray-4094@sha256:6bcd1fe581af0796691ac3527b940d43e5e1bfd6539adeecebb12f3e4b25dc2c; echo dataset=SWE-bench/SWE-bench_Lite; echo split=test; echo "testbed_head=$head"; echo "testbed_tree=$tree"; } > /etc/autobench-instance; chmod 0644 /etc/autobench-instance /etc/autobench-tracked; rm -rf /testbed/.git; test ! -e /testbed/.git || { echo 'FATAL /testbed/.git survived rm -rf' >&2; exit 1; } ) \
    && ( set -eu; cd /testbed; git init -q .; printf '__pycache__/\n*.pyc\n.pytest_cache/\n*.egg-info/\nbuild/\ndist/\n.eggs/\n*.so\n*.o\n.venv/\nvenv/\nnode_modules/\n.mypy_cache/\n.tox/\n' >> .git/info/exclude; xargs -0 -a /etc/autobench-tracked -r git -c user.name=autobench -c user.email=autobench@invalid add -f --; git -c user.name=autobench -c user.email=autobench@invalid commit -q -m baseline; git rev-list --count HEAD ) \
    && ( set -eu; git -C /testbed config user.useConfigOnly true ) \
    && ( set -eu; chown -R 1000:1000 /testbed )     # git refuses uid-1000 access when rootless-podman userns shifts present
    # /testbed as root-owned (seen after a host power-cycle/resize); the
    # system gitconfig is honored for every uid, immune to the shift
    && ( set -eu; git config --system safe.directory /testbed )
WORKDIR /testbed
