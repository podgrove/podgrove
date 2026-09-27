FROM python:3.12-slim
WORKDIR /opt/podgrove
COPY pyproject.toml README.md ./
COPY podgrove ./podgrove
RUN pip install --no-cache-dir .
# The reaper image needs kubectl only; Compose/Docker are laptop dependencies.
ARG KUBECTL_VERSION=v1.36.4
ARG TARGETARCH=amd64
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSLo /usr/local/bin/kubectl https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl \
    && curl -fsSLo /tmp/kubectl.sha256 https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl.sha256 \
    && echo "$(cat /tmp/kubectl.sha256)  /usr/local/bin/kubectl" | sha256sum --check \
    && chmod 755 /usr/local/bin/kubectl \
    && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* /tmp/kubectl.sha256
USER 65532:65532
ENTRYPOINT ["podgrove"]
