from abc import ABC, abstractmethod

from .schemas import (
    MailImportBatchDeleteRequest,
    MailImportDeleteRequest,
    MailImportExecuteRequest,
    MailImportProviderDescriptor,
    MailImportResponse,
    MailImportSnapshot,
    MailImportSnapshotRequest,
)


class BaseMailImportStrategy(ABC):
    @property
    @abstractmethod
    def descriptor(self) -> MailImportProviderDescriptor:
        raise NotImplementedError

    @abstractmethod
    def execute(self, request: MailImportExecuteRequest) -> MailImportResponse:
        raise NotImplementedError

    @abstractmethod
    def get_snapshot(self, request: MailImportSnapshotRequest) -> MailImportSnapshot:
        raise NotImplementedError

    @abstractmethod
    def delete(self, request: MailImportDeleteRequest) -> MailImportResponse:
        raise NotImplementedError

    @abstractmethod
    def batch_delete(self, request: MailImportBatchDeleteRequest) -> MailImportResponse:
        raise NotImplementedError
