# Copyright (c) Microsoft. All rights reserved.

import base64
import json
import os
import socket
from pathlib import Path
from urllib.parse import urlparse

from agent_framework import Agent, tool
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from dotenv import load_dotenv

# Shared credential reused by the agent and the whoami tool so the reported
# identity matches the one used to call Foundry / Azure services.
_credential = DefaultAzureCredential()

# Load environment variables from .env file
load_dotenv(override=True)


def _is_valid_appinsights_connection_string(value: str | None) -> bool:
    if not value:
        return False

    pairs = [item for item in value.split(";") if item.strip()]
    if not pairs:
        return False

    # Every segment must follow key=value format.
    return all("=" in item for item in pairs)


def _sanitize_invalid_appinsights_connection_string() -> None:
    conn_str = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if conn_str and not _is_valid_appinsights_connection_string(conn_str):
        # Avoid exporter initialization crashes caused by malformed values.
        os.environ.pop("APPLICATIONINSIGHTS_CONNECTION_STRING", None)
        print(
            "WARNING: Ignoring malformed APPLICATIONINSIGHTS_CONNECTION_STRING "
            "to prevent telemetry exporter initialization failures."
        )


def _read_project_id_from_foundry_deployment() -> str | None:
    deployment_file = Path(".foundry/.deployment.json")
    if not deployment_file.exists():
        return None

    try:
        payload = json.loads(deployment_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    project_id = payload.get("projectId")
    return str(project_id).strip() if project_id else None


def _normalize_project_endpoint() -> str:
    endpoint = os.environ["AZURE_AI_PROJECT_ENDPOINT"].strip().rstrip("/")
    if "/api/projects/" in endpoint:
        return endpoint

    parsed = urlparse(endpoint)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(
            "AZURE_AI_PROJECT_ENDPOINT must be a valid URL, such as "
            "https://<resource>.services.ai.azure.com/api/projects/<project-id>."
        )

    project_id = (
        os.getenv("AZURE_AI_PROJECT_ID")
        or os.getenv("FOUNDRY_PROJECT_ID")
        or _read_project_id_from_foundry_deployment()
    )
    if not project_id:
        raise ValueError(
            "AZURE_AI_PROJECT_ENDPOINT is missing /api/projects/<project-id> and no "
            "project ID was found in AZURE_AI_PROJECT_ID, FOUNDRY_PROJECT_ID, or "
            ".foundry/.deployment.json."
        )

    normalized = f"{parsed.scheme}://{parsed.netloc}/api/projects/{project_id}"
    print(f"INFO: Normalized AZURE_AI_PROJECT_ENDPOINT to {normalized}")
    return normalized


@tool(name="resolve_fqdn", description="Resolve a fully-qualified domain name (FQDN) to IP addresses.")
def resolve_fqdn(fqdn: str) -> str:
    host = fqdn.strip()
    if not host:
        return "Please provide a non-empty FQDN."

    try:
        _, aliases, addresses = socket.gethostbyname_ex(host)
    except socket.gaierror as exc:
        return f"DNS resolution failed for '{host}': {exc}"

    unique_addresses = sorted(set(addresses))
    alias_text = ", ".join(aliases) if aliases else "(none)"
    ip_text = ", ".join(unique_addresses) if unique_addresses else "(none)"
    return f"FQDN: {host}\nAliases: {alias_text}\nIP addresses: {ip_text}"


def _decode_jwt_claims(token: str) -> dict:
    try:
        payload_segment = token.split(".")[1]
        padding = "=" * (-len(payload_segment) % 4)
        decoded = base64.urlsafe_b64decode(payload_segment + padding)
        return json.loads(decoded)
    except Exception:  # noqa: BLE001 - claims are best-effort only
        return {}


@tool(name="whoami", description="Return the Entra ID identity used by this agent to call Azure services.")
def whoami() -> str:
    try:
        access_token = _credential.get_token("https://management.azure.com/.default")
    except Exception as exc:  # noqa: BLE001
        return f"Failed to acquire token: {exc}"

    claims = _decode_jwt_claims(access_token.token)
    fields = {
        "upn": claims.get("upn") or claims.get("preferred_username") or claims.get("unique_name"),
        "name": claims.get("name"),
        "oid": claims.get("oid"),
        "appid": claims.get("appid") or claims.get("azp"),
        "tid": claims.get("tid"),
        "idtyp": claims.get("idtyp"),
        "iss": claims.get("iss"),
    }
    fields = {k: v for k, v in fields.items() if v}
    if not fields:
        return "Token acquired but no identity claims could be decoded."
    return "Agent identity:\n" + "\n".join(f"- {k}: {v}" for k, v in fields.items())


@tool(
    name="read_keyvault_secret",
    description=(
        "Read a secret from Azure Key Vault using the agent's own identity. "
        "Input must be the full secret URL, e.g. "
        "https://<vault-name>.vault.azure.net/secrets/<secret-name> "
        "or https://<vault-name>.vault.azure.net/secrets/<secret-name>/<version>."
    ),
)
def read_keyvault_secret(secret_url: str) -> str:
    url = (secret_url or "").strip()
    if not url:
        return "Please provide the full Key Vault secret URL."

    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        return "Invalid secret URL: must start with https:// and include the vault host."

    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2 or parts[0] != "secrets":
        return (
            "Invalid secret URL: expected path /secrets/<name> or "
            "/secrets/<name>/<version>."
        )

    vault_url = f"{parsed.scheme}://{parsed.netloc}"
    secret_name = parts[1]
    version = parts[2] if len(parts) >= 3 else None

    try:
        client = SecretClient(vault_url=vault_url, credential=_credential)
        secret = client.get_secret(secret_name, version=version)
    except Exception as exc:  # noqa: BLE001
        return f"Failed to read secret '{secret_name}' from {vault_url}: {exc}"

    return (
        f"Secret '{secret.name}' (version {secret.properties.version}) "
        f"from {vault_url}:\nvalue: {secret.value}"
    )


def main():
    _sanitize_invalid_appinsights_connection_string()

    client = FoundryChatClient(
        project_endpoint=_normalize_project_endpoint(),
        model=os.environ["MODEL_DEPLOYMENT_NAME"],
        credential=_credential,
    )

    agent = Agent(
        client=client,
        instructions=(
            "You are a friendly assistant. Keep your answers brief. "
            "When users ask to resolve an FQDN, call the resolve_fqdn tool. "
            "When users ask who you are, which identity you use, or to show "
            "your service principal/user, call the whoami tool. "
            "When users ask to read a Key Vault secret and provide a full "
            "https://<vault>.vault.azure.net/secrets/... URL, call the "
            "read_keyvault_secret tool."
        ),
        tools=[resolve_fqdn, whoami, read_keyvault_secret],
        # History will be managed by the hosting infrastructure, thus there
        # is no need to store history by the service. Learn more at:
        # https://developers.openai.com/api/reference/resources/responses/methods/create
        default_options={"store": False},
    )

    server = ResponsesHostServer(agent)
    server.run()


if __name__ == "__main__":
    main()
