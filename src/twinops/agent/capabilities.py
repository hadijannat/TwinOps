"""Semantic capability index for tool retrieval."""

from dataclasses import dataclass
from typing import Any

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from twinops.agent.schema_gen import ToolSpec
from twinops.common.logging import get_logger

logger = get_logger(__name__)


@dataclass
class CapabilityHit:
    """A tool matching a search query with relevance score."""

    tool: ToolSpec
    score: float


class CapabilityIndex:
    """
    TF-IDF based index over tool descriptions.

    This provides fast semantic search to retrieve relevant tools
    for a given user query, reducing context window usage and
    preventing hallucinated tool calls.

    For production deployments, consider replacing with:
    - Dense embeddings (sentence-transformers, OpenAI embeddings)
    - Vector database (Pinecone, Weaviate, Qdrant)
    """

    def __init__(self, tools: list[ToolSpec] | None = None):
        """
        Initialize the capability index.

        Args:
            tools: Initial list of tools to index
        """
        self._tools: list[ToolSpec] = []
        self._vectorizer = TfidfVectorizer(
            stop_words="english",
            ngram_range=(1, 2),
            max_features=1000,
        )
        # TF-IDF sparse matrix (scipy sparse type); keep as Any for typing flexibility.
        self._matrix: Any | None = None
        self._is_fitted = False

        if tools:
            self.add_tools(tools)

    def add_tools(self, tools: list[ToolSpec]) -> None:
        """
        Add tools to the index.

        Args:
            tools: List of tools to add
        """
        self._tools.extend(tools)
        self._reindex()

    def set_tools(self, tools: list[ToolSpec]) -> None:
        """
        Replace all tools in the index.

        Args:
            tools: Complete list of tools
        """
        self._tools = list(tools)
        self._reindex()

    def _reindex(self) -> None:
        """Rebuild the TF-IDF index."""
        if not self._tools:
            self._is_fitted = False
            self._matrix = None
            return

        # Build text corpus from tool metadata
        texts = []
        for tool in self._tools:
            # Combine name, description, and parameter names
            param_names = " ".join(tool.input_schema.get("properties", {}).keys())
            text = f"{tool.name} {tool.description} {param_names}"
            texts.append(text)

        self._matrix = self._vectorizer.fit_transform(texts)
        self._is_fitted = True

        logger.debug("Capability index rebuilt", tool_count=len(self._tools))

    def search(self, query: str, top_k: int = 12) -> list[CapabilityHit]:
        """
        Search for tools matching a query.

        Args:
            query: Natural language query
            top_k: Maximum number of results

        Returns:
            List of matching tools with scores, sorted by relevance
        """
        if not self._is_fitted or self._matrix is None:
            return []

        # Transform query
        try:
            q_vec = self._vectorizer.transform([query])
        except Exception:
            # Query contains only unknown terms
            return []

        # Compute cosine similarity
        scores = (self._matrix @ q_vec.T).toarray().flatten()

        # Get top-k indices
        if len(scores) <= top_k:
            indices = np.argsort(scores)[::-1]
        else:
            # Partial sort for efficiency
            indices = np.argpartition(scores, -top_k)[-top_k:]
            indices = indices[np.argsort(scores[indices])[::-1]]

        # Build results
        results = []
        for idx in indices:
            if scores[idx] > 0:
                results.append(
                    CapabilityHit(
                        tool=self._tools[idx],
                        score=float(scores[idx]),
                    )
                )

        return results

    def get_tool_by_name(self, name: str) -> ToolSpec | None:
        """
        Get a tool by exact name match.

        Args:
            name: Tool name

        Returns:
            ToolSpec or None
        """
        for tool in self._tools:
            if tool.name == name:
                return tool
        return None

    def get_all_tools(self) -> list[ToolSpec]:
        """Get all indexed tools."""
        return list(self._tools)

    @property
    def tool_count(self) -> int:
        """Number of tools in the index."""
        return len(self._tools)

    def get_tools_by_risk(self, risk_level: str) -> list[ToolSpec]:
        """
        Get all tools with a specific risk level.

        Args:
            risk_level: Risk level to filter by

        Returns:
            List of matching tools
        """
        return [t for t in self._tools if t.risk_level == risk_level]

    def get_tools_for_submodel(self, submodel_id: str) -> list[ToolSpec]:
        """
        Get all tools from a specific submodel.

        Args:
            submodel_id: Submodel identifier

        Returns:
            List of matching tools
        """
        return [t for t in self._tools if t.submodel_id == submodel_id]


class HybridCapabilityIndex(CapabilityIndex):
    """
    Enhanced index with priority boosting for certain tools.

    Useful for ensuring critical tools are always available.
    """

    def __init__(
        self,
        tools: list[ToolSpec] | None = None,
        always_include: list[str] | None = None,
    ):
        """
        Initialize hybrid index.

        Args:
            tools: Initial tools
            always_include: Tool names that should always be included in results
        """
        super().__init__(tools)
        self._always_include = set(always_include or [])

    def search(self, query: str, top_k: int = 12) -> list[CapabilityHit]:
        """
        Search with priority tool inclusion.

        Priority tools are always included at the start of results,
        followed by query-matched tools up to top_k total.
        """
        # Get priority tools
        priority_results = []
        for tool in self._tools:
            if tool.name in self._always_include:
                priority_results.append(CapabilityHit(tool=tool, score=1.0))

        # Get search results
        remaining_k = max(0, top_k - len(priority_results))
        search_results = super().search(query, top_k=remaining_k + len(priority_results))

        # Merge, avoiding duplicates
        priority_names = {r.tool.name for r in priority_results}
        filtered_search = [r for r in search_results if r.tool.name not in priority_names]

        return priority_results + filtered_search[:remaining_k]
