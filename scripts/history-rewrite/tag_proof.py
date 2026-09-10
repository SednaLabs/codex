#!/usr/bin/env python3
"""Emit tag evidence without claiming cryptographic signature validity."""
import argparse
import csv
import hashlib
import json
import re
import subprocess
from pathlib import Path


HEX = re.compile(r"[0-9a-f]{40}\Z")
SIGNATURE_ARMOR = (
    (
        "openpgp",
        b"-----BEGIN PGP SIGNATURE-----",
        b"-----END PGP SIGNATURE-----",
    ),
    (
        "ssh",
        b"-----BEGIN SSH SIGNATURE-----",
        b"-----END SSH SIGNATURE-----",
    ),
    (
        "x509",
        b"-----BEGIN SIGNED MESSAGE-----",
        b"-----END SIGNATURE-----",
    ),
)
SIGNATURE_LIKE = re.compile(rb"(?m)^-----BEGIN [^\r\n]*(?:SIGNATURE|SIGNED MESSAGE)[^\r\n]*-----\r?$")


def fail(message: str) -> None:
    raise SystemExit(message)


def git(repo: Path, *args: str, text: bool = True) -> str | bytes:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=text)


def signature_presence(raw_tag: bytes) -> str:
    _, separator, message = raw_tag.partition(b"\n\n")
    if separator != b"\n\n":
        fail("tag object lacks a header/message separator")
    recognized = []
    for name, begin, end in SIGNATURE_ARMOR:
        begin_match = re.search(rb"(?m)^" + re.escape(begin) + rb"\r?$", message)
        if begin_match is None:
            continue
        end_match = re.search(rb"(?m)^" + re.escape(end) + rb"\r?$", message[begin_match.end() :])
        if end_match is None:
            fail("tag signature armor is unterminated")
        absolute_end = begin_match.end() + end_match.end()
        if message[absolute_end:] not in {b"", b"\n", b"\r\n"}:
            fail("tag signature armor is not the terminal message block")
        recognized.append(name)
    if len(recognized) > 1:
        fail("tag object contains multiple recognized signature blocks")
    if not recognized:
        if SIGNATURE_LIKE.search(message):
            fail("tag object contains unrecognized signature armor")
        return "absent"
    return "present-" + recognized[0]


def read_commit_map(path: Path) -> dict[str, str]:
    mapping = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if not fields or fields[0].lower() in {"old", "old_commit"}:
            continue
        if len(fields) != 2 or not all(HEX.fullmatch(field) and field != "0" * 40 for field in fields) or fields[0] in mapping:
            fail("tag proof received a malformed commit map")
        mapping[fields[0]] = fields[1]
    if not mapping:
        fail("tag proof received an empty commit map")
    return mapping


