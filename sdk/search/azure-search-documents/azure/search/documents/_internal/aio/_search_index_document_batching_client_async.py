# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for
# license information.
# --------------------------------------------------------------------------
from typing import cast, List, TYPE_CHECKING

from azure.core.tracing.decorator_async import distributed_trace_async
from azure.core.exceptions import HttpResponseError
from ._timer import Timer
from .._utils import is_retryable_status_code
from .._generated_serviceclient.aio import SearchServiceClient
from .._generated.aio import SearchIndexClient
from .._generated.models import IndexBatch, IndexingResult
from .._search_documents_error import RequestEntityTooLargeError
from .._index_documents_batch import IndexDocumentsBatch
from ..._api_versions import validate_api_version
from ..._headers_mixin import HeadersMixin
from ..._version import SDK_MONIKER

if TYPE_CHECKING:
    # pylint:disable=unused-import,ungrouped-imports
    from typing import Any
    from azure.core.credentials import AzureKeyCredential


class SearchIndexDocumentBatchingClient(HeadersMixin):
    """A client to do index document batching.

    :param endpoint: The URL endpoint of an Azure search service
    :type endpoint: str
    :param index_name: The name of the index to connect to
    :type index_name: str
    :param credential: A credential to authorize search client requests
    :type credential: ~azure.core.credentials.AzureKeyCredential
    :keyword int window: how many seconds if there is no changes that triggers auto flush.
        if window is less or equal than 0, it will disable auto flush
    :keyword int batch_size: batch size. It only takes affect when auto_flush is on
    :keyword str api_version: The Search API version to use for requests.

    .. admonition:: Example:

        .. literalinclude:: ../samples/async_samples/sample_authentication_async.py
            :start-after: [START create_search_client_with_key_async]
            :end-before: [END create_search_client_with_key_async]
            :language: python
            :dedent: 4
            :caption: Creating the SearchClient with an API key.
    """

    _ODATA_ACCEPT = "application/json;odata.metadata=none"  # type: str
    _DEFAULT_WINDOW = 0
    _DEFAULT_BATCH_SIZE = 1000

    def __init__(self, endpoint, index_name, credential, **kwargs):
        # type: (str, str, AzureKeyCredential, **Any) -> None

        api_version = kwargs.pop('api_version', None)
        validate_api_version(api_version)
        self._batch_size = kwargs.pop('batch_size', self._DEFAULT_BATCH_SIZE)
        self._window = kwargs.pop('window', self._DEFAULT_WINDOW)
        self._auto_flush = self._window > 0
        self._index_documents_batch = IndexDocumentsBatch()
        self._endpoint = endpoint  # type: str
        self._index_name = index_name  # type: str
        self._index_key = None
        self._credential = credential  # type: AzureKeyCredential
        self._client = SearchIndexClient(
            endpoint=endpoint, index_name=index_name, sdk_moniker=SDK_MONIKER, **kwargs
        )  # type: SearchIndexClient
        if self._auto_flush:
            self._timer = Timer(self._window, self.flush)

    def cleanup(self):
        # type: () -> None
        if self._auto_flush:
            self._timer.cancel()

    def __repr__(self):
        # type: () -> str
        return "<SearchClient [endpoint={}, index={}]>".format(
            repr(self._endpoint), repr(self._index_name)
        )[:1024]

    @property
    def actions(self):
        # type: () -> List[IndexAction]
        """The list of currently configured index actions in queue.
        :rtype: List[IndexAction]
        """
        return self._index_documents_batch.actions

    @property
    def batch_size(self):
        # type: () -> int
        return self._batch_size

    async def close(self):
        # type: () -> None
        """Close the :class:`~azure.search.aio.SearchClient` session.

        """
        return await self._client.close()

    async def flush(self, raise_error=False):
        # type: (bool) -> None
        """Flush the batch.
        :param bool raise_error: raise error if there are failures during flushing
            Default to False which re-queue the failed tasks and retry on next flush.
        """
        # get actions
        actions = self._index_documents_batch.dequeue_actions()
        try:
            results = self._index_documents_actions(actions=actions)
            # re-queue 207:
            if not self._index_key:
                client = SearchServiceClient(self._endpoint)
                kwargs = {"headers": self._merge_client_headers({})}
                result = await client.indexes.get(self._index_name, **kwargs)
                if not result:
                    # Cannot find the index
                    self._index_key = ""
                else:
                    for field in result.fields:
                        if field.key:
                            self._index_key = field.name
                            break

            for result in results:
                if is_retryable_status_code(result.status_code):
                    requeue = [x for x in actions if x.get(self._index_key) == result.key]
                    self._index_documents_batch.enqueue_actions(requeue)

            if raise_error:
                raise HttpResponseError(message="Some actions failed. Failed actions are re-queued.")

        except Exception:  # pylint: disable=broad-except
            # Do we want to re-queue these failures?
            self._index_documents_batch.enqueue_actions(actions)
            if raise_error:
                raise

    async def _flush_if_needed(self):
        # type: () -> bool
        """ Every time when a new action is queued, this method
            will be triggered. It checks the actions already queued and flushes them if:
            1. Auto_flush is on
            2. There are self._batch_size actions queued
        """
        if not self._auto_flush:
            return

        # reset the timer
        self._timer.cancel()
        self._timer = Timer(self._window, self.flush)

        if len(self._index_documents_batch.actions) < self._batch_size:
            return

        await self.flush(raise_error=False)

    async def upload_documents_actions(self, documents):
        # type: (List[dict]) -> None
        """Queue upload documents actions.
        :param documents: A list of documents to upload.
        :type documents: List[dict]
        """
        self._index_documents_batch.add_upload_actions(documents)
        await self._flush_if_needed()

    async def delete_documents_actions(self, documents):
        # type: (List[dict]) -> None
        """Queue delete documents actions
        :param documents: A list of documents to delete.
        :type documents: List[dict]
        """
        self._index_documents_batch.add_delete_actions(documents)
        await self._flush_if_needed()

    async def merge_documents_actions(self, documents):
        # type: (List[dict]) -> None
        """Queue merge documents actions
        :param documents: A list of documents to merge.
        :type documents: List[dict]
        """
        self._index_documents_batch.add_merge_actions(documents)
        await self._flush_if_needed()

    async def merge_or_upload_documents_actions(self, documents):
        # type: (List[dict]) -> None
        """Queue merge documents or upload documents actions
        :param documents: A list of documents to merge or upload.
        :type documents: List[dict]
        """
        self._index_documents_batch.add_merge_or_upload_actions(documents)
        await self._flush_if_needed()

    @distributed_trace_async
    async def _index_documents_actions(self, actions, **kwargs):
        # type: (List[IndexAction], **Any) -> List[IndexingResult]
        error_map = {413: RequestEntityTooLargeError}

        kwargs["headers"] = self._merge_client_headers(kwargs.get("headers"))
        try:
            index_documents = IndexBatch(actions=actions)
            batch_response = await self._client.documents.index(batch=index_documents, error_map=error_map, **kwargs)
            return cast(List[IndexingResult], batch_response.results)
        except RequestEntityTooLargeError:
            if len(actions) == 1:
                raise
            pos = round(len(actions) / 2)
            batch_response_first_half = await self._index_documents_actions(
                actions=actions[:pos],
                error_map=error_map,
                **kwargs
            )
            if batch_response_first_half:
                result_first_half = cast(List[IndexingResult], batch_response_first_half.results)
            else:
                result_first_half = []
            batch_response_second_half = await self._index_documents_actions(
                actions=actions[pos:],
                error_map=error_map,
                **kwargs
            )
            if batch_response_second_half:
                result_second_half = cast(List[IndexingResult], batch_response_second_half.results)
            else:
                result_second_half = []
            return result_first_half.extend(result_second_half)

    async def __aenter__(self):
        # type: () -> SearchClient
        await self._client.__aenter__()  # pylint: disable=no-member
        return self

    async def __aexit__(self, *args):
        # type: (*Any) -> None
        await self._client.__aexit__(*args)  # pylint: disable=no-member
