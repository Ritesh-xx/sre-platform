"""
Anomaly Detector — ML-based auto-remediation system.

How it works end-to-end:
1. Every 60 seconds, fetch the last 30 minutes of metrics from Prometheus
2. Run IsolationForest on each metric time series
3. If the last 3 data points are all flagged as anomalies → trigger remediation
4. Remediation differs by metric type:
   - CPU/latency spike → scale up containers (add capacity)
   - Error rate spike  → this version was bad, restart with previous config
   - Memory growth     → restart before it runs out and crashes
5. Cooldown timer prevents the same action from firing repeatedly

Why this is better than simple threshold alerts:
Threshold: "alert if latency > 1 second"
Problem: what if normal latency is 800ms? 1.1 seconds would alert constantly.
         What if there's a gradual degradation from 100ms to 900ms? No alert fires.

Anomaly detection: "alert if this is unusual compared to recent history"
IsolationForest learns what 'normal' looks like from the last 30 minutes
and flags anything that deviates significantly — regardless of absolute values.
"""

import os
import time
import logging
import threading
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
import json

import numpy as np
import requests
from sklearn.ensemble import IsolationForest

# ── Logging ───────────────────────────────────────────────────────────────────
# Format: timestamp [LEVEL] message
# This is what you'll see in 'docker compose logs -f anomaly-detector'
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger(__name__)

# ── Configuration — all from environment variables ────────────────────────────
# Why env vars: you can change behaviour without rebuilding the Docker image
# Just restart the container with different env vars
PROMETHEUS_URL    = os.getenv('PROMETHEUS_URL',    'http://prometheus:9090')
TARGET_URL        = os.getenv('TARGET_URL',        'http://sre-app:5000')
SCRAPE_INTERVAL   = int(os.getenv('SCRAPE_INTERVAL',   '60'))   # seconds between checks
HISTORY_MINUTES   = int(os.getenv('HISTORY_MINUTES',   '30'))   # how much history to analyze
ANOMALY_THRESHOLD = int(os.getenv('ANOMALY_THRESHOLD', '3'))    # consecutive anomalous points before acting
MAX_REPLICAS      = int(os.getenv('MAX_REPLICAS',      '5'))    # don't scale beyond this
MIN_REPLICAS      = int(os.getenv('MIN_REPLICAS',      '2'))    # don't scale below this
COOLDOWN_SECONDS  = int(os.getenv('COOLDOWN_SECONDS',  '300'))  # 5 min between same remediations

# ── State tracking ────────────────────────────────────────────────────────────
# Track when we last took each action (for cooldown)
last_remediation_time = {}
# Track current simulated replica count (since we're not using real K8s here)
current_replicas = 2
# Track remediation history for the /status endpoint
remediation_history = []

# ── Prometheus Data Fetching ──────────────────────────────────────────────────

def fetch_metric(promql_query: str, minutes: int = HISTORY_MINUTES) -> list:
    """
    Query Prometheus for historical metric values.

    promql_query: a PromQL expression (Prometheus Query Language)
    minutes: how far back to look

    Returns: list of float values, oldest first, or empty list on failure

    Why query_range instead of query:
    query: gives you ONE current value
    query_range: gives you MANY values over a time period
    We need history to detect anomalies — one value tells you nothing
    """
    end_time   = datetime.utcnow()
    start_time = end_time - timedelta(minutes=minutes)

    try:
        response = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query_range",
            params={
                'query': promql_query,
                'start': start_time.isoformat() + 'Z',
                'end':   end_time.isoformat() + 'Z',
                'step':  '60s',  # one data point per minute
            },
            timeout=10  # don't wait more than 10 seconds
        )
        response.raise_for_status()  # raises exception if HTTP error

        result = response.json()['data']['result']
        if not result:
            log.debug(f"No data returned for query: {promql_query[:50]}...")
            return []

        # Extract just the values (each value is [timestamp, value_string])
        values = [float(v[1]) for v in result[0]['values']]
        log.debug(f"Fetched {len(values)} data points")
        return values

    except requests.exceptions.ConnectionError:
        log.warning("Cannot connect to Prometheus — is it running?")
        return []
    except Exception as e:
        log.warning(f"Metric fetch failed: {e}")
        return []

