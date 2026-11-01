# image-factory testbed for psf__requests-1142, emitted by factory/emit_oci.py (oci/1).
# Generated: change factory/testbed.py, never this file.
FROM docker.io/swebench/sweb.eval.x86_64.psf_1776_requests-1142@sha256:b2733e57feb4cfaeac24ab5ec496d4cb89a37db53bd0ca72eaededdb2ce19d1c
LABEL io.rossoctl.image-factory.instance="psf__requests-1142"
USER root
SHELL ["/bin/bash", "-c"]
# step 1: destroy the clone that reaches the fix, after proving the image
# step 2: one baseline commit tracking exactly what the clone tracked
# step 3: an agent commit must fail loudly, not empty the patch
# step 4: hand the tree to the exec uid, numerically
RUN ( set -eu; head=$(git -C /testbed rev-parse HEAD); tree=$(git -C /testbed rev-parse 'HEAD^{tree}'); git -C /testbed merge-base --is-ancestor 22623bd8c265b78b161542663ee980738441c307 HEAD || { echo "FATAL base_commit 22623bd8c265 is not an ancestor of /testbed HEAD ($head) -- this is not this instance's image" >&2; echo '      FIX: re-run `scripts/swebench-fetch.py --instances <id> --force`. If the published image really moved, the new head is a NEW INSTRUMENT: every template built on the old one must be rebuilt, not mixed with it' >&2; exit 1; }; if [ "$head" != 546130c8c878adabd16ac5221a36e39e4dab3645 ] || [ "$tree" != 3b8ba30961592000c9a9a73b41d8452c871f47b4 ]; then   echo "FATAL /testbed is at head=$head tree=$tree; instance.json recorded head=546130c8c878adabd16ac5221a36e39e4dab3645 tree=3b8ba30961592000c9a9a73b41d8452c871f47b4" >&2;   echo '      FIX: re-run `scripts/swebench-fetch.py --instances <id> --force`. If the published image really moved, the new head is a NEW INSTRUMENT: every template built on the old one must be rebuilt, not mixed with it' >&2; exit 1; fi; git -C /testbed ls-files -z > /etc/autobench-tracked; test -s /etc/autobench-tracked || { echo 'FATAL the clone tracks no files' >&2; exit 1; }; { echo instance_id=psf__requests-1142; echo base_commit=22623bd8c265b78b161542663ee980738441c307; echo image_digest=swebench/sweb.eval.x86_64.psf_1776_requests-1142@sha256:b2733e57feb4cfaeac24ab5ec496d4cb89a37db53bd0ca72eaededdb2ce19d1c; echo dataset=SWE-bench/SWE-bench_Verified; echo split=test; echo "testbed_head=$head"; echo "testbed_tree=$tree"; } > /etc/autobench-instance; chmod 0644 /etc/autobench-instance /etc/autobench-tracked; rm -rf /testbed/.git; test ! -e /testbed/.git || { echo 'FATAL /testbed/.git survived rm -rf' >&2; exit 1; } ) \
    && ( set -eu; cd /testbed; git init -q .; printf '__pycache__/\n*.pyc\n.pytest_cache/\n*.egg-info/\nbuild/\ndist/\n.eggs/\n*.so\n*.o\n.venv/\nvenv/\nnode_modules/\n.mypy_cache/\n.tox/\n' >> .git/info/exclude; xargs -0 -a /etc/autobench-tracked -r git -c user.name=autobench -c user.email=autobench@invalid add -f --; git -c user.name=autobench -c user.email=autobench@invalid commit -q -m baseline; git rev-list --count HEAD ) \
    && ( set -eu; git -C /testbed config user.useConfigOnly true ) \
    && ( set -eu; chown -R 1000:1000 /testbed )
WORKDIR /testbed
