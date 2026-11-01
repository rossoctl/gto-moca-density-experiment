# image-factory testbed for sympy__sympy-18189, emitted by factory/emit_oci.py (oci/1).
# Generated: change factory/testbed.py, never this file.
FROM docker.io/swebench/sweb.eval.x86_64.sympy_1776_sympy-18189@sha256:7f3118533e22d47a19b9784ab86c7b0251cd6fec0acc71f7fe8805809f052724
LABEL io.rossoctl.image-factory.instance="sympy__sympy-18189"
USER root
SHELL ["/bin/bash", "-c"]
# step 1: destroy the clone that reaches the fix, after proving the image
# step 2: one baseline commit tracking exactly what the clone tracked
# step 3: an agent commit must fail loudly, not empty the patch
# step 4: hand the tree to the exec uid, numerically
RUN ( set -eu; head=$(git -C /testbed rev-parse HEAD); tree=$(git -C /testbed rev-parse 'HEAD^{tree}'); git -C /testbed merge-base --is-ancestor 1923822ddf8265199dbd9ef9ce09641d3fd042b9 HEAD || { echo "FATAL base_commit 1923822ddf82 is not an ancestor of /testbed HEAD ($head) -- this is not this instance's image" >&2; echo '      FIX: re-run `scripts/swebench-fetch.py --instances <id> --force`. If the published image really moved, the new head is a NEW INSTRUMENT: every template built on the old one must be rebuilt, not mixed with it' >&2; exit 1; }; if [ "$head" != 32537e483e2c24352d01019e86ed5199a9350ae4 ] || [ "$tree" != cdeb81de0eaea114053d5f2427930110d5c34ad6 ]; then   echo "FATAL /testbed is at head=$head tree=$tree; instance.json recorded head=32537e483e2c24352d01019e86ed5199a9350ae4 tree=cdeb81de0eaea114053d5f2427930110d5c34ad6" >&2;   echo '      FIX: re-run `scripts/swebench-fetch.py --instances <id> --force`. If the published image really moved, the new head is a NEW INSTRUMENT: every template built on the old one must be rebuilt, not mixed with it' >&2; exit 1; fi; git -C /testbed ls-files -z > /etc/autobench-tracked; test -s /etc/autobench-tracked || { echo 'FATAL the clone tracks no files' >&2; exit 1; }; { echo instance_id=sympy__sympy-18189; echo base_commit=1923822ddf8265199dbd9ef9ce09641d3fd042b9; echo image_digest=swebench/sweb.eval.x86_64.sympy_1776_sympy-18189@sha256:7f3118533e22d47a19b9784ab86c7b0251cd6fec0acc71f7fe8805809f052724; echo dataset=SWE-bench/SWE-bench_Lite; echo split=test; echo "testbed_head=$head"; echo "testbed_tree=$tree"; } > /etc/autobench-instance; chmod 0644 /etc/autobench-instance /etc/autobench-tracked; rm -rf /testbed/.git; test ! -e /testbed/.git || { echo 'FATAL /testbed/.git survived rm -rf' >&2; exit 1; } ) \
    && ( set -eu; cd /testbed; git init -q .; printf '__pycache__/\n*.pyc\n.pytest_cache/\n*.egg-info/\nbuild/\ndist/\n.eggs/\n*.so\n*.o\n.venv/\nvenv/\nnode_modules/\n.mypy_cache/\n.tox/\n' >> .git/info/exclude; xargs -0 -a /etc/autobench-tracked -r git -c user.name=autobench -c user.email=autobench@invalid add -f --; git -c user.name=autobench -c user.email=autobench@invalid commit -q -m baseline; git rev-list --count HEAD ) \
    && ( set -eu; git -C /testbed config user.useConfigOnly true ) \
    && ( set -eu; chown -R 1000:1000 /testbed )
WORKDIR /testbed
