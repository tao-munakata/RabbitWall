FROM mcr.microsoft.com/devcontainers/typescript-node:1-22-bookworm
RUN apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nftables iproute2 iputils-ping python3-dnspython \
    && apt-get clean
