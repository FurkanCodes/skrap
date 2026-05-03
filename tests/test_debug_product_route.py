from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_debug_test_product_route_renders_default_product_page() -> None:
    client = TestClient(app)

    response = client.get("/debug/test-product")

    assert response.status_code == 200
    assert "Skrap Test Product" in response.text
    assert "9999.00 TRY" in response.text
    assert "In Stock" in response.text
    assert "https://schema.org/InStock" in response.text


def test_debug_test_product_route_respects_query_parameters() -> None:
    client = TestClient(app)

    response = client.get(
        "/debug/test-product",
        params={
            "name": "Route Test Product",
            "sku": "ROUTE-001",
            "price": "1234.56",
            "currency": "USD",
            "stock": "out_of_stock",
            "include_noise": "false",
        },
    )

    assert response.status_code == 200
    assert "Route Test Product" in response.text
    assert "ROUTE-001" in response.text
    assert "1234.56 USD" in response.text
    assert "Currently unavailable" in response.text
    assert "https://schema.org/OutOfStock" in response.text
    assert "Intentional noise block" not in response.text