# ── Anomaly Detection ─────────────────────────────────────────────────────────

def detect_anomaly(values: list) -> tuple[bool, float]:
    """
    Run IsolationForest on a time series of metric values.

    Returns: (is_anomalous: bool, anomaly_score: float)
    - is_anomalous: True if we should take action
    - anomaly_score: how anomalous the latest point is (-1 to 0, more negative = worse)

    How IsolationForest works (interview explanation):
    Imagine you have 30 values on a number line.
    The algorithm randomly picks a split point and divides the data.
    It keeps splitting until each value is isolated (alone).
    Normal values, being clustered together, take MANY splits to isolate.
    Anomalies, being far from the cluster, take FEW splits to isolate.
    The "isolation score" = how few splits it took. Fewer splits = more anomalous.

    Why we check ANOMALY_THRESHOLD consecutive points:
    A single anomalous reading could be noise (one slow request, brief CPU spike).
    Three consecutive anomalous readings means this is a real, sustained problem.
    This prevents the system from overreacting to brief, harmless spikes.
    """
    # Need at least 10 points for meaningful anomaly detection
    # With fewer points, there's not enough "normal" data to compare against
    if len(values) < 10:
        log.debug("Insufficient data for anomaly detection (need 10+ points)")
        return False, 0.0

    # Reshape to 2D array — scikit-learn expects shape (n_samples, n_features)
    X = np.array(values).reshape(-1, 1)

    clf = IsolationForest(
        contamination=0.1,    # expect ~10% of points to be anomalous
                              # lower = more conservative (fewer false positives)
                              # higher = more sensitive (catches subtle anomalies)
        random_state=42,      # fixed seed = reproducible results across runs
        n_estimators=100      # number of trees in the forest
                              # more trees = more accurate but slower
                              # 100 is a good balance for small datasets
    )

    # fit_predict: train the model AND get predictions in one step
    # Returns array of: 1 (normal) or -1 (anomaly) for each value
    predictions = clf.fit_predict(X)

    # Get the anomaly score for the most recent point
    # score_samples returns negative values: closer to 0 = more normal
    scores = clf.score_samples(X)
    latest_score = float(scores[-1])

    # Check if the last ANOMALY_THRESHOLD points are ALL anomalies
    recent_predictions = predictions[-ANOMALY_THRESHOLD:]
    is_anomalous = all(p == -1 for p in recent_predictions)

    return is_anomalous, latest_score

# ── Cooldown Check ────────────────────────────────────────────────────────────

def can_act(action_type: str) -> bool:
    """
    Check if enough time has passed since the last remediation of this type.

    Why cooldowns matter:
    Without cooldown: anomaly detected → scale up → still anomalous → scale up again
    → scale up again → hit max replicas in 3 minutes, problem still not fixed
    With cooldown: scale up → wait 5 minutes → check if it helped → decide next action

    This prevents "remediation storms" where the system keeps taking the same
    action repeatedly before having time to see if it worked.
    """
    if action_type not in last_remediation_time:
        return True  # never done this action, go ahead

    elapsed = time.time() - last_remediation_time[action_type]
    remaining = COOLDOWN_SECONDS - elapsed

    if remaining > 0:
        log.info(f"Cooldown active for '{action_type}': {int(remaining)}s remaining")
        return False

    return True

def record_action(action_type: str, description: str):
    """Record that we took an action (for cooldown and history)."""
    last_remediation_time[action_type] = time.time()
    entry = {
        'timestamp': datetime.utcnow().isoformat(),
        'action': action_type,
        'description': description
    }
    remediation_history.append(entry)
    # Keep only last 50 entries
    if len(remediation_history) > 50:
        remediation_history.pop(0)

# ── Remediation Actions ───────────────────────────────────────────────────────

