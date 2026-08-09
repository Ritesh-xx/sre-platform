
"""
Flask application — simulates an e-commerce product API.

This app does two things:
1. Serves fake product and checkout endpoints (the "business logic")
2. Exposes a /metrics endpoint that Prometheus scrapes every 15 seconds

The INJECT_FAULT environment variable lets you simulate problems:
- Set INJECT_FAULT=true and FAULT_TYPE=latency to make requests slow
- Set INJECT_FAULT=true and FAULT_TYPE=errors to make requests fail
This is how you'll test your anomaly detector without real production issues.
"""

import os
import time
import random
from flask import Flask, jsonify
from prometheus_client import (
    Counter,
    Histogram,
    Gauge,
    generate_latest,
    CONTENT_TYPE_LATEST
)

app = Flask(__name__)

# ── Prometheus Metrics ────────────────────────────────────────────────────────
#
# There are 4 types of Prometheus metrics. We use 3 here:
#
# Counter: A number that only ever goes up (like an odometer).
#   Use for: total requests, total errors, total events.
#   Example: app has received 10,432 requests total since startup.
#
# Histogram: Tracks the distribution of values.
#   Use for: latency (how long did requests take?), response sizes.
#   It creates "buckets" — e.g. "how many requests took under 100ms?"
#   This lets you calculate P50, P95, P99 latency in Grafana.
#
# Gauge: A number that can go up or down (like a speedometer).
#   Use for: current active connections, queue depth, replica count.
#   Example: there are currently 47 active requests being processed.

REQUEST_COUNT = Counter(
    'app_requests_total',
    'Total number of HTTP requests received',
    ['method', 'endpoint', 'status_code']
    # Labels let you slice the metric:
    # "give me only POST requests" or "only 500 errors on /checkout"
)

REQUEST_LATENCY = Histogram(
    'app_request_latency_seconds',
    'How long each request took to process, in seconds',
    ['endpoint'],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]
    # Buckets: these are the latency thresholds Prometheus tracks
    # e.g. "how many requests completed in under 100ms (0.1)?"
    # Choose buckets that make sense for your SLO
    # Our SLO is P95 under 1 second, so we need granularity below 1s
)

ACTIVE_REQUESTS = Gauge(
    'app_active_requests_current',
    'Number of requests currently being processed right now'
)

# ── Fault Injection ────────────────────────────────────────────────────────────
# Read from environment variables so we can change behaviour
# without rebuilding the Docker image — just restart with different env vars

INJECT_FAULT = os.getenv('INJECT_FAULT', 'false').lower() == 'true'
FAULT_TYPE   = os.getenv('FAULT_TYPE', 'latency')  # 'latency' or 'errors'

# ── Helper: Simulate Processing Work ──────────────────────────────────────────

def process_request(endpoint: str, base_latency_seconds: float = 0.05):
    """
    Simulates the work an endpoint does.
    Records metrics before and after, with optional fault injection.

    base_latency_seconds: how long the endpoint normally takes
    Returns: HTTP status code as string ('200' or '500')
    """
    ACTIVE_REQUESTS.inc()    # one more request in flight
    start_time = time.time()
    status_code = '200'

    try:
        if INJECT_FAULT and FAULT_TYPE == 'latency':
            # High latency fault: 30% of requests take 2-5 seconds
            # instead of the normal 50ms
            # This simulates: database slow, downstream service slow, CPU starved
            if random.random() < 0.30:
                sleep_time = random.uniform(2.0, 5.0)
                time.sleep(sleep_time)
            else:
                time.sleep(base_latency_seconds + random.uniform(0, 0.02))

        elif INJECT_FAULT and FAULT_TYPE == 'errors':
            # Error fault: 40% of requests return 500
            # This simulates: code bug deployed, dependency down, config wrong
            if random.random() < 0.40:
                status_code = '500'
            else:
                time.sleep(base_latency_seconds + random.uniform(0, 0.02))

        else:
            # Normal operation: base latency plus tiny random variation
            # The variation makes it realistic — real services aren't perfectly consistent
            time.sleep(base_latency_seconds + random.uniform(0, 0.03))

    except Exception as e:
        status_code = '500'

    finally:
        # Always record metrics even if an exception occurred
        duration = time.time() - start_time
        REQUEST_LATENCY.labels(endpoint=endpoint).observe(duration)
        ACTIVE_REQUESTS.dec()    # request is done, decrement gauge

    return status_code

# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.route('/api/products', methods=['GET'])
def get_products():
    """
    Product listing endpoint.
    Lightweight — base latency 50ms.
    This is the most-hit endpoint (70% of traffic in load tests).
    """
    status = process_request('/api/products', base_latency_seconds=0.05)

    REQUEST_COUNT.labels(
        method='GET',
        endpoint='/api/products',
        status_code=status
    ).inc()

    if status == '500':
        return jsonify({'error': 'Failed to fetch products'}), 500

    return jsonify({
        'products': [
            {'id': 1, 'name': 'Laptop',  'price': 75000, 'stock': 15},
            {'id': 2, 'name': 'Phone',   'price': 25000, 'stock': 42},
            {'id': 3, 'name': 'Tablet',  'price': 35000, 'stock': 8},
            {'id': 4, 'name': 'Monitor', 'price': 18000, 'stock': 23},
        ]
    }), int(status)


@app.route('/api/checkout', methods=['GET'])
def checkout():
    """
    Checkout endpoint.
    Heavier — base latency 150ms (simulates database writes, payment gateway calls).
    30% of traffic in load tests.
    """
    status = process_request('/api/checkout', base_latency_seconds=0.15)

    REQUEST_COUNT.labels(
        method='GET',
        endpoint='/api/checkout',
        status_code=status
    ).inc()

    if status == '500':
        return jsonify({'error': 'Checkout failed — please retry'}), 500

    return jsonify({
        'order_id':  random.randint(100000, 999999),
        'status':    'confirmed',
        'amount':    random.randint(10000, 100000),
        'timestamp': time.time()
    }), int(status)


@app.route('/healthz', methods=['GET'])
def health_check():
    """
    Kubernetes liveness probe endpoint.

    Kubernetes calls /healthz every 15 seconds.
    If it gets a non-200 response 3 times in a row, it restarts the pod.
    This is how K8s knows "this container is broken, kill it and start fresh."

    Convention: always use /healthz or /health for this endpoint.
    Never put business logic here — just return 200 if the process is alive.
    """
    return jsonify({
        'status': 'healthy',
        'timestamp': time.time(),
        'fault_injection': INJECT_FAULT,
        'fault_type': FAULT_TYPE if INJECT_FAULT else None
    }), 200


@app.route('/metrics', methods=['GET'])
def metrics():
    """
    Prometheus scrape endpoint.

    Prometheus calls this every 15 seconds (configurable).
    It reads all the Counter, Histogram, Gauge values we've been updating
    and stores them in its time-series database.

    Convention: always use /metrics. Prometheus expects this by default.
    The prometheus_client library formats everything correctly for us.
    """
    return generate_latest(), 200, {'Content-Type': CONTENT_TYPE_LATEST}


if __name__ == '__main__':
    print(f"Starting SRE app — fault injection: {INJECT_FAULT} ({FAULT_TYPE})")
    app.run(host='0.0.0.0', port=5000, debug=False)
    # host='0.0.0.0' means "listen on all network interfaces"
    # Without this, the app only listens on localhost and Docker can't reach it
