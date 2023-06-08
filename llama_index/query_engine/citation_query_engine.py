from typing import Any, Dict, List, Optional, Sequence

from llama_index.callbacks.schema import CBEventType
from llama_index.callbacks.base import CallbackManager
from llama_index.data_structs.node import NodeWithScore, Node
from llama_index.indices.base_retriever import BaseRetriever
from llama_index.indices.postprocessor.types import BaseNodePostprocessor
from llama_index.indices.query.response_synthesis import ResponseSynthesizer
from llama_index.indices.query.schema import QueryBundle
from llama_index.indices.response.type import ResponseMode
from llama_index.indices.service_context import ServiceContext
from llama_index.langchain_helpers.text_splitter import TokenTextSplitter
from llama_index.query_engine import RetrieverQueryEngine
from llama_index.optimization.optimizer import BaseTokenUsageOptimizer
from llama_index.prompts.base import Prompt
from llama_index.prompts.prompts import (
    QuestionAnswerPrompt,
    RefinePrompt,
    SimpleInputPrompt,
)
from llama_index.response.schema import RESPONSE_TYPE


CITATION_QA_TEMPLATE = Prompt(
    "Please provide an answer based only on the given sources. "
    "When referencing information from a source, cite the appropriate source(s). "
    "For example:\n"
    "Source 1:\n"
    "The sky is red.\n"
    "Source 2:\n"
    "Water is wet.\n"
    "Query: What color is the sky?\n"
    "Answer: The sky is red [1].\n"
    "Now it's your turn. Below are several numbered sources of information:"
    "\n------\n"
    "{context_str}"
    "\n------\n"
    "Query: {query_str}\n"
    "Answer: "
)

CITATION_REFINE_TEMPLATE = Prompt(
    "Please provide an answer based only on the given sources. "
    "When referencing information from a source, cite the appropriate source(s). "
    "For example:\n"
    "Source 1:\n"
    "The sky is red.\n"
    "Source 2:\n"
    "Water is wet.\n"
    "Query: What color is the sky?\n"
    "Answer: The sky is red [1].\n"
    "Now it's your turn. "
    "We have provided an existing answer: {existing_answer}"
    "Below are several numbered sources of information. Use them to refine the existing answer. "
    "If the provided sources are not helpful, repeat the exsting answer.\n"
    "Begin!"
    "\n------\n"
    "{context_msg}"
    "\n------\n"
    "Query: {query_str}\n"
    "Answer: "
)


