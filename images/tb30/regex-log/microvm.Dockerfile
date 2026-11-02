# image-factory tb-microvm image for regex-log, emitted by factory/tb.py (oci-tb-microvm/1).
# Generated: change factory/tb.py, never this file.
FROM docker.io/alexgshaw/regex-log@sha256:90101b2e815323a8da20528a1439bebc407eb9761c9c68a3d557730856c878e9
LABEL io.rossoctl.image-factory.instance="regex-log"
LABEL io.rossoctl.image-factory.platform="tb-microvm"
USER root
SHELL ["/bin/bash", "-c"]
# /etc/hosts: sha256 672f53473c9c6f189f2c658dcd4ab1eb0f2a42df241333787b8897cde4ed4fdf
COPY hosts /etc/hosts
# /tmp/rg: sha256 e62198eb19b136b88c330af83647b5a962cb99b6b1f066758568f12de1974849 (checked by step 2)
COPY rg /tmp/rg
# step 1: the environment's /app to /opt/testbed-src; /app links to the per-session workspace
# step 2: the tools moca's rootfs builder requires, only where missing
# step 3: the distro's ca-certificates: this environment ships no CA bundle
RUN ( set -eu; mv /app /opt/testbed-src; mkdir -p /workspace; ln -s /workspace /app; test -L /app && test -z "$(ls -A /workspace)" ) \
    && ( set -eu; echo 'e62198eb19b136b88c330af83647b5a962cb99b6b1f066758568f12de1974849  /tmp/rg' | sha256sum -c --quiet -; install -m 0755 /tmp/rg /usr/local/bin/rg; rm -f /tmp/rg; pkgs=''; for tp in git:git file:file python3:python3; do t=${tp%%:*}; for d in /usr/local/bin /usr/bin /bin; do test -x $d/$t && continue 2; done; pkgs="$pkgs ${tp#*:}"; done; if [ -n "$pkgs" ]; then export DEBIAN_FRONTEND=noninteractive; apt-get update -qq; apt-get install -y -qq --no-install-recommends $pkgs >/dev/null; rm -rf /var/lib/apt/lists/*; fi; printf '%s\n' $pkgs > /etc/image-factory-tb-added ) \
    && ( set -eu; export DEBIAN_FRONTEND=noninteractive; apt-get update -qq; apt-get install -y -qq --no-install-recommends ca-certificates >/dev/null; rm -rf /var/lib/apt/lists/*; update-ca-certificates >/dev/null; test -s /etc/ssl/certs/ca-certificates.crt )
WORKDIR /workspace
