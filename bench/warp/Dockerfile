# warp v1.6.1 has no published Docker image -- minio/warp on Docker Hub stopped
# at v1.3.1 when MinIO ended community distribution. We build from the official
# release asset and verify its checksum, which pins us harder than a tag would.
#
# CORRECTION 1 (base image): the brief specified `FROM alpine:3.21`. The v1.6.1
# linux/arm64 asset is a *dynamically linked glibc* binary, not a static Go
# build:
#   ELF 64-bit LSB executable, ARM aarch64, dynamically linked,
#   interpreter /lib/ld-linux-aarch64.so.1, for GNU/Linux 3.7.0
# It cannot run on musl at all. It also needs glibc >= 2.38 and
# GLIBCXX_3.4.32, which rules out debian:bookworm-slim (glibc 2.36 -- verified:
# "version `GLIBC_2.38' not found"). debian:trixie-slim (glibc 2.41) runs it.
#
# CORRECTION 2 (runtime libraries): v1.6.1 publishes only the "warp-rdma"
# flavour for linux, and that binary hard-links against the RDMA stack --
# libnuma.so.1, libibverbs.so.1, librdmacm.so.1, libmlx5.so.1 -- plus the
# NVIDIA cuFile libraries bundled inside the tarball. Without them it dies at
# exec time with "error while loading shared libraries: libnuma.so.1". They are
# link-time requirements only: this harness benchmarks plain HTTP against local
# containers and never enables RDMA (`--rdma` and `--rdma.*` default off), so
# these packages satisfy the dynamic linker and nothing more.
#
# CORRECTION 3 (tarball layout): the brief assumed the binary is at `warp`
# inside the archive. `tar -tzf` says otherwise -- everything lives under a
# `warp-rdma/` prefix, and the binary needs the sibling `warp-rdma/lib/`
# directory (its RPATH is ORIGIN/lib) to resolve libcufile/libcuobjclient. So
# the whole directory is installed, not just one file.

FROM debian:trixie-slim@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258 AS fetch

ARG WARP_VERSION=v1.6.1
ARG WARP_ASSET=warp-rdma_linux_arm64.tar.gz
ARG WARP_SHA256=7ce9f0061fe5d0d0ff809384234ddbab9f9b66a6b15e7fa0b3e0819f63ce0cfc

# A checksum mismatch must fail the build -- that is the point of pinning by
# content instead of by tag.
RUN set -eu \
 && apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && curl -fsSL -o /tmp/warp.tgz \
      "https://github.com/minio/warp/releases/download/${WARP_VERSION}/${WARP_ASSET}" \
 && echo "${WARP_SHA256}  /tmp/warp.tgz" | sha256sum -c - \
 && mkdir -p /opt \
 && tar -xzf /tmp/warp.tgz -C /opt \
 && mv /opt/warp-rdma /opt/warp \
 && chmod +x /opt/warp/warp \
 && rm -f /tmp/warp.tgz

FROM debian:trixie-slim@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258

RUN set -eu \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates libnuma1 libibverbs1 librdmacm1 ibverbs-providers \
 && rm -rf /var/lib/apt/lists/*

COPY --from=fetch /opt/warp /opt/warp

# The binary's RPATH already points at its sibling lib/ directory; this is
# belt-and-braces so an absolute-path invocation can never miss it.
ENV LD_LIBRARY_PATH=/opt/warp/lib

# warp writes its benchdata (.csv.zst) next to wherever it is told; every
# invocation in this harness bind-mounts the run's results directory here.
WORKDIR /results
ENTRYPOINT ["/opt/warp/warp"]