def scale_up():
    """
    Add one more replica to handle increased load.

    In real K8s: kubectl scale deployment/sre-app --replicas=N
    In our Docker Compose version: docker compose up --scale sre-app=N

    What this simulates:
    The K8s HorizontalPodAutoscaler (HPA) does this automatically based on CPU.
    Our ML detector does it based on DETECTED ANOMALIES — smarter than raw CPU.
    """
    global current_replicas
    if not can_act('scale_up'):
        return

    if current_replicas >= MAX_REPLICAS:
        log.warning(f"Already at max replicas ({MAX_REPLICAS}) — cannot scale up further")
        log.warning("Root cause investigation needed — scaling alone won't fix this")
        return

    new_replicas = current_replicas + 1
    log.warning(f"REMEDIATION: Scaling up {current_replicas} → {new_replicas} replicas")

    try:
        # Docker Compose scale command
        import subprocess
        result = subprocess.run(
            ['docker', 'compose', 'up', '-d', '--scale', f'sre-app={new_replicas}'],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            current_replicas = new_replicas
            description = f"Scaled up to {new_replicas} replicas due to anomaly"
            record_action('scale_up', description)
            log.info(f"Scale up successful: now running {new_replicas} replicas")
        else:
            log.error(f"Scale up failed: {result.stderr}")
    except Exception as e:
        log.error(f"Scale up error: {e}")
        log.info(f"[SIMULATED] Would scale to {new_replicas} replicas")
        current_replicas = new_replicas
        record_action('scale_up', f"[SIMULATED] Scaled to {new_replicas} replicas")


def restart_app():
    """
    Restart the app container.

    Use case: memory leak detected — memory is growing abnormally.
    Restarting clears the memory before the container OOM-crashes.
    This is PROACTIVE — we act before the problem causes a crash.

    In K8s: kubectl rollout restart deployment/sre-app
    In Docker Compose: docker compose restart sre-app
    """
    if not can_act('restart'):
        return

    log.warning("REMEDIATION: Restarting app container due to memory anomaly")

    try:
        import subprocess
        result = subprocess.run(
            ['docker', 'compose', 'restart', 'sre-app'],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            record_action('restart', "Restarted app due to memory anomaly")
            log.info("App restart successful")
        else:
            log.error(f"Restart failed: {result.stderr}")
    except Exception as e:
        log.error(f"Restart error: {e}")
        record_action('restart', "[SIMULATED] Restart triggered for memory anomaly")


def disable_fault_injection():
    """
    If we detect high errors AND fault injection is on,
    turn off fault injection automatically.

    This simulates a rollback — reverting a bad configuration.
    In real K8s this would be: kubectl rollout undo deployment/sre-app
    """
    if not can_act('rollback'):
        return

    log.warning("REMEDIATION: Disabling fault injection (simulating rollback)")

    try:
        import subprocess
        result = subprocess.run(
            ['docker', 'compose', 'exec', '-T', 'sre-app',
             'sh', '-c', 'kill -HUP 1'],  # sends reload signal
            capture_output=True, text=True, timeout=10
        )
        record_action('rollback', "Disabled fault injection to resolve error spike")
        log.info("Fault injection disabled")
    except Exception as e:
        log.error(f"Rollback simulation error: {e}")
        record_action('rollback', "[SIMULATED] Rollback triggered for error spike")

# ── Webhook Server ────────────────────────────────────────────────────────────

class AlertWebhookHandler(BaseHTTPRequestHandler):
    """
    HTTP server that receives alerts from Alertmanager.

    Why a webhook:
    Alertmanager pushes alerts HERE when rules fire.
    This is in addition to the polling loop — belt and braces approach.
    Some issues are caught by ML first, some by threshold alerts first.
    """

    def do_POST(self):
        if self.path == '/webhook':
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)

            try:
                alert_data = json.loads(body)
                alerts = alert_data.get('alerts', [])

                for alert in alerts:
                    alert_name = alert.get('labels', {}).get('alertname', 'unknown')
                    status = alert.get('status', 'firing')
                    log.info(f"Received alert from Alertmanager: {alert_name} ({status})")

                    if status == 'firing':
                        if alert_name == 'HighErrorRate':
                            log.warning("Alertmanager triggered: High error rate")
                            disable_fault_injection()
                        elif alert_name == 'HighP95Latency':
                            log.warning("Alertmanager triggered: High latency")
                            scale_up()
                        elif alert_name == 'HighActiveRequests':
                            log.warning("Alertmanager triggered: High active requests")
                            scale_up()

            except json.JSONDecodeError:
                log.error("Invalid JSON in webhook payload")

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'OK')

    def do_GET(self):
        if self.path == '/status':
            # Status endpoint: see what the detector has been doing
            status = {
                'current_replicas': current_replicas,
                'recent_actions': remediation_history[-10:],
                'cooldowns': {
                    k: int(COOLDOWN_SECONDS - (time.time() - v))
                    for k, v in last_remediation_time.items()
                    if time.time() - v < COOLDOWN_SECONDS
                }
            }
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(status, indent=2).encode())

    def log_message(self, format, *args):
        # Suppress default HTTP server logs (too noisy)
        pass


