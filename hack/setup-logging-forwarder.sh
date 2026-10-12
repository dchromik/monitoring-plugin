#!/usr/bin/env bash
set -euo pipefail

namespace=${LOGGING_NAMESPACE:-openshift-logging}
loki_namespace=${LOKI_NAMESPACE:-openshift-logging}
loki_stack=${LOKI_STACK:-logging-loki}
channel=${LOGGING_CHANNEL:-stable-6.5}
ca_configmap=${LOKI_CA_CONFIGMAP:-openshift-service-ca.crt}

command -v oc >/dev/null || { echo 'oc is required' >&2; exit 1; }
command -v python3 >/dev/null || { echo 'python3 is required' >&2; exit 1; }
oc whoami >/dev/null
oc get namespace "$namespace" >/dev/null
oc get lokistack "$loki_stack" -n "$loki_namespace" >/dev/null
oc get service "${loki_stack}-gateway-http" -n "$loki_namespace" >/dev/null

# The Logging and Loki Operators must use the same release channel.
loki_channel=$(oc get subscriptions.operators.coreos.com -A -o json | python3 -c '
import json, sys
matches = [item["spec"].get("channel", "") for item in json.load(sys.stdin).get("items", [])
           if item.get("spec", {}).get("name") == "loki-operator"]
if len(matches) != 1:
    sys.exit("Expected exactly one Loki Operator subscription; set up Loki first")
print(matches[0])
')
if [[ "$channel" != "$loki_channel" ]]; then
  echo "LOGGING_CHANNEL=$channel must match the Loki Operator channel ($loki_channel)" >&2
  exit 1
fi

if ! oc get operatorgroup cluster-logging -n "$namespace" >/dev/null 2>&1; then
  if [[ -z "$(oc get operatorgroups -n "$namespace" -o name)" ]]; then
    oc apply -f - <<EOF
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: cluster-logging
  namespace: ${namespace}
spec:
  upgradeStrategy: Default
EOF
  fi
fi

if oc get subscription cluster-logging -n "$namespace" >/dev/null 2>&1; then
  installed_channel=$(oc get subscription cluster-logging -n "$namespace" -o jsonpath='{.spec.channel}')
  [[ "$installed_channel" == "$channel" ]] || {
    echo "Existing cluster-logging subscription uses $installed_channel, expected $channel" >&2
    exit 1
  }
else
  oc apply -f - <<EOF
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: cluster-logging
  namespace: ${namespace}
spec:
  channel: ${channel}
  installPlanApproval: Automatic
  name: cluster-logging
  source: redhat-operators
  sourceNamespace: openshift-marketplace
EOF
fi

echo 'Waiting for Red Hat OpenShift Logging Operator'
csv=''
for _ in {1..90}; do
  csv=$(oc get subscription cluster-logging -n "$namespace" -o jsonpath='{.status.installedCSV}' 2>/dev/null || true)
  [[ -n "$csv" ]] && break
  sleep 2
done
[[ -n "$csv" ]] || { echo 'Timed out waiting for the operator CSV' >&2; exit 1; }
oc wait --for=jsonpath='{.status.phase}'=Succeeded "csv/$csv" -n "$namespace" --timeout=5m

oc create serviceaccount logging-collector -n "$namespace" --dry-run=client -o yaml | oc apply -f -
for role in logging-collector-logs-writer collect-application-logs collect-infrastructure-logs; do
  oc adm policy add-cluster-role-to-user "$role" -z logging-collector -n "$namespace"
done

if oc get clusterlogforwarder instance -n "$namespace" >/dev/null 2>&1; then
  if ! oc get clusterlogforwarder instance -n "$namespace" -o json | python3 -c '
import json, sys
obj = json.load(sys.stdin)
spec = obj.get("spec", {})
target = next((o for o in spec.get("outputs", []) if o.get("name") == "lokistack-out"), {})
loki = target.get("lokiStack", {})
pipeline = next((p for p in spec.get("pipelines", []) if p.get("name") == "app-infra"), {})
ok = (spec.get("serviceAccount", {}).get("name") == "logging-collector"
      and target.get("type") == "lokiStack"
      and loki.get("target", {}).get("name") == sys.argv[1]
      and loki.get("target", {}).get("namespace") == sys.argv[2]
      and loki.get("authentication", {}).get("token", {}).get("from") == "serviceAccount"
      and bool(target.get("tls", {}).get("ca"))
      and {"application", "infrastructure"}.issubset(set(pipeline.get("inputRefs", [])))
      and "lokistack-out" in pipeline.get("outputRefs", []))
sys.exit(0 if ok else 1)
' "$loki_stack" "$loki_namespace"; then
    echo 'Existing ClusterLogForwarder/instance does not match this LokiStack; refusing to replace it' >&2
    exit 1
  fi
  echo 'Keeping existing matching ClusterLogForwarder/instance'
else
  oc get configmap "$ca_configmap" -n "$namespace" >/dev/null
  oc apply -f - <<EOF
apiVersion: observability.openshift.io/v1
kind: ClusterLogForwarder
metadata:
  name: instance
  namespace: ${namespace}
spec:
  serviceAccount:
    name: logging-collector
  outputs:
  - name: lokistack-out
    type: lokiStack
    lokiStack:
      target:
        name: ${loki_stack}
        namespace: ${loki_namespace}
      authentication:
        token:
          from: serviceAccount
    tls:
      ca:
        configMapName: ${ca_configmap}
        key: service-ca.crt
  pipelines:
  - name: app-infra
    inputRefs:
    - application
    - infrastructure
    outputRefs:
    - lokistack-out
EOF
fi

oc wait --for=condition=Ready clusterlogforwarder/instance -n "$namespace" --timeout=5m
oc rollout status daemonset/instance -n "$namespace" --timeout=5m
echo "Application and infrastructure logs are forwarding to ${loki_namespace}/${loki_stack}."
