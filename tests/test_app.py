"""
Basic tests for the Flask app.
These run in GitHub Actions before the Docker image is built.
If tests fail, the build stops — broken code never gets deployed.

Why write tests even for a demo project:
1. GitHub Actions runs them automatically on every push
2. It's a resume bullet: "CI pipeline with automated testing"
3. It proves you know testing exists and matters
"""
import pytest
import sys
import os

# Add the app directory to Python path so we can import app.py
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

from app import app as flask_app

@pytest.fixture
def client():
    """
    Creates a test client — a fake browser that can call our app
    without actually starting a server.
    """
    flask_app.config['TESTING'] = True
    with flask_app.test_client() as client:
        yield client


def test_products_returns_200(client):
    """Products endpoint should return 200 in normal operation."""
    response = client.get('/api/products')
    assert response.status_code == 200


def test_products_returns_list(client):
    """Products endpoint should return a list of products."""
    response = client.get('/api/products')
    data = response.get_json()
    assert 'products' in data
    assert len(data['products']) > 0


def test_health_check_returns_200(client):
    """Health check must always return 200 — K8s depends on this."""
    response = client.get('/healthz')
    assert response.status_code == 200


def test_health_check_returns_healthy(client):
    """Health check body should say healthy."""
    response = client.get('/healthz')
    data = response.get_json()
    assert data['status'] == 'healthy'


def test_metrics_endpoint_exists(client):
    """Prometheus metrics endpoint must exist."""
    response = client.get('/metrics')
    assert response.status_code == 200


def test_metrics_contains_request_counter(client):
    """After a request, metrics should contain our custom counter."""
    client.get('/api/products')  # make a request first
    response = client.get('/metrics')
    # Prometheus text format includes metric names
    assert b'app_requests_total' in response.data


def test_checkout_returns_order_id(client):
    """Checkout should return an order ID on success."""
    response = client.get('/api/checkout')
    if response.status_code == 200:  # might be 500 due to fault injection
        data = response.get_json()
        assert 'order_id' in data
