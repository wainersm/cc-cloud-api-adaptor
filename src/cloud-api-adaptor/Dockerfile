ARG BUILD_TYPE=dev
# quay.io/confidential-containers/golang-fedora:1.25.9-41
ARG BUILDER_BASE=quay.io/confidential-containers/golang-fedora@sha256:ee140c59abb205d6385ef89fb0dfc5352f480435b1b58a7fa9941492904dcc07
# registry.fedoraproject.org/fedora:41
ARG BASE=registry.fedoraproject.org/fedora@sha256:893f7eeffce8e3e2bb2bc62786ba5603fb1e0bd6ba07091acb892266b95bafdf

# This dockerfile uses Go cross-compilation to build the binary,
# we build on the host platform ($BUILDPLATFORM) and then copy the
# binary into the container image of the target platform ($TARGETPLATFORM)
# that was specified with --platform. For more details see:
# https://www.docker.com/blog/faster-multi-platform-builds-dockerfile-cross-compilation-guide/
FROM --platform=$BUILDPLATFORM $BUILDER_BASE AS builder-release
ARG YQ_VERSION
RUN go install github.com/mikefarah/yq/v4@$YQ_VERSION
RUN dnf install -y git && dnf clean all

# For `dev` builds due to CGO constraints we have to emulate the target platform
# instead of using Go's cross-compilation
FROM --platform=$TARGETPLATFORM $BUILDER_BASE AS builder-dev
ARG YQ_VERSION
RUN go install github.com/mikefarah/yq/v4@$YQ_VERSION
RUN dnf install -y libvirt-devel binutils-gold && dnf clean all

FROM builder-${BUILD_TYPE} AS builder
ARG RELEASE_BUILD
ARG COMMIT
ARG VERSION
ARG TARGETARCH

WORKDIR /work
COPY cloud-api-adaptor/go.mod cloud-api-adaptor/go.sum ./cloud-api-adaptor/
COPY cloud-providers ./cloud-providers
COPY peerpod-ctrl ./peerpod-ctrl
WORKDIR /work/cloud-api-adaptor
RUN go mod download
COPY cloud-api-adaptor/entrypoint.sh cloud-api-adaptor/Makefile cloud-api-adaptor/Makefile.defaults cloud-api-adaptor/versions.yaml ./
COPY cloud-api-adaptor/cmd   ./cmd
COPY cloud-api-adaptor/pkg   ./pkg
COPY cloud-api-adaptor/proto ./proto

RUN CC=gcc ARCH=$TARGETARCH make COMMIT=$COMMIT VERSION=$VERSION RELEASE_BUILD=$RELEASE_BUILD cloud-api-adaptor

FROM builder-release AS iptables

ARG TARGETARCH

COPY cloud-api-adaptor/versions.yaml /versions.yaml

RUN echo "Building iptables-wrapper... " \
    && git clone https://github.com/kubernetes-sigs/iptables-wrappers.git /iptables \
    && cd /iptables \
    && version=$(grep 'iptables-wrapper:' /versions.yaml | awk '{print $2}') \
    && git checkout $version \
    && GOARCH=$TARGETARCH make BIN_DIR=. build

FROM --platform=$TARGETPLATFORM $BASE AS base-release

RUN dnf install -y iptables iptables-legacy iptables-nft nftables && dnf clean all
RUN --mount=type=cache,target=/iptables,from=iptables,source=/iptables,readonly \
    cp /iptables/iptables-wrapper /usr/sbin \
    && /usr/sbin/iptables-wrapper install

FROM base-release AS base-dev
RUN dnf install -y libvirt-libs /usr/bin/ssh && dnf clean all

FROM base-${BUILD_TYPE}
COPY --from=builder /work/cloud-api-adaptor/cloud-api-adaptor /work/cloud-api-adaptor/entrypoint.sh /usr/local/bin/
CMD ["entrypoint.sh"]
