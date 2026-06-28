FROM golang:1.22-bookworm AS builder

RUN apt-get update && \
    apt-get install -y cmake g++ zlib1g-dev pkg-config wget libutfcpp-dev && \
    rm -rf /var/lib/apt/lists/*

# Build taglib 2.x from source (distro packages only have 1.x)
WORKDIR /taglib-build
RUN wget -q https://github.com/taglib/taglib/releases/download/v2.0.2/taglib-2.0.2.tar.gz && \
    tar xzf taglib-2.0.2.tar.gz && \
    cd taglib-2.0.2 && \
    cmake -DCMAKE_INSTALL_PREFIX=/usr \
          -DBUILD_SHARED_LIBS=ON \
          -DBUILD_TESTS=OFF \
          -DCMAKE_BUILD_TYPE=Release . && \
    make -j$(nproc) && \
    make install && \
    ldconfig

WORKDIR /src
COPY . .

RUN CGO_ENABLED=1 \
    go build \
    -mod=vendor \
    -ldflags "-w -linkmode external -extldflags '-lstdc++'" \
    -buildmode pie \
    -o beatportdl \
    ./cmd/beatportdl

FROM debian:bookworm-slim

RUN apt-get update && \
    apt-get install -y ca-certificates libstdc++6 zlib1g && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/lib/x86_64-linux-gnu/libtag.so* /usr/lib/x86_64-linux-gnu/
COPY --from=builder /usr/lib/x86_64-linux-gnu/libtag_c.so* /usr/lib/x86_64-linux-gnu/
COPY --from=builder /src/beatportdl /usr/local/bin/beatportdl

RUN chmod +x /usr/local/bin/beatportdl && ldconfig

WORKDIR /config

ENTRYPOINT ["beatportdl"]
