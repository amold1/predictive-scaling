#!/usr/bin/env bash
set -euo pipefail

CMD=${1:-}

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$ROOT_DIR/.env"

KUBECONFIG_READY_TIMEOUT="${KUBECONFIG_READY_TIMEOUT:-900}"
KUBECONFIG_READY_INTERVAL="${KUBECONFIG_READY_INTERVAL:-10}"

create() {
  echo "Looking for LKE cluster labeled '$CLUSTER_LABEL'..."

  EXISTING_ID="$(linode-cli lke clusters-list --json | jq --arg CLUSTER_LABEL "$CLUSTER_LABEL" '.[] | select(.label==$CLUSTER_LABEL) | .id')"

  if [[ -n "$EXISTING_ID" ]]; then
    CLUSTER_ID="$EXISTING_ID"
    echo "Found existing LKE cluster '$CLUSTER_LABEL' with ID: $EXISTING_ID"
  else
    echo "No existing LKE cluster found with label '$CLUSTER_LABEL'"
    echo "Creating new LKE cluster..."
    CLUSTER_ID=$(linode-cli lke cluster-create \
      --label "$CLUSTER_LABEL" \
      --region "$REGION" \
      --k8s_version "$K8S_VERSION" \
      --node_pools.type "$NODE_TYPE" --node_pools.count "$NODE_COUNT" \
      --format id --text --no-headers)
    echo "Created cluster '$CLUSTER_LABEL' with ID: $CLUSTER_ID"
  fi

  echo -n "$CLUSTER_ID" > "$ROOT_DIR/.cluster_id"
  echo "Wrote cluster id to $ROOT_DIR/.cluster_id"
  kubeconfig $CLUSTER_ID
}

kubeconfig() {
  CLUSTER_ID=$(linode-cli lke clusters-list --json | jq --arg CLUSTER_LABEL "$CLUSTER_LABEL" '.[] | select(.label==$CLUSTER_LABEL) | .id')
  echo "Fetching kubeconfig for $CLUSTER_ID ..."
  deadline=$((SECONDS + KUBECONFIG_READY_TIMEOUT))
  while :; do
    if linode-cli lke kubeconfig-view "$CLUSTER_ID" --json >/tmp/kcfg.json 2>/dev/null; then
      b64="$(jq -r '.[0].kubeconfig' </tmp/kcfg.json)"
      if [[ -n "$b64" && "$b64" != "null" ]]; then
        jq -r '.[0].kubeconfig' </tmp/kcfg.json | base64 -d > "$KUBECONFIG_PATH"
        chmod 600 "$KUBECONFIG_PATH"
        echo "Wrote kubeconfig to $KUBECONFIG_PATH"
        break
      fi
    fi
    [[ $SECONDS -ge $deadline ]] && { echo "Timed out waiting for LKE kubeconfig availability." >&2; return 1; }
    echo "Kubeconfig not ready yet. Retrying in $KUBECONFIG_READY_INTERVAL seconds..."
    sleep "$KUBECONFIG_READY_INTERVAL"
  done
}

delete() {
  CLUSTER_ID=$(linode-cli lke clusters-list --json | jq --arg CLUSTER_LABEL "$CLUSTER_LABEL" '.[] | select(.label==$CLUSTER_LABEL) | .id')
  if [[ -n "$CLUSTER_ID" ]]; then
    echo "Deleting LKE cluster with ID: $CLUSTER_ID ..."
    linode-cli lke cluster-delete "$CLUSTER_ID" || true
    echo "Deleted cluster and removed local state files."
  else
    echo "No cluster found with label $CLUSTER_LABEL. Nothing to delete."
  fi
  rm -f "$ROOT_DIR/.cluster_id" "$KUBECONFIG_PATH"
}

case "$CMD" in
  create) create ;;
  kubeconfig) kubeconfig ;;
  delete) delete ;;
  *) echo "Usage: $0 {create|kubeconfig}"; exit 1 ;;
esac