def read_ref_map(path: Path) -> list[tuple[str, str, str]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if not fields or fields[0].lower() in {"old", "old_object"}:
            continue
        if len(fields) != 3 or not HEX.fullmatch(fields[0]) or not HEX.fullmatch(fields[1]):
            fail("tag proof received a malformed ref map")
        rows.append((fields[0], fields[1], fields[2]))
    if not rows or len({name for _, _, name in rows}) != len(rows):
        fail("tag proof received an empty or duplicate ref map")
    return rows


def load_original_to_isolated(path: Path | None, ref_rows: list[tuple[str, str, str]]) -> dict[str, str]:
    if path is None:
        mapping = {name: name for _, _, name in ref_rows if name.startswith(("refs/heads/", "refs/tags/"))}
    else:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not value:
            fail("original-to-isolated ref map must be a non-empty object")
        mapping = value
    if not all(
        isinstance(original, str)
        and original.startswith(("refs/heads/", "refs/tags/"))
        and isinstance(isolated, str)
        and isolated.startswith("refs/")
        for original, isolated in mapping.items()
    ):
        fail("original-to-isolated ref map contains an invalid ref")
    if len(set(mapping.values())) != len(mapping):
        fail("original-to-isolated ref map is not bijective")
    ref_domain = {name for _, _, name in ref_rows}
    if path is not None:
        expected = {
            original: "refs/rewrites/selected/" + hashlib.sha256(original.encode("utf-8")).hexdigest()
            for original in mapping
        }
        if mapping != expected:
            fail("original-to-isolated ref map does not follow the exact naming function")
        if ref_domain != set(mapping.values()) | {"refs/rewrites/source"}:
            fail("original-to-isolated ref map does not cover the exact ref-map domain")
    elif not set(mapping.values()).issubset(ref_domain):
        fail("logical ref map is outside the exact ref-map domain")
    return mapping


def tag_consequence(old_presence: str, new_presence: str) -> str:
    old_signed = old_presence.startswith("present-")
    new_signed = new_presence.startswith("present-")
    if not old_signed and new_signed:
        fail("rewrite introduced unexpected tag signature armor")
    if old_signed and not new_signed:
        return "old-signature-not-carried-requires-resign"
    if old_signed and new_signed:
        return "signature-presence-retained-validity-not-assessed"
    return "unsigned-tag-object-before-and-after"


def write_tag_proof(
    preimage: Path,
    rewritten: Path,
    commit_mapping: dict[str, str],
    ref_rows: list[tuple[str, str, str]],
    original_to_isolated: dict[str, str],
    output: Path,
    summary_output: Path,
) -> None:
    by_internal = {name: (old, new) for old, new, name in ref_rows}
    rows = []
    for original, internal in sorted(original_to_isolated.items()):
        if not original.startswith("refs/tags/"):
            continue
        old, new = by_internal[internal]
        old_type = git(preimage, "cat-file", "-t", old).strip()
        new_type = git(rewritten, "cat-file", "-t", new).strip()
        if old_type == new_type == "commit":
            if commit_mapping.get(old) != new:
                fail("lightweight tag is not commit-map bound")
            kind = "lightweight"
            old_peeled, new_peeled = old, new
            old_presence = new_presence = "not-applicable"
            consequence = "lightweight-tag-has-no-tag-object-signature"
        elif old_type == new_type == "tag":
            kind = "annotated"
            old_peeled = git(preimage, "rev-parse", f"{old}^{{commit}}").strip()
            new_peeled = git(rewritten, "rev-parse", f"{new}^{{commit}}").strip()
            if commit_mapping.get(old_peeled) != new_peeled:
                fail("annotated tag peeled identity is not commit-map bound")
            old_presence = signature_presence(git(preimage, "cat-file", "tag", old, text=False))
            new_presence = signature_presence(git(rewritten, "cat-file", "tag", new, text=False))
            consequence = tag_consequence(old_presence, new_presence)
        else:
            fail("tag ref object type changed")
        rows.append(
            (
                original,
                internal,
                kind,
                old,
                new,
                old_peeled,
                new_peeled,
                old_presence,
                new_presence,
                "not-assessed",
                consequence,
            )
        )
    expected_tags = sum(name.startswith("refs/tags/") for name in original_to_isolated)
    if not rows or len(rows) != expected_tags or len({row[0] for row in rows}) != len(rows):
        fail("tag proof is not exactly one row per original tag")
    output.write_text(
        "original_ref\tisolated_ref\tkind\told_object\tnew_object\told_peeled_commit\tnew_peeled_commit\told_signature_presence\tnew_signature_presence\tcryptographic_validity\tconsequence\n"
        + "".join("\t".join(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    annotated = [row for row in rows if row[2] == "annotated"]
    lightweight = [row for row in rows if row[2] == "lightweight"]
    summary = {
        "schema": "history-rewrite-tag-proof-v1",
        "tag_count": len(rows),
        "annotated_count": len(annotated),
        "lightweight_count": len(lightweight),
        "old_recognized_signature_count": sum(row[7].startswith("present-") for row in annotated),
        "new_recognized_signature_count": sum(row[8].startswith("present-") for row in annotated),
        "cryptographic_validity": "not-assessed",
    }
    summary_output.write_text(json.dumps(summary, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preimage", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--commit-map", type=Path, required=True)
    parser.add_argument("--ref-map", type=Path, required=True)
    parser.add_argument("--original-to-isolated-ref-map", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary-output", type=Path, required=True)
    args = parser.parse_args()
    write_tag_proof(
        args.preimage,
        args.repo,
        read_commit_map(args.commit_map),
        read_ref_map(args.ref_map),
        load_original_to_isolated(args.original_to_isolated_ref_map, read_ref_map(args.ref_map)),
        args.output,
        args.summary_output,
    )


if __name__ == "__main__":
    main()
