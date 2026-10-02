# Two stages: the toolchain never reaches the running image.
FROM rust:1-slim AS build
WORKDIR /src
RUN apt-get update && apt-get install -y --no-install-recommends pkg-config && rm -rf /var/lib/apt/lists/*
COPY Cargo.toml Cargo.lock ./
COPY shared-rs ./shared-rs
COPY server ./server
COPY agent ./agent
COPY tools ./tools
COPY shared ./shared
# Only the server. The agent and the desktop shell belong on an operator's
# machine, not on a box that holds other people's metadata.
RUN cargo build --release -p fury-server

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
# Not root: a container that only needs to listen and talk to Postgres has no
# business being able to write to its own filesystem as root.
RUN useradd --system --uid 10001 fury
# Where bundles live (FURY_BUNDLE_DIR's default), made here and handed to the
# user the server runs as. Without it the server refused to start: it checks
# the directory before opening its socket, and uid 10001 cannot create
# anything under /var/lib -- "cannot create the bundle directory
# /var/lib/fury/bundles (Permission denied)", reproduced 02.10.2026 by running
# the binary as an ordinary user. docker-compose.yml mounts a volume here; a
# new named volume is filled from the image, ownership included, which is why
# the chown matters.
RUN mkdir -p /var/lib/fury/bundles && chown -R fury:fury /var/lib/fury
COPY --from=build /src/target/release/fury-server /usr/local/bin/fury-server
USER fury
EXPOSE 8901
CMD ["fury-server"]
