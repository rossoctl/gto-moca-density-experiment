# image-factory testbed for django__django-11133, emitted by factory/emit_oci.py (oci/1).
# Generated: change factory/testbed.py, never this file.
FROM docker.io/swebench/sweb.eval.x86_64.django_1776_django-11133@sha256:914093363dbe12def92c5d36e9bdc5270cce5ed0aa6f3daad8bc4ef0389c29bf
LABEL io.rossoctl.image-factory.instance="django__django-11133"
USER root
SHELL ["/bin/bash", "-c"]
# step 1: destroy the clone that reaches the fix, after proving the image
# step 2: one baseline commit tracking exactly what the clone tracked
# step 3: an agent commit must fail loudly, not empty the patch
# step 4: hand the tree to the exec uid, numerically
RUN ( set -eu; head=$(git -C /testbed rev-parse HEAD); tree=$(git -C /testbed rev-parse 'HEAD^{tree}'); git -C /testbed merge-base --is-ancestor 879cc3da6249e920b8d54518a0ae06de835d7373 HEAD || { echo "FATAL base_commit 879cc3da6249 is not an ancestor of /testbed HEAD ($head) -- this is not this instance's image" >&2; echo '      FIX: re-run `scripts/swebench-fetch.py --instances <id> --force`. If the published image really moved, the new head is a NEW INSTRUMENT: every template built on the old one must be rebuilt, not mixed with it' >&2; exit 1; }; if [ "$head" != 95d0ab8311f8180cdd9c02006be1dc16cc706548 ] || [ "$tree" != 11289694a9c6c1129addb27d9109242c0a37606b ]; then   echo "FATAL /testbed is at head=$head tree=$tree; instance.json recorded head=95d0ab8311f8180cdd9c02006be1dc16cc706548 tree=11289694a9c6c1129addb27d9109242c0a37606b" >&2;   echo '      FIX: re-run `scripts/swebench-fetch.py --instances <id> --force`. If the published image really moved, the new head is a NEW INSTRUMENT: every template built on the old one must be rebuilt, not mixed with it' >&2; exit 1; fi; git -C /testbed ls-files -z > /etc/autobench-tracked; test -s /etc/autobench-tracked || { echo 'FATAL the clone tracks no files' >&2; exit 1; }; { echo instance_id=django__django-11133; echo base_commit=879cc3da6249e920b8d54518a0ae06de835d7373; echo image_digest=swebench/sweb.eval.x86_64.django_1776_django-11133@sha256:914093363dbe12def92c5d36e9bdc5270cce5ed0aa6f3daad8bc4ef0389c29bf; echo dataset=SWE-bench/SWE-bench_Lite; echo split=test; echo "testbed_head=$head"; echo "testbed_tree=$tree"; } > /etc/autobench-instance; chmod 0644 /etc/autobench-instance /etc/autobench-tracked; rm -rf /testbed/.git; test ! -e /testbed/.git || { echo 'FATAL /testbed/.git survived rm -rf' >&2; exit 1; } ) \
    && ( set -eu; cd /testbed; git init -q .; printf '__pycache__/\n*.pyc\n.pytest_cache/\n*.egg-info/\nbuild/\ndist/\n.eggs/\n*.so\n*.o\n.venv/\nvenv/\nnode_modules/\n.mypy_cache/\n.tox/\n' >> .git/info/exclude; xargs -0 -a /etc/autobench-tracked -r git -c user.name=autobench -c user.email=autobench@invalid add -f --; git -c user.name=autobench -c user.email=autobench@invalid commit -q -m baseline; git rev-list --count HEAD ) \
    && ( set -eu; git -C /testbed config user.useConfigOnly true ) \
    && ( set -eu; chown -R 1000:1000 /testbed )     # git refuses uid-1000 access when rootless-podman userns shifts present
    # /testbed as root-owned (seen after a host power-cycle/resize); the
    # system gitconfig is honored for every uid, immune to the shift
    && ( set -eu; git config --system safe.directory /testbed )
WORKDIR /testbed
