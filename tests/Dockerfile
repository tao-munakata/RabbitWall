FROM debian:trixie-slim
RUN apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 python3-dnspython nftables iproute2 iputils-ping openssl nodejs kmod procps \
    && rm -rf /var/lib/apt/lists/*
