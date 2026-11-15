#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

REGISTRY="registry.gmk.lan"

for arg in "$@"; do
  case "$arg" in
    --local) REGISTRY="localhost:5001" ;;  # kind-registry (docker ps: 127.0.0.1:5001->5000)
    *) echo "Unbekannte Option: $arg" >&2; echo "Usage: $0 [--local]" >&2; exit 1 ;;
  esac
done

IMAGE="$REGISTRY/mailserver:latest"

echo "==> Baue Image (Dockerfile) ..."
docker build -f Dockerfile -t mailserver .

echo "==> Tagge Image als $IMAGE ..."
docker tag mailserver "$IMAGE"

echo "==> Push Image nach $IMAGE ..."
docker push "$IMAGE"

echo "==> Helm-Upgrade mailserver-mock ..."
helm upgrade --install mailserver-mock ./mailserver-mock-chart -n mailmock \
  --set image.repository="$REGISTRY/mailserver"

echo "==> Rollout-Restart mailserver-mock ..."
kubectl rollout restart deployment/mailserver-mock -n mailmock

echo
echo "Fertig."
