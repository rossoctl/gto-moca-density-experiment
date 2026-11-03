# image-factory tb-microvm image for qemu-alpine-ssh, emitted by factory/tb.py (oci-tb-microvm/1).
# Generated: change factory/tb.py, never this file.
FROM docker.io/alexgshaw/qemu-alpine-ssh@sha256:541e8214a8c4faa7b556531b899d45da21048fa2ca6dd328d8cb6c4cffa8e964
LABEL io.rossoctl.image-factory.instance="qemu-alpine-ssh"
LABEL io.rossoctl.image-factory.platform="tb-microvm"
USER root
SHELL ["/bin/bash", "-c"]
# /etc/hosts: sha256 672f53473c9c6f189f2c658dcd4ab1eb0f2a42df241333787b8897cde4ed4fdf
COPY hosts /etc/hosts
# step 1: the environment's /app to /opt/testbed-src; /app links to the per-session workspace
# step 2: the tools moca's rootfs builder requires, only where missing
RUN ( set -eu; mv /app /opt/testbed-src; mkdir -p /workspace; ln -s /workspace /app; test -L /app && test -z "$(ls -A /workspace)" ) \
    && ( set -eu; pkgs=''; for tp in git:git file:file python3:python3; do t=${tp%%:*}; for d in /usr/local/bin /usr/bin /bin; do test -x $d/$t && continue 2; done; pkgs="$pkgs ${tp#*:}"; done; if [ -n "$pkgs" ]; then export DEBIAN_FRONTEND=noninteractive; mkdir -p /tmp/image-factory-apt/lists/partial /tmp/image-factory-apt/archives/partial; printf 'deb http://archive.debian.org/debian bullseye main\ndeb http://archive.debian.org/debian-security bullseye-security main\ndeb http://archive.debian.org/debian bullseye-updates main\n' > /tmp/image-factory-apt/sources.list; apt-get -o Dir::Etc::SourceList=/tmp/image-factory-apt/sources.list -o Dir::Etc::SourceParts=/dev/null -o Dir::State::Lists=/tmp/image-factory-apt/lists -o Dir::Cache::Archives=/tmp/image-factory-apt/archives -o Acquire::Check-Valid-Until=false update -qq; apt-get -o Dir::Etc::SourceList=/tmp/image-factory-apt/sources.list -o Dir::Etc::SourceParts=/dev/null -o Dir::State::Lists=/tmp/image-factory-apt/lists -o Dir::Cache::Archives=/tmp/image-factory-apt/archives -o Acquire::Check-Valid-Until=false install -y -qq --no-install-recommends $pkgs >/dev/null; ls /tmp/image-factory-apt/archives/*.deb 2>/dev/null | xargs -r -n1 basename > /etc/image-factory-tb-archive-debs; rm -rf /tmp/image-factory-apt; fi; printf '%s\n' $pkgs > /etc/image-factory-tb-added )
WORKDIR /workspace
