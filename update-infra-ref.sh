#!/bin/bash

# Utility to simplify updating 'infra-deployments' reference to our repo

set -euo pipefail

if ! type -p yq >/dev/null; then
    echo "ERROR: No 'yq' utility available" >&2
    exit 1
fi

base="../redhat-appstudio--infra-deployments/components/monitoring/grafana"
files=(
    "$base/development/dashboards/perfscale/kustomization.yaml"
    "$base/staging/dashboards/perfscale/kustomization.yaml"
)

for f in "${files[@]}"; do
    if ! [ -w "$f" ]; then
        echo "ERROR: File not writable or missing: $f" >&2
        exit 1
    fi
done

commit=$1
update="https://github.com/konflux-ci/perfscale/grafana/?ref=$commit"

for f in "${files[@]}"; do
    yq --in-place --yaml-output '.resources[0] = "'"$update"'"' "$f"
    echo "Updated $f"
done

echo "NOTE: Production ref not updated. Update it manually after soak time in staging:"
echo "  $base/production/dashboards/perfscale/kustomization.yaml"