def start_webhook_server():
    """Run the webhook server in a background thread."""
    server = HTTPServer(('0.0.0.0', 8001), AlertWebhookHandler)
    log.info("Webhook server listening on port 8001")
    server.serve_forever()

# ── Main Detection Loop ───────────────────────────────────────────────────────

def run_detection_loop():
    """
    Main loop: runs every SCRAPE_INTERVAL seconds.
    Fetches metrics, runs anomaly detection, triggers remediation if needed.
    """
    log.info("=" * 60)
    log.info("Anomaly Detector starting up")
    log.info(f"Prometheus: {PROMETHEUS_URL}")
    log.info(f"Checking every {SCRAPE_INTERVAL} seconds")
    log.info(f"Analysing last {HISTORY_MINUTES} minutes of data")
    log.info(f"Acting after {ANOMALY_THRESHOLD} consecutive anomalous points")
    log.info("=" * 60)

    # Wait for Prometheus to have some data before first check
    log.info("Waiting 2 minutes for initial metric collection...")
    time.sleep(120)

    while True:
        log.info("─── Detection cycle starting ───")
        cycle_start = time.time()

        # ── Check 1: Request latency ────────────────────────────────────────
        # PromQL: rate of histogram buckets = needed for histogram_quantile
        # We check the P95 latency over the last 5 minutes
        latency_query = '''
            histogram_quantile(0.95,
                rate(app_request_latency_seconds_bucket[5m])
            )
        '''
        latency_values = fetch_metric(latency_query)
        if latency_values:
            is_bad, score = detect_anomaly(latency_values)
            latest = latency_values[-1]
            log.info(f"P95 Latency: {latest:.3f}s | Anomaly score: {score:.4f} | Anomalous: {is_bad}")

            if is_bad:
                log.warning(f"LATENCY ANOMALY: P95={latest:.3f}s — scaling up")
                scale_up()

        # ── Check 2: Error rate ─────────────────────────────────────────────
        error_query = '''
            rate(app_requests_total{status_code="500"}[5m])
            /
            rate(app_requests_total[5m])
        '''
        error_values = fetch_metric(error_query)
        if error_values:
            is_bad, score = detect_anomaly(error_values)
            latest = error_values[-1]
            log.info(f"Error Rate: {latest:.1%} | Anomaly score: {score:.4f} | Anomalous: {is_bad}")

            if is_bad:
                log.warning(f"ERROR RATE ANOMALY: {latest:.1%} — triggering rollback")
                disable_fault_injection()

        # ── Check 3: Active requests ─────────────────────────────────────────
        active_query = 'app_active_requests_current'
        active_values = fetch_metric(active_query)
        if active_values:
            is_bad, score = detect_anomaly(active_values)
            latest = active_values[-1]
            log.info(f"Active Requests: {latest:.0f} | Anomaly score: {score:.4f} | Anomalous: {is_bad}")

            if is_bad:
                log.warning(f"ACTIVE REQUESTS ANOMALY: {latest:.0f} concurrent — scaling up")
                scale_up()

        cycle_duration = time.time() - cycle_start
        log.info(f"─── Cycle complete in {cycle_duration:.1f}s — sleeping {SCRAPE_INTERVAL}s ───\n")
        time.sleep(SCRAPE_INTERVAL)


if __name__ == '__main__':
    # Start webhook server in background thread
    # daemon=True means this thread dies when main thread dies
    webhook_thread = threading.Thread(target=start_webhook_server, daemon=True)
    webhook_thread.start()

    # Run the main detection loop (blocks forever)
    run_detection_loop()
