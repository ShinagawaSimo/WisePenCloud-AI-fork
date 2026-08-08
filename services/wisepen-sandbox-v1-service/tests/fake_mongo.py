from __future__ import annotations

import copy
from typing import Any


class FakeCursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs

    def __aiter__(self):
        self._iter = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class FakeCollection:
    """Small async collection fake for repository contract tests."""

    def __init__(self) -> None:
        self.docs: dict[Any, dict[str, Any]] = {}
        self.indexes: list[tuple[tuple[tuple[str, int], ...], dict[str, Any]]] = []

    async def create_index(self, keys, **kwargs):
        self.indexes.append((tuple(keys), kwargs))

    async def replace_one(self, query, doc, upsert=False):
        current = self._first(query)
        if current is None and not upsert:
            return None
        self.docs[doc["_id"]] = copy.deepcopy(doc)
        return None

    async def update_one(self, query, update, upsert=False):
        current = self._first(query)
        if current is None:
            if not upsert:
                return None
            doc = {"_id": query.get("_id")}
            self._apply_update(doc, {"$setOnInsert": update.get("$setOnInsert", {})})
            self._apply_update(
                doc,
                {key: value for key, value in update.items() if key != "$setOnInsert"},
            )
            self.docs[doc["_id"]] = doc
            return None

        self._apply_update(current, update)
        return None

    async def update_many(self, query, update):
        for doc in self.docs.values():
            if self._matches(doc, query):
                self._apply_update(doc, update)
        return None

    async def delete_one(self, query):
        for key, doc in list(self.docs.items()):
            if self._matches(doc, query):
                del self.docs[key]
                break
        return None

    async def find_one(self, query):
        result = self._first(query)
        return copy.deepcopy(result) if result is not None else None

    async def find_one_and_update(
        self,
        query,
        update,
        *,
        sort=None,
        return_document=True,
    ):
        matches = [doc for doc in self.docs.values() if self._matches(doc, query)]
        if sort:
            for field, direction in reversed(sort):
                matches.sort(
                    key=lambda item: self._get(item, field),
                    reverse=direction < 0,
                )
        if not matches:
            return None
        doc = matches[0]
        self._apply_update(doc, update)
        return copy.deepcopy(doc)

    def find(self, query=None, projection=None):
        query = query or {}
        return FakeCursor(
            [
                copy.deepcopy(doc)
                for doc in self.docs.values()
                if self._matches(doc, query)
            ]
        )

    async def count_documents(self, query):
        return sum(1 for doc in self.docs.values() if self._matches(doc, query))

    def _first(self, query):
        for doc in self.docs.values():
            if self._matches(doc, query):
                return doc
        return None

    def _matches(self, doc, query) -> bool:
        for key, expected in query.items():
            actual = self._get(doc, key)
            if isinstance(expected, dict):
                if "$ne" in expected and actual == expected["$ne"]:
                    return False
                if "$in" in expected and actual not in expected["$in"]:
                    return False
                if "$lte" in expected and actual > expected["$lte"]:
                    return False
                continue
            if actual != expected:
                return False
        return True

    def _apply_update(self, doc, update) -> None:
        for key, values in update.items():
            if key == "$set":
                for field, value in values.items():
                    self._set(doc, field, copy.deepcopy(value))
            elif key == "$inc":
                for field, value in values.items():
                    self._set(doc, field, (self._get(doc, field) or 0) + value)
            elif key == "$setOnInsert":
                for field, value in values.items():
                    if self._get(doc, field) is None:
                        self._set(doc, field, copy.deepcopy(value))

    @staticmethod
    def _get(doc, dotted: str):
        current = doc
        for part in dotted.split("."):
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        return current

    @staticmethod
    def _set(doc, dotted: str, value) -> None:
        current = doc
        parts = dotted.split(".")
        for part in parts[:-1]:
            current = current.setdefault(part, {})
        current[parts[-1]] = value


class FakeDatabase:
    def __init__(self) -> None:
        self.collections: dict[str, FakeCollection] = {}
        self.commands: list[str] = []

    def __getitem__(self, name: str) -> FakeCollection:
        if name not in self.collections:
            self.collections[name] = FakeCollection()
        return self.collections[name]

    async def command(self, name: str):
        self.commands.append(name)
        return {"ok": 1}
