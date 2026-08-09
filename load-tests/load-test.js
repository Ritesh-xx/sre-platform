/**
 * k6 Load Test
 *
 * Install k6: sudo apt-get install k6
 * Or: sudo snap install k6
 * Run: k6 run load-tests/load-test.js
 *
 * What each stage does:
 * Stage 1 (2 min, 0→10 users):  Warm up. Everything should be healthy.
 * Stage 2 (5 min, 10 users):    Steady state. Establish baseline metrics in Prometheus.
 * Stage 3 (1 min, 10→100 users): Ramp up. Watch latency start to climb.
 * Stage 4 (3 min, 100 users):   Sustained load. Anomaly detector should notice.
 * Stage 5 (1 min, 100→300 users): Spike. Triggers scaling.
 * Stage 6 (2 min, 300 users):   Hold spike. Watch auto-scale react.
 * Stage 7 (3 min, 300→0 users): Cool down. Watch replica count stabilise.
 *
 * thresholds: if these are violated, k6 exits with error code 1
 * This means GitHub Actions can fail the test if SLOs are breached.
 */

import http from 'k6/http';
import { sleep, check, group } from 'k6';
import { Rate, Trend, Counter } from 'k6/metrics';

// Custom metrics — appear in k6 summary output
const errorRate      = new Rate('sre_error_rate');
const productLatency = new Trend('sre_product_latency_ms');
const checkoutLatency = new Trend('sre_checkout_latency_ms');
const totalRequests  = new Counter('sre_total_requests');

export let options = {
  stages: [
    { duration: '2m', target: 10  },
    { duration: '5m', target: 10  },
    { duration: '1m', target: 100 },
    { duration: '3m', target: 100 },
    { duration: '1m', target: 300 },
    { duration: '2m', target: 300 },
    { duration: '3m', target: 0   },
  ],
  thresholds: {
    // SLO 1: 95% of requests must complete under 1 second
    'http_req_duration': ['p(95)<1000'],
    // SLO 2: Error rate must stay below 5%
    'sre_error_rate': ['rate<0.05'],
  },
};

const BASE_URL = __ENV.BASE_URL || 'http://localhost:5000';

export default function() {
  totalRequests.add(1);

  // 70% of users browse products, 30% checkout
  // This simulates realistic e-commerce traffic patterns
  if (Math.random() < 0.7) {
    group('Browse Products', () => {
      const res = http.get(`${BASE_URL}/api/products`, {
        tags: { endpoint: 'products' }
      });

      productLatency.add(res.timings.duration);
      errorRate.add(res.status >= 500);

      check(res, {
        'products status 200': (r) => r.status === 200,
        'products has data':   (r) => r.json('products') !== undefined,
        'products under 2s':   (r) => r.timings.duration < 2000,
      });
    });
  } else {
    group('Checkout', () => {
      const res = http.get(`${BASE_URL}/api/checkout`, {
        tags: { endpoint: 'checkout' }
      });

      checkoutLatency.add(res.timings.duration);
      errorRate.add(res.status >= 500);

      check(res, {
        'checkout status 2xx': (r) => r.status < 500,
        'checkout under 3s':   (r) => r.timings.duration < 3000,
      });
    });
  }

  // Think time: real users don't hammer endpoints instantly
  // 1 second between requests per virtual user
  sleep(1);
}

// Called once after all virtual users finish
// Prints a summary of what happened
export function handleSummary(data) {
  console.log('\n=== SRE Platform Load Test Summary ===');
  console.log(`Total requests: ${data.metrics.sre_total_requests.values.count}`);
  console.log(`Error rate: ${(data.metrics.sre_error_rate.values.rate * 100).toFixed(2)}%`);
  console.log(`Product P95 latency: ${data.metrics.sre_product_latency_ms.values['p(95)'].toFixed(0)}ms`);
  console.log(`Checkout P95 latency: ${data.metrics.sre_checkout_latency_ms.values['p(95)'].toFixed(0)}ms`);

  return {
    'load-test-results.json': JSON.stringify(data, null, 2),
  };
}