class CitaitonQueryEngine(RetrieverQueryEngine):
    """Citation query engine."""

    @classmethod
    def from_args(
        cls,
        retriever: BaseRetriever,
        service_context: Optional[ServiceContext] = None,
        node_postprocessors: Optional[List[BaseNodePostprocessor]] = None,
        verbose: bool = False,
        # response synthesizer args
        response_mode: ResponseMode = ResponseMode.COMPACT,
        text_qa_template: Optional[QuestionAnswerPrompt] = CITATION_QA_TEMPLATE,
        refine_template: Optional[RefinePrompt] = CITATION_REFINE_TEMPLATE,
        simple_template: Optional[SimpleInputPrompt] = None,
        response_kwargs: Optional[Dict] = None,
        use_async: bool = False,
        streaming: bool = False,
        optimizer: Optional[BaseTokenUsageOptimizer] = None,
        # class-specific args
        **kwargs: Any,
    ) -> "RetrieverQueryEngine":
        """Initialize a RetrieverQueryEngine object."

        Args:
            retriever (BaseRetriever): A retriever object.
            service_context (Optional[ServiceContext]): A ServiceContext object.
            node_postprocessors (Optional[List[BaseNodePostprocessor]]): A list of
                node postprocessors.
            verbose (bool): Whether to print out debug info.
            response_mode (ResponseMode): A ResponseMode object.
            text_qa_template (Optional[QuestionAnswerPrompt]): A QuestionAnswerPrompt
                object.
            refine_template (Optional[RefinePrompt]): A RefinePrompt object.
            simple_template (Optional[SimpleInputPrompt]): A SimpleInputPrompt object.
            response_kwargs (Optional[Dict]): A dict of response kwargs.
            use_async (bool): Whether to use async.
            streaming (bool): Whether to use streaming.
            optimizer (Optional[BaseTokenUsageOptimizer]): A BaseTokenUsageOptimizer
                object.

        """
        response_synthesizer = ResponseSynthesizer.from_args(
            service_context=service_context,
            text_qa_template=text_qa_template,
            refine_template=refine_template,
            simple_template=simple_template,
            response_mode=response_mode,
            response_kwargs=response_kwargs,
            use_async=use_async,
            streaming=streaming,
            optimizer=optimizer,
            node_postprocessors=node_postprocessors,
            verbose=verbose,
        )

        callback_manager = (
            service_context.callback_manager if service_context else CallbackManager([])
        )

        return cls(
            retriever=retriever,
            response_synthesizer=response_synthesizer,
            callback_manager=callback_manager,
        )

    def _create_citation_nodes(self, nodes: List[NodeWithScore]) -> List[NodeWithScore]:
        """Modify retrieved nodes to be granular sources."""
        text_splitter = TokenTextSplitter(chunk_size=256, chunk_overlap=20)

        new_nodes = []
        for node in nodes:
            text_chunks = text_splitter.split_text_with_overlaps(node.node.text)
            for chunk in text_chunks:
                text = f"Source {len(new_nodes)+1}:\n{chunk.text_chunk.strip()}\n"
                new_nodes.append(
                    NodeWithScore(
                        node=Node(
                            text=text,
                            extra_info=node.node.extra_info,
                            relationships=node.node.relationships,
                        ),
                        score=node.score,
                    )
                )
        return new_nodes

    def synthesize(
        self,
        query_bundle: QueryBundle,
        nodes: List[NodeWithScore],
        additional_source_nodes: Optional[Sequence[NodeWithScore]] = None,
    ) -> RESPONSE_TYPE:
        nodes = self._create_citation_nodes(nodes)
        response = self._response_synthesizer.synthesize(
            query_bundle=query_bundle,
            nodes=nodes,
            additional_source_nodes=additional_source_nodes,
        )
        return response

    async def asynthesize(
        self,
        query_bundle: QueryBundle,
        nodes: List[NodeWithScore],
        additional_source_nodes: Optional[Sequence[NodeWithScore]] = None,
    ) -> RESPONSE_TYPE:
        nodes = self._create_citation_nodes(nodes)
        return await self._response_synthesizer.asynthesize(
            query_bundle=query_bundle,
            nodes=nodes,
            additional_source_nodes=additional_source_nodes,
        )

    def _query(self, query_bundle: QueryBundle) -> RESPONSE_TYPE:
        """Answer a query."""
        query_id = self.callback_manager.on_event_start(
            CBEventType.QUERY, payload={"query_str": query_bundle.query_str}
        )

        retrieve_id = self.callback_manager.on_event_start(CBEventType.RETRIEVE)
        nodes = self._retriever.retrieve(query_bundle)
        nodes = self._create_citation_nodes(nodes)
        self.callback_manager.on_event_end(
            CBEventType.RETRIEVE, payload={"nodes": nodes}, event_id=retrieve_id
        )

        response = self._response_synthesizer.synthesize(
            query_bundle=query_bundle,
            nodes=nodes,
        )

        self.callback_manager.on_event_end(
            CBEventType.QUERY,
            payload={"response": response},
            event_id=query_id,
        )
        return response

    async def _aquery(self, query_bundle: QueryBundle) -> RESPONSE_TYPE:
        """Answer a query."""
        query_id = self.callback_manager.on_event_start(
            CBEventType.QUERY, payload={"query_str": query_bundle.query_str}
        )

        retrieve_id = self.callback_manager.on_event_start(CBEventType.RETRIEVE)
        nodes = self._retriever.retrieve(query_bundle)
        nodes = self._create_citation_nodes(nodes)
        self.callback_manager.on_event_end(
            CBEventType.RETRIEVE, payload={"nodes": nodes}, event_id=retrieve_id
        )

        response = await self._response_synthesizer.asynthesize(
            query_bundle=query_bundle,
            nodes=nodes,
        )

        self.callback_manager.on_event_end(
            CBEventType.QUERY,
            payload={"response": response},
            event_id=query_id,
        )
        return response