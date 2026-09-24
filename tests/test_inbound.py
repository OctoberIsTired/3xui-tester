import asyncio

import pytest

from app.inbound.manager import InboundManager


class FakePanel:
    def __init__(self) -> None:
        self.source = {"id": 4, "port": 443, "remark": "production", "protocol": "vless", "clientStats": []}
        self.items = {4: self.source.copy()}
        self.deleted: list[int] = []

    async def get_inbound(self, inbound_id: int) -> dict:
        return self.items[inbound_id].copy()

    async def create_inbound(self, payload: dict) -> dict:
        self.items[9] = {**payload, "id": 9}
        return {"id": 9}

    async def list_inbounds(self) -> list[dict]:
        return list(self.items.values())

    async def update_inbound(self, inbound_id: int, payload: dict) -> dict:
        self.items[inbound_id] = payload.copy()
        return {}

    async def delete_inbound(self, inbound_id: int) -> None:
        self.deleted.append(inbound_id)


def test_clone_never_modifies_source_and_is_deleted() -> None:
    panel = FakePanel()
    manager = InboundManager(panel, "clone", 4)
    manager.choose_port = lambda: 23456  # type: ignore[method-assign]
    assert asyncio.run(manager.prepare()) == 9
    assert panel.items[4]["port"] == 443
    assert panel.items[9]["port"] == 23456
    asyncio.run(manager.cleanup())
    assert panel.deleted == [9]


def test_fixed_test_port_is_used() -> None:
    panel = FakePanel()
    manager = InboundManager(panel, "clone", 4, test_port=9443)
    assert manager.choose_port() == 9443


def test_clone_cleanup_retries_after_transient_delete_timeout() -> None:
    class FlakyDeletePanel(FakePanel):
        def __init__(self) -> None:
            super().__init__()
            self.delete_attempts = 0

        async def delete_inbound(self, inbound_id: int) -> None:
            self.delete_attempts += 1
            if self.delete_attempts == 1:
                raise TimeoutError("temporary panel timeout")
            self.deleted.append(inbound_id)
            self.items.pop(inbound_id, None)

    panel = FlakyDeletePanel()
    manager = InboundManager(panel, "clone", 4)
    manager.choose_port = lambda: 23456  # type: ignore[method-assign]
    asyncio.run(manager.prepare())
    asyncio.run(manager.cleanup())
    assert panel.delete_attempts == 2
    assert panel.deleted == [9]
    assert manager.test_inbound_id is None


def test_clone_cleanup_accepts_delete_that_succeeded_before_timeout() -> None:
    class DelayedDeletePanel(FakePanel):
        async def delete_inbound(self, inbound_id: int) -> None:
            self.deleted.append(inbound_id)
            self.items.pop(inbound_id, None)
            raise TimeoutError("response timed out after server deleted clone")

    panel = DelayedDeletePanel()
    manager = InboundManager(panel, "clone", 4)
    manager.choose_port = lambda: 23456  # type: ignore[method-assign]
    asyncio.run(manager.prepare())
    asyncio.run(manager.cleanup())
    assert panel.deleted == [9]
    assert manager.test_inbound_id is None


def test_unidentified_clone_is_deleted_before_prepare_raises() -> None:
    class NoIdPanel(FakePanel):
        async def create_inbound(self, payload: dict) -> dict:
            self.items[9] = {**payload, "id": 9}
            return {}

        async def list_inbounds(self) -> list[dict]:
            clone = self.items[9]
            return [*self.items.values(), {**clone, "id": 10}]

    panel = NoIdPanel()
    manager = InboundManager(panel, "clone", 4)
    manager.choose_port = lambda: 23456  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="could not be determined"):
        asyncio.run(manager.prepare())
    assert 9 in panel.deleted and 10 in panel.deleted
