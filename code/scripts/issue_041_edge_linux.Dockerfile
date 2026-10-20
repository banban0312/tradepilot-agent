FROM node:22-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392

COPY edge-package.deb /tmp/edge.deb
RUN apt-get update && apt-get install -y --no-install-recommends /tmp/edge.deb && test -x /usr/bin/microsoft-edge-stable && rm -rf /var/lib/apt/lists/* /tmp/edge.deb

COPY issue_041_browser_smoke.mjs /verify/issue_041_browser_smoke.mjs
ENTRYPOINT ["node", "/verify/issue_041_browser_smoke.mjs"]
