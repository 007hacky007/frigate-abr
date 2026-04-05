ARG FRIGATE_VERSION=0.17.1
FROM ghcr.io/blakeblackshear/frigate:${FRIGATE_VERSION}

# Install Python dependencies at build time
COPY sidecar/requirements.txt /opt/frigate-abr/sidecar/requirements.txt
RUN pip3 install --no-cache-dir -r /opt/frigate-abr/sidecar/requirements.txt

# Sidecar application code
COPY sidecar/ /opt/frigate-abr/sidecar/

# Default ABR config (users should mount their own)
COPY config.yml /opt/frigate-abr/config.yml

# S6 oneshot: patches nginx.conf before nginx starts
COPY overlay/s6/abr-patch/ /etc/s6-overlay/s6-rc.d/abr-patch/
RUN chmod +x /etc/s6-overlay/s6-rc.d/abr-patch/run

# S6 longrun: ABR sidecar transcoding service
COPY overlay/s6/abr-sidecar/ /etc/s6-overlay/s6-rc.d/abr-sidecar/
RUN chmod +x /etc/s6-overlay/s6-rc.d/abr-sidecar/run

# Register services in s6 user pipeline
COPY overlay/s6/user-contents/ /etc/s6-overlay/s6-rc.d/user/contents.d/

# Add abr-patch as a dependency of nginx
COPY overlay/s6/nginx-deps/abr-patch /etc/s6-overlay/s6-rc.d/nginx/dependencies.d/abr-patch

# Frontend overlay (quality selector JS/CSS)
COPY overlay/web/abr/ /opt/frigate/web/abr/

# Transcoding cache directory
RUN mkdir -p /tmp/cache/abr
