"""Every published port in docker-compose.yml stays on loopback.

The proxy port was locked to 127.0.0.1 because an LLM proxy fronting the
operator's provider credentials has no business listening on a coffee-shop
network. The same argument applies, harder, to the two datastores sitting
beside it: Qdrant ships with no authentication at all and holds the embeddings
of stored conversation content, and Neo4j holds the graph half of the same
data. Locking one and leaving the others is not a security posture.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _published_ports(service: dict) -> list[str]:
    return [p for p in service.get("ports", []) if isinstance(p, str)]


def test_every_published_port_binds_loopback(compose: dict) -> None:
    offenders: list[str] = []
    for name, service in compose.get("services", {}).items():
        for mapping in _published_ports(service):
            # "127.0.0.1:6333:6333" is bound; "6333:6333" is not.
            if not mapping.startswith("127.0.0.1:"):
                offenders.append(f"{name}: {mapping}")
    assert offenders == [], (
        f"these compose services publish on all interfaces; prefix each with 127.0.0.1: {offenders}"
    )


def test_no_service_is_left_without_an_explicit_bind(compose: dict) -> None:
    """A port published as an int (6333) cannot carry a bind address."""
    offenders = [
        f"{name}: {p}"
        for name, service in compose.get("services", {}).items()
        for p in service.get("ports", [])
        if not isinstance(p, str)
    ]
    assert offenders == [], f"integer port mappings cannot bind loopback: {offenders}"


def test_neo4j_has_no_default_password(compose: dict) -> None:
    """A well-known fallback password is one forgotten override from being real."""
    neo4j = compose.get("services", {}).get("neo4j")
    if neo4j is None:
        pytest.skip("no neo4j service in compose")
    auth = [e for e in neo4j.get("environment", []) if str(e).startswith("NEO4J_AUTH=")]
    assert auth, "NEO4J_AUTH is not set at all"
    value = auth[0]
    assert ":-" not in value, (
        f"NEO4J_AUTH provides a default value ({value}); use :? so compose fails "
        "fast when it is unset instead of silently using a known password"
    )
    assert ":?" in value, f"NEO4J_AUTH should use the :? required-variable form, got {value}"
