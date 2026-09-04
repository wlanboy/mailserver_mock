#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

IMAGE="registry.gmk.lan/mailserver:latest"

echo "==> Baue Image (Dockerfile) ..."
docker build -f Dockerfile -t mailserver .

echo "==> Tagge Image als $IMAGE ..."
docker tag mailserver "$IMAGE"

echo "==> Push Image nach $IMAGE ..."
docker push "$IMAGE"

echo "==> Helm-Upgrade mailserver-mock ..."
helm upgrade mailserver-mock ./mailserver-mock-chart -n mailmock

echo "==> Rollout-Restart mailserver-mock ..."
kubectl rollout restart deployment/mailserver-mock -n mailmock

echo
echo "Fertig."
