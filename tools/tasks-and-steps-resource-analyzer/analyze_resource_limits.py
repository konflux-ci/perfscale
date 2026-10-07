#!/usr/bin/env python3
"""
Analyze resource consumption data and provide recommendations for resource limits.

Thin CLI shim — implementation lives in the ``resource_analyzer`` package.

Usage:
    # From piped input:
    ./wrapper_for_promql_for_all_clusters.sh 7 --csv | ./analyze_resource_limits.py

    # From YAML file (auto-runs data collection):
    ./analyze_resource_limits.py --file /path/to/buildah.yaml
    ./analyze_resource_limits.py --file https://github.com/.../buildah.yaml

    # Update YAML file with recommendations:
    ./analyze_resource_limits.py --file /path/to/buildah.yaml --update
"""

from resource_analyzer.cli import main

if __name__ == "__main__":
    main()
