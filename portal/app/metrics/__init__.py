"""Monitoring of nodes and models (task 2-33).

Nodes push a sample every NODE_PUSH_INTERVAL_S (node/rootfs/opt/llm/bin/node-metrics.sh, POST /api/nodes/{name}/metrics);
the API keeps the latest one in Redis (`live`). The controllers process writes one row per node and per deployment
into `metric_samples` every minute (app/controllers/metrics.py) and deletes what is older than the retention setting.
"""

NODE_PUSH_INTERVAL_S = 20  # the node's interval; the live sample of a silent node expires after NODE_LIVE_TTL_S
