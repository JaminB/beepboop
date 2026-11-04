# beepboop-caddy image: stock Caddy 2 in v0.1 (Tier 0, `tls internal`). The DNS-provider
# module for Tier 1 ACME is v0.2 (ADR 0013) and will add an xcaddy build stage here.
ARG CADDY_IMAGE=caddy:2@sha256:36c24ad91a7ff7bc4c87fe22bfa8df3c4b18da0773d73fc12ee2c52b98c0bb7e
FROM ${CADDY_IMAGE}
