#!/usr/bin/env bash
set -euo pipefail

echo "Scaling up observability-operator..."
kubectl scale --replicas="${COO_REPLICAS:-1}" -n openshift-cluster-observability-operator deployment/observability-operator
