#!/usr/bin/env python3
"""Shared bounded Git object access for the history-rewrite proof drivers.

The index keeps Git's commit ordering and byte-oriented tree ordering, while
amortising object reads through one cat-file process.  It deliberately does
not retain blob bodies: callers cache only the identity-derived analysis they
need.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
from collections.abc import Iterable, Iterator
from pathlib import Path


@dataclasses.dataclass(frozen=True)
class CommitRecord:
    identity: str
    tree: str
    parents: tuple[str, ...]
    subject: str
    body: str


@dataclasses.dataclass(frozen=True)
class TreeEntry:
    mode: bytes
    kind: bytes
    identity: bytes
    name: bytes


@dataclasses.dataclass(frozen=True)
class DiffEntry:
    old_mode: bytes
    new_mode: bytes
    old_identity: bytes
    new_identity: bytes
    status: bytes
    path: bytes


class GitCatFileBatch:
    def __init__(self, repo: Path):
        self.repo = repo
        self.process = subprocess.Popen(
            ["git", "-C", str(repo), "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def read(self, identity: str | bytes) -> tuple[bytes, bytes]:
        value = identity.encode("ascii") if isinstance(identity, str) else identity
        if self.process.stdin is None or self.process.stdout is None:
            raise RuntimeError("git cat-file --batch pipes are unavailable")
        self.process.stdin.write(value + b"\n")
        self.process.stdin.flush()
        header = self.process.stdout.readline()
        if not header:
            raise RuntimeError("git cat-file --batch terminated unexpectedly")
        fields = header.rstrip(b"\n").split()
        if len(fields) == 2 and fields[1] == b"missing":
            raise RuntimeError(f"git cat-file --batch missing object {value.decode('ascii', 'replace')}")
        if len(fields) != 3:
            raise RuntimeError("malformed git cat-file --batch response")
        try:
            size = int(fields[2])
        except ValueError as exc:
            raise RuntimeError("malformed git cat-file --batch object size") from exc
        contents = self.process.stdout.read(size)
        if len(contents) != size or self.process.stdout.read(1) != b"\n":
            raise RuntimeError("truncated git cat-file --batch response")
        return fields[1], contents

    def close(self) -> None:
        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        if self.process.poll() is None:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
        if self.process.stdout is not None:
            self.process.stdout.close()


class GitObjectIndex:
    """Read commits once and immutable tree/blob objects through one process."""

    def __init__(self, repo: Path):
        self.repo = repo
        self.batch = GitCatFileBatch(repo)
        self._commits: tuple[CommitRecord, ...] | None = None
        self._commits_by_id: dict[str, CommitRecord] = {}
        self._trees: dict[str, tuple[TreeEntry, ...]] = {}
        self._tree_maps: dict[str, dict[bytes, TreeEntry]] = {}
        self._leaf_counts: dict[str, int] = {}
        self._object_kinds: dict[str, bytes] = {}

    def close(self) -> None:
        self.batch.close()

    def __enter__(self) -> GitObjectIndex:
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def git_text(self, *args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(self.repo), *args],
            text=True,
            errors="replace",
        )

    def commits(self) -> tuple[CommitRecord, ...]:
        if self._commits is not None:
            return self._commits
        identities = self.git_text("rev-list", "--all").splitlines()
        records: list[CommitRecord] = []
        for identity in identities:
            # One subprocess is an unambiguous outer frame.  split(..., 4)
            # leaves every later NUL in the body, exactly as the former
            # per-commit git-show classifier did.
            raw = self.git_text(
                "show",
                "-s",
                "--format=%H%x00%T%x00%P%x00%s%x00%b",
                identity,
            )
            fields = raw.split("\x00", 4)
            if len(fields) != 5 or fields[0] != identity:
                raise RuntimeError(f"malformed git show framing for commit {identity}")
            record = CommitRecord(
                identity=fields[0],
                tree=fields[1],
                parents=tuple(fields[2].split()) if fields[2] else (),
                subject=fields[3],
                body=fields[4],
            )
            records.append(record)
            self._commits_by_id[identity] = record
        self._commits = tuple(records)
        return self._commits

    def commit(self, identity: str) -> CommitRecord:
        self.commits()
        try:
            return self._commits_by_id[identity]
        except KeyError as exc:
            raise RuntimeError(f"commit is outside the indexed domain: {identity}") from exc

    @staticmethod
    def _entry_kind(mode: bytes) -> bytes:
        if mode in {b"40000", b"040000"}:
            return b"tree"
        if mode == b"160000":
            return b"commit"
        return b"blob"

    def tree(self, identity: str | bytes) -> tuple[TreeEntry, ...]:
        oid = identity.decode("ascii") if isinstance(identity, bytes) else identity
        cached = self._trees.get(oid)
        if cached is not None:
            return cached
        kind, raw = self.batch.read(oid)
        if kind != b"tree":
            raise RuntimeError(f"expected tree object {oid}, got {kind.decode('ascii', 'replace')}")
        entries: list[TreeEntry] = []
        offset = 0
        while offset < len(raw):
            space = raw.find(b" ", offset)
            nul = raw.find(b"\0", space + 1)
            if space < 0 or nul < 0 or nul + 21 > len(raw):
                raise RuntimeError(f"malformed raw tree object {oid}")
            mode = raw[offset:space]
            name = raw[space + 1 : nul]
            object_bytes = raw[nul + 1 : nul + 21]
            if not mode or not name or b"/" in name or len(object_bytes) != 20:
                raise RuntimeError(f"malformed raw tree entry in {oid}")
            entries.append(
                TreeEntry(
                    mode=mode,
                    kind=self._entry_kind(mode),
                    identity=object_bytes.hex().encode("ascii"),
                    name=name,
                )
            )
            offset = nul + 21
        result = tuple(entries)
        self._trees[oid] = result
        self._tree_maps[oid] = {entry.name: entry for entry in result}
        if len(self._tree_maps[oid]) != len(result):
            raise RuntimeError(f"tree contains duplicate entry names: {oid}")
        return result

    def blob(self, identity: str | bytes) -> bytes:
        kind, raw = self.batch.read(identity)
        if kind != b"blob":
            value = identity.decode("ascii") if isinstance(identity, bytes) else identity
            raise RuntimeError(f"expected blob object {value}, got {kind.decode('ascii', 'replace')}")
        return raw

    def object_kind(self, identity: str | bytes) -> bytes:
        oid = identity.decode("ascii") if isinstance(identity, bytes) else identity
        cached = self._object_kinds.get(oid)
        if cached is not None:
            return cached
        kind, _raw = self.batch.read(oid)
        self._object_kinds[oid] = kind
        return kind

    def walk_tree(self, identity: str | bytes, prefix: bytes = b"") -> Iterator[tuple[bytes, TreeEntry]]:
        for entry in self.tree(identity):
            path = prefix + entry.name
            if entry.kind == b"tree":
                yield from self.walk_tree(entry.identity, path + b"/")
            else:
                yield path, entry

    def unique_tree_occurrences(self) -> Iterator[tuple[str, bytes]]:
        seen: set[tuple[str, bytes]] = set()

        def visit(tree: str | bytes, prefix: bytes) -> Iterator[tuple[str, bytes]]:
            oid = tree.decode("ascii") if isinstance(tree, bytes) else tree
            key = (oid, prefix)
            if key in seen:
                return
            seen.add(key)
            yield key
            for entry in self.tree(oid):
                if entry.kind == b"tree":
                    yield from visit(entry.identity, prefix + entry.name + b"/")

        roots_seen: set[str] = set()
        for commit in self.commits():
            if commit.tree in roots_seen:
                continue
            roots_seen.add(commit.tree)
            yield from visit(commit.tree, b"")

    def lookup_path(self, tree: str | bytes, path: bytes) -> TreeEntry | None:
        oid = tree.decode("ascii") if isinstance(tree, bytes) else tree
        parts = path.split(b"/")
        if not path or any(not part for part in parts):
            raise RuntimeError("lookup path must be canonical and non-empty")
        for position, part in enumerate(parts):
            self.tree(oid)
            entry = self._tree_maps[oid].get(part)
            if entry is None:
                return None
            if position == len(parts) - 1:
                return entry
            if entry.kind != b"tree":
                return None
            oid = entry.identity.decode("ascii")
        return None

    def leaf_count(self, tree: str | bytes) -> int:
        oid = tree.decode("ascii") if isinstance(tree, bytes) else tree
        cached = self._leaf_counts.get(oid)
        if cached is not None:
            return cached
        count = 0
        for entry in self.tree(oid):
            count += self.leaf_count(entry.identity) if entry.kind == b"tree" else 1
        self._leaf_counts[oid] = count
        return count

    def paths_for_oids(self, identities: set[bytes]) -> dict[bytes, set[bytes]]:
        result = {identity: set() for identity in identities}
        if not result:
            return result
        for tree_oid, prefix in self.unique_tree_occurrences():
            for entry in self.tree(tree_oid):
                if entry.kind != b"tree" and entry.identity in result:
                    result[entry.identity].add(prefix + entry.name)
        return result

    def batch_diff_trees(
        self,
        pairs: Iterable[tuple[str, str]],
        *,
        alternate_objects: Path | None = None,
    ) -> dict[tuple[str, str], tuple[DiffEntry, ...]]:
        ordered = tuple(pairs)
        if not ordered:
            return {}
        request = "".join(f"{old} {new}\n" for old, new in ordered).encode("ascii")
        environment = os.environ.copy()
        if alternate_objects is not None:
            prior = environment.get("GIT_ALTERNATE_OBJECT_DIRECTORIES")
            value = str(alternate_objects.resolve())
            environment["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = value if not prior else value + os.pathsep + prior
        raw = subprocess.check_output(
            [
                "git",
                "-C",
                str(self.repo),
                "diff-tree",
                "--stdin",
                "--raw",
                "-r",
                "-z",
                "--no-renames",
                "--full-index",
            ],
            input=request,
            env=environment,
        )
        output: dict[tuple[str, str], tuple[DiffEntry, ...]] = {}
        offset = 0
        for index, pair in enumerate(ordered):
            header = f"{pair[0]} {pair[1]}\n".encode("ascii")
            if raw[offset : offset + len(header)] != header:
                raise RuntimeError("malformed git diff-tree pair framing")
            offset += len(header)
            next_header = (
                f"{ordered[index + 1][0]} {ordered[index + 1][1]}\n".encode("ascii")
                if index + 1 < len(ordered)
                else None
            )
            entries: list[DiffEntry] = []
            while offset < len(raw) and (next_header is None or raw[offset : offset + len(next_header)] != next_header):
                if raw[offset : offset + 1] != b":":
                    raise RuntimeError("malformed git diff-tree raw record")
                meta_end = raw.find(b"\0", offset)
                path_end = raw.find(b"\0", meta_end + 1)
                if meta_end < 0 or path_end < 0:
                    raise RuntimeError("truncated git diff-tree raw record")
                fields = raw[offset + 1 : meta_end].split()
                if len(fields) != 5 or fields[4][:1] not in {b"A", b"D", b"M", b"T"}:
                    raise RuntimeError("unsupported git diff-tree raw status")
                entries.append(
                    DiffEntry(
                        old_mode=fields[0],
                        new_mode=fields[1],
                        old_identity=fields[2],
                        new_identity=fields[3],
                        status=fields[4],
                        path=raw[meta_end + 1 : path_end],
                    )
                )
                offset = path_end + 1
            output[pair] = tuple(entries)
        if offset != len(raw):
            raise RuntimeError("trailing git diff-tree output")
        return output
