"""Production web replica configuration tests.

These tests validate the production Docker Compose and Caddy configuration:

- local development keeps a single directly exposed `web` service;
- the production overlay declares two `web` replicas;
- `web` has no fixed `container_name`, allowing horizontal scaling;
- production removes the direct `web` host port so Caddy remains the
  only public entry point;
- Caddy uses Docker DNS-based dynamic upstream discovery and round-robin
  load balancing across healthy `web` replicas.

The checks are intentionally text-based rather than parsed with PyYAML
because docker-compose.prod.yml uses Docker Compose's `!override` YAML tag,
which generic YAML parsers do not necessarily support.
"""
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _read(relative_path: str) -> str:
    path = _REPO_ROOT / relative_path
    assert path.is_file(), f"expected {relative_path} to exist at {path}"
    return path.read_text(encoding="utf-8")


def _service_block(compose_text: str, service_name: str) -> str:
    """Returns the text of one top-level `services.<name>:` block (up to
    the next top-level-under-services key), with full-line `#` comments
    stripped out first - good enough for this file's flat, hand-written
    compose files (whose comments are always whole lines, never trailing)
    without needing a full YAML parse (see module docstring). Comments are
    stripped precisely so a comment merely *mentioning* a key/value (this
    repo's comments explain production/scaling behavior in detail, and
    quote things like `container_name` and `"8000:8000"` while explaining
    why they're absent) can never be mistaken for the real YAML content."""
    lines = [
        line for line in compose_text.splitlines()
        if not line.lstrip().startswith("#")
    ]
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^  {re.escape(service_name)}:\s*$", line):
            start = i + 1
            break
    assert start is not None, f"service {service_name!r} not found"
    end = len(lines)
    for i in range(start, len(lines)):
        if re.match(r"^  \S", lines[i]) or re.match(r"^\S", lines[i]):
            end = i
            break
    return "\n".join(lines[start:end])


# --- Base/local compose: unaffected by this work ---


def test_base_compose_web_service_has_no_scale_key():
    """Plain `docker compose up` (base file only) must not force multiple
    web replicas - the two-replica default is production-only."""
    block = _service_block(_read("docker-compose.yml"), "web")
    assert not re.search(r"^\s*scale:", block, re.MULTILINE)


def test_base_compose_web_service_still_publishes_local_dev_port():
    """Base file keeps web's direct host port for local/dev use - only the
    production overlay removes it (see docker-compose.prod.yml's own
    `ports: !override []`)."""
    block = _service_block(_read("docker-compose.yml"), "web")
    assert '"8000:8000"' in block


def test_base_compose_web_service_has_no_container_name():
    """A fixed container_name structurally blocks scaling web to N>1 - the
    base file must keep leaving it unset (unlike every other service)."""
    block = _service_block(_read("docker-compose.yml"), "web")
    assert "container_name" not in block


# --- Production overlay: declares the 2-replica default ---


def test_prod_compose_declares_web_scale_2():
    block = _service_block(_read("docker-compose.prod.yml"), "web")
    assert re.search(r"^\s*scale:\s*2\s*$", block, re.MULTILINE), block


def test_prod_compose_web_service_still_has_no_container_name():
    block = _service_block(_read("docker-compose.prod.yml"), "web")
    assert "container_name" not in block


def test_prod_compose_web_service_still_has_no_published_port():
    """`ports: !override []` must still fully remove web's host port in
    production - Caddy remains the only public entry point."""
    block = _service_block(_read("docker-compose.prod.yml"), "web")
    assert "ports: !override []" in block
    # No literal host:container port mapping anywhere in web's own block.
    assert not re.search(r'"\d+:\d+"', block)


def test_prod_compose_only_caddy_declares_host_ports():
    prod = _read("docker-compose.prod.yml")
    for service_name in ("web", "recommendation", "llm", "catalog", "mongo",
                          "ollama", "redis", "seed"):
        block = _service_block(prod, service_name)
        assert not re.search(r'"\d+:\d+"', block), (
            f"unexpected host-published port on service {service_name!r}"
        )
    caddy_block = _service_block(prod, "caddy")
    assert '"80:80"' in caddy_block
    assert '"443:443"' in caddy_block


# --- Caddy's dynamic multi-replica upstream config is preserved ---


def test_caddyfile_still_uses_dynamic_a_upstream_for_load_balancing():
    """A plain `reverse_proxy web:8000` resolves the web hostname once and
    never load-balances across replicas (reproduced live) - this must
    stay Caddy's dynamic DNS-based upstream
    source, unaffected by the declarative scale change."""
    caddyfile = _read("Caddyfile")
    assert "dynamic a web 8000" in caddyfile
    assert "lb_policy round_robin" in caddyfile

