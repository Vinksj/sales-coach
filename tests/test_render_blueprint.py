"""render.yaml describes the cloud deployment docs/deploy-cloud.md documents, and would start.

The blueprint had gone stale: it asked for SALESCOACH_PASSWORD "until Google sign-in (Phase 3)" and declared none
of the Google client, the allowed domains, the token keys, the bootstrap admin or the owner role, so a cloud
process built from it refused to start. These checks read the file:

  * three services from the one Dockerfile, `serve --role web|worker|scheduler`, and a managed Postgres;
  * a pre-deploy `salescoach migrate` with DATABASE_MIGRATE_URL, which the serving process does not keep;
  * with exactly the variables a service declares (dummy values), `cli.cloud_problems(role)`, the check
    `salescoach serve` runs at a cloud start, finds nothing missing;
  * every variable docs/deploy-cloud.md's Environment table marks required for a role is declared there;
  * secrets are `sync: false`, never a value in the file; no password (cloud mode signs in with Google only).
"""
import re
from pathlib import Path

import pytest
import yaml

from salescoach import cli, identity

ROOT = Path(__file__).resolve().parent.parent
BLUEPRINT = yaml.safe_load((ROOT / "render.yaml").read_text())
SERVICES = {s["envVars"][0]["value"] if s["envVars"][0]["key"] == "SALESCOACH_ROLE" else None: s
            for s in BLUEPRINT["services"]}
ROLES = ("web", "worker", "scheduler")
PROVIDER_KEYS = {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY"}
SECRETS = {"DATABASE_URL", "SALESCOACH_SESSION_SECRET", "GOOGLE_CLIENT_SECRET", "SALESCOACH_TOKEN_KEYS"} | PROVIDER_KEYS
# What the launch asked for beyond the startup check (the task list and the docs' launch checklist).
ALSO = {"web": {"SALESCOACH_PUBLIC_URL", "SALESCOACH_BOOTSTRAP_ADMIN", "LLM_BUDGET_ORG_USD_DAY", "LLM_BUDGET_USER_USD_DAY"},
        "worker": {"WORKER_CONCURRENCY", "LLM_BUDGET_ORG_USD_DAY", "LLM_BUDGET_USER_USD_DAY"},
        "scheduler": set()}
TOKEN_RING = "k1:" + "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE="


def env_of(role) -> dict:
    return {v["key"]: v for v in SERVICES[role]["envVars"]}


def test_three_roles_from_one_dockerfile_and_a_managed_postgres():
    assert set(SERVICES) == set(ROLES)
    assert [d["name"] for d in BLUEPRINT["databases"]] == ["salescoach-db"]
    for role, svc in SERVICES.items():
        assert svc["runtime"] == "docker" and svc["dockerfilePath"] == "./Dockerfile"
        assert f"salescoach serve --role {role}" in svc["dockerCommand"]
        assert env_of(role)["SALESCOACH_MODE"]["value"] == "cloud"
    assert SERVICES["web"]["type"] == "web" and SERVICES["web"]["healthCheckPath"] == "/health"


def test_the_owner_role_migrates_before_deploy_and_no_serving_process_keeps_it():
    web = SERVICES["web"]
    assert web["preDeployCommand"].strip() == "salescoach migrate"
    assert env_of("web")["DATABASE_MIGRATE_URL"]["fromDatabase"]["name"] == "salescoach-db"
    assert re.match(r"sh -c 'unset DATABASE_MIGRATE_URL; exec salescoach serve ", web["dockerCommand"])
    for role in ("worker", "scheduler"):
        assert "DATABASE_MIGRATE_URL" not in env_of(role)
    for role in ROLES:                                   # the app role, entered by hand, never the database owner
        assert env_of(role)["DATABASE_URL"] == {"key": "DATABASE_URL", "sync": False}


@pytest.mark.parametrize("role", ROLES)
def test_a_service_declares_everything_the_cloud_start_check_needs(role, monkeypatch):
    names = set(env_of(role))
    for name in ("SALESCOACH_SESSION_SECRET", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_ALLOWED_DOMAINS",
                 "SALESCOACH_TOKEN_KEYS", identity.MODE_ENV):
        monkeypatch.delenv(name, raising=False)
    assert cli.cloud_problems(role)                      # the check is live: with nothing set it refuses
    for name in names:
        monkeypatch.setenv(name, TOKEN_RING if name == "SALESCOACH_TOKEN_KEYS" else "x.example.test")
    assert cli.cloud_problems(role) == []
    assert names & PROVIDER_KEYS and {"DATABASE_URL", "SALESCOACH_DATA", "SALESCOACH_RUNTIME"} <= names
    assert ALSO[role] <= names


def _documented_required() -> dict:
    """{role: {variable}} from docs/deploy-cloud.md's Environment table: rows not marked `no`."""
    text = (ROOT / "docs" / "deploy-cloud.md").read_text()
    table = text.split("## Environment", 1)[1].split("\n## ", 1)[0]
    out = {role: set() for role in ROLES}
    for line in table.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4 or not cells[0].startswith("`"):
            continue
        names, where, required = re.findall(r"`([A-Z][A-Z0-9_]+)`", cells[0]), cells[1], cells[2]
        if required == "no":
            continue
        if "salescoach migrate" in where:                # the owner role: the service that runs the migrate
            out["web"].update(names)
            continue
        for role in ROLES:
            if "all three" in where or re.search(rf"\b{role}\b", where):
                out[role].update(names)
    return out


def test_every_variable_the_docs_require_is_declared_for_its_role():
    documented = _documented_required()
    assert documented["worker"] >= {"SALESCOACH_TOKEN_KEYS", "GOOGLE_CLIENT_ID"}      # the table was read
    for role in ROLES:
        declared = set(env_of(role)) - {"SALESCOACH_ROLE"}
        assert documented[role] - {"SALESCOACH_ROLE"} <= declared, (role, documented[role] - declared)


def test_secrets_are_asked_for_never_written_and_there_is_no_password():
    for role in ROLES:
        for key, var in env_of(role).items():
            assert not key.startswith("SALESCOACH_PASSWORD"), f"{role}: cloud mode has no password"
            if key in SECRETS or "SECRET" in key or key.endswith("_KEYS") or key.endswith("_API_KEY"):
                assert var.get("sync") is False and "value" not in var and "generateValue" not in var, (role, key)
