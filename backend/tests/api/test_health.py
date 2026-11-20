from app.api.main import app


class PoolThatMustNotBeUsed:
    def connection(self):
        raise AssertionError("/health touched the database")


def test_health_answers_without_touching_the_database(client):
    # The host polls /health constantly; a query here would keep the database from suspending.
    real_pool = app.state.pool
    app.state.pool = PoolThatMustNotBeUsed()
    try:
        response = client.get("/health")
    finally:
        app.state.pool = real_pool  # so the lifespan closes the real one
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
