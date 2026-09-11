# Browser adapters need Chromium and its system libraries, which is exactly the
# kind of thing nobody wants to install on a CI runner by hand. This image has
# them already.
FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

LABEL org.opencontainers.image.title="pubkit" \
      org.opencontainers.image.description="Publish one source to many platforms, safely." \
      org.opencontainers.image.source="https://github.com/arunsingh/pubkit" \
      org.opencontainers.image.licenses="Apache-2.0"

WORKDIR /work
COPY . /src
RUN pip install --no-cache-dir "/src[all]" && rm -rf /src

# Mount your content and your state:
#   docker run --rm -v "$PWD:/work" -e PUBKIT_DEVTO_TOKEN ghcr.io/arunsingh/pubkit \
#     publish content/ --to devto --confirm
ENTRYPOINT ["pubkit"]
CMD ["--help"]
