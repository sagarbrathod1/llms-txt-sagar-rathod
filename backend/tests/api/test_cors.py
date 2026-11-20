"""Which browser origins may call the API.

Uses the regex from fly.toml itself, so these tests check what is actually deployed. Each test
applies the API's CORS setup to a throwaway app, which needs no database.
"""

import tomllib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.main import add_cors
from app.config import Settings

FLY_ENV = tomllib.loads((Path(__file__).parents[2] / "fly.toml").read_text())["env"]
PRODUCTION = "https://llms-txt-sagar-rathod.vercel.app"


def client_for(**settings) -> TestClient:
    # _env_file=None: a developer's local .env must not change what these tests see.
    app = FastAPI()
    app.get("/ping")(lambda: "pong")
    add_cors(app, Settings(_env_file=None, **settings))
    return TestClient(app)


def allowed(client: TestClient, origin: str) -> bool:
    response = client.get("/ping", headers={"Origin": origin})
    return response.headers.get("access-control-allow-origin") == origin


@pytest.fixture
def production() -> TestClient:
    return client_for(
        cors_origins=FLY_ENV["CORS_ORIGINS"], cors_origin_regex=FLY_ENV["CORS_ORIGIN_REGEX"]
    )


def test_production_origin_is_allowed(production):
    assert allowed(production, PRODUCTION)


@pytest.mark.parametrize(
    "origin",
    [
        # Per deployment: a 9-character hash.
        "https://llms-txt-sagar-rathod-k3j9x2m4q-sagarbrathod1s-projects.vercel.app",
        # Per branch, including branch names with hyphens.
        "https://llms-txt-sagar-rathod-git-main-sagarbrathod1s-projects.vercel.app",
        "https://llms-txt-sagar-rathod-git-ci-deploy-sagarbrathod1s-projects.vercel.app",
    ],
)
def test_this_projects_preview_deployments_are_allowed(production, origin):
    assert allowed(production, origin)


@pytest.mark.parametrize(
    "origin",
    [
        # Other projects, and other accounts' projects.
        "https://other-project-k3j9x2m4q-sagarbrathod1s-projects.vercel.app",
        "https://llms-txt-sagar-rathod-k3j9x2m4q-someone-elses-projects.vercel.app",
        "https://evil.vercel.app",
        # Lookalikes: the allowed name as a prefix of another domain, or with extra labels.
        "https://llms-txt-sagar-rathod-k3j9x2m4q-sagarbrathod1s-projects.vercel.app.evil.com",
        "https://evil.com/llms-txt-sagar-rathod-k3j9x2m4q-sagarbrathod1s-projects.vercel.app",
        "https://x.llms-txt-sagar-rathod-k3j9x2m4q-sagarbrathod1s-projects.vercel.app",
        "https://llms-txt-sagar-rathod-k3j9x2m4q-sagarbrathod1s-projects-vercel.app",
        # Plain HTTP, a port, and an uppercase hash are not URLs Vercel generates.
        "http://llms-txt-sagar-rathod-k3j9x2m4q-sagarbrathod1s-projects.vercel.app",
        "https://llms-txt-sagar-rathod-k3j9x2m4q-sagarbrathod1s-projects.vercel.app:8443",
        "https://llms-txt-sagar-rathod-K3J9X2M4Q-sagarbrathod1s-projects.vercel.app",
        # A deployment hash of the wrong length.
        "https://llms-txt-sagar-rathod-abc-sagarbrathod1s-projects.vercel.app",
    ],
)
def test_other_origins_are_rejected(production, origin):
    assert not allowed(production, origin)


def test_without_a_regex_only_the_exact_list_is_allowed():
    client = client_for(cors_origins=f"{PRODUCTION}, http://localhost:3000")
    assert allowed(client, PRODUCTION)
    assert allowed(client, "http://localhost:3000")
    assert not allowed(
        client, "https://llms-txt-sagar-rathod-k3j9x2m4q-sagarbrathod1s-projects.vercel.app"
    )


def test_preflight_for_a_preview_origin_succeeds(production):
    # The web app's POST /sites sends JSON, so the browser asks first with OPTIONS.
    origin = "https://llms-txt-sagar-rathod-git-main-sagarbrathod1s-projects.vercel.app"
    response = production.options(
        "/ping",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin


def test_local_dev_works_with_no_configuration():
    # A fresh clone has no .env: the Next.js dev server's origin must be allowed by default.
    assert allowed(client_for(), "http://localhost:3000")


def test_an_invalid_regex_fails_at_startup():
    with pytest.raises(ValidationError, match="not a valid regex"):
        Settings(_env_file=None, cors_origin_regex="(unclosed")
