"""BaSyx V2 MQTT topic encoding and decoding utilities."""

import base64
from dataclasses import dataclass
from enum import Enum
from typing import NamedTuple


class EventType(str, Enum):
    """BaSyx event types."""

    CREATED = "created"
    UPDATED = "updated"
    DELETED = "deleted"


class RepositoryType(str, Enum):
    """BaSyx repository types."""

    AAS = "aas-repository"
    SUBMODEL = "submodel-repository"


@dataclass(frozen=True)
class ParsedTopic:
    """Parsed BaSyx MQTT topic."""

    repository_type: RepositoryType
    repo_id: str
    event_type: EventType
    entity_id: str | None = None
    element_path: str | None = None


class TopicSubscription(NamedTuple):
    """MQTT topic subscription with QoS."""

    topic: str
    qos: int = 0


def b64url_encode_nopad(text: str) -> str:
    """
    Encode text to Base64 URL-safe without padding.

    BaSyx encodes IDs in topic paths using this format.

    Args:
        text: Plain text to encode

    Returns:
        Base64 URL-safe encoded string without padding
    """
    raw = text.encode("utf-8")
    enc = base64.urlsafe_b64encode(raw).decode("ascii")
    return enc.rstrip("=")


def b64url_decode_nopad(text: str) -> str:
    """
    Decode Base64 URL-safe string without padding.

    Args:
        text: Base64 encoded string

    Returns:
        Decoded plain text
    """
    # Add padding back
    pad = "=" * ((4 - (len(text) % 4)) % 4)
    raw = base64.urlsafe_b64decode((text + pad).encode("ascii"))
    return raw.decode("utf-8")


def build_aas_subscriptions(repo_id: str) -> list[TopicSubscription]:
    """
    Build MQTT subscription topics for AAS repository events.

    Args:
        repo_id: Repository identifier

    Returns:
        List of topic subscriptions
    """
    return [
        TopicSubscription(f"aas-repository/{repo_id}/shells/#", 0),
    ]


def build_submodel_subscriptions(repo_id: str) -> list[TopicSubscription]:
    """
    Build MQTT subscription topics for Submodel repository events.

    Args:
        repo_id: Repository identifier

    Returns:
        List of topic subscriptions
    """
    return [
        TopicSubscription(f"submodel-repository/{repo_id}/submodels/#", 0),
    ]


def build_all_subscriptions(repo_id: str) -> list[TopicSubscription]:
    """
    Build all MQTT subscription topics for a repository.

    Note: Use build_subscriptions_split() for separate AAS and Submodel repo IDs.

    Args:
        repo_id: Repository identifier (used for both AAS and Submodel repos)

    Returns:
        List of all topic subscriptions
    """
    return build_aas_subscriptions(repo_id) + build_submodel_subscriptions(repo_id)


def build_subscriptions_split(
    aas_repo_id: str,
    submodel_repo_id: str,
) -> list[TopicSubscription]:
    """
    Build MQTT subscription topics with separate repo IDs for AAS and Submodel repositories.

    Real BaSyx deployments often have separate repository IDs for AAS and Submodel
    repositories. This function allows subscribing to events from both.

    Args:
        aas_repo_id: Repository ID for AAS repository events
        submodel_repo_id: Repository ID for Submodel repository events

    Returns:
        List of all topic subscriptions for both repositories
    """
    return build_aas_subscriptions(aas_repo_id) + build_submodel_subscriptions(submodel_repo_id)


def parse_topic(topic: str) -> ParsedTopic | None:
    """
    Parse a BaSyx MQTT topic into its components.

    Topic formats:
    - aas-repository/{repoId}/shells/created
    - aas-repository/{repoId}/shells/{aasIdBase64}/updated
    - submodel-repository/{repoId}/submodels/{smIdBase64}/submodelElements/{path}/updated

    Args:
        topic: MQTT topic string

    Returns:
        ParsedTopic or None if topic doesn't match expected format
    """
    parts = topic.split("/")
    if len(parts) < 4:
        return None

    # Determine repository type
    try:
        repo_type = RepositoryType(parts[0])
    except ValueError:
        return None

    repo_id = parts[1]

    # Skip the entity collection name (shells/submodels)
    if len(parts) == 4:
        # Collection-level event: aas-repository/{repoId}/shells/created
        try:
            event_type = EventType(parts[3])
            return ParsedTopic(
                repository_type=repo_type,
                repo_id=repo_id,
                event_type=event_type,
            )
        except ValueError:
            return None

    # Entity-specific event
    entity_id_encoded = parts[3]
    try:
        entity_id = b64url_decode_nopad(entity_id_encoded)
    except Exception:
        entity_id = entity_id_encoded  # Keep as-is if decode fails

    # Check for submodelElements path
    element_path: str | None = None
    event_index = 4

    if len(parts) > 5 and parts[4] == "submodelElements":
        # Find the event type (last part)
        event_index = len(parts) - 1
        # Everything between submodelElements and event is the path
        element_path = "/".join(parts[5:event_index])

    try:
        event_type = EventType(parts[event_index])
    except ValueError:
        return None

    return ParsedTopic(
        repository_type=repo_type,
        repo_id=repo_id,
        event_type=event_type,
        entity_id=entity_id,
        element_path=element_path,
    )


def build_element_update_topic(
    repo_id: str,
    submodel_id: str,
    element_path: str,
) -> str:
    """
    Build an MQTT topic for a specific submodel element update.

    Args:
        repo_id: Repository identifier
        submodel_id: Submodel identifier
        element_path: idShort path to the element

    Returns:
        MQTT topic string
    """
    sm_encoded = b64url_encode_nopad(submodel_id)
    return f"submodel-repository/{repo_id}/submodels/{sm_encoded}/submodelElements/{element_path}/updated"
