#!/bin/bash

set -euo pipefail

type -p jsonnet

function build() {
    local in="${1}"
    local out="${2}"
    time jsonnet -J vendor "${in}" | jq "." >"${out}"
}

# Dashboards used in custom Grafana, not deployed via infra-deployments
build src/loadtest-probe.jsonnet generated/loadtest-probe.json
build src/loadtest-probe-multiarch.jsonnet generated/loadtest-probe-multiarch.json
build src/loadtest-probe-rpm.jsonnet generated/loadtest-probe-rpm.json
build src/trending-errors.jsonnet generated/trending-errors.json

# Dashboards that live in this repo, deployed via infra-deployments
build src/controllers-overview-a-n.jsonnet ../grafana/dashboards/controllers-overview-a-n.json
build src/controllers-overview-o-z.jsonnet ../grafana/dashboards/controllers-overview-o-z.json
