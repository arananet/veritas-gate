"""Artifact loading. The core treats every artifact as untrusted text."""

from veritas.artifacts.base import Artifact, ArtifactSegment
from veritas.artifacts.document import load_document
from veritas.artifacts.repository import load_repository

__all__ = ["Artifact", "ArtifactSegment", "load_document", "load_repository"]
