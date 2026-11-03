# image-factory tb-microvm image for sparql-university, emitted by factory/tb.py (oci-tb-microvm/1).
# Generated: change factory/tb.py, never this file.
FROM docker.io/alexgshaw/sparql-university@sha256:92fa4304e51167f9fb4466144eba33ef9272a7df0ac1cab9fc3986bccd1ab708
LABEL io.rossoctl.image-factory.instance="sparql-university"
LABEL io.rossoctl.image-factory.platform="tb-microvm"
USER root
SHELL ["/bin/bash", "-c"]
# /etc/hosts: sha256 672f53473c9c6f189f2c658dcd4ab1eb0f2a42df241333787b8897cde4ed4fdf
COPY hosts /etc/hosts
# step 1: the environment's /app to /opt/testbed-src; /app links to the per-session workspace
# step 2: the tools moca's rootfs builder requires, only where missing
# step 3: the distro's ca-certificates: this environment ships no CA bundle
RUN ( set -eu; mv /app /opt/testbed-src; mkdir -p /workspace; ln -s /workspace /app; test -L /app && test -z "$(ls -A /workspace)" ) \
    && ( set -eu; pkgs=''; for tp in git:git file:file python3:python3; do t=${tp%%:*}; for d in /usr/local/bin /usr/bin /bin; do test -x $d/$t && continue 2; done; pkgs="$pkgs ${tp#*:}"; done; if [ -n "$pkgs" ]; then export DEBIAN_FRONTEND=noninteractive; apt-get update -qq; apt-get install -y -qq --no-install-recommends $pkgs >/dev/null; rm -rf /var/lib/apt/lists/*; fi; printf '%s\n' $pkgs > /etc/image-factory-tb-added ) \
    && ( set -eu; export DEBIAN_FRONTEND=noninteractive; apt-get update -qq; apt-get install -y -qq --no-install-recommends ca-certificates >/dev/null; rm -rf /var/lib/apt/lists/*; update-ca-certificates >/dev/null; test -s /etc/ssl/certs/ca-certificates.crt )
WORKDIR /workspace
