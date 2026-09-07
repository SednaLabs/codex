#!/usr/bin/env python3
"""Inspect or build the exact hosted SDK/build consumer candidate for w13825.

Trusted code comes from the reviewed workflow checkout.  The accepted SDK
artifact and reviewed build-source commit are immutable data inputs.  The
consumer verifies every input tuple, retains the three already-materialized
patch dependencies, and applies only the exact declared source overlay.  Its
metadata-only mode emits complete content-free tree manifests without running
generators or creating Git objects.  Its build mode validates the accepted
manifest inventory, resolves the composed Cargo lock conservatively from its
accepted seed, regenerates five coupled outputs with repository-pinned tools,
and emits a fresh-bare-verified Git bundle without publishing a ref.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import tomllib
from typing import Any


REPOSITORY = "sednalabs/codex"
REPOSITORY_ID = "1152496647"
WORKFLOW_PATH = ".github/workflows/apply-upstream-cohort.yml"
VALIDATION_BRANCH = "worker/w13825-sdk-network-proxy-diagnostic"
VALIDATION_REF = f"refs/heads/{VALIDATION_BRANCH}"
PUSH_PREDECESSOR_SHA = "7a80f886b7ebf979b8324f7e3527afdbe83a65b3"

BASE_SHA = "5eb6ca6519b1a79e8997bf21321885de1fd9ed01"
BASE_TREE = "7a4e9d32c7a13a22215335a850cf879e284fdc63"
GLOBAL_UPSTREAM_SHA = "008bbd5884122dc95aaece19ecfe0fc6a59dcf36"
GLOBAL_UPSTREAM_TREE = "721cd395f53962482b3f6d140d0b9942fef3baac"
MATERIALIZED_SHA = "f5bb378d2e575b8f6f3cf266a0939ef404c37203"
MATERIALIZED_TREE = "49af672a3965958bfb1668f27c0caa27ba48554a"

SDK_SOURCE_SHA = "bc8884624330b6e681cfa3ce5fc575ce8298ed1b"
SDK_SOURCE_TREE = "1e143e2bc5964a4308d9a6f36ca3e2af028e79e9"
SDK_SOURCE_BRANCH = "worker/w13825-sdk-source-authoring-20260906"
SDK_SOURCE_PATHS_SHA256 = "90eeb76b9ab63af38822f137a3afaff05fc06bb5b9032ab66948c389fc6d68a9"
SDK_SOURCE_PATHS = [
    "sdk/python/scripts/update_sdk_artifacts.py",
    "sdk/python/tests/test_client_rpc_methods.py",
    "sdk/typescript/package.json",
]

SDK_INPUT_ARTIFACT_ID = "9992014028"
SDK_INPUT_ARTIFACT_NAME = "upstream-sdk-candidate-34042133059-1"
SDK_INPUT_ARTIFACT_SIZE = 451611604
SDK_INPUT_ARCHIVE_SHA256 = "bce7300bd77efe798a463d8006b4291049bfaff5b996c878853ebab95087d433"
SDK_INPUT_RUN_ID = "34042133059"
SDK_INPUT_RUN_ATTEMPT = "1"
SDK_INPUT_WORKFLOW_SHA = "0384d8ae9205e142fa4b352256d8c85a9e05f8c4"
SDK_INPUT_WORKFLOW_TREE = "636a1c9d84d2adec2d063ad932565b96a0dff7e6"
SDK_INPUT_WORKFLOW_REF = (
    "sednalabs/codex/.github/workflows/apply-upstream-cohort.yml@"
    "refs/heads/worker/w13825-sdk-producer-validation-20260907"
)
SDK_INPUT_BUNDLE_SHA256 = "5dd47aa65221838c8f6d625d83de78bbba101001157866eb5abb1270dae2425c"
SDK_INPUT_RECEIPT_SHA256 = "811992f09b22f610b8ab01983a01da0950ed090931813c67e91f4983d0ca82a6"
SDK_CANDIDATE_SHA = "3a26f7dad12e96ea41dae025e77472af0dd273a8"
SDK_CANDIDATE_TREE = "6867e9e14ea8f416ee3075f959b880d038fe2cc0"
SDK_CANDIDATE_PARENT = MATERIALIZED_SHA
DIAGNOSTIC_SOURCE_SHA = "cb3c6b5abd9b08d93f38aec0a959703a5915c6dd"
DIAGNOSTIC_SOURCE_TREE = "97459288bcebe0fbcff352778293d95ed79aeef1"
DIAGNOSTIC_SOURCE_PARENT = "c56b0290e019a536da55cc4ae902ac4db55676c5"
DIAGNOSTIC_HELPER_SHA = "7a80f886b7ebf979b8324f7e3527afdbe83a65b3"
TARGET_HELPER_BRANCH = "worker/w13825-sdk-build-consumer"
TARGET_HELPER_SHA = "c989b54e24385559d8be2243a3fd9c1756cb598b"
TARGET_HELPER_TREE = "85966159f29a883da63c5a3ecca521eb5fae9d19"
TARGET_HELPER_PARENT = "88c06eebaf1354517447268f63cd900885131f3a"

COMMON_SOURCE_RUN_ID = "34035744523"
COMMON_SOURCE_RUN_ATTEMPT = "1"
COMMON_SOURCE_ARTIFACT = "upstream-composition-34035744523-1"
COMMON_BUNDLE_SHA256 = "b383183cf21ade4b50244986cf1589988b248259ee51f099932bb0c06b026dd6"
COMMON_RECEIPT_SHA256 = "2bcebca05cb45d6d2caad475ec5348a3883566f99e6a98d24196382d52d39e93"
COMMON_MANIFEST_SHA256 = "0451d500a2a9868825337ddd0e6c16cd73c5088116131d75b4f27f801885328b"
COMMON_PROVENANCE_SHA256 = "afbf269c8593c978ed706c9f2fddc0031383350fe216d88512ec3707c8a55cb9"
COMMON_STAGED_PATCH_SHA256 = "dd4b59d9be8c2727d08de673085b36a1c61f6cee617855f210706412a5bfc66c"
COMMON_STAGED_PATHS_SHA256 = "90b44134bb538a07fa03dfd674e96f08de4ba04a40252f6dc9f5c740dd5bb1ae"

BUILD_SOURCE_SHA = "5de836bbd93d4d62f01d7860d8bfed5d635b533c"
BUILD_SOURCE_TREE = "954e3e5ab099d247a1b641d74d6df19154a4be9a"
BUILD_SOURCE_PARENT = "b8c1a1a176d30bec1c9265cae3d36c66a5dd3841"
BUILD_SOURCE_BRANCH = "worker/w13825-build-source-authoring-20260907"
VOICE_HOST_DEFERRAL_BASELINE_SHA = "22a0c45ee711dc5ce47847dc04cbc5e7e76507c0"
VOICE_HOST_DEFERRAL_BASELINE_ROOT_ENTRY = ("100644", "blob", "7bd8c144e52b169b907928bcf743363949d12cb2")
BUILD_PATHS_SHA256 = "52ef6b8ac2325fc482bf2e20fe09b1032ad671338d76272e2b9faa63085ce137"
BUILD_SOURCE_ENTRIES: dict[str, tuple[str, str, str] | None] = {
    ".github/workflows/bazel.yml": (
        "100644",
        "blob",
        "2f17836a13cabd85309cad454fa999390f9b3a3f",
    ),
    ".github/workflows/blob-size-policy.yml": (
        "100644",
        "blob",
        "51ab52110f3ce388caa37ea8a1bf6fc8773dc92b",
    ),
    ".github/workflows/rust-ci-full.yml": (
        "100644",
        "blob",
        "0c96ec9a62fad00e0c129b2af6ebf2905b4fe9b4",
    ),
    ".github/workflows/rust-ci.yml": (
        "100644",
        "blob",
        "b7815de8d48740b63651290d939e0d8050c365d1",
    ),
    ".github/workflows/v8-canary.yml": (
        "100644",
        "blob",
        "b946a6f84e850e5b0e5e724accd70b3c2c26f7b0",
    ),
    "MODULE.bazel": (
        "100644",
        "blob",
        "ea89c1d5152f8dbe007f23ca63e07dd59c054ae1",
    ),
    "codex-rs/Cargo.lock": (
        "100644",
        "blob",
        "67c9cc2ba7d8e03117b388df989eab676501b238",
    ),
    "codex-rs/Cargo.toml": (
        "100644",
        "blob",
        "8b68f14c6901d583716329172bf28f263f39cd02",
    ),
    "codex-rs/http-client/src/lib.rs": ("100644", "blob", "e200f61af40f4c8464d819cb8c4d017f85fcd4ba"),
    "codex-rs/http-client/src/tls_backend_fallback.rs": ("100644", "blob", "760df1824ad752fe8d4b5f3f2b0d0e3e848c0144"),
    "codex-rs/http-client/src/tls_backend_fallback_tests.rs": ("100644", "blob", "c7ca0adceb94a6729d8657c7a7feb4e066044f68"),
    "codex-rs/realtime-webrtc/BUILD.bazel": (
        "100644",
        "blob",
        "d9cfeb6cfaf7b7c40e7648f8547b7785c284cc28",
    ),
    "patches/BUILD.bazel": (
        "100644",
        "blob",
        "075b8e30d98baabca4ff60f0b2649d1813ce83d1",
    ),
    "third_party/v8/rusty_v8_150_4_0.sha256": (
        "100644",
        "blob",
        "fc884c8ebc1e2f36154a12ecbcd4cbe509d3bbc5",
    ),
}
BUILD_SOURCE_PREIMAGE_ENTRIES: dict[str, tuple[str, str, str]] = {
    ".github/workflows/bazel.yml": (
        "100644",
        "blob",
        "cd687ef62d9d2562eb2780d65023e0b5df44451b",
    ),
    ".github/workflows/blob-size-policy.yml": (
        "100644",
        "blob",
        "24548e1e021366e94e67c9eb7fce9be6d0b0afe5",
    ),
    ".github/workflows/rust-ci-full.yml": (
        "100644",
        "blob",
        "efe29828cca7089cf92738e519384023cb8f71bb",
    ),
    ".github/workflows/rust-ci.yml": (
        "100644",
        "blob",
        "dba33d8033e95198117a83832ad8617673095b79",
    ),
    ".github/workflows/v8-canary.yml": (
        "100644",
        "blob",
        "fb3948522a3b46c04bfefe7f392442a9a151dd91",
    ),
    "MODULE.bazel": (
        "100644",
        "blob",
        "da0bd8c284695716325ef518cd4f57714d1c3444",
    ),
    "codex-rs/Cargo.lock": (
        "100644",
        "blob",
        "5117062519706acb938639c20c1e56c2dfe03274",
    ),
    "codex-rs/Cargo.toml": (
        "100644",
        "blob",
        "b7d06b98391ef2f3307096d963eea4e19853d8f0",
    ),
    "codex-rs/http-client/src/lib.rs": ("100644", "blob", "837d1dc27e2409f41d27c84a1cab638c71e47bbc"),
    "codex-rs/http-client/src/tls_backend_fallback.rs": ("100644", "blob", "760df1824ad752fe8d4b5f3f2b0d0e3e848c0144"),
    "codex-rs/http-client/src/tls_backend_fallback_tests.rs": ("100644", "blob", "c7ca0adceb94a6729d8657c7a7feb4e066044f68"),
    "codex-rs/realtime-webrtc/BUILD.bazel": (
        "100644",
        "blob",
        "1be89f035d902d96e03b3a1aeb9d1f9b66e1dc82",
    ),
    "patches/BUILD.bazel": (
        "100644",
        "blob",
        "1909fed6cf65fceddba1ba476f448551cd9a6018",
    ),
    "third_party/v8/rusty_v8_150_4_0.sha256": (
        "100644",
        "blob",
        "628ae7a9ac94eee0e0dd66c927964a0ad06544d7",
    ),
}
CORE_SKILLS_SOURCE_ENTRIES: dict[str, tuple[str, str, str]] = {
    "codex-rs/core-skills/BUILD.bazel": ("100644", "blob", "77c4253e73ba223700cac0be15235a4e468aa701"),
    "codex-rs/core-skills/Cargo.toml": ("100644", "blob", "f86ecdf75b8ffe2507f33ab8557718c46a7de9e6"),
    "codex-rs/core-skills/src/config_rules.rs": ("100644", "blob", "8a64adfa69c41752819c4336b94fb7a6ceb1b1f7"),
    "codex-rs/core-skills/src/injection.rs": ("100644", "blob", "2a243b9ee9f69437ffc206de7220f10737029374"),
    "codex-rs/core-skills/src/injection_tests.rs": ("100644", "blob", "1b6e14dac3ed39ee672ea2c68b7637d6eb761879"),
    "codex-rs/core-skills/src/invocation_utils.rs": ("100644", "blob", "50864a8349192c719e6da4e70e84db949be6dccf"),
    "codex-rs/core-skills/src/invocation_utils_tests.rs": ("100644", "blob", "cbbf4c52aff9a8ce9f354842cb87fcc9409acbf2"),
    "codex-rs/core-skills/src/lib.rs": ("100644", "blob", "dcfc6568a5dcbc6ac0872418c1df19fd68e75ab0"),
    "codex-rs/core-skills/src/loader.rs": ("100644", "blob", "bb043ab971775c8af0378b699687c6ea550c223f"),
    "codex-rs/core-skills/src/loader/discovery.rs": ("100644", "blob", "cadc8218c02030ef13d3b8a6d4130be826c5bfe5"),
    "codex-rs/core-skills/src/loader/environment.rs": ("100644", "blob", "0e881b6c24dad0766fd381822ea985c3b3f9269d"),
    "codex-rs/core-skills/src/loader/environment_tests.rs": ("100644", "blob", "a817830b37e90316fab40f7e8e89bb9084659ade"),
    "codex-rs/core-skills/src/loader/namespace.rs": ("100644", "blob", "fa00b1005d19794f8ae7a954cbc6d18a6a9d4142"),
    "codex-rs/core-skills/src/loader_tests.rs": ("100644", "blob", "f052485a57d5fbbc70545d6a1314758fd71ff8ba"),
    "codex-rs/core-skills/src/mention_counts.rs": ("100644", "blob", "b7482ca36ecc3f9f58cc85a3e5a17c58fd91e424"),
    "codex-rs/core-skills/src/model.rs": ("100644", "blob", "e146b7986638c02f4396a29ca2f0d1849123af6e"),
    "codex-rs/core-skills/src/remote.rs": ("100644", "blob", "b7e27e34f7e873c11fba0981e8bd587debf92639"),
    "codex-rs/core-skills/src/render.rs": ("100644", "blob", "73a37e502bcb33383c9d6cae0d08e0589712b026"),
    "codex-rs/core-skills/src/root_loader.rs": ("100644", "blob", "f18f96e85d1fb2d220a9526805aba57a6a2746d1"),
    "codex-rs/core-skills/src/service.rs": ("100644", "blob", "82f3c18ac8a22ee411f5ad3142fef4d6eef15290"),
    "codex-rs/core-skills/src/service_tests.rs": ("100644", "blob", "968867bd48d929695a163b9acda887bcfb5bb069"),
    "codex-rs/core-skills/src/skill_instructions.rs": ("100644", "blob", "b2a002d9025b7ef4594812da19bf08249759948e"),
    "codex-rs/core-skills/src/system.rs": ("100644", "blob", "5eec94c72967246ee76b960155e250b273c464b7"),
    "codex-rs/core-skills/tests/environment_loader.rs": ("100644", "blob", "e33b010b4db345d49d9de8df0b125f9860fb6446"),
}
CORE_SKILLS_SOURCE_PREIMAGE_ENTRIES: dict[str, tuple[str, str, str] | None] = {
    path: (entry if path in {"codex-rs/core-skills/src/loader_tests.rs", "codex-rs/core-skills/src/service.rs"} else None)
    for path, entry in CORE_SKILLS_SOURCE_ENTRIES.items()
}
CORE_SKILLS_PATHS = list(CORE_SKILLS_SOURCE_ENTRIES)
CORE_SKILLS_PATHS_SHA256 = "35f2221d0ac15b26a75bf34789f4abc3d9391808313ec42c601d5627c8b4ca9b"
CORE_SKILLS_ENTRIES_SHA256 = "3d750d875777906261a420e189759982b9d8f8da2a535ec23a5f119856918d04"
CORE_SKILLS_ADDITIONS_SHA256 = "04f0c6022797fad1b9eca3b28187331050753c76545167b8cfb783f15f2179b2"
CORE_SKILLS_EXACT_SHA256 = "6b58a39c530f7d9c02138d51e85860aec1b36892d806028d55f8a2005fbf04a2"

BUILD_PATHS = list(BUILD_SOURCE_ENTRIES)

RESTORE_SOURCE_ENTRIES: dict[str, tuple[str, str, str]] = {
    "codex-rs/ext/guardian/BUILD.bazel": (
        "100644",
        "blob",
        "adcbb090bcdadbfadffb23fe181a4b328072cfbd",
    ),
    "codex-rs/ext/guardian/Cargo.toml": (
        "100644",
        "blob",
        "513254b7cf754392d685ce099b0874e576fc9c7a",
    ),
    "codex-rs/ext/guardian/src/lib.rs": (
        "100644",
        "blob",
        "a64cf4fed40ae756efb2bc163fe80034376835f0",
    ),
    "codex-rs/mcp-server/BUILD.bazel": (
        "100644",
        "blob",
        "5bf39611b93e767ca9a22fd4ba4c238981d94afd",
    ),
    "codex-rs/mcp-server/Cargo.toml": (
        "100644",
        "blob",
        "610672f0c2e62b139989543fb329675cb7ac5146",
    ),
    "codex-rs/mcp-server/src/codex_tool_config.rs": (
        "100644",
        "blob",
        "2ceaac3000697aa00d828dd763fa7c944f1db300",
    ),
    "codex-rs/mcp-server/src/codex_tool_runner.rs": (
        "100644",
        "blob",
        "1100a035d2fe85214b026125183eb04a46c76e26",
    ),
    "codex-rs/mcp-server/src/exec_approval.rs": (
        "100644",
        "blob",
        "c1427a0578594ed0a6d27fbe0b1391acca6e7b61",
    ),
    "codex-rs/mcp-server/src/lib.rs": (
        "100644",
        "blob",
        "7524234ace207d0028c408a3f85c9f94a5462a52",
    ),
    "codex-rs/mcp-server/src/main.rs": (
        "100644",
        "blob",
        "220507446aaa7dd5f604fb581bf3b174a11197db",
    ),
    "codex-rs/mcp-server/src/message_processor.rs": (
        "100644",
        "blob",
        "957d14f818f4db26ae2ba671218d9f0ab6772167",
    ),
    "codex-rs/mcp-server/src/outgoing_message.rs": (
        "100644",
        "blob",
        "30b96fd18da1b31e5957314a86cb45e504291f9c",
    ),
    "codex-rs/mcp-server/src/patch_approval.rs": (
        "100644",
        "blob",
        "56eca276b3c22aaf3f0d2c83a82e6df0d16980a9",
    ),
    "codex-rs/mcp-server/tests/all.rs": (
        "100644",
        "blob",
        "fdf98aa9455bffb74f4dc11df6d237aa948248ef",
    ),
    "codex-rs/mcp-server/tests/common/BUILD.bazel": (
        "100644",
        "blob",
        "d588b5b8dcced243962c8faab884d18fe0562c16",
    ),
    "codex-rs/mcp-server/tests/common/Cargo.toml": (
        "100644",
        "blob",
        "515aa01b5a4a098a6f49cad9d5e4ad3cbbe54f77",
    ),
    "codex-rs/mcp-server/tests/common/lib.rs": (
        "100644",
        "blob",
        "57c0ce825d05241523ca5ae39ee33ada1115e9bb",
    ),
    "codex-rs/mcp-server/tests/common/mcp_process.rs": (
        "100644",
        "blob",
        "5bfcb39760d1ab500d633379616cdeba934f7687",
    ),
    "codex-rs/mcp-server/tests/common/mock_model_server.rs": (
        "100644",
        "blob",
        "7734ae12cd8c603272e37171c97e67ab2bfdce54",
    ),
    "codex-rs/mcp-server/tests/common/responses.rs": (
        "100644",
        "blob",
        "48a575a4c6ba255bb760fa185f19f0d460474a43",
    ),
    "codex-rs/mcp-server/tests/suite/codex_tool.rs": (
        "100644",
        "blob",
        "41cdc3bbb2e4f283f2a6cc179b246b73daebc400",
    ),
    "codex-rs/mcp-server/tests/suite/mod.rs": (
        "100644",
        "blob",
        "6b50853b165b5e940a5514b0919bcb45fdb05acb",
    ),
}
RESTORE_SOURCE_PREIMAGE_ENTRIES: dict[str, tuple[str, str, str] | None] = {
    path: (entry if path in {
        "codex-rs/mcp-server/Cargo.toml",
        "codex-rs/mcp-server/src/codex_tool_config.rs",
        "codex-rs/mcp-server/src/codex_tool_runner.rs",
        "codex-rs/mcp-server/src/lib.rs",
        "codex-rs/mcp-server/src/message_processor.rs",
        "codex-rs/mcp-server/src/outgoing_message.rs",
        "codex-rs/mcp-server/tests/common/mcp_process.rs",
        "codex-rs/mcp-server/tests/suite/codex_tool.rs",
    } else None)
    for path, entry in RESTORE_SOURCE_ENTRIES.items()
}
RESTORE_PATHS = list(RESTORE_SOURCE_ENTRIES)
RESTORE_PATHS_SHA256 = "5428c8fdbb4cc499c3218272fdbd01bb822de4b254de51ee83346a389b44e7b6"
RESTORE_ENTRIES_SHA256 = "76075be6752151e58794d773314f0dd38e2b98b86d962669876ff9e57a3100db"

OVERLAY_SOURCE_ENTRIES: dict[str, tuple[str, str, str] | None] = dict(
    sorted({**BUILD_SOURCE_ENTRIES, **RESTORE_SOURCE_ENTRIES, **CORE_SKILLS_SOURCE_ENTRIES}.items())
)
OVERLAY_SOURCE_PREIMAGE_ENTRIES: dict[str, tuple[str, str, str] | None] = dict(
    sorted({
        **BUILD_SOURCE_PREIMAGE_ENTRIES,
        **RESTORE_SOURCE_PREIMAGE_ENTRIES,
        **CORE_SKILLS_SOURCE_PREIMAGE_ENTRIES,
    }.items())
)
OVERLAY_PATHS = list(OVERLAY_SOURCE_ENTRIES)
OVERLAY_PATHS_SHA256 = "b608dcd757e604dbdd7da294c074ac346d3801765c0f8a46c73e89b327987e7f"
OVERLAY_CHANGED_PATHS = [
    path
    for path in OVERLAY_PATHS
    if OVERLAY_SOURCE_PREIMAGE_ENTRIES[path] != OVERLAY_SOURCE_ENTRIES[path]
]
OVERLAY_CHANGED_PATHS_SHA256 = "5de67fc1154adfc0375de5e04a23dfef3e34c6229005fb56f1d57e2aa0fd6e70"

PATCH_DEPENDENCIES: dict[str, tuple[str, str, str]] = {
    "patches/rules_rs_windows_msvc_linker.patch": (
        "100644",
        "blob",
        "66feb78569348668ee0f4fce86c7b50276fc097d",
    ),
    "patches/rules_rs_zlib_snapshot_urls.patch": (
        "100644",
        "blob",
        "fffbca8fcd265a4c65f911a07f26434ca4f47188",
    ),
    "patches/rules_rust_windows_msvc_direct_link_args.patch": (
        "100644",
        "blob",
        "aa5fb274e1d5e7b473771cf71183c132a80e1b36",
    ),
}

V8_COMPOSED_ENTRIES: dict[str, tuple[str, str, str]] = {
    "patches/v8_bazel_rules.patch": (
        "100644",
        "blob",
        "c907d32d214127de359a869a69b4af6bb1b6efb6",
    ),
    "patches/v8_module_deps.patch": (
        "100644",
        "blob",
        "d179617e6302ebe573662bb08e962dc41218dfdd",
    ),
    "patches/v8_source_portability.patch": (
        "100644",
        "blob",
        "6b6537cb9cd7696a0e3402f644c9ef6404a1c08b",
    ),
    "third_party/v8/BUILD.bazel": (
        "100644",
        "blob",
        "d874bce0e01211011d79238887a94029e74aaef2",
    ),
}

COMPOSITE_RUNTIME_INPUTS: dict[str, tuple[str, str, str]] = {
    "sdk/python/pyproject.toml": (
        "100644",
        "blob",
        "c5a04b7268ae22a1077a711456b997442ac995f4",
    ),
    "sdk/python/uv.lock": (
        "100644",
        "blob",
        "6f6f867ede321b6be3a94612ba3eebb853a1ac04",
    ),
}

GENERATED_PATHS = [
    "codex-rs/Cargo.lock",
    "MODULE.bazel.lock",
    "pnpm-lock.yaml",
    "sdk/python/src/openai_codex/api.py",
    "sdk/python/src/openai_codex/generated/notification_registry.py",
    "sdk/python/src/openai_codex/generated/v2_all.py",
]
SDK_GENERATED_PATHS = GENERATED_PATHS[3:]
SDK_BUNDLE_PROBE_PATHS = [
    "codex-rs/app-server/src/message_processor.rs",
    "codex-rs/app-server/src/request_processors.rs",
    "codex-rs/app-server/src/request_processors/account_processor.rs",
    "codex-rs/codex-api/src/endpoint/responses_websocket.rs",
    "codex-rs/core/src/client.rs",
    "codex-rs/core/src/client_tests.rs",
    "codex-rs/core/src/config/mod.rs",
    "codex-rs/core/src/session/tests.rs",
    "codex-rs/core/tests/suite/remote_env.rs",
    "codex-rs/exec-server/BUILD.bazel",
    "codex-rs/exec-server/Cargo.toml",
    "codex-rs/exec-server/README.md",
    "codex-rs/exec-server/src/capability_discovery.rs",
    "codex-rs/exec-server/src/capability_discovery_cache.rs",
    "codex-rs/exec-server/src/client.rs",
    "codex-rs/exec-server/src/client/accepted.rs",
    "codex-rs/exec-server/src/client/accepted_tests.rs",
    "codex-rs/exec-server/src/client/http_response_body_stream.rs",
    "codex-rs/exec-server/src/client/network_policy_audit.rs",
    "codex-rs/exec-server/src/client/route_aware_http_client.rs",
    "codex-rs/exec-server/src/client/tests/network_policy_tests.rs",
    "codex-rs/exec-server/src/client_api.rs",
    "codex-rs/exec-server/src/client_recovery.rs",
    "codex-rs/exec-server/src/client_recovery_tests.rs",
    "codex-rs/exec-server/src/client_refresh.rs",
    "codex-rs/exec-server/src/client_refresh_tests.rs",
    "codex-rs/exec-server/src/client_telemetry.rs",
    "codex-rs/exec-server/src/client_transport.rs",
    "codex-rs/exec-server/src/client_transport_tests.rs",
    "codex-rs/exec-server/src/connection.rs",
    "codex-rs/exec-server/src/environment.rs",
    "codex-rs/exec-server/src/environment/accepted.rs",
    "codex-rs/exec-server/src/environment/connect_options.rs",
    "codex-rs/exec-server/src/environment/connect_options_tests.rs",
    "codex-rs/exec-server/src/environment_config.rs",
    "codex-rs/exec-server/src/environment_toml.rs",
    "codex-rs/exec-server/src/forward.rs",
    "codex-rs/exec-server/src/forward_tests.rs",
    "codex-rs/exec-server/src/fs_helper.rs",
    "codex-rs/exec-server/src/fs_helper_main.rs",
    "codex-rs/exec-server/src/fs_sandbox.rs",
    "codex-rs/exec-server/src/fs_sandbox_windows_tests.rs",
    "codex-rs/exec-server/src/lib.rs",
    "codex-rs/exec-server/src/local_file_system.rs",
    "codex-rs/exec-server/src/local_file_system_path_uri_tests.rs",
    "codex-rs/exec-server/src/local_process.rs",
    "codex-rs/exec-server/src/network_policy_decisions.rs",
    "codex-rs/exec-server/src/network_policy_decisions_tests.rs",
    "codex-rs/exec-server/src/no_follow/mod.rs",
    "codex-rs/exec-server/src/no_follow/unix.rs",
    "codex-rs/exec-server/src/no_follow/windows.rs",
    "codex-rs/exec-server/src/no_follow/windows_tests.rs",
    "codex-rs/exec-server/src/noise_relay/executor_stream.rs",
    "codex-rs/exec-server/src/noise_relay/executor_stream_tests.rs",
    "codex-rs/exec-server/src/noise_relay/forward_stream_tests.rs",
    "codex-rs/exec-server/src/noise_relay/harness.rs",
    "codex-rs/exec-server/src/noise_relay/harness_tests.rs",
    "codex-rs/exec-server/src/noise_relay/message_framing.rs",
    "codex-rs/exec-server/src/noise_relay/message_framing_tests.rs",
    "codex-rs/exec-server/src/noise_relay/mod.rs",
    "codex-rs/exec-server/src/noise_relay/stream_handler.rs",
    "codex-rs/exec-server/src/noise_relay/stream_handler_tests.rs",
    "codex-rs/exec-server/src/process.rs",
    "codex-rs/exec-server/src/process_sandbox.rs",
    "codex-rs/exec-server/src/process_sandbox_tests.rs",
    "codex-rs/exec-server/src/process_telemetry.rs",
    "codex-rs/exec-server/src/proto/codex.exec_server.relay.v1.proto",
    "codex-rs/exec-server/src/proto/codex.exec_server.relay.v1.rs",
    "codex-rs/exec-server/src/regular_file.rs",
    "codex-rs/exec-server/src/regular_file_tests.rs",
    "codex-rs/exec-server/src/relay.rs",
    "codex-rs/exec-server/src/relay_noise_tests.rs",
    "codex-rs/exec-server/src/remote.rs",
    "codex-rs/exec-server/src/remote/direct.rs",
    "codex-rs/exec-server/src/remote/direct_tests.rs",
    "codex-rs/exec-server/src/remote/registration_retry.rs",
    "codex-rs/exec-server/src/remote/registration_retry_tests.rs",
    "codex-rs/exec-server/src/remote_file_system.rs",
    "codex-rs/exec-server/src/remote_file_system_path_uri_tests.rs",
    "codex-rs/exec-server/src/remote_process.rs",
    "codex-rs/exec-server/src/rpc.rs",
    "codex-rs/exec-server/src/rpc_client_metrics_tests.rs",
    "codex-rs/exec-server/src/rpc_server_requests.rs",
    "codex-rs/exec-server/src/runtime_paths.rs",
    "codex-rs/exec-server/src/sandboxed_file_open.rs",
    "codex-rs/exec-server/src/sandboxed_file_system.rs",
    "codex-rs/exec-server/src/sandboxed_file_system_path_uri_tests.rs",
    "codex-rs/exec-server/src/server.rs",
    "codex-rs/exec-server/src/server/file_system_handler.rs",
    "codex-rs/exec-server/src/server/handler.rs",
    "codex-rs/exec-server/src/server/handler/tests.rs",
    "codex-rs/exec-server/src/server/process_handler.rs",
    "codex-rs/exec-server/src/server/process_otel_tests.rs",
    "codex-rs/exec-server/src/server/processor.rs",
    "codex-rs/exec-server/src/server/registry.rs",
    "codex-rs/exec-server/src/server/release_version.rs",
    "codex-rs/exec-server/src/server/request_dispatcher.rs",
    "codex-rs/exec-server/src/server/request_dispatcher_tests.rs",
    "codex-rs/exec-server/src/server/transport.rs",
    "codex-rs/exec-server/src/server/transport_tests.rs",
    "codex-rs/exec-server/src/shell_snapshot.rs",
    "codex-rs/exec-server/src/shell_snapshot_tests.rs",
    "codex-rs/exec-server/src/telemetry.rs",
    "codex-rs/exec-server/src/trace_context.rs",
    "codex-rs/exec-server/testing/exec_server.rs",
    "codex-rs/exec-server/testing/wine_remote_test_runner.rs",
    "codex-rs/exec-server/tests/accepted_websocket.rs",
    "codex-rs/exec-server/tests/capability_discovery.rs",
    "codex-rs/exec-server/tests/chatgpt_cloudflare_affinity.rs",
    "codex-rs/exec-server/tests/common/exec_server.rs",
    "codex-rs/exec-server/tests/common/fake_bwrap.rs",
    "codex-rs/exec-server/tests/common/mod.rs",
    "codex-rs/exec-server/tests/common/relay.rs",
    "codex-rs/exec-server/tests/deferred_environment.rs",
    "codex-rs/exec-server/tests/environment.rs",
    "codex-rs/exec-server/tests/environment_config.rs",
    "codex-rs/exec-server/tests/exec_process.rs",
    "codex-rs/exec-server/tests/file_stream.rs",
    "codex-rs/exec-server/tests/file_system/shared.rs",
    "codex-rs/exec-server/tests/file_system/support.rs",
    "codex-rs/exec-server/tests/file_system_unix.rs",
    "codex-rs/exec-server/tests/file_system_windows.rs",
    "codex-rs/exec-server/tests/forward.rs",
    "codex-rs/exec-server/tests/http_client.rs",
    "codex-rs/exec-server/tests/http_request.rs",
    "codex-rs/exec-server/tests/initialize.rs",
    "codex-rs/exec-server/tests/process.rs",
    "codex-rs/exec-server/tests/relay.rs",
    "codex-rs/exec-server/tests/relay/registration_retry_tests.rs",
    "codex-rs/exec-server/tests/support/BUILD.bazel",
    "codex-rs/exec-server/tests/support/Cargo.toml",
    "codex-rs/exec-server/tests/support/lib.rs",
    "codex-rs/exec-server/tests/support/relay.rs",
    "codex-rs/exec-server/tests/unit/client_provisioning_tests.rs",
    "codex-rs/login/Cargo.toml",
    "codex-rs/login/src/auth/agent_identity.rs",
    "codex-rs/login/src/auth/auth_headers.rs",
    "codex-rs/login/src/auth/auth_tests.rs",
    "codex-rs/login/src/auth/bedrock_access_keys.rs",
    "codex-rs/login/src/auth/bedrock_api_key.rs",
    "codex-rs/login/src/auth/bedrock_api_key_tests.rs",
    "codex-rs/login/src/auth/default_client.rs",
    "codex-rs/login/src/auth/external_bearer.rs",
    "codex-rs/login/src/auth/manager.rs",
    "codex-rs/login/src/auth/mod.rs",
    "codex-rs/login/src/auth/storage.rs",
    "codex-rs/login/src/auth/storage_tests.rs",
    "codex-rs/login/src/auth/workload_identity.rs",
    "codex-rs/login/src/auth/workload_identity_tests.rs",
    "codex-rs/login/src/callback_params.rs",
    "codex-rs/login/src/callback_params_tests.rs",
    "codex-rs/login/src/device_code_auth.rs",
    "codex-rs/login/src/lib.rs",
    "codex-rs/login/src/server.rs",
    "codex-rs/login/src/test_support.rs",
    "codex-rs/login/src/token_data_tests.rs",
    "codex-rs/login/tests/suite/auth_refresh.rs",
    "codex-rs/login/tests/suite/login_server_e2e.rs",
    "codex-rs/login/tests/suite/logout.rs",
    "codex-rs/websocket-client/src/dialer.rs",
    "codex-rs/websocket-client/src/dialer_tests.rs",
    "codex-rs/websocket-client/src/lib.rs",
    "codex-rs/websocket-client/src/lib_tests.rs",
]
SDK_BUNDLE_PROBE_PATHS_SHA256 = "bafd2e6ed26638c260c5979c65db9a60ea5b085c69189439bdd7a9759c9e114d"
RMCP_PREIMAGE_PATHS = [
    "codex-rs/rmcp-client/Cargo.toml",
    "codex-rs/rmcp-client/src/auth_status.rs",
    "codex-rs/rmcp-client/src/bin/rmcp_test_server.rs",
    "codex-rs/rmcp-client/src/bin/test_mcp_2026_discovery_stdio_server.rs",
    "codex-rs/rmcp-client/src/bin/test_mcp_2026_stdio_server.rs",
    "codex-rs/rmcp-client/src/bin/test_stdio_server.rs",
    "codex-rs/rmcp-client/src/bin/test_streamable_http_server.rs",
    "codex-rs/rmcp-client/src/bounded_stdio_transport.rs",
    "codex-rs/rmcp-client/src/elicitation_client_service.rs",
    "codex-rs/rmcp-client/src/ema_auth_policy.rs",
    "codex-rs/rmcp-client/src/ema_auth_policy_tests.rs",
    "codex-rs/rmcp-client/src/ema_claims.rs",
    "codex-rs/rmcp-client/src/ema_exchange.rs",
    "codex-rs/rmcp-client/src/ema_exchange_tests.rs",
    "codex-rs/rmcp-client/src/ema_identity.rs",
    "codex-rs/rmcp-client/src/ema_identity_tests.rs",
    "codex-rs/rmcp-client/src/event_notification_transport.rs",
    "codex-rs/rmcp-client/src/executor_process_transport.rs",
    "codex-rs/rmcp-client/src/executor_process_transport_tests.rs",
    "codex-rs/rmcp-client/src/http_client_adapter.rs",
    "codex-rs/rmcp-client/src/http_client_adapter/www_authenticate.rs",
    "codex-rs/rmcp-client/src/http_client_adapter/www_authenticate_tests.rs",
    "codex-rs/rmcp-client/src/http_client_adapter_tests.rs",
    "codex-rs/rmcp-client/src/http_client_redirect.rs",
    "codex-rs/rmcp-client/src/http_client_redirect_tests.rs",
    "codex-rs/rmcp-client/src/http_headers.rs",
    "codex-rs/rmcp-client/src/http_headers_tests.rs",
    "codex-rs/rmcp-client/src/lib.rs",
    "codex-rs/rmcp-client/src/local_child.rs",
    "codex-rs/rmcp-client/src/local_stdio_transport.rs",
    "codex-rs/rmcp-client/src/logging_client_handler.rs",
    "codex-rs/rmcp-client/src/macos_stdio.rs",
    "codex-rs/rmcp-client/src/macos_stdio_tests.rs",
    "codex-rs/rmcp-client/src/oauth.rs",
    "codex-rs/rmcp-client/src/oauth/credential_store.rs",
    "codex-rs/rmcp-client/src/oauth/ema_identity.rs",
    "codex-rs/rmcp-client/src/oauth/ema_identity_tests.rs",
    "codex-rs/rmcp-client/src/oauth/issuer_binding.rs",
    "codex-rs/rmcp-client/src/oauth/refresh_lock.rs",
    "codex-rs/rmcp-client/src/oauth/refresh_transaction.rs",
    "codex-rs/rmcp-client/src/oauth/resolved_store.rs",
    "codex-rs/rmcp-client/src/oauth/runtime.rs",
    "codex-rs/rmcp-client/src/oauth/store_lock.rs",
    "codex-rs/rmcp-client/src/oauth/test_support.rs",
    "codex-rs/rmcp-client/src/oauth/tests/credential_store_tests.rs",
    "codex-rs/rmcp-client/src/oauth/tests/persistor_tests.rs",
    "codex-rs/rmcp-client/src/oauth/tests/store_lock_tests.rs",
    "codex-rs/rmcp-client/src/oauth_callback.rs",
    "codex-rs/rmcp-client/src/oauth_callback_tests.rs",
    "codex-rs/rmcp-client/src/oauth_client_registration.rs",
    "codex-rs/rmcp-client/src/oauth_client_registration_tests.rs",
    "codex-rs/rmcp-client/src/oauth_http_client.rs",
    "codex-rs/rmcp-client/src/oauth_http_client_security_tests.rs",
    "codex-rs/rmcp-client/src/oauth_refresh_mode.rs",
    "codex-rs/rmcp-client/src/perform_oauth_device_login.rs",
    "codex-rs/rmcp-client/src/perform_oauth_login.rs",
    "codex-rs/rmcp-client/src/program_resolver.rs",
    "codex-rs/rmcp-client/src/protocol_mode.rs",
    "codex-rs/rmcp-client/src/rmcp_client.rs",
    "codex-rs/rmcp-client/src/service_error.rs",
    "codex-rs/rmcp-client/src/startup_error.rs",
    "codex-rs/rmcp-client/src/startup_error_tests.rs",
    "codex-rs/rmcp-client/src/stdio_server_launcher.rs",
    "codex-rs/rmcp-client/src/streamable_http_retry.rs",
    "codex-rs/rmcp-client/src/streamable_http_retry_tests.rs",
    "codex-rs/rmcp-client/src/utils.rs",
    "codex-rs/rmcp-client/tests/mcp_2026_discovery.rs",
    "codex-rs/rmcp-client/tests/mcp_2026_message_limits.rs",
    "codex-rs/rmcp-client/tests/mcp_2026_mrtr.rs",
    "codex-rs/rmcp-client/tests/mcp_2026_oauth_discovery.rs",
    "codex-rs/rmcp-client/tests/mcp_2026_sse_discovery.rs",
    "codex-rs/rmcp-client/tests/mcp_2026_stdio.rs",
    "codex-rs/rmcp-client/tests/mcp_2026_stdio_discovery.rs",
    "codex-rs/rmcp-client/tests/mcp_events.rs",
    "codex-rs/rmcp-client/tests/process_group_cleanup.rs",
    "codex-rs/rmcp-client/tests/resources.rs",
    "codex-rs/rmcp-client/tests/stdio_message_limits.rs",
    "codex-rs/rmcp-client/tests/streamable_http_oauth_startup.rs",
    "codex-rs/rmcp-client/tests/streamable_http_oauth_store_pinning.rs",
    "codex-rs/rmcp-client/tests/streamable_http_recovery.rs",
    "codex-rs/rmcp-client/tests/streamable_http_remote.rs",
    "codex-rs/rmcp-client/tests/streamable_http_test_support.rs",
]
RMCP_PREIMAGE_PATHS_SHA256 = "706df09a76a0d40d204dbd13b950ce57f83e43b6999f73343e3d2b873ff98ba7"
SDK_BUNDLE_ROUTE_WITNESS = ("100644", "blob", "29705e44eb66f235adee5a8932264ee778f98ced")
ALLOWED_MUTABLE_PATHS = sorted(set(OVERLAY_CHANGED_PATHS) | set(GENERATED_PATHS))

ROOT_MANIFEST_PATH = "codex-rs/Cargo.toml"
ROOT_LOCK_PATH = "codex-rs/Cargo.lock"
ROOT_CLOSURE_SHA256 = "489401f326edc4f9e9b3a4b2aa57de6be1ff5eb5cc29390761d9d1b6f2ac29fb"
REQUIRED_ROOT_MEMBERS = {
    "agent-roles",
    "app-server-protocol-noop-macros",
    "attachment-store",
    "build-info",
    "code-mode-protocol",
    "code-mode-runtime",
    "codex-home",
    "config-schema",
    "diagnostics",
    "ext/guardian-v2",
    "ext/history-notes",
    "ext/queue",
    "guardian-context",
    "history",
    "mxc-sandbox",
    "otel-trace-websocket",
    "utils/audio",
    "utils/git-discovery",
    "utils/redacted-string",
    "windows-sandbox-service",
    "workload-identity",
    "worktree",
}
DEFERRED_ROOT_MEMBERS = {"voice-host"}
REQUIRED_WORKSPACE_DEPENDENCIES: dict[str, Any] = {
    "appcontainer_common": {
        "git": "https://github.com/microsoft/mxc",
        "rev": "6cd3d58f05d3447e67109cfb75e042803b843ca4",
    },
    "bitflags": "2.13.1",
    "codex-agent-roles": {"path": "agent-roles"},
    "codex-app-server-protocol-noop-macros": {"path": "app-server-protocol-noop-macros"},
    "codex-attachment-store": {"path": "attachment-store"},
    "codex-build-info": {"path": "build-info"},
    "codex-code-mode-runtime": {"path": "code-mode-runtime"},
    "codex-diagnostics": {"path": "diagnostics"},
    "codex-guardian-context": {"path": "guardian-context"},
    "codex-history": {"path": "history"},
    "codex-mxc-sandbox": {"path": "mxc-sandbox"},
    "codex-otel-trace-websocket": {"path": "otel-trace-websocket"},
    "codex-utils-audio": {"path": "utils/audio"},
    "codex-utils-git-discovery": {"path": "utils/git-discovery"},
    "codex-utils-redacted-string": {"path": "utils/redacted-string"},
    "gix-url": "0.35.2",
    "learning_mode_windows": {
        "git": "https://github.com/microsoft/mxc",
        "rev": "6cd3d58f05d3447e67109cfb75e042803b843ca4",
    },
    "rustix": {"version": "1.1.4", "features": ["net"]},
    "tikv-jemallocator": "=0.7.0",
    "tree-sitter-powershell": "=0.26.4",
    "wxc_common": {
        "git": "https://github.com/microsoft/mxc",
        "rev": "6cd3d58f05d3447e67109cfb75e042803b843ca4",
    },
}
DEFERRED_WORKSPACE_DEPENDENCIES = {
    "codex-guardian-v2",
    "codex-history-notes-extension",
    "codex-queue-extension",
    "codex-workload-identity",
    "codex-worktree",
}
EXPECTED_V8_LOCK_ENTRY = {
    "name": "v8",
    "version": "150.4.0",
    "source": "registry+https://github.com/rust-lang/crates.io-index",
    "checksum": "42a978ff11f15b24e5c05a7123cf2b68f41e763546699781a924ef4e2cf43a49",
    "dependencies": [
        "bindgen",
        "bitflags 2.13.1",
        "fslock",
        "gzip-header",
        "home",
        "miniz_oxide",
        "paste",
        "temporal_capi",
        "which 6.0.3",
    ],
}
EXPECTED_LOCK_EDGES_BY_STAGE = {
    "accepted-seed": {
        ("prost-build", "0.12.6"): "heck 0.4.1",
        ("prost-build", "0.14.4"): "heck 0.4.1",
    },
    "resolved-composed": {
        ("prost-build", "0.12.6"): "heck 0.5.0",
        ("prost-build", "0.14.4"): "heck 0.5.0",
    },
}
# A non-empty resolver delta is diagnostic-only until the root explicitly
# accepts its exact canonical digest in a reviewed successor.
ACCEPTED_LOCK_DELTA_SHA256: str | None = "8d80a8c0eae8c1055d266730a5c1ac3e645f59349614ba0e4eb641ee622ea4d6"
MAX_MANIFEST_COUNT = 512
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_LOCK_BYTES = 16 * 1024 * 1024
MAX_LOCK_PACKAGE_COUNT = 20_000

V8_DIAGNOSTIC_PATHS = sorted(
    [
        "MODULE.bazel",
        "MODULE.bazel.lock",
        "codex-rs/Cargo.lock",
        "codex-rs/Cargo.toml",
        "patches/v8_bazel_rules.patch",
        "patches/v8_module_deps.patch",
        "patches/v8_source_portability.patch",
        "third_party/v8/BUILD.bazel",
        "third_party/v8/README.md",
        "third_party/v8/libcxx.BUILD.bazel",
        "third_party/v8/llvm_libc.BUILD.bazel",
        "third_party/v8/rusty_v8_149_2_0.sha256",
        "third_party/v8/rusty_v8_150_4_0.sha256",
    ]
)
V8_PATCH_PATHS = [
    "patches/v8_bazel_rules.patch",
    "patches/v8_module_deps.patch",
    "patches/v8_source_portability.patch",
]
EXPECTED_V8_PATCH_ORDER = [
    "v8_module_deps.patch",
    "v8_bazel_rules.patch",
    "v8_source_portability.patch",
]
MAX_TREE_ENTRY_COUNT = 100_000
MAX_METADATA_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_METADATA_BLOB_BYTES = 2 * 1024 * 1024

SDK_RUNTIME_DEPENDENCY = "openai-codex-cli-bin==0.147.0"
SDK_RUNTIME_VERSION = "0.147.0"
UV_VERSION = "0.11.3"
NODE_MAJOR = 22
BAZELISK_VERSION = "1.28.1"
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
PACKAGE_MANAGER_PATTERN = re.compile(r"pnpm@(\d+\.\d+\.\d+)\+sha512\.[A-Za-z0-9+/=]+")
MINIMUM_VERSION_PATTERN = re.compile(r">=(\d+)(?:\.(\d+))?(?:\.(\d+))?")
UV_IDENTITY_PATTERN = re.compile(
    r"(?P<name>[a-z][a-z0-9-]{0,31}) (?P<version>\d+\.\d+\.\d+)"
    r"(?: \((?P<target>[A-Za-z0-9][A-Za-z0-9_.+-]{0,63})\))?"
)
MAX_INPUT_TEXT_BYTES = 2 * 1024 * 1024

EXECUTION_INPUT_MODES = {
    ".bazelversion": "100644",
    ".github/actions/setup-bazel-ci/action.yml": "100644",
    ".github/workflows/repo-checks.yml": "100644",
    "MODULE.bazel": "100644",
    "MODULE.bazel.lock": "100644",
    "package.json": "100644",
    "pnpm-lock.yaml": "100644",
    "pnpm-workspace.yaml": "100644",
    "sdk/python/pyproject.toml": "100644",
    "sdk/python/uv.lock": "100644",
    "sdk/python/scripts/update_sdk_artifacts.py": "100755",
    "sdk/python/tests/test_contract_generation.py": "100644",
    "sdk/python/tests/test_client_rpc_methods.py": "100644",
    "sdk/python/src/openai_codex/api.py": "100644",
    "sdk/python/src/openai_codex/generated/notification_registry.py": "100644",
    "sdk/python/src/openai_codex/generated/v2_all.py": "100644",
    "sdk/typescript/package.json": "100644",
    **{
        path: postimage[0]
        for path, postimage in OVERLAY_SOURCE_ENTRIES.items()
        if postimage is not None
    },
    **{path: "100644" for path in PATCH_DEPENDENCIES},
}

EXPECTED_SDK_BUNDLE_HEADS = {
    f"refs/w13825-sdk-{SDK_INPUT_RUN_ID}-{SDK_INPUT_RUN_ATTEMPT}/base": BASE_SHA,
    f"refs/w13825-sdk-{SDK_INPUT_RUN_ID}-{SDK_INPUT_RUN_ATTEMPT}/candidate": SDK_CANDIDATE_SHA,
    f"refs/w13825-sdk-{SDK_INPUT_RUN_ID}-{SDK_INPUT_RUN_ATTEMPT}/materialized": MATERIALIZED_SHA,
    f"refs/w13825-sdk-{SDK_INPUT_RUN_ID}-{SDK_INPUT_RUN_ATTEMPT}/source": SDK_SOURCE_SHA,
    f"refs/w13825-sdk-{SDK_INPUT_RUN_ID}-{SDK_INPUT_RUN_ATTEMPT}/upstream": GLOBAL_UPSTREAM_SHA,
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def run(
    *args: str,
    cwd: pathlib.Path | None = None,
    env: dict[str, str] | None = None,
) -> str:
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    ).stdout


def run_bytes(*args: str, cwd: pathlib.Path | None = None) -> bytes:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True).stdout


def run_tool(
    label: str,
    *args: str,
    cwd: pathlib.Path,
    env: dict[str, str] | None = None,
) -> str:
    result = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        combined = "\n".join(part for part in (result.stdout, result.stderr) if part)
        safe = combined.replace(str(cwd), "<candidate-worktree>")
        safe = re.sub(r"https?://\S+", "<redacted-url>", safe)
        safe = re.sub(
            r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b",
            "<redacted-token>",
            safe,
        )
        lines = safe.splitlines()
        excerpt = "\n".join(lines[-80:]) or "<no diagnostic output>"
        raise SystemExit(f"{label} failed with exit {result.returncode}\n{excerpt}")
    return result.stdout


def sanitize_git_stderr(stderr: str, *private_paths: pathlib.Path) -> str:
    replacements = sorted((str(path) for path in private_paths), key=len, reverse=True)
    diagnostics: list[str] = []
    omitted = False
    for raw_line in stderr.splitlines():
        line = raw_line
        for value in replacements:
            line = line.replace(value, "<path>")
        line = re.sub(r"https?://\S+", "<redacted-url>", line)
        line = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b", "<redacted-token>", line)
        line = "".join(character for character in line if character.isprintable())
        if re.match(r"^(fatal|error|warning|hint):", line) is None:
            omitted = True
            continue
        if len(diagnostics) == 8:
            omitted = True
            break
        diagnostics.append(line[:240])
        omitted = omitted or len(line) > 240
    if omitted:
        diagnostics.append("<additional Git stderr omitted>")
    return "\n".join(diagnostics) or "<no sanitized Git diagnostic>"


def fetch_bundle_ref(bare: pathlib.Path, bundle: pathlib.Path, source_ref: str, target_ref: str) -> None:
    result = subprocess.run(
        ("git", "-C", str(bare), "fetch", str(bundle), f"+{source_ref}:{target_ref}"),
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        diagnostic = sanitize_git_stderr(result.stderr, bare, bundle, bundle.parent)
        raise SystemExit(f"candidate bundle import failed (git exit {result.returncode})\n{diagnostic}")


def digest(path: pathlib.Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def load(path: pathlib.Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        result = json.load(stream)
    require(isinstance(result, dict), f"{path.name} must contain an object")
    return result


def path_digest(paths: list[str]) -> str:
    data = ("\n".join(paths) + ("\n" if paths else "")).encode()
    return hashlib.sha256(data).hexdigest()


def mode_oid_path_digest(entries: dict[str, tuple[str, str, str]]) -> str:
    canonical = b"".join(
        f"{mode} {oid}\t{path}".encode("utf-8") + b"\0"
        for path, (mode, _object_type, oid) in sorted(entries.items())
    )
    return hashlib.sha256(canonical).hexdigest()


def mode_type_oid_path_digest(entries: dict[str, tuple[str, str, str]]) -> str:
    canonical = b"".join(
        f"{mode} {_object_type} {oid}\t{path}".encode("utf-8") + b"\0"
        for path, (mode, _object_type, oid) in sorted(entries.items())
    )
    return hashlib.sha256(canonical).hexdigest()


def overlay_operation(
    preimage: tuple[str, str, str] | None,
    postimage: tuple[str, str, str] | None,
) -> str:
    require(preimage is not None or postimage is not None, "overlay entry cannot be absent on both sides")
    if preimage is None:
        return "A"
    if postimage is None:
        return "D"
    if preimage == postimage:
        return "E"
    return "M"


def verify_overlay_contract(repo: pathlib.Path) -> dict[str, Any]:
    require(list(BUILD_SOURCE_ENTRIES) == BUILD_PATHS, "build source path set mismatch")
    require(list(BUILD_SOURCE_PREIMAGE_ENTRIES) == BUILD_PATHS, "build source preimage set mismatch")
    require(list(RESTORE_SOURCE_ENTRIES) == RESTORE_PATHS, "restore source path set mismatch")
    require(
        list(RESTORE_SOURCE_PREIMAGE_ENTRIES) == RESTORE_PATHS,
        "restore source preimage set mismatch",
    )
    require(list(OVERLAY_SOURCE_ENTRIES) == OVERLAY_PATHS, "overlay source path set mismatch")
    require(
        list(OVERLAY_SOURCE_PREIMAGE_ENTRIES) == OVERLAY_PATHS,
        "overlay source preimage set mismatch",
    )
    require(path_digest(BUILD_PATHS) == BUILD_PATHS_SHA256, "build source path digest mismatch")
    require(path_digest(RESTORE_PATHS) == RESTORE_PATHS_SHA256, "restore path digest mismatch")
    require(path_digest(CORE_SKILLS_PATHS) == CORE_SKILLS_PATHS_SHA256, "core-skills path digest mismatch")
    require(path_digest(OVERLAY_PATHS) == OVERLAY_PATHS_SHA256, "overlay path digest mismatch")
    require(
        path_digest(OVERLAY_CHANGED_PATHS) == OVERLAY_CHANGED_PATHS_SHA256,
        "changed overlay path digest mismatch",
    )
    require(
        mode_oid_path_digest(RESTORE_SOURCE_ENTRIES) == RESTORE_ENTRIES_SHA256,
        "restore entry digest mismatch",
    )
    require(
        mode_type_oid_path_digest(CORE_SKILLS_SOURCE_ENTRIES) == CORE_SKILLS_ENTRIES_SHA256,
        "core-skills entry digest mismatch",
    )
    core_additions = {
        path: entry
        for path, entry in CORE_SKILLS_SOURCE_ENTRIES.items()
        if CORE_SKILLS_SOURCE_PREIMAGE_ENTRIES[path] is None
    }
    core_exact = {
        path: entry
        for path, entry in CORE_SKILLS_SOURCE_ENTRIES.items()
        if CORE_SKILLS_SOURCE_PREIMAGE_ENTRIES[path] == entry
    }
    require(path_digest(sorted(core_additions)) == CORE_SKILLS_ADDITIONS_SHA256, "core-skills additions digest mismatch")
    require(path_digest(sorted(core_exact)) == CORE_SKILLS_EXACT_SHA256, "core-skills exact digest mismatch")
    operations: dict[str, list[str]] = {state: [] for state in ("A", "M", "D", "E")}
    for path in OVERLAY_PATHS:
        preimage = OVERLAY_SOURCE_PREIMAGE_ENTRIES[path]
        postimage = OVERLAY_SOURCE_ENTRIES[path]
        operation = overlay_operation(preimage, postimage)
        operations[operation].append(path)
        require(tree_entry(repo, SDK_CANDIDATE_SHA, path) == preimage, f"overlay preimage mismatch: {path}")
        require(tree_entry(repo, BUILD_SOURCE_SHA, path) == postimage, f"overlay postimage mismatch: {path}")
    changed = sorted([*operations["A"], *operations["M"], *operations["D"]])
    require(changed == OVERLAY_CHANGED_PATHS, "overlay changed path set mismatch")
    require(len(operations["A"]) == 36, "overlay addition count mismatch")
    require(len(operations["M"]) == 12, "overlay modification count mismatch")
    require(not operations["D"], "unexpected current overlay deletion")
    require(len(operations["E"]) == 12, "overlay exact-retention count mismatch")
    return {
        "declared_path_count": len(OVERLAY_PATHS),
        "declared_path_set_sha256": OVERLAY_PATHS_SHA256,
        "changed_path_count": len(OVERLAY_CHANGED_PATHS),
        "changed_path_set_sha256": OVERLAY_CHANGED_PATHS_SHA256,
        "restore_path_count": len(RESTORE_PATHS),
        "restore_path_set_sha256": RESTORE_PATHS_SHA256,
        "restore_entries_sha256": RESTORE_ENTRIES_SHA256,
        "core_skills_path_count": len(CORE_SKILLS_PATHS),
        "core_skills_path_set_sha256": CORE_SKILLS_PATHS_SHA256,
        "core_skills_entries_sha256": CORE_SKILLS_ENTRIES_SHA256,
        "core_skills_additions_sha256": CORE_SKILLS_ADDITIONS_SHA256,
        "core_skills_exact_sha256": CORE_SKILLS_EXACT_SHA256,
        "operations": {
            state: {
                "count": len(paths),
                "path_set_sha256": path_digest(paths),
                "paths": paths,
            }
            for state, paths in operations.items()
        },
    }


def absolute_argument(path: pathlib.Path, name: str, *, must_exist: bool) -> pathlib.Path:
    require(path.is_absolute(), f"{name} must be absolute")
    return path.resolve(strict=must_exist)


def tuple_json(entry: tuple[str, str, str] | None) -> dict[str, str] | None:
    if entry is None:
        return None
    mode, object_type, oid = entry
    return {"mode": mode, "type": object_type, "oid": oid}


def tree_entry(
    repo: pathlib.Path,
    revision: str,
    path: str,
) -> tuple[str, str, str] | None:
    output = run_bytes("git", "ls-tree", "--full-tree", "-z", revision, "--", path, cwd=repo)
    if not output:
        return None
    require(output.endswith(b"\0") and output.count(b"\0") == 1, f"ambiguous tree entry: {path}")
    metadata, listed = output[:-1].split(b"\t", 1)
    mode, object_type, oid = metadata.decode("ascii").split()
    require(listed.decode("utf-8", "surrogateescape") == path, f"unexpected tree path: {path}")
    require(mode in {"100644", "100755", "120000", "160000"}, f"unsupported Git mode for {path}: {mode}")
    require(object_type == ("commit" if mode == "160000" else "blob"), f"mode/type mismatch for {path}")
    require(SHA_PATTERN.fullmatch(oid) is not None, f"invalid object ID for {path}")
    return mode, object_type, oid


def normalized_tree_path(raw: bytes) -> str:
    try:
        path = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SystemExit("tree entry path is not UTF-8") from error
    require(path and len(path.encode("utf-8")) <= 4096, "tree entry path length is invalid")
    require(re.search(r"[\x00-\x1f\x7f]", path) is None, f"tree entry path contains control bytes: {path!r}")
    normalized = pathlib.PurePosixPath(path)
    require(not normalized.is_absolute(), f"tree entry path is absolute: {path}")
    require(all(part not in {"", ".", ".."} for part in normalized.parts), f"tree entry path is unsafe: {path}")
    require(normalized.as_posix() == path, f"tree entry path is not normalized: {path}")
    return path


def canonical_entry_bytes(entries: list[list[str]]) -> bytes:
    return b"".join(
        f"{mode} {object_type} {oid}\t{path}".encode("utf-8") + b"\0"
        for mode, object_type, oid, path in entries
    )


def full_tree_manifest(
    repo: pathlib.Path,
    revision: str,
    expected_tree: str,
    expected_parent: str,
) -> dict[str, Any]:
    require(SHA_PATTERN.fullmatch(revision) is not None, "tree manifest revision is invalid")
    require(SHA_PATTERN.fullmatch(expected_tree) is not None, "tree manifest expected tree is invalid")
    require(SHA_PATTERN.fullmatch(expected_parent) is not None, "tree manifest expected parent is invalid")
    require(
        run("git", "rev-parse", f"{revision}^{{tree}}", cwd=repo).strip() == expected_tree,
        "tree manifest root mismatch",
    )
    require(
        run("git", "show", "-s", "--format=%P", revision, cwd=repo).split() == [expected_parent],
        "tree manifest parent mismatch",
    )

    output = run_bytes("git", "ls-tree", "--full-tree", "-r", "-z", revision, cwd=repo)
    require(output and output.endswith(b"\0"), "tree manifest listing is empty or incomplete")
    entries: list[list[str]] = []
    seen: set[str] = set()
    for record in output[:-1].split(b"\0"):
        require(record and b"\t" in record, "tree manifest record is malformed")
        metadata, raw_path = record.split(b"\t", 1)
        try:
            mode, object_type, oid = metadata.decode("ascii").split()
        except (UnicodeDecodeError, ValueError) as error:
            raise SystemExit("tree manifest metadata is malformed") from error
        path = normalized_tree_path(raw_path)
        require(path not in seen, f"tree manifest contains duplicate path: {path}")
        require(mode in {"100644", "100755", "120000", "160000"}, f"unsupported Git mode for {path}: {mode}")
        require(object_type == ("commit" if mode == "160000" else "blob"), f"mode/type mismatch for {path}")
        require(SHA_PATTERN.fullmatch(oid) is not None, f"invalid object ID for {path}")
        seen.add(path)
        entries.append([mode, object_type, oid, path])
        require(len(entries) <= MAX_TREE_ENTRY_COUNT, "tree manifest entry bound exceeded")
    entries.sort(key=lambda entry: entry[3].encode("utf-8"))
    paths = [entry[3] for entry in entries]
    canonical = canonical_entry_bytes(entries)
    return {
        "complete": True,
        "commit_sha": revision,
        "tree_sha": expected_tree,
        "parent_sha": expected_parent,
        "entry_count": len(entries),
        "path_set_sha256": path_digest(paths),
        "canonical_entries_sha256": hashlib.sha256(canonical).hexdigest(),
        "entries": entries,
    }


def manifest_entry_map(subject: dict[str, Any]) -> dict[str, list[str]]:
    entries = subject.get("entries")
    require(isinstance(entries, list), "tree manifest entries are unavailable")
    result: dict[str, list[str]] = {}
    for entry in entries:
        require(isinstance(entry, list) and len(entry) == 4, "tree manifest entry shape is invalid")
        mode, object_type, oid, path = entry
        require(all(isinstance(value, str) for value in entry), "tree manifest entry value is invalid")
        require(path not in result, f"tree manifest entry is duplicated: {path}")
        result[path] = [mode, object_type, oid, path]
    return result


def in_memory_composed_manifest(
    repo: pathlib.Path,
    sdk_manifest: dict[str, Any],
) -> dict[str, Any]:
    entries = manifest_entry_map(sdk_manifest)
    before = {path: list(entry) for path, entry in entries.items()}
    overlay_contract = verify_overlay_contract(repo)
    for path in OVERLAY_PATHS:
        expected = OVERLAY_SOURCE_ENTRIES[path]
        if expected is None:
            entries.pop(path, None)
        else:
            mode, object_type, oid = expected
            entries[path] = [mode, object_type, oid, path]

    changed = sorted(path for path in set(entries) | set(before) if entries.get(path) != before.get(path))
    require(
        changed == OVERLAY_CHANGED_PATHS,
        "in-memory overlay escaped or omitted the exact declared changed path set",
    )
    composed_entries = sorted(entries.values(), key=lambda entry: entry[3].encode("utf-8"))
    expected_entry_count = len(before) + len(overlay_contract["operations"]["A"]["paths"])
    expected_entry_count -= len(overlay_contract["operations"]["D"]["paths"])
    require(len(composed_entries) == expected_entry_count, "in-memory overlay entry count mismatch")
    canonical = canonical_entry_bytes(composed_entries)
    return {
        "complete": True,
        "identity_kind": "in-memory-overlay",
        "base_commit_sha": SDK_CANDIDATE_SHA,
        "base_tree_sha": SDK_CANDIDATE_TREE,
        "build_source_sha": BUILD_SOURCE_SHA,
        "build_source_tree": BUILD_SOURCE_TREE,
        "overlay_path_count": len(changed),
        "overlay_path_set_sha256": path_digest(changed),
        "overlay_contract": overlay_contract,
        "entry_count": len(composed_entries),
        "path_set_sha256": path_digest([entry[3] for entry in composed_entries]),
        "canonical_entries_sha256": hashlib.sha256(canonical).hexdigest(),
        "entries": composed_entries,
    }


def index_entry(repo: pathlib.Path, path: str) -> tuple[str, str, str] | None:
    output = run_bytes("git", "ls-files", "--stage", "-z", "--", path, cwd=repo)
    if not output:
        return None
    require(output.endswith(b"\0") and output.count(b"\0") == 1, f"ambiguous index entry: {path}")
    metadata, listed = output[:-1].split(b"\t", 1)
    mode, oid, stage = metadata.decode("ascii").split()
    require(stage == "0", f"unmerged index entry: {path}")
    require(listed.decode("utf-8", "surrogateescape") == path, f"unexpected index path: {path}")
    return mode, "blob", oid


def write_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    require(load(path) == value, f"{path.name} readback mismatch")


def manifest_inventory_from_tree(repo: pathlib.Path, revision: str) -> list[dict[str, str]]:
    output = run_bytes("git", "ls-tree", "--full-tree", "-r", "-z", revision, "--", "codex-rs", cwd=repo)
    inventory: list[dict[str, str]] = []
    for record in output.removesuffix(b"\0").split(b"\0") if output else []:
        metadata, raw_path = record.split(b"\t", 1)
        mode, object_type, oid = metadata.decode("ascii").split()
        path = normalized_tree_path(raw_path)
        if path == ROOT_MANIFEST_PATH or path.endswith("/Cargo.toml"):
            require(object_type == "blob", f"accepted manifest is not a blob: {path}")
            inventory.append({"mode": mode, "oid": oid, "path": path})
    inventory.sort(key=lambda item: item["path"].encode("utf-8"))
    require(0 < len(inventory) <= MAX_MANIFEST_COUNT, "accepted manifest inventory bound exceeded")
    return inventory


def manifest_inventory_from_index(repo: pathlib.Path) -> list[dict[str, str]]:
    output = run_bytes("git", "ls-files", "--stage", "-z", "--", "codex-rs", cwd=repo)
    inventory: list[dict[str, str]] = []
    for record in output.removesuffix(b"\0").split(b"\0") if output else []:
        metadata, raw_path = record.split(b"\t", 1)
        mode, oid, stage = metadata.decode("ascii").split()
        path = normalized_tree_path(raw_path)
        if path == ROOT_MANIFEST_PATH or path.endswith("/Cargo.toml"):
            require(stage == "0", f"composed manifest is unmerged: {path}")
            inventory.append({"mode": mode, "oid": oid, "path": path})
    inventory.sort(key=lambda item: item["path"].encode("utf-8"))
    require(0 < len(inventory) <= MAX_MANIFEST_COUNT, "composed manifest inventory bound exceeded")
    return inventory


def manifest_inventory_digest(inventory: list[dict[str, str]]) -> str:
    canonical = b"".join(
        f"{item['mode']} {item['oid']}\t{item['path']}".encode("utf-8") + b"\0"
        for item in inventory
    )
    return hashlib.sha256(canonical).hexdigest()


def toml_blob(repo: pathlib.Path, object_name: str, label: str) -> dict[str, Any]:
    raw = run_bytes("git", "show", object_name, cwd=repo)
    require(0 < len(raw) <= MAX_MANIFEST_BYTES, f"{label} size bound exceeded")
    try:
        value = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise SystemExit(f"{label} is not valid UTF-8 TOML: {error}") from error
    require(isinstance(value, dict), f"{label} root is not a table")
    return value


def dependency_tables(manifest: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    result: list[tuple[str, dict[str, Any]]] = []
    names = ("dependencies", "dev-dependencies", "build-dependencies")
    for name in names:
        table = manifest.get(name)
        if isinstance(table, dict):
            result.append((name, table))
    targets = manifest.get("target")
    if isinstance(targets, dict):
        for target, target_table in targets.items():
            if not isinstance(target_table, dict):
                continue
            for name in names:
                table = target_table.get(name)
                if isinstance(table, dict):
                    result.append((f"target.{target}.{name}", table))
    return result


def joined_manifest_path(owner_manifest: str, declared_path: str) -> str | None:
    if not declared_path or "\\" in declared_path or pathlib.PurePosixPath(declared_path).is_absolute():
        return None
    if re.search(r"[\x00-\x1f\x7f]", declared_path):
        return None
    parts = list(pathlib.PurePosixPath(owner_manifest).parent.parts)
    for part in pathlib.PurePosixPath(declared_path).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    if not parts:
        return None
    return pathlib.PurePosixPath(*parts, "Cargo.toml").as_posix()


def manifest_graph_diagnostics(parsed: dict[str, dict[str, Any]]) -> dict[str, Any]:
    mismatches: list[dict[str, Any]] = []
    root = parsed.get(ROOT_MANIFEST_PATH, {})
    workspace = root.get("workspace") if isinstance(root.get("workspace"), dict) else {}
    raw_members = workspace.get("members", [])
    raw_excludes = workspace.get("exclude", [])
    if not isinstance(raw_members, list):
        mismatches.append({"kind": "workspace-members-invalid"})
        raw_members = []
    if not isinstance(raw_excludes, list):
        mismatches.append({"kind": "workspace-excludes-invalid"})
        raw_excludes = []
    elif raw_excludes:
        mismatches.append(
            {
                "kind": "workspace-excludes-unsupported",
                "excludes": raw_excludes,
            }
        )

    resolved_members: list[str] = []
    nested_workspace_count = 0
    for member in raw_members:
        if not isinstance(member, str) or not member:
            mismatches.append({"kind": "workspace-member-invalid", "member": member})
            continue
        if re.search(r"[*?\[]", member):
            mismatches.append({"kind": "workspace-member-glob-unsupported", "member": member})
            continue
        manifest_path = joined_manifest_path(ROOT_MANIFEST_PATH, member)
        if manifest_path is None:
            mismatches.append({"kind": "workspace-member-path-invalid", "member": member})
            continue
        resolved_members.append(manifest_path)
        if manifest_path not in parsed:
            mismatches.append(
                {
                    "kind": "workspace-member-target-missing",
                    "member": member,
                    "target": manifest_path,
                }
            )
        elif isinstance(parsed[manifest_path].get("workspace"), dict):
            nested_workspace_count += 1
            mismatches.append(
                {
                    "kind": "nested-workspace-member-unsupported",
                    "member": member,
                    "target": manifest_path,
                }
            )

    workspace_dependencies = (
        workspace.get("dependencies") if isinstance(workspace.get("dependencies"), dict) else {}
    )
    direct_path_edge_count = 0
    inherited_workspace_edge_count = 0
    path_edges: list[dict[str, str]] = []

    def record_path_edge(
        *,
        consumer: str,
        table: str,
        dependency: str,
        declaration: Any,
        edge_kind: str,
        path_owner: str | None = None,
    ) -> None:
        nonlocal direct_path_edge_count
        path_value = declaration.get("path") if isinstance(declaration, dict) else None
        if path_value is None:
            return
        if edge_kind != "workspace-inherited":
            direct_path_edge_count += 1
        if not isinstance(path_value, str):
            mismatches.append(
                {
                    "kind": "dependency-path-invalid",
                    "consumer": consumer,
                    "table": table,
                    "dependency": dependency,
                }
            )
            return
        target = joined_manifest_path(path_owner or consumer, path_value)
        if target is None:
            mismatches.append(
                {
                    "kind": "dependency-path-invalid",
                    "consumer": consumer,
                    "table": table,
                    "dependency": dependency,
                    "path": path_value,
                }
            )
            return
        edge = {
            "consumer": consumer,
            "table": table,
            "dependency": dependency,
            "edge_kind": edge_kind,
            "declared_path": path_value,
            "target": target,
        }
        path_edges.append(edge)
        if target not in parsed:
            mismatches.append({"kind": "dependency-path-target-missing", **edge})

    for name, declaration in sorted(workspace_dependencies.items()):
        record_path_edge(
            consumer=ROOT_MANIFEST_PATH,
            table="workspace.dependencies",
            dependency=name,
            declaration=declaration,
            edge_kind="workspace-definition",
        )

    for path, manifest in sorted(parsed.items()):
        for table_name, table in dependency_tables(manifest):
            for name, declaration in sorted(table.items()):
                if isinstance(declaration, dict) and declaration.get("workspace") is True:
                    inherited_workspace_edge_count += 1
                    workspace_declaration = workspace_dependencies.get(name)
                    if workspace_declaration is not None:
                        record_path_edge(
                            consumer=path,
                            table=table_name,
                            dependency=name,
                            declaration=workspace_declaration,
                            edge_kind="workspace-inherited",
                            path_owner=ROOT_MANIFEST_PATH,
                        )
                else:
                    record_path_edge(
                        consumer=path,
                        table=table_name,
                        dependency=name,
                        declaration=declaration,
                        edge_kind="direct",
                    )

    mismatches.sort(key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
    missing_targets = sorted(
        {
            item["target"]
            for item in mismatches
            if item["kind"] in {"workspace-member-target-missing", "dependency-path-target-missing"}
        }
    )
    return {
        "workspace_member_spec_count": len(raw_members),
        "workspace_member_literal_count": len(resolved_members),
        "workspace_member_resolved_count": sum(path in parsed for path in resolved_members),
        "workspace_exclude_count": len(raw_excludes),
        "nested_workspace_count": nested_workspace_count,
        "dependency_consumer_edge_count": direct_path_edge_count + inherited_workspace_edge_count,
        "direct_path_edge_count": direct_path_edge_count,
        "inherited_workspace_edge_count": inherited_workspace_edge_count,
        "path_edge_count": len(path_edges),
        "path_edges": path_edges,
        "missing_target_count": len(missing_targets),
        "missing_targets": missing_targets,
        "mismatches": mismatches,
    }


def manifest_structure_fixture_receipt() -> dict[str, Any]:
    direct_consumer = "codex-rs/mcp-server/Cargo.toml"
    direct_path_cases = [
        {
            "table": "dependencies",
            "dependency": "missing_direct_normal",
            "declared_path": "tests/direct-normal",
            "target": "codex-rs/mcp-server/tests/direct-normal/Cargo.toml",
        },
        {
            "table": "dev-dependencies",
            "dependency": "missing_direct_dev",
            "declared_path": "tests/direct-dev",
            "target": "codex-rs/mcp-server/tests/direct-dev/Cargo.toml",
        },
        {
            "table": "build-dependencies",
            "dependency": "missing_direct_build",
            "declared_path": "tests/direct-build",
            "target": "codex-rs/mcp-server/tests/direct-build/Cargo.toml",
        },
        {
            "table": "target.cfg(unix).dependencies",
            "dependency": "missing_direct_target",
            "declared_path": "tests/direct-target",
            "target": "codex-rs/mcp-server/tests/direct-target/Cargo.toml",
        },
    ]
    root = {
        "workspace": {
            "members": ["core-skills", "ext/guardian", "mcp-server"],
            "dependencies": {
                "codex-guardian": {"path": "ext/guardian"},
                "mcp_test_support": {"path": "mcp-server/tests/common"},
            },
        }
    }
    invalid = {
        ROOT_MANIFEST_PATH: root,
        "codex-rs/app-server/Cargo.toml": {
            "dependencies": {"codex-guardian": {"workspace": True}},
        },
        "codex-rs/mcp-server/Cargo.toml": {
            "dependencies": {
                "missing_direct_normal": {"path": "tests/direct-normal"},
            },
            "dev-dependencies": {
                "mcp_test_support": {"workspace": True},
                "missing_direct_dev": {"path": "tests/direct-dev"},
            },
            "build-dependencies": {
                "missing_direct_build": {"path": "tests/direct-build"},
            },
            "target": {
                "cfg(unix)": {
                    "dependencies": {
                        "missing_direct_target": {"path": "tests/direct-target"},
                    },
                },
            },
        },
    }
    rejected = manifest_graph_diagnostics(invalid)
    rejected_kinds = {item["kind"] for item in rejected["mismatches"]}
    require(
        rejected_kinds
        == {"workspace-member-target-missing", "dependency-path-target-missing"},
        "fixture mismatch kinds drifted",
    )
    for case in direct_path_cases:
        expected = {
            "kind": "dependency-path-target-missing",
            "consumer": direct_consumer,
            "table": case["table"],
            "dependency": case["dependency"],
            "edge_kind": "direct",
            "declared_path": case["declared_path"],
            "target": case["target"],
        }
        require(
            expected in rejected["mismatches"],
            f"fixture did not traverse {case['table']}",
        )
    require(rejected["missing_target_count"] >= 4, "fixture did not aggregate missing targets")

    accepted_root = {
        "workspace": {
            "members": ["ext/guardian", "mcp-server"],
            "dependencies": root["workspace"]["dependencies"],
        }
    }
    accepted = {
        **invalid,
        ROOT_MANIFEST_PATH: accepted_root,
        "codex-rs/ext/guardian/Cargo.toml": {"package": {"name": "codex-guardian"}},
        "codex-rs/mcp-server/tests/common/Cargo.toml": {
            "package": {"name": "mcp-test-support"},
        },
        **{
            case["target"]: {"package": {"name": case["dependency"].replace("_", "-")}}
            for case in direct_path_cases
        },
    }
    accepted_result = manifest_graph_diagnostics(accepted)
    require(not accepted_result["mismatches"], "fixture rejected exact declared additions")
    known_additions = [
        "codex-rs/ext/guardian/Cargo.toml",
        "codex-rs/mcp-server/tests/common/Cargo.toml",
    ]
    require(all(path in RESTORE_SOURCE_ENTRIES for path in known_additions), "fixture additions drifted")
    return {
        "schema": "sdk-build-manifest-structure-fixture",
        "version": 1,
        "status": "ready",
        "rejected_mismatch_count": len(rejected["mismatches"]),
        "rejected_mismatch_kinds": sorted(rejected_kinds),
        "rejected_missing_targets": rejected["missing_targets"],
        "direct_dependency_case_count": len(direct_path_cases),
        "direct_dependency_table_coverage": [case["table"] for case in direct_path_cases],
        "accepted_manifest_count": len(accepted),
        "accepted_exact_additions": known_additions,
        "accepted_exact_additions_sha256": path_digest(known_additions),
        "accepted_missing_target_count": accepted_result["missing_target_count"],
    }


def validate_composed_manifests(worktree: pathlib.Path, diagnostics: pathlib.Path) -> dict[str, Any]:
    accepted_inventory = manifest_inventory_from_tree(worktree, SDK_CANDIDATE_SHA)
    composed_inventory = manifest_inventory_from_index(worktree)
    accepted = {item["path"]: item for item in accepted_inventory}
    composed = {item["path"]: item for item in composed_inventory}
    mismatches: list[dict[str, Any]] = []

    overlay_manifest_paths = {
        path for path in OVERLAY_PATHS if path == ROOT_MANIFEST_PATH or path.endswith("/Cargo.toml")
    }
    for path in sorted(set(accepted) | set(composed) | overlay_manifest_paths):
        before = accepted.get(path)
        after = composed.get(path)
        expected_tuple = (
            OVERLAY_SOURCE_ENTRIES[path]
            if path in OVERLAY_SOURCE_ENTRIES
            else (
                (before["mode"], "blob", before["oid"])
                if before is not None
                else None
            )
        )
        actual_tuple = (
            (after["mode"], "blob", after["oid"])
            if after is not None
            else None
        )
        if actual_tuple != expected_tuple:
            mismatches.append(
                {
                    "kind": "manifest-overlay-tuple-mismatch",
                    "path": path,
                    "accepted": before,
                    "expected": tuple_json(expected_tuple),
                    "actual": tuple_json(actual_tuple),
                }
            )

    parsed: dict[str, dict[str, Any]] = {}
    for item in composed_inventory:
        path = item["path"]
        try:
            parsed[path] = toml_blob(worktree, f":{path}", f"composed manifest {path}")
        except SystemExit as error:
            mismatches.append({"kind": "manifest-parse", "path": path, "diagnostic": str(error)[:240]})

    root = parsed.get(ROOT_MANIFEST_PATH, {})
    workspace = root.get("workspace") if isinstance(root.get("workspace"), dict) else {}
    members = workspace.get("members") if isinstance(workspace.get("members"), list) else []
    workspace_dependencies = (
        workspace.get("dependencies") if isinstance(workspace.get("dependencies"), dict) else {}
    )
    graph = manifest_graph_diagnostics(parsed)
    mismatches.extend(graph["mismatches"])
    prior_root = toml_blob(
        worktree,
        f"{VOICE_HOST_DEFERRAL_BASELINE_SHA}:{ROOT_MANIFEST_PATH}",
        "accepted root manifest preimage",
    )
    prior_workspace = prior_root.get("workspace", {})
    prior_members = prior_workspace.get("members", [])
    prior_dependencies = prior_workspace.get("dependencies", {})

    removed_root_members = sorted(set(prior_members) - set(members))
    deferred_root_members = sorted(DEFERRED_ROOT_MEMBERS)
    for member in removed_root_members:
        if member not in DEFERRED_ROOT_MEMBERS:
            mismatches.append({"kind": "prior-member-removed", "member": member})
    for member in deferred_root_members:
        if member not in removed_root_members:
            mismatches.append({"kind": "deferred-member-not-removed", "member": member})
        manifest_path = f"codex-rs/{member}/Cargo.toml"
        if manifest_path not in composed:
            mismatches.append(
                {"kind": "deferred-member-manifest-missing", "member": member, "path": manifest_path}
            )
    for name in sorted(prior_dependencies):
        if workspace_dependencies.get(name) != prior_dependencies[name]:
            mismatches.append(
                {
                    "kind": "prior-workspace-dependency-changed",
                    "dependency": name,
                    "accepted": prior_dependencies[name],
                    "actual": workspace_dependencies.get(name),
                }
            )
    for member in sorted(REQUIRED_ROOT_MEMBERS - set(members)):
        mismatches.append({"kind": "required-member-missing", "member": member})
    for member in sorted(REQUIRED_ROOT_MEMBERS):
        manifest_path = f"codex-rs/{member}/Cargo.toml"
        if manifest_path not in composed:
            mismatches.append(
                {"kind": "required-member-manifest-missing", "member": member, "path": manifest_path}
            )
    for name, expected in sorted(REQUIRED_WORKSPACE_DEPENDENCIES.items()):
        actual = workspace_dependencies.get(name)
        if actual != expected:
            mismatches.append(
                {
                    "kind": "required-workspace-dependency-mismatch",
                    "dependency": name,
                    "expected": expected,
                    "actual": actual,
                }
            )
    for name in sorted(DEFERRED_WORKSPACE_DEPENDENCIES & set(workspace_dependencies)):
        mismatches.append({"kind": "deferred-workspace-dependency-present", "dependency": name})

    workspace_package = workspace.get("package") if isinstance(workspace.get("package"), dict) else {}
    workspace_lints = workspace.get("lints") if isinstance(workspace.get("lints"), dict) else None
    inherited_dependencies: dict[str, list[dict[str, str]]] = {}
    for path, manifest in sorted(parsed.items()):
        for table_name, table in dependency_tables(manifest):
            for name, declaration in table.items():
                if isinstance(declaration, dict) and declaration.get("workspace") is True:
                    inherited_dependencies.setdefault(name, []).append({"path": path, "table": table_name})
        package = manifest.get("package")
        if isinstance(package, dict):
            for field, value in package.items():
                if isinstance(value, dict) and value.get("workspace") is True and field not in workspace_package:
                    mismatches.append(
                        {"kind": "workspace-package-field-missing", "field": field, "path": path}
                    )
        lints = manifest.get("lints")
        if isinstance(lints, dict) and lints.get("workspace") is True and workspace_lints is None:
            mismatches.append({"kind": "workspace-lints-missing", "path": path})
    for name in sorted(set(inherited_dependencies) - set(workspace_dependencies)):
        mismatches.append(
            {
                "kind": "inherited-workspace-dependency-missing",
                "dependency": name,
                "consumers": inherited_dependencies[name],
            }
        )

    closure_lines = [f"member\t{name}" for name in sorted(REQUIRED_ROOT_MEMBERS)]
    closure_lines.extend(f"deferred-member\t{name}" for name in deferred_root_members)
    closure_lines.extend(
        f"dependency\t{name}\t{json.dumps(value, sort_keys=True, separators=(',', ':'))}"
        for name, value in sorted(REQUIRED_WORKSPACE_DEPENDENCIES.items())
    )
    closure_sha256 = hashlib.sha256(("\n".join(closure_lines) + "\n").encode()).hexdigest()
    if closure_sha256 != ROOT_CLOSURE_SHA256:
        mismatches.append(
            {
                "kind": "helper-closure-constant-mismatch",
                "expected": ROOT_CLOSURE_SHA256,
                "actual": closure_sha256,
            }
        )

    mismatches.sort(key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
    receipt = {
        "schema": "sdk-build-composed-manifest-structure",
        "version": 2,
        "status": "ready" if not mismatches else "invalid",
        "input_sdk_candidate": SDK_CANDIDATE_SHA,
        "input_sdk_tree": SDK_CANDIDATE_TREE,
        "build_source_sha": BUILD_SOURCE_SHA,
        "build_source_tree": BUILD_SOURCE_TREE,
        "accepted_manifest_count": len(accepted_inventory),
        "accepted_manifest_inventory_sha256": manifest_inventory_digest(accepted_inventory),
        "accepted_manifest_inventory": accepted_inventory,
        "composed_manifest_count": len(composed_inventory),
        "composed_manifest_inventory_sha256": manifest_inventory_digest(composed_inventory),
        "composed_manifest_inventory": composed_inventory,
        "root_closure_sha256": closure_sha256,
        "required_root_member_count": len(REQUIRED_ROOT_MEMBERS),
        "deferred_root_members": deferred_root_members,
        "removed_root_members": removed_root_members,
        "required_workspace_dependency_count": len(REQUIRED_WORKSPACE_DEPENDENCIES),
        "deferred_workspace_dependencies": sorted(DEFERRED_WORKSPACE_DEPENDENCIES),
        "inherited_workspace_dependency_count": len(inherited_dependencies),
        "workspace_member_spec_count": graph["workspace_member_spec_count"],
        "workspace_member_literal_count": graph["workspace_member_literal_count"],
        "workspace_member_resolved_count": graph["workspace_member_resolved_count"],
        "workspace_exclude_count": graph["workspace_exclude_count"],
        "nested_workspace_count": graph["nested_workspace_count"],
        "dependency_consumer_edge_count": graph["dependency_consumer_edge_count"],
        "direct_path_edge_count": graph["direct_path_edge_count"],
        "inherited_workspace_edge_count": graph["inherited_workspace_edge_count"],
        "path_edge_count": graph["path_edge_count"],
        "missing_target_count": graph["missing_target_count"],
        "missing_targets": graph["missing_targets"],
        "path_edges": graph["path_edges"],
        "overlay_contract": verify_overlay_contract(worktree),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
    }
    write_json(diagnostics / "structural-receipt.json", receipt)
    require(not mismatches, f"composed manifest structure invalid: {len(mismatches)} mismatches")
    return receipt


def safe_command_diagnostic(result: subprocess.CompletedProcess[str], worktree: pathlib.Path) -> list[str]:
    combined = "\n".join(part for part in (result.stdout, result.stderr) if part)
    safe = combined.replace(str(worktree), "<candidate-worktree>")
    safe = re.sub(r"https?://\S+", "<redacted-url>", safe)
    safe = re.sub(
        r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b",
        "<redacted-token>",
        safe,
    )
    lines = ["".join(character for character in line if character.isprintable())[:240] for line in safe.splitlines()]
    return lines[-20:]


def parse_lock_bytes(raw: bytes, label: str) -> tuple[dict[tuple[str, str, str], dict[str, Any]], str | None]:
    if not 0 < len(raw) <= MAX_LOCK_BYTES:
        return {}, f"{label}:size-bound"
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {}, f"{label}:invalid-toml"
    packages = document.get("package")
    if not isinstance(packages, list) or len(packages) > MAX_LOCK_PACKAGE_COUNT:
        return {}, f"{label}:package-bound"
    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    for package in packages:
        if not isinstance(package, dict):
            return {}, f"{label}:package-shape"
        name = package.get("name")
        version = package.get("version")
        source = package.get("source", "")
        if not all(isinstance(value, str) for value in (name, version, source)):
            return {}, f"{label}:package-identity"
        key = (name, version, source)
        if key in result:
            return {}, f"{label}:duplicate-package-identity"
        result[key] = package
    return result, None


def lock_package_sort_key(package: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(package.get("name", "")),
        str(package.get("version", "")),
        str(package.get("source", "")),
    )


def normalized_v8_lock_entries(
    packages: dict[tuple[str, str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            **package,
            "dependencies": sorted(package.get("dependencies", [])),
        }
        for package in sorted(
            (package for package in packages.values() if package.get("name") == "v8"),
            key=lock_package_sort_key,
        )
    ]


def observed_seed_lock_edges(
    packages: dict[tuple[str, str, str], dict[str, Any]],
    stage: str,
    mismatches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    expected_edges = EXPECTED_LOCK_EDGES_BY_STAGE.get(stage)
    if expected_edges is None:
        mismatches.append({"kind": "unknown-lock-edge-stage", "stage": stage})
        return []
    observations: list[dict[str, Any]] = []
    for (name, version), expected_edge in sorted(expected_edges.items()):
        matches = [
            package
            for package in packages.values()
            if package.get("name") == name and package.get("version") == version
        ]
        actual_edges = sorted(
            dependency
            for package in matches
            for dependency in package.get("dependencies", [])
            if isinstance(dependency, str) and (dependency == "heck" or dependency.startswith("heck "))
        )
        observation = {
            "stage": stage,
            "package": name,
            "version": version,
            "expected": expected_edge,
            "actual": actual_edges,
        }
        observations.append(observation)
        if len(matches) != 1 or actual_edges != [expected_edge]:
            mismatches.append({"kind": "seed-lock-edge-mismatch", **observation})
    return observations


def resolve_composed_cargo_lock(worktree: pathlib.Path, diagnostics: pathlib.Path) -> dict[str, Any]:
    lock_path = worktree / ROOT_LOCK_PATH
    before_raw = lock_path.read_bytes()
    before, before_error = parse_lock_bytes(before_raw, "accepted-build-source-lock")
    mismatches: list[dict[str, Any]] = []
    if before_error is not None:
        mismatches.append({"kind": "lock-preimage", "diagnostic": before_error})

    commands: list[dict[str, Any]] = []
    for label, args in (
        (
            "cargo-metadata-seed-preserving-resolution",
            (
                "cargo",
                "metadata",
                "--manifest-path",
                ROOT_MANIFEST_PATH,
                "--format-version",
                "1",
            ),
        ),
        (
            "cargo-metadata-locked",
            (
                "cargo",
                "metadata",
                "--manifest-path",
                ROOT_MANIFEST_PATH,
                "--locked",
                "--format-version",
                "1",
            ),
        ),
    ):
        if commands and commands[-1]["exit_code"] != 0:
            break
        try:
            result = subprocess.run(
                args,
                cwd=worktree,
                check=False,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except OSError:
            command = {
                "label": label,
                "argv": list(args),
                "exit_code": None,
                "diagnostic": ["command executable unavailable"],
            }
            mismatches.append({"kind": "lock-command-unavailable", "command": label})
            commands.append(command)
            break
        command = {"label": label, "argv": list(args), "exit_code": result.returncode}
        if result.returncode != 0:
            command["diagnostic"] = safe_command_diagnostic(result, worktree)
            mismatches.append({"kind": "lock-command-failed", "command": label, "exit_code": result.returncode})
        commands.append(command)

    after_raw = lock_path.read_bytes()
    after, after_error = parse_lock_bytes(after_raw, "generated-composed-lock")
    if after_error is not None:
        mismatches.append({"kind": "lock-output", "diagnostic": after_error})

    before_keys = set(before)
    after_keys = set(after)
    added = sorted((after[key] for key in after_keys - before_keys), key=lock_package_sort_key)
    removed = sorted((before[key] for key in before_keys - after_keys), key=lock_package_sort_key)
    changed = [
        {"identity": list(key), "before": before[key], "after": after[key]}
        for key in sorted(before_keys & after_keys)
        if before[key] != after[key]
    ]

    dependency_edge_changes = []
    for key in sorted(before_keys & after_keys):
        before_edges = sorted(
            dependency
            for dependency in before[key].get("dependencies", [])
            if isinstance(dependency, str)
        )
        after_edges = sorted(
            dependency
            for dependency in after[key].get("dependencies", [])
            if isinstance(dependency, str)
        )
        if before_edges != after_edges:
            dependency_edge_changes.append(
                {
                    "identity": list(key),
                    "added": sorted(set(after_edges) - set(before_edges)),
                    "removed": sorted(set(before_edges) - set(after_edges)),
                    "before": before_edges,
                    "after": after_edges,
                }
            )

    preservation_violations: list[dict[str, Any]] = []
    for key in sorted(before_keys):
        package = before[key]
        if key not in after:
            same_name_after = sorted(
                (candidate for candidate in after.values() if candidate.get("name") == package.get("name")),
                key=lock_package_sort_key,
            )
            preservation_violations.append(
                {
                    "kind": "accepted-package-identity-removed-or-reidentified",
                    "accepted": package,
                    "same_name_after": same_name_after,
                }
            )
        elif before[key].get("checksum") != after[key].get("checksum"):
            preservation_violations.append(
                {
                    "kind": "accepted-package-checksum-changed",
                    "identity": list(key),
                    "accepted": before[key].get("checksum"),
                    "actual": after[key].get("checksum"),
                }
            )
    mismatches.extend(preservation_violations)

    normalized_expected_v8 = {
        **EXPECTED_V8_LOCK_ENTRY,
        "dependencies": sorted(EXPECTED_V8_LOCK_ENTRY["dependencies"]),
    }
    before_v8 = normalized_v8_lock_entries(before)
    after_v8 = normalized_v8_lock_entries(after)
    if before_v8 != [normalized_expected_v8]:
        mismatches.append(
            {
                "kind": "accepted-v8-lock-entry-mismatch",
                "expected": normalized_expected_v8,
                "actual": before_v8,
            }
        )
    if after_v8 != [normalized_expected_v8]:
        mismatches.append(
            {
                "kind": "resolved-v8-lock-entry-mismatch",
                "expected": normalized_expected_v8,
                "actual": after_v8,
            }
        )

    seed_edge_observations = [
        *observed_seed_lock_edges(before, "accepted-seed", mismatches),
        *observed_seed_lock_edges(after, "resolved-composed", mismatches),
    ]

    new_package_dependency_edges = [
        {
            "identity": list(lock_package_sort_key(package)),
            "dependencies": sorted(
                dependency
                for dependency in package.get("dependencies", [])
                if isinstance(dependency, str)
            ),
        }
        for package in added
    ]
    delta = {
        "preimage_sha256": hashlib.sha256(before_raw).hexdigest(),
        "resolved_sha256": hashlib.sha256(after_raw).hexdigest(),
        "raw_lock_changed": before_raw != after_raw,
        "added": added,
        "removed": removed,
        "changed": changed,
        "new_package_dependency_edges": new_package_dependency_edges,
        "dependency_edge_changes": dependency_edge_changes,
    }
    delta_sha256 = hashlib.sha256(
        json.dumps(delta, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    delta_requires_approval = bool(added or removed or changed or before_raw != after_raw)
    delta_approved = not delta_requires_approval or (
        ACCEPTED_LOCK_DELTA_SHA256 is not None and delta_sha256 == ACCEPTED_LOCK_DELTA_SHA256
    )
    if delta_requires_approval and ACCEPTED_LOCK_DELTA_SHA256 is None:
        mismatches.append(
            {
                "kind": "exact-lock-delta-approval-required",
                "actual_delta_sha256": delta_sha256,
            }
        )
    elif delta_requires_approval and not delta_approved:
        mismatches.append(
            {
                "kind": "accepted-lock-delta-digest-mismatch",
                "expected": ACCEPTED_LOCK_DELTA_SHA256,
                "actual": delta_sha256,
            }
        )

    if all(command["exit_code"] == 0 for command in commands) and len(commands) == 2:
        run("git", "add", "--", ROOT_LOCK_PATH, cwd=worktree)
        try:
            require_candidate_paths(
                worktree,
                sorted([*OVERLAY_CHANGED_PATHS, *SDK_GENERATED_PATHS]),
                "composed Cargo lock resolution",
            )
        except SystemExit as error:
            mismatches.append({"kind": "lock-path-boundary", "diagnostic": str(error)[:240]})

    mismatches.sort(key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
    acceptance = "ACCEPTED" if not mismatches else "UNACCEPTED"
    receipt = {
        "schema": "sdk-build-composed-cargo-lock-attribution",
        "version": 2,
        "status": "ready" if acceptance == "ACCEPTED" else "unaccepted",
        "acceptance": acceptance,
        "promotion_permitted": acceptance == "ACCEPTED",
        "input_sdk_candidate": SDK_CANDIDATE_SHA,
        "build_source_sha": BUILD_SOURCE_SHA,
        "accepted_seed_git_entry": tuple_json(BUILD_SOURCE_ENTRIES[ROOT_LOCK_PATH]),
        "manifest_structure_receipt_sha256": digest(diagnostics / "structural-receipt.json"),
        "generation_context": "actual-composed-index-and-worktree-with-accepted-lock-seed",
        "resolution_policy": "cargo-metadata-preserves-compatible-accepted-lock-entries",
        "accepted_lock_delta_sha256": ACCEPTED_LOCK_DELTA_SHA256,
        "actual_lock_delta_sha256": delta_sha256,
        "delta_requires_approval": delta_requires_approval,
        "delta_approved": delta_approved,
        "commands": commands,
        "preimage_sha256": hashlib.sha256(before_raw).hexdigest(),
        "preimage_bytes": len(before_raw),
        "preimage_package_count": len(before),
        "resolved_sha256": hashlib.sha256(after_raw).hexdigest(),
        "resolved_bytes": len(after_raw),
        "resolved_package_count": len(after),
        "actual_delta_count": len(added) + len(removed) + len(changed),
        "actual_delta": delta,
        "workspace_package_additions": [package for package in added if not package.get("source")],
        "external_package_additions": [package for package in added if package.get("source")],
        "new_package_dependency_edges": new_package_dependency_edges,
        "dependency_edge_changes": dependency_edge_changes,
        "accepted_package_preservation_violation_count": len(preservation_violations),
        "accepted_package_preservation_violations": preservation_violations,
        "accepted_v8_entry": before_v8,
        "resolved_v8_entry": after_v8,
        "seed_lock_edges": seed_edge_observations,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
    }
    write_json(diagnostics / "lock-attribution.json", receipt)
    require(
        acceptance == "ACCEPTED",
        f"composed Cargo lock is UNACCEPTED: {len(mismatches)} blockers; exact delta decision required",
    )
    return receipt


def classify(
    base: tuple[str, str, str] | None,
    upstream: tuple[str, str, str] | None,
    source: tuple[str, str, str] | None,
) -> str:
    if source == base:
        return "base"
    if source == upstream:
        return "upstream"
    if source is None:
        return "delete"
    return "manual"


def require_fields(actual: dict[str, Any], expected: dict[str, Any], label: str) -> None:
    for key, value in expected.items():
        require(str(actual.get(key)) == str(value), f"{label} mismatch: {key}")


def load_event() -> dict[str, Any]:
    event_path = pathlib.Path(os.environ.get("GITHUB_EVENT_PATH", ""))
    require(event_path.is_absolute() and event_path.is_file(), "invalid consumer event path")
    event = json.loads(event_path.read_text(encoding="utf-8"))
    require(isinstance(event, dict), "consumer event must be an object")
    return event


def verify_runtime(expected_workflow_sha: str, expected_workflow_tree: str) -> dict[str, str]:
    require(os.environ.get("GITHUB_REPOSITORY") == REPOSITORY, "current repository mismatch")
    event_name = os.environ.get("GITHUB_EVENT_NAME")
    require(event_name in {"push", "workflow_dispatch"}, "current event mismatch")
    require(os.environ.get("GITHUB_REF") == VALIDATION_REF, "current ref mismatch")
    require(SHA_PATTERN.fullmatch(expected_workflow_sha) is not None, "invalid expected workflow SHA")
    require(SHA_PATTERN.fullmatch(expected_workflow_tree) is not None, "invalid expected workflow tree")
    require(os.environ.get("GITHUB_SHA") == expected_workflow_sha, "workflow run SHA mismatch")
    for name in (
        "GITHUB_REPOSITORY_ID",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_WORKFLOW_REF",
    ):
        require(bool(os.environ.get(name)), f"missing consumer identity: {name}")
    require(os.environ["GITHUB_REPOSITORY_ID"] == REPOSITORY_ID, "current repository ID mismatch")
    expected_workflow_ref = f"{REPOSITORY}/{WORKFLOW_PATH}@{VALIDATION_REF}"
    require(os.environ["GITHUB_WORKFLOW_REF"] == expected_workflow_ref, "consumer workflow ref mismatch")

    event = load_event()
    repository = event.get("repository")
    require(isinstance(repository, dict) and repository.get("full_name") == REPOSITORY, "event repository mismatch")
    requested_sha = os.environ.get("REQUESTED_WORKFLOW_SHA", "")
    requested_tree = os.environ.get("REQUESTED_WORKFLOW_TREE", "")
    if event_name == "workflow_dispatch":
        inputs = event.get("inputs")
        require(
            isinstance(inputs, dict) and set(inputs) == {"expected_workflow_sha", "expected_workflow_tree"},
            "workflow dispatch input set mismatch",
        )
        require(inputs["expected_workflow_sha"] == requested_sha, "workflow dispatch SHA receipt mismatch")
        require(inputs["expected_workflow_tree"] == requested_tree, "workflow dispatch tree receipt mismatch")
        require(requested_sha == expected_workflow_sha, "workflow dispatch SHA does not match run checkout")
        require(requested_tree == expected_workflow_tree, "workflow dispatch tree does not match run checkout")
    else:
        require(not requested_sha and not requested_tree, "push event must not supply workflow dispatch inputs")
        require(event.get("ref") == VALIDATION_REF, "push event ref mismatch")
        require(event.get("before") == PUSH_PREDECESSOR_SHA, "push predecessor mismatch")
        require(event.get("after") == expected_workflow_sha, "push target mismatch")
        require(event.get("forced") is False, "push event must not be forced")
        require(event.get("deleted") is False, "push event must not delete ref")
        require(event.get("created") is False, "push event must fast-forward the existing consumer ref")
        head_commit = event.get("head_commit")
        require(
            isinstance(head_commit, dict) and head_commit.get("id") == expected_workflow_sha,
            "push head commit mismatch",
        )
    return {
        "consumer_repository": REPOSITORY,
        "consumer_repository_id": os.environ["GITHUB_REPOSITORY_ID"],
        "consumer_workflow_head": expected_workflow_sha,
        "consumer_workflow_tree": expected_workflow_tree,
        "consumer_workflow_ref": os.environ["GITHUB_WORKFLOW_REF"],
        "consumer_run_id": os.environ["GITHUB_RUN_ID"],
        "consumer_run_attempt": os.environ["GITHUB_RUN_ATTEMPT"],
        "consumer_event": os.environ["GITHUB_EVENT_NAME"],
    }


def verify_build_source_checkout(repo: pathlib.Path) -> None:
    require(run("git", "rev-parse", "HEAD", cwd=repo).strip() == BUILD_SOURCE_SHA, "build source head mismatch")
    require(run("git", "rev-parse", "HEAD^{tree}", cwd=repo).strip() == BUILD_SOURCE_TREE, "build source tree mismatch")
    require(run("git", "show", "-s", "--format=%P", BUILD_SOURCE_SHA, cwd=repo).split() == [BUILD_SOURCE_PARENT], "build source parent mismatch")
    run("git", "merge-base", "--is-ancestor", VOICE_HOST_DEFERRAL_BASELINE_SHA, BUILD_SOURCE_SHA, cwd=repo)
    require(
        tree_entry(repo, VOICE_HOST_DEFERRAL_BASELINE_SHA, ROOT_MANIFEST_PATH)
        == VOICE_HOST_DEFERRAL_BASELINE_ROOT_ENTRY,
        "voice-host deferral baseline root manifest tuple mismatch",
    )
    require(not run("git", "status", "--porcelain", cwd=repo), "build source checkout is dirty")
    changed = run(
        "git",
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        BASE_SHA,
        BUILD_SOURCE_SHA,
        cwd=repo,
    ).splitlines()
    require(changed == BUILD_PATHS, "build source diff is not the exact eleven-path authored cohort")
    require(path_digest(changed) == BUILD_PATHS_SHA256, "build source path-set digest mismatch")
    for path, expected in OVERLAY_SOURCE_ENTRIES.items():
        require(tree_entry(repo, BUILD_SOURCE_SHA, path) == expected, f"build source tuple mismatch: {path}")


def expected_sdk_dispositions(repo: pathlib.Path) -> list[dict[str, Any]]:
    dispositions: list[dict[str, Any]] = []
    for path in SDK_SOURCE_PATHS:
        base_entry = tree_entry(repo, BASE_SHA, path)
        upstream_entry = tree_entry(repo, GLOBAL_UPSTREAM_SHA, path)
        source_entry = tree_entry(repo, SDK_SOURCE_SHA, path)
        materialized_entry = tree_entry(repo, MATERIALIZED_SHA, path)
        require(materialized_entry == base_entry, f"materialized SDK entry is not frozen base: {path}")
        dispositions.append(
            {
                "path": path,
                "disposition": classify(base_entry, upstream_entry, source_entry),
                "base": tuple_json(base_entry),
                "upstream": tuple_json(upstream_entry),
                "source": tuple_json(source_entry),
                "materialized": tuple_json(materialized_entry),
                "selected": tuple_json(source_entry),
                "source_equals_base": source_entry == base_entry,
                "source_equals_upstream": source_entry == upstream_entry,
                "materialized_equals_base": materialized_entry == base_entry,
            }
        )
    return dispositions


def verify_sdk_artifact_files(artifact: pathlib.Path) -> dict[str, pathlib.Path]:
    expected_names = {"candidate.bundle", "receipt.json", "provenance.json"}
    require({path.name for path in artifact.iterdir()} == expected_names, "SDK artifact file set mismatch")
    files: dict[str, pathlib.Path] = {}
    for key, name in (("bundle", "candidate.bundle"), ("receipt", "receipt.json"), ("provenance", "provenance.json")):
        raw = artifact / name
        require(raw.is_file() and not raw.is_symlink() and raw.stat().st_size > 0, f"invalid SDK artifact file: {name}")
        path = raw.resolve(strict=True)
        require(path.parent == artifact, f"SDK artifact file escaped input directory: {name}")
        files[key] = path
    require(digest(files["bundle"]) == SDK_INPUT_BUNDLE_SHA256, "SDK input bundle digest mismatch")
    require(digest(files["receipt"]) == SDK_INPUT_RECEIPT_SHA256, "SDK input receipt digest mismatch")

    receipt = load(files["receipt"])
    require_fields(
        receipt,
        {
            "schema": "upstream-cohort-disposition",
            "version": 2,
            "repository": REPOSITORY,
            "cohort": "sdk-public-contract",
            "path_count": len(SDK_SOURCE_PATHS),
            "path_set_sha256": SDK_SOURCE_PATHS_SHA256,
            "base_sha": BASE_SHA,
            "base_tree": BASE_TREE,
            "upstream_sha": GLOBAL_UPSTREAM_SHA,
            "upstream_tree": GLOBAL_UPSTREAM_TREE,
            "materialized_sha": MATERIALIZED_SHA,
            "materialized_tree": MATERIALIZED_TREE,
            "source_branch": SDK_SOURCE_BRANCH,
            "source_sha": SDK_SOURCE_SHA,
            "source_tree": SDK_SOURCE_TREE,
            "source_parent": BASE_SHA,
            "candidate_sha": SDK_CANDIDATE_SHA,
            "candidate_tree": SDK_CANDIDATE_TREE,
            "candidate_parent": SDK_CANDIDATE_PARENT,
        },
        "SDK input receipt",
    )

    provenance = load(files["provenance"])
    require(provenance.get("signed") is False, "SDK input provenance must remain explicitly unsigned")
    require_fields(
        provenance,
        {
            "schema": "upstream-cohort-candidate-provenance",
            "version": 2,
            "producer_repository": REPOSITORY,
            "producer_repository_id": REPOSITORY_ID,
            "producer_workflow_head": SDK_INPUT_WORKFLOW_SHA,
            "producer_workflow_tree": SDK_INPUT_WORKFLOW_TREE,
            "producer_workflow_ref": SDK_INPUT_WORKFLOW_REF,
            "producer_run_id": SDK_INPUT_RUN_ID,
            "producer_run_attempt": SDK_INPUT_RUN_ATTEMPT,
            "producer_event": "push",
            "source_repository": REPOSITORY,
            "source_run_id": COMMON_SOURCE_RUN_ID,
            "source_run_attempt": COMMON_SOURCE_RUN_ATTEMPT,
            "source_artifact": COMMON_SOURCE_ARTIFACT,
            "source_bundle_sha256": COMMON_BUNDLE_SHA256,
            "source_receipt_sha256": COMMON_RECEIPT_SHA256,
            "source_provenance_sha256": COMMON_PROVENANCE_SHA256,
            "source_manifest_sha256": COMMON_MANIFEST_SHA256,
            "source_staged_patch_sha256": COMMON_STAGED_PATCH_SHA256,
            "source_staged_paths_sha256": COMMON_STAGED_PATHS_SHA256,
            "base_sha": BASE_SHA,
            "base_tree": BASE_TREE,
            "upstream_sha": GLOBAL_UPSTREAM_SHA,
            "upstream_tree": GLOBAL_UPSTREAM_TREE,
            "materialized_sha": MATERIALIZED_SHA,
            "materialized_tree": MATERIALIZED_TREE,
            "sdk_source_branch": SDK_SOURCE_BRANCH,
            "sdk_source_sha": SDK_SOURCE_SHA,
            "sdk_source_tree": SDK_SOURCE_TREE,
            "sdk_source_parent": BASE_SHA,
            "cohort": "sdk-public-contract",
            "path_count": len(SDK_SOURCE_PATHS),
            "path_set_sha256": SDK_SOURCE_PATHS_SHA256,
            "candidate_sha": SDK_CANDIDATE_SHA,
            "candidate_tree": SDK_CANDIDATE_TREE,
            "candidate_parent": SDK_CANDIDATE_PARENT,
            "candidate_bundle_sha256": SDK_INPUT_BUNDLE_SHA256,
            "disposition_receipt_sha256": SDK_INPUT_RECEIPT_SHA256,
        },
        "SDK input provenance",
    )
    require(provenance.get("candidate_bundle_heads") == EXPECTED_SDK_BUNDLE_HEADS, "SDK input bundle head receipt mismatch")
    return files


def import_sdk_bundle(repo: pathlib.Path, bundle: pathlib.Path, temp: pathlib.Path) -> None:
    bare = temp / "verify-sdk-input.git"
    run("git", "init", "--bare", str(bare))
    run("git", "-C", str(bare), "bundle", "verify", str(bundle))
    heads: dict[str, str] = {}
    for line in run("git", "-C", str(bare), "bundle", "list-heads", str(bundle)).splitlines():
        oid, ref = line.split(maxsplit=1)
        require(ref not in heads, f"duplicate SDK bundle ref: {ref}")
        heads[ref] = oid
    require(heads == EXPECTED_SDK_BUNDLE_HEADS, "SDK input bundle head map mismatch")
    for source_ref, oid in heads.items():
        suffix = source_ref.rsplit("/", 1)[-1]
        target_ref = f"refs/w13825-sdk-input/{suffix}"
        run("git", "fetch", str(bundle), f"+{source_ref}:{target_ref}", cwd=repo)
        require(run("git", "rev-parse", target_ref, cwd=repo).strip() == oid, f"SDK bundle import mismatch: {suffix}")

    verify_imported_sdk_objects(repo)


def verify_imported_sdk_objects(repo: pathlib.Path) -> None:
    for source_ref, oid in EXPECTED_SDK_BUNDLE_HEADS.items():
        suffix = source_ref.rsplit("/", 1)[-1]
        target_ref = f"refs/w13825-sdk-input/{suffix}"
        require(run("git", "rev-parse", target_ref, cwd=repo).strip() == oid, f"imported SDK ref mismatch: {suffix}")

    require(run("git", "rev-parse", f"{SDK_CANDIDATE_SHA}^{{tree}}", cwd=repo).strip() == SDK_CANDIDATE_TREE, "SDK candidate tree mismatch")
    require(run("git", "show", "-s", "--format=%P", SDK_CANDIDATE_SHA, cwd=repo).split() == [MATERIALIZED_SHA], "SDK candidate parent mismatch")
    require(run("git", "rev-parse", f"{MATERIALIZED_SHA}^{{tree}}", cwd=repo).strip() == MATERIALIZED_TREE, "materialized tree mismatch")
    require(run("git", "show", "-s", "--format=%P", MATERIALIZED_SHA, cwd=repo).split() == [BASE_SHA], "materialized parent mismatch")
    require(run("git", "rev-parse", f"{SDK_SOURCE_SHA}^{{tree}}", cwd=repo).strip() == SDK_SOURCE_TREE, "SDK source tree mismatch")
    require(run("git", "show", "-s", "--format=%P", SDK_SOURCE_SHA, cwd=repo).split() == [BASE_SHA], "SDK source parent mismatch")


def import_sdk_bundle_for_probe(bundle: pathlib.Path, temp: pathlib.Path) -> pathlib.Path:
    repo = temp / "probe-sdk.git"
    run("git", "init", "--bare", str(repo))
    bundle_verifier = temp / "verify-sdk-input.git"
    run("git", "init", "--bare", str(bundle_verifier))
    run("git", "-C", str(bundle_verifier), "bundle", "verify", str(bundle))
    heads: dict[str, str] = {}
    for line in run("git", "-C", str(bundle_verifier), "bundle", "list-heads", str(bundle)).splitlines():
        oid, ref = line.split(maxsplit=1)
        require(ref not in heads, f"duplicate SDK bundle ref: {ref}")
        heads[ref] = oid
    require(heads == EXPECTED_SDK_BUNDLE_HEADS, "SDK input bundle head map mismatch")
    for source_ref, oid in heads.items():
        suffix = source_ref.rsplit("/", 1)[-1]
        target_ref = f"refs/w13825-sdk-input/{suffix}"
        run("git", "fetch", str(bundle), f"+{source_ref}:{target_ref}", cwd=repo)
        require(run("git", "rev-parse", target_ref, cwd=repo).strip() == oid, f"SDK bundle import mismatch: {suffix}")
    verify_imported_sdk_objects(repo)
    return repo


def source_checkout_inventory(repo: pathlib.Path) -> dict[str, Any]:
    refs = run(
        "git",
        "for-each-ref",
        "--format=%(refname)=%(objectname)",
        cwd=repo,
    ).splitlines()
    return {
        "refs": refs,
        "status": run("git", "status", "--porcelain", cwd=repo),
        "head": run("git", "rev-parse", "HEAD", cwd=repo).strip(),
        "tree": run("git", "rev-parse", "HEAD^{tree}", cwd=repo).strip(),
        "parent": run("git", "show", "-s", "--format=%P", "HEAD", cwd=repo).split(),
    }


def verify_probe_provider_receipt(provider_receipt: pathlib.Path) -> dict[str, Any]:
    receipt = load(provider_receipt)
    require(isinstance(receipt, dict), "diagnostic provider receipt must be an object")
    require_fields(
        receipt,
        {
            "target_helper_branch": TARGET_HELPER_BRANCH,
            "target_helper_sha": TARGET_HELPER_SHA,
            "target_helper_tree": TARGET_HELPER_TREE,
            "target_helper_parent": TARGET_HELPER_PARENT,
        },
        "target helper provider receipt",
    )
    return receipt


def emit_sdk_bundle_path_receipt(repo: pathlib.Path, diagnostics: pathlib.Path) -> dict[str, Any]:
    paths = list(SDK_BUNDLE_PROBE_PATHS)
    require(paths == sorted(paths) and len(paths) == len(set(paths)), "SDK bundle probe path set is not exact")
    entries = [
        {"path": path, "entry": tuple_json(tree_entry(repo, SDK_CANDIDATE_SHA, path))}
        for path in paths
    ]
    receipt = {
        "schema": "sdk-bundle-path-probe",
        "version": 1,
        "candidate_sha": SDK_CANDIDATE_SHA,
        "candidate_tree": SDK_CANDIDATE_TREE,
        "paths": paths,
        "path_set_sha256": path_digest(paths),
        "entries": entries,
    }
    write_json(diagnostics / "sdk-bundle-path-receipt.json", receipt)
    return receipt


def emit_sdk_rmcp_preimage_receipt(
    repo: pathlib.Path,
    output: pathlib.Path,
    provider_receipt: pathlib.Path,
    workflow_sha: str,
    workflow_tree: str,
) -> dict[str, Any]:
    require(RMCP_PREIMAGE_PATHS == sorted(RMCP_PREIMAGE_PATHS), "diagnostic paths are not sorted")
    require(len(RMCP_PREIMAGE_PATHS) == 82, "diagnostic path scope is not exactly 82 paths")
    require(path_digest(RMCP_PREIMAGE_PATHS) == RMCP_PREIMAGE_PATHS_SHA256, "diagnostic path-set digest mismatch")
    entries = [
        {"path": path, "entry": tuple_json(tree_entry(repo, SDK_CANDIDATE_SHA, path))}
        for path in RMCP_PREIMAGE_PATHS
    ]
    missing_paths = sorted(item["path"] for item in entries if item["entry"] is None)
    require(set(missing_paths).issubset(set(RMCP_PREIMAGE_PATHS)), "diagnostic missing path escaped probe set")
    provider = verify_probe_provider_receipt(provider_receipt)
    receipt = {
        "schema": "sdk-rmcp-preimage-diagnostic",
        "version": 1,
        "repository": REPOSITORY,
        "diagnostic_workflow_sha": workflow_sha,
        "diagnostic_workflow_tree": workflow_tree,
        "helper_sha": DIAGNOSTIC_HELPER_SHA,
        "target_helper_branch": provider["target_helper_branch"],
        "target_helper_sha": provider["target_helper_sha"],
        "target_helper_tree": provider["target_helper_tree"],
        "target_helper_parent": provider["target_helper_parent"],
        "source_sha": DIAGNOSTIC_SOURCE_SHA,
        "source_tree": DIAGNOSTIC_SOURCE_TREE,
        "source_parent": DIAGNOSTIC_SOURCE_PARENT,
        "artifact_provider": provider,
        "candidate_sha": SDK_CANDIDATE_SHA,
        "candidate_tree": SDK_CANDIDATE_TREE,
        "path_set_sha256": path_digest(RMCP_PREIMAGE_PATHS),
        "missing_paths": missing_paths,
        "missing_path_count": len(missing_paths),
        "entries": entries,
    }
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "receipt.json", receipt)
    return receipt


def verify_sdk_input_entries(repo: pathlib.Path, receipt: dict[str, Any]) -> list[dict[str, Any]]:
    require(
        not set(COMPOSITE_RUNTIME_INPUTS).intersection(ALLOWED_MUTABLE_PATHS),
        "composite runtime input entered the mutable cohort",
    )
    sdk_changed = run(
        "git",
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        MATERIALIZED_SHA,
        SDK_CANDIDATE_SHA,
        cwd=repo,
    ).splitlines()
    require(sdk_changed == SDK_SOURCE_PATHS, "accepted SDK candidate path set mismatch")
    require(path_digest(sdk_changed) == SDK_SOURCE_PATHS_SHA256, "accepted SDK candidate path digest mismatch")
    expected_dispositions = expected_sdk_dispositions(repo)
    require(receipt.get("paths") == expected_dispositions, "SDK input disposition map mismatch")
    for path in SDK_SOURCE_PATHS:
        require(
            tree_entry(repo, SDK_CANDIDATE_SHA, path) == tree_entry(repo, SDK_SOURCE_SHA, path),
            f"accepted SDK candidate/source tuple mismatch: {path}",
        )
    require(
        tree_entry(repo, SDK_CANDIDATE_SHA, "codex-rs/http-client/src/route_aware_client_pool.rs")
        == SDK_BUNDLE_ROUTE_WITNESS,
        "accepted SDK route witness tuple mismatch",
    )
    for path, expected in PATCH_DEPENDENCIES.items():
        require(tree_entry(repo, MATERIALIZED_SHA, path) == expected, f"materialized patch dependency mismatch: {path}")
        require(tree_entry(repo, SDK_CANDIDATE_SHA, path) == expected, f"SDK candidate patch dependency mismatch: {path}")
    for path, expected in V8_COMPOSED_ENTRIES.items():
        require(tree_entry(repo, MATERIALIZED_SHA, path) == expected, f"materialized V8 preservation mismatch: {path}")
        require(tree_entry(repo, SDK_CANDIDATE_SHA, path) == expected, f"SDK candidate V8 preservation mismatch: {path}")
    verified_runtime_inputs: list[dict[str, Any]] = []
    for path, expected in COMPOSITE_RUNTIME_INPUTS.items():
        materialized_entry = tree_entry(repo, MATERIALIZED_SHA, path)
        candidate_entry = tree_entry(repo, SDK_CANDIDATE_SHA, path)
        expected_tuple = "/".join(expected)
        materialized_tuple = "/".join(materialized_entry) if materialized_entry is not None else "missing"
        candidate_tuple = "/".join(candidate_entry) if candidate_entry is not None else "missing"
        require(
            materialized_entry == expected,
            f"materialized runtime input mismatch: {path} expected={expected_tuple} observed={materialized_tuple}",
        )
        require(
            candidate_entry == expected,
            f"SDK candidate runtime input mismatch: {path} expected={expected_tuple} observed={candidate_tuple}",
        )
        verified_runtime_inputs.append(
            {
                "path": path,
                "materialized": tuple_json(materialized_entry),
                "sdk_candidate": tuple_json(candidate_entry),
            }
        )
    return verified_runtime_inputs


def bounded_text(value: Any, *, maximum: int = 256) -> str | None:
    if not isinstance(value, str) or not value or len(value) > maximum:
        return None
    return value


def exact_text_blob(
    repo: pathlib.Path,
    entries: dict[str, list[str]],
    path: str,
) -> tuple[bytes, dict[str, str]]:
    entry = entries.get(path)
    require(entry is not None, f"metadata input path is absent: {path}")
    mode, object_type, oid, listed_path = entry
    require(listed_path == path, f"metadata input path changed: {path}")
    require(mode in {"100644", "100755"} and object_type == "blob", f"metadata input is not a regular blob: {path}")
    size = int(run("git", "cat-file", "-s", oid, cwd=repo).strip())
    require(0 < size <= MAX_METADATA_BLOB_BYTES, f"metadata input size is invalid: {path}")
    content = run_bytes("git", "cat-file", "blob", oid, cwd=repo)
    require(len(content) == size, f"metadata input size changed: {path}")
    require(b"\0" not in content, f"metadata input is not text: {path}")
    return content, {"mode": mode, "type": object_type, "oid": oid}


def exact_match(pattern: str, text: str, label: str, *, flags: int = 0) -> str:
    values = re.findall(pattern, text, flags)
    require(len(values) == 1, f"{label} must have one value")
    value = bounded_text(values[0], maximum=512)
    require(value is not None, f"{label} is invalid")
    return value


def starlark_blocks(text: str, function: str) -> list[str]:
    pattern = rf"(?ms)^{re.escape(function)}\(\n.*?^\)\n"
    return re.findall(pattern, text)


def normalized_v8_metadata(repo: pathlib.Path, composed: dict[str, Any]) -> dict[str, Any]:
    entries = manifest_entry_map(composed)
    module_bytes, module_entry = exact_text_blob(repo, entries, "MODULE.bazel")
    cargo_bytes, cargo_entry = exact_text_blob(repo, entries, "codex-rs/Cargo.toml")
    try:
        module_text = module_bytes.decode("utf-8")
        cargo_text = cargo_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SystemExit("V8 selector input is not UTF-8") from error

    v8_version = exact_match(
        r'^bazel_dep\(name\s*=\s*"v8",\s*version\s*=\s*"([^"]+)"\)$',
        module_text,
        "V8 Bazel module version",
        flags=re.MULTILINE,
    )
    archive_blocks = [
        block
        for block in starlark_blocks(module_text, "archive_override")
        if re.search(r'^\s*module_name\s*=\s*"v8",\s*$', block, re.MULTILINE)
    ]
    require(len(archive_blocks) == 1, "V8 archive override must be unique")
    archive = archive_blocks[0]
    archive_integrity = exact_match(
        r'^\s*integrity\s*=\s*"([^"]+)",\s*$',
        archive,
        "V8 archive integrity",
        flags=re.MULTILINE,
    )
    archive_strip_prefix = exact_match(
        r'^\s*strip_prefix\s*=\s*"([^"]+)",\s*$',
        archive,
        "V8 archive strip prefix",
        flags=re.MULTILINE,
    )
    archive_urls = sorted(set(re.findall(r'"(https://[^"]+)"', archive)))
    require(len(archive_urls) == 1 and bounded_text(archive_urls[0], maximum=512), "V8 archive URL is invalid")
    patch_order = re.findall(r'"//patches:([A-Za-z0-9_.-]+)"', archive)
    require(patch_order == EXPECTED_V8_PATCH_ORDER, "V8 patch order is invalid")

    crate_archives: list[dict[str, Any]] = []
    for block in starlark_blocks(module_text, "http_archive"):
        names = re.findall(r'^\s*name\s*=\s*"(v8_crate_[0-9_]+)",\s*$', block, re.MULTILINE)
        if not names:
            continue
        require(len(names) == 1 and len(crate_archives) < 8, "V8 crate archive set is invalid")
        urls = sorted(set(re.findall(r'"(https://[^"]+)"', block)))
        require(len(urls) == 1 and bounded_text(urls[0], maximum=512), "V8 crate archive URL is invalid")
        crate_archives.append(
            {
                "name": names[0],
                "sha256": exact_match(
                    r'^\s*sha256\s*=\s*"([0-9a-f]{64})",\s*$',
                    block,
                    f"{names[0]} SHA-256",
                    flags=re.MULTILINE,
                ),
                "strip_prefix": exact_match(
                    r'^\s*strip_prefix\s*=\s*"([^"]+)",\s*$',
                    block,
                    f"{names[0]} strip prefix",
                    flags=re.MULTILINE,
                ),
                "url": urls[0],
            }
        )
    require(crate_archives, "V8 crate archive metadata is absent")
    crate_archives.sort(key=lambda value: value["name"])

    cargo_v8_version = exact_match(
        r'^v8\s*=\s*"=([^"]+)"\s*$',
        cargo_text,
        "Cargo V8 version",
        flags=re.MULTILINE,
    )
    patches: list[dict[str, Any]] = []
    for path in V8_PATCH_PATHS:
        content, entry = exact_text_blob(repo, entries, path)
        try:
            patch_text = content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SystemExit(f"V8 patch is not UTF-8: {path}") from error
        orig_versions = sorted(set(re.findall(r"(?m)^--- a/orig/v8-([^/\s]+)/", patch_text)))
        mod_versions = sorted(set(re.findall(r"(?m)^\+\+\+ b/mod/v8-([^/\s]+)/", patch_text)))
        require(orig_versions and mod_versions, f"V8 patch target versions are absent: {path}")
        require(len(orig_versions) <= 8 and len(mod_versions) <= 8, f"V8 patch target version bound exceeded: {path}")
        require(
            all(bounded_text(version, maximum=64) is not None for version in [*orig_versions, *mod_versions]),
            f"V8 patch target version is invalid: {path}",
        )
        patches.append(
            {
                "path": path,
                "entry": entry,
                "byte_size": len(content),
                "content_sha256": hashlib.sha256(content).hexdigest(),
                "orig_target_versions": orig_versions,
                "mod_target_versions": mod_versions,
            }
        )

    return {
        "module": {
            "entry": module_entry,
            "bazel_module_version": v8_version,
            "archive_integrity": archive_integrity,
            "archive_strip_prefix": archive_strip_prefix,
            "archive_url": archive_urls[0],
            "patch_order": patch_order,
            "crate_archives": crate_archives,
        },
        "cargo": {
            "entry": cargo_entry,
            "v8_version": cargo_v8_version,
        },
        "patches": patches,
    }


def projected_entries(subject: dict[str, Any]) -> list[dict[str, Any]]:
    entries = manifest_entry_map(subject)
    return [
        {
            "path": path,
            "entry": (
                {
                    "mode": entries[path][0],
                    "type": entries[path][1],
                    "oid": entries[path][2],
                }
                if path in entries
                else None
            ),
        }
        for path in V8_DIAGNOSTIC_PATHS
    ]


def emit_tree_metadata_manifest(
    repo: pathlib.Path,
    files: dict[str, pathlib.Path],
    output: pathlib.Path,
    runtime: dict[str, str],
) -> dict[str, Any]:
    require(output.parent.is_dir(), "metadata output parent is unavailable")
    require(not output.exists(), "metadata output directory already exists")

    materialized = full_tree_manifest(repo, MATERIALIZED_SHA, MATERIALIZED_TREE, BASE_SHA)
    sdk_candidate = full_tree_manifest(repo, SDK_CANDIDATE_SHA, SDK_CANDIDATE_TREE, MATERIALIZED_SHA)
    materialized_entries = manifest_entry_map(materialized)
    sdk_entries = manifest_entry_map(sdk_candidate)
    sdk_changed = sorted(
        path
        for path in set(materialized_entries) | set(sdk_entries)
        if materialized_entries.get(path) != sdk_entries.get(path)
    )
    require(sdk_changed == SDK_SOURCE_PATHS, "full SDK tree manifest delta is not the accepted path set")
    composed = in_memory_composed_manifest(repo, sdk_candidate)
    v8_projection = {
        "materialized": projected_entries(materialized),
        "sdk_candidate": projected_entries(sdk_candidate),
        "composed_pre_generation": projected_entries(composed),
    }
    v8_metadata = normalized_v8_metadata(repo, composed)
    identity_fields = {
        "materialized_sha": MATERIALIZED_SHA,
        "materialized_tree": MATERIALIZED_TREE,
        "materialized_entries_sha256": materialized["canonical_entries_sha256"],
        "sdk_candidate_sha": SDK_CANDIDATE_SHA,
        "sdk_candidate_tree": SDK_CANDIDATE_TREE,
        "sdk_candidate_entries_sha256": sdk_candidate["canonical_entries_sha256"],
        "build_source_sha": BUILD_SOURCE_SHA,
        "build_source_tree": BUILD_SOURCE_TREE,
        "composed_entries_sha256": composed["canonical_entries_sha256"],
        "v8_projection_sha256": hashlib.sha256(
            json.dumps(v8_projection, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }
    manifest_identity_sha256 = hashlib.sha256(
        json.dumps(identity_fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    manifest = {
        "schema": "sdk-build-content-free-tree-manifest",
        "version": 2,
        "repository": REPOSITORY,
        "signed": False,
        "status": "complete",
        "truncated": False,
        "content_policy": "git-entry-metadata-and-bounded-normalized-v8-selectors-only",
        "entry_format": ["mode", "type", "oid", "path"],
        **runtime,
        "input_sdk_artifact": {
            "id": SDK_INPUT_ARTIFACT_ID,
            "name": SDK_INPUT_ARTIFACT_NAME,
            "size_bytes": SDK_INPUT_ARTIFACT_SIZE,
            "archive_sha256": SDK_INPUT_ARCHIVE_SHA256,
            "run_id": SDK_INPUT_RUN_ID,
            "run_attempt": SDK_INPUT_RUN_ATTEMPT,
            "workflow_sha": SDK_INPUT_WORKFLOW_SHA,
            "workflow_tree": SDK_INPUT_WORKFLOW_TREE,
            "bundle_sha256": SDK_INPUT_BUNDLE_SHA256,
            "receipt_sha256": SDK_INPUT_RECEIPT_SHA256,
            "provenance_sha256": digest(files["provenance"]),
        },
        "identity_fields": identity_fields,
        "manifest_identity_sha256": manifest_identity_sha256,
        "relationships": {
            "materialized_parent": BASE_SHA,
            "sdk_candidate_parent": MATERIALIZED_SHA,
            "sdk_candidate_changed_path_count": len(sdk_changed),
            "sdk_candidate_changed_path_set_sha256": path_digest(sdk_changed),
            "sdk_candidate_changed_paths": sdk_changed,
            "build_overlay_path_count": len(OVERLAY_CHANGED_PATHS),
            "build_overlay_path_set_sha256": OVERLAY_CHANGED_PATHS_SHA256,
            "build_source_authored_path_count": len(BUILD_PATHS),
            "build_source_authored_path_set_sha256": BUILD_PATHS_SHA256,
            "declared_overlay_path_count": len(OVERLAY_PATHS),
            "declared_overlay_path_set_sha256": OVERLAY_PATHS_SHA256,
            "changed_overlay_path_count": len(OVERLAY_CHANGED_PATHS),
            "changed_overlay_path_set_sha256": OVERLAY_CHANGED_PATHS_SHA256,
            "restore_path_count": len(RESTORE_PATHS),
            "restore_path_set_sha256": RESTORE_PATHS_SHA256,
            "restore_entries_sha256": RESTORE_ENTRIES_SHA256,
            "core_skills_path_count": len(CORE_SKILLS_PATHS),
            "core_skills_path_set_sha256": CORE_SKILLS_PATHS_SHA256,
            "core_skills_entries_sha256": CORE_SKILLS_ENTRIES_SHA256,
            "core_skills_additions_sha256": CORE_SKILLS_ADDITIONS_SHA256,
            "core_skills_exact_sha256": CORE_SKILLS_EXACT_SHA256,
        },
        "subjects": {
            "materialized": materialized,
            "sdk_candidate": sdk_candidate,
            "composed_pre_generation": composed,
        },
        "v8_projection": v8_projection,
        "v8_metadata": v8_metadata,
    }
    payload = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    require(0 < len(payload) <= MAX_METADATA_MANIFEST_BYTES, "metadata manifest size bound exceeded")
    output.mkdir()
    manifest_path = output / "composition-tree-manifest.json"
    manifest_path.write_bytes(payload)
    require([path.name for path in output.iterdir()] == [manifest_path.name], "metadata output file set mismatch")
    require(manifest_path.stat().st_size == len(payload), "metadata manifest size changed after write")
    require(load(manifest_path) == manifest, "metadata manifest readback mismatch")
    return {
        "schema": "sdk-build-content-free-tree-manifest-emission",
        "version": 1,
        "status": "complete",
        "manifest_file": manifest_path.name,
        "manifest_sha256": digest(manifest_path),
        "manifest_size_bytes": len(payload),
        "manifest_identity_sha256": manifest_identity_sha256,
        "materialized_entry_count": materialized["entry_count"],
        "sdk_candidate_entry_count": sdk_candidate["entry_count"],
        "composed_entry_count": composed["entry_count"],
    }


def read_input_text(
    worktree: pathlib.Path,
    entries: dict[str, tuple[str, str, str] | None],
    path: str,
    errors: list[str],
) -> str | None:
    entry = entries.get(path)
    if entry is None or entry[1] != "blob":
        errors.append(f"{path}:content-unavailable")
        return None
    try:
        size = int(run("git", "cat-file", "-s", entry[2], cwd=worktree).strip())
        if size > MAX_INPUT_TEXT_BYTES:
            errors.append(f"{path}:content-too-large")
            return None
        return run_bytes("git", "cat-file", "blob", entry[2], cwd=worktree).decode("utf-8")
    except (subprocess.CalledProcessError, UnicodeDecodeError, ValueError):
        errors.append(f"{path}:content-read-error")
        return None


def parse_package_manager(value: Any, label: str, errors: list[str]) -> dict[str, Any]:
    observed = bounded_text(value)
    match = PACKAGE_MANAGER_PATTERN.fullmatch(observed or "")
    if match is None:
        errors.append(f"{label}:invalid-package-manager")
        return {"status": "invalid", "value": observed, "version": None}
    return {"status": "observed", "value": observed, "version": match.group(1)}


def minimum_version(requirement: Any, label: str, errors: list[str]) -> tuple[int, int, int] | None:
    observed = bounded_text(requirement, maximum=64)
    match = MINIMUM_VERSION_PATTERN.fullmatch(observed or "")
    if match is None:
        errors.append(f"{label}:unsupported-version-requirement")
        return None
    return tuple(int(value or 0) for value in match.groups())


def version_tuple(value: str, *, prefix: str = "") -> tuple[int, int, int] | None:
    pattern = rf"{re.escape(prefix)}(\d+)\.(\d+)\.(\d+)"
    match = re.fullmatch(pattern, value)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def parse_uv_identity(value: Any, expected_version: str) -> tuple[dict[str, Any], list[str]]:
    observed = bounded_text(value, maximum=128)
    match = UV_IDENTITY_PATTERN.fullmatch(observed or "")
    if match is None:
        return (
            {
                "status": "invalid",
                "raw": observed,
                "name": None,
                "version": None,
                "target": None,
            },
            ["uv:malformed-identity"],
        )
    name = match.group("name")
    version = match.group("version")
    errors: list[str] = []
    if name != "uv":
        errors.append("uv:name-mismatch")
    if version != expected_version:
        errors.append("uv:version-mismatch")
    return (
        {
            "status": "observed" if not errors else "invalid",
            "raw": observed,
            "name": name,
            "version": version,
            "target": match.group("target"),
        },
        errors,
    )


def uv_identity_receipt(value: Any) -> dict[str, Any]:
    identity, errors = parse_uv_identity(value, UV_VERSION)
    return {
        "schema": "uv-tool-identity",
        "version": 1,
        "expected_name": "uv",
        "expected_version": UV_VERSION,
        "identity": identity,
        "errors": errors,
        "status": "ready" if not errors else "invalid",
    }


def workspace_packages(text: str | None) -> list[str]:
    if text is None:
        return []
    packages: list[str] = []
    in_packages = False
    for line in text.splitlines():
        if line == "packages:":
            in_packages = True
            continue
        if in_packages and line and not line.startswith((" ", "\t")):
            break
        if in_packages:
            match = re.fullmatch(r"\s+-\s+['\"]?([^'\"]+)['\"]?\s*", line)
            if match is not None and len(packages) < 32:
                packages.append(match.group(1)[:128])
    return packages


def collect_execution_inputs(
    worktree: pathlib.Path,
    runtime: dict[str, str],
    verified_runtime_inputs: list[dict[str, Any]],
) -> dict[str, Any]:
    errors: list[str] = []
    entries: dict[str, tuple[str, str, str] | None] = {}
    path_observations: list[dict[str, Any]] = []
    for path, expected_mode in sorted(EXECUTION_INPUT_MODES.items()):
        try:
            entry = index_entry(worktree, path)
        except (subprocess.CalledProcessError, SystemExit, UnicodeDecodeError, ValueError):
            entry = None
            status = "read-error"
        else:
            if entry is None:
                status = "missing"
            elif entry[0] != expected_mode or entry[1] != "blob":
                status = "mode-type-mismatch"
            else:
                status = "observed"
        entries[path] = entry
        if status != "observed":
            errors.append(f"{path}:{status}")
        path_observations.append(
            {
                "path": path,
                "expected_mode": expected_mode,
                "status": status,
                "entry": tuple_json(entry),
            }
        )

    def parse_json_input(path: str) -> dict[str, Any]:
        text = read_input_text(worktree, entries, path, errors)
        if text is None:
            return {}
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            errors.append(f"{path}:json-parse-error")
            return {}
        if not isinstance(value, dict):
            errors.append(f"{path}:json-not-object")
            return {}
        return value

    root_package = parse_json_input("package.json")
    sdk_package = parse_json_input("sdk/typescript/package.json")
    root_manager = parse_package_manager(root_package.get("packageManager"), "root-package", errors)
    sdk_manager = parse_package_manager(sdk_package.get("packageManager"), "sdk-package", errors)
    root_engines = root_package.get("engines") if isinstance(root_package.get("engines"), dict) else {}
    sdk_engines = sdk_package.get("engines") if isinstance(sdk_package.get("engines"), dict) else {}
    root_node_requirement = bounded_text(root_engines.get("node"), maximum=64)
    root_pnpm_requirement = bounded_text(root_engines.get("pnpm"), maximum=64)
    sdk_node_requirement = bounded_text(sdk_engines.get("node"), maximum=64)
    minimum_version(root_node_requirement, "root-node-engine", errors)
    minimum_version(root_pnpm_requirement, "root-pnpm-engine", errors)
    minimum_version(sdk_node_requirement, "sdk-node-engine", errors)
    sdk_scripts_source = sdk_package.get("scripts") if isinstance(sdk_package.get("scripts"), dict) else {}
    sdk_scripts = {name: bounded_text(sdk_scripts_source.get(name)) for name in ("build", "lint", "test")}
    for name, value in sdk_scripts.items():
        if value is None:
            errors.append(f"sdk-package:missing-{name}-script")

    workspace_text = read_input_text(worktree, entries, "pnpm-workspace.yaml", errors)
    packages = workspace_packages(workspace_text)
    if "sdk/typescript" not in packages:
        errors.append("pnpm-workspace:sdk-typescript-missing")

    pyproject_text = read_input_text(worktree, entries, "sdk/python/pyproject.toml", errors)
    pyproject: dict[str, Any] = {}
    if pyproject_text is not None:
        try:
            pyproject = tomllib.loads(pyproject_text)
        except tomllib.TOMLDecodeError:
            errors.append("sdk-python-pyproject:toml-parse-error")
    build_system = pyproject.get("build-system") if isinstance(pyproject.get("build-system"), dict) else {}
    project = pyproject.get("project") if isinstance(pyproject.get("project"), dict) else {}
    build_requires = build_system.get("requires") if isinstance(build_system.get("requires"), list) else []
    python_requirement = bounded_text(project.get("requires-python"), maximum=64)
    minimum_version(python_requirement, "sdk-python-engine", errors)
    dependencies = project.get("dependencies") if isinstance(project.get("dependencies"), list) else []
    runtime_dependencies = [
        value[:128]
        for value in dependencies
        if isinstance(value, str) and value.startswith("openai-codex-cli-bin")
    ][:4]
    python_build = {
        "backend": bounded_text(build_system.get("build-backend"), maximum=128),
        "requires": [value[:128] for value in build_requires if isinstance(value, str)][:16],
        "requires_python": python_requirement,
        "runtime_dependencies": runtime_dependencies,
    }
    if python_build["backend"] != "uv_build":
        errors.append("sdk-python-pyproject:build-backend-mismatch")
    if not any(value.startswith("uv_build") for value in python_build["requires"]):
        errors.append("sdk-python-pyproject:uv-build-requirement-missing")
    if runtime_dependencies != [SDK_RUNTIME_DEPENDENCY]:
        errors.append("sdk-python-pyproject:runtime-dependency-mismatch")

    lock_text = read_input_text(worktree, entries, "sdk/python/uv.lock", errors)
    lock: dict[str, Any] = {}
    if lock_text is not None:
        try:
            lock = tomllib.loads(lock_text)
        except tomllib.TOMLDecodeError:
            errors.append("sdk-python-lock:toml-parse-error")
    lock_packages = lock.get("package") if isinstance(lock.get("package"), list) else []
    runtime_versions: list[str] = []
    runtime_specifiers: list[str] = []
    for package in lock_packages:
        if not isinstance(package, dict):
            continue
        if package.get("name") == "openai-codex-cli-bin" and isinstance(package.get("version"), str):
            runtime_versions.append(package["version"][:64])
        if package.get("name") != "openai-codex":
            continue
        metadata = package.get("metadata") if isinstance(package.get("metadata"), dict) else {}
        requires_dist = metadata.get("requires-dist") if isinstance(metadata.get("requires-dist"), list) else []
        for requirement in requires_dist:
            if (
                isinstance(requirement, dict)
                and requirement.get("name") == "openai-codex-cli-bin"
                and isinstance(requirement.get("specifier"), str)
            ):
                runtime_specifiers.append(requirement["specifier"][:64])
    lock_python_requirement = bounded_text(lock.get("requires-python"), maximum=64)
    minimum_version(lock_python_requirement, "sdk-python-lock-engine", errors)
    python_lock = {
        "version": lock.get("version"),
        "requires_python": lock_python_requirement,
        "runtime_versions": sorted(set(runtime_versions)),
        "runtime_specifiers": sorted(set(runtime_specifiers)),
    }
    if python_lock["version"] != 1:
        errors.append("sdk-python-lock:format-mismatch")
    if python_lock["runtime_versions"] != [SDK_RUNTIME_VERSION]:
        errors.append("sdk-python-lock:runtime-version-mismatch")
    if python_lock["runtime_specifiers"] != [f"=={SDK_RUNTIME_VERSION}"]:
        errors.append("sdk-python-lock:runtime-specifier-mismatch")

    bazel_text = read_input_text(worktree, entries, ".bazelversion", errors)
    bazel_version = bounded_text(bazel_text.strip() if bazel_text is not None else None, maximum=64)
    if bazel_version is None or version_tuple(bazel_version) is None:
        errors.append("bazel-version:invalid")

    repo_checks = read_input_text(worktree, entries, ".github/workflows/repo-checks.yml", errors)
    uv_versions = sorted(set(re.findall(r'version:\s*["\']?(\d+\.\d+\.\d+)', repo_checks or "")))
    uv_version = UV_VERSION if UV_VERSION in uv_versions else None
    if uv_version is None:
        errors.append("repo-checks:uv-version-missing")

    bazel_setup = read_input_text(worktree, entries, ".github/actions/setup-bazel-ci/action.yml", errors)
    bazelisk_versions = sorted(set(re.findall(r"bazelisk-version:\s*(\d+\.\d+\.\d+)", bazel_setup or "")))
    bazelisk_version = BAZELISK_VERSION if BAZELISK_VERSION in bazelisk_versions else None
    if bazelisk_version is None:
        errors.append("setup-bazel-ci:bazelisk-version-missing")

    pnpm_lock_text = read_input_text(worktree, entries, "pnpm-lock.yaml", errors)
    lockfile_match = re.search(r"^lockfileVersion:\s*['\"]?([^'\"\s]+)", pnpm_lock_text or "", re.MULTILINE)
    pnpm_lock_version = bounded_text(lockfile_match.group(1), maximum=32) if lockfile_match is not None else None
    if pnpm_lock_version is None:
        errors.append("pnpm-lock:lockfile-version-missing")

    errors = sorted(set(errors))
    return {
        "schema": "sdk-build-execution-inputs",
        "version": 1,
        **runtime,
        "input_sdk_artifact_id": SDK_INPUT_ARTIFACT_ID,
        "input_sdk_candidate": SDK_CANDIDATE_SHA,
        "input_sdk_tree": SDK_CANDIDATE_TREE,
        "build_source_sha": BUILD_SOURCE_SHA,
        "build_source_tree": BUILD_SOURCE_TREE,
        "path_observations": path_observations,
        "root_package": {
            "package_manager": root_manager,
            "node_engine": root_node_requirement,
            "pnpm_engine": root_pnpm_requirement,
        },
        "sdk_package": {
            "package_manager": sdk_manager,
            "node_engine": sdk_node_requirement,
            "scripts": sdk_scripts,
        },
        "package_manager_equality": root_manager.get("value") == sdk_manager.get("value"),
        "package_manager_equality_required": False,
        "workspace_packages": packages,
        "python_build": python_build,
        "python_lock": python_lock,
        "pnpm_lock_version": pnpm_lock_version,
        "uv_version": uv_version,
        "node_major": NODE_MAJOR,
        "bazel_version": bazel_version,
        "bazelisk_version": bazelisk_version,
        "verified_composite_runtime_inputs": verified_runtime_inputs,
        "errors": errors,
        "status": "ready" if not errors else "invalid",
    }


def probe_tool(label: str, *args: str, cwd: pathlib.Path) -> dict[str, Any]:
    try:
        result = subprocess.run(args, cwd=cwd, check=False, text=True, capture_output=True)
    except OSError:
        return {"label": label, "status": "error", "exit_code": None, "observed": None}
    output = result.stdout.strip()
    if result.returncode != 0 or not output or "\n" in output or len(output) > 128:
        return {"label": label, "status": "error", "exit_code": result.returncode, "observed": None}
    return {"label": label, "status": "observed", "exit_code": 0, "observed": output}


def collect_tool_observations(
    worktree: pathlib.Path,
    execution_inputs: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    observations = {
        "uv": probe_tool("uv", "uv", "--version", cwd=worktree),
        "pnpm": probe_tool("pnpm", "pnpm", "--version", cwd=worktree),
        "node": probe_tool("node", "node", "--version", cwd=worktree),
        "bazel": probe_tool("bazel", "bazel", "--version", cwd=worktree),
        "cargo": probe_tool("cargo", "cargo", "--version", cwd=worktree),
        "python": {
            "label": "python",
            "status": "observed",
            "exit_code": 0,
            "observed": ".".join(str(part) for part in sys.version_info[:3]),
        },
    }
    errors: list[str] = []
    root_package = execution_inputs["root_package"]
    sdk_package = execution_inputs["sdk_package"]
    root_pnpm_version = root_package["package_manager"].get("version")
    if observations["pnpm"].get("observed") != root_pnpm_version:
        errors.append("pnpm:root-package-version-mismatch")
    uv_identity, uv_errors = parse_uv_identity(
        observations["uv"].get("observed"),
        execution_inputs["uv_version"],
    )
    observations["uv"]["identity"] = uv_identity
    errors.extend(uv_errors)
    node_observed = observations["node"].get("observed")
    node_version = version_tuple(node_observed or "", prefix="v")
    python_version = version_tuple(observations["python"]["observed"])
    pnpm_version = version_tuple(observations["pnpm"].get("observed") or "")
    root_node_minimum = minimum_version(root_package.get("node_engine"), "root-node-engine", errors)
    sdk_node_minimum = minimum_version(sdk_package.get("node_engine"), "sdk-node-engine", errors)
    root_pnpm_minimum = minimum_version(root_package.get("pnpm_engine"), "root-pnpm-engine", errors)
    python_minimum = minimum_version(execution_inputs["python_build"].get("requires_python"), "python-engine", errors)
    if node_version is None or node_version[0] != NODE_MAJOR:
        errors.append("node:major-version-mismatch")
    if node_version is not None and root_node_minimum is not None and node_version < root_node_minimum:
        errors.append("node:root-engine-mismatch")
    if node_version is not None and sdk_node_minimum is not None and node_version < sdk_node_minimum:
        errors.append("node:sdk-engine-mismatch")
    if pnpm_version is None or root_pnpm_minimum is None or pnpm_version < root_pnpm_minimum:
        errors.append("pnpm:root-engine-mismatch")
    if python_version is None or python_minimum is None or python_version < python_minimum:
        errors.append("python:sdk-engine-mismatch")
    bazel_observed = observations["bazel"].get("observed")
    if not isinstance(bazel_observed, str) or not bazel_observed.endswith(f" {execution_inputs['bazel_version']}"):
        errors.append("bazel:version-mismatch")
    for name, observation in observations.items():
        if observation["status"] != "observed":
            errors.append(f"{name}:probe-error")
    errors = sorted(set(errors))
    receipt = {
        "schema": "sdk-build-tool-observations",
        "version": 1,
        "observations": observations,
        "errors": errors,
        "status": "ready" if not errors else "invalid",
    }
    generator_identity = {
        "sdk_generator": "sdk/python/scripts/update_sdk_artifacts.py generate-types",
        "sdk_runtime_dependency": SDK_RUNTIME_DEPENDENCY,
        "uv": observations["uv"].get("observed") or "unavailable",
        "root_pnpm_package_manager": root_package["package_manager"]["value"] or "unavailable",
        "sdk_pnpm_package_manager": sdk_package["package_manager"]["value"] or "unavailable",
        "pnpm": observations["pnpm"].get("observed") or "unavailable",
        "node": observations["node"].get("observed") or "unavailable",
        "python": observations["python"]["observed"],
        "bazel_pin": execution_inputs["bazel_version"] or "unavailable",
        "bazel": observations["bazel"].get("observed") or "unavailable",
        "bazelisk": execution_inputs["bazelisk_version"] or "unavailable",
        "cargo": observations["cargo"].get("observed") or "unavailable",
    }
    return receipt, generator_identity


def require_no_untracked(worktree: pathlib.Path) -> None:
    untracked = run("git", "ls-files", "--others", "--exclude-standard", cwd=worktree).splitlines()
    require(not untracked, f"unexpected untracked candidate paths: {untracked[:8]}")


def require_changed_path_boundary(changed: list[str], allowed: list[str], label: str) -> None:
    require(len(changed) == len(set(changed)), f"{label} contains duplicate changed paths")
    require(changed == sorted(changed), f"{label} changed paths are not canonical")
    changed_set = set(changed)
    require(
        set(OVERLAY_CHANGED_PATHS).issubset(changed_set),
        f"{label} omitted a declared changed overlay path",
    )
    require(changed_set.issubset(allowed), f"{label} escaped the allowed generated-path subset")


def require_declared_path_statuses(status_lines: list[str], changed: list[str], label: str) -> None:
    status_paths: list[str] = []
    for line in status_lines:
        fields = line.split("\t")
        require(len(fields) == 2, f"{label} contains a rename or malformed status: {line}")
        status, path = fields
        if path in OVERLAY_SOURCE_ENTRIES:
            expected = overlay_operation(
                OVERLAY_SOURCE_PREIMAGE_ENTRIES[path],
                OVERLAY_SOURCE_ENTRIES[path],
            )
        else:
            expected = "M"
        require(status == expected, f"{label} path status mismatch for {path}: {status} != {expected}")
        status_paths.append(path)
    require(status_paths == changed, f"{label} status/path mismatch")


def require_candidate_paths(worktree: pathlib.Path, allowed: list[str], label: str) -> list[str]:
    changed = run("git", "diff", "--name-only", SDK_CANDIDATE_SHA, "--", cwd=worktree).splitlines()
    require_changed_path_boundary(changed, allowed, label)
    status_lines = run("git", "diff", "--name-status", SDK_CANDIDATE_SHA, "--", cwd=worktree).splitlines()
    require_declared_path_statuses(status_lines, changed, label)
    require_no_untracked(worktree)
    return changed


def prepare_candidate_worktree(repo: pathlib.Path, temp: pathlib.Path) -> pathlib.Path:
    verify_overlay_contract(repo)
    worktree = temp / "candidate-worktree"
    run("git", "clone", "--shared", "--no-checkout", str(repo), str(worktree))
    run("git", "checkout", "--detach", SDK_CANDIDATE_SHA, cwd=worktree)
    require(not run("git", "status", "--porcelain", cwd=worktree), "candidate worktree is not initially clean")
    selected_paths = [path for path in OVERLAY_PATHS if OVERLAY_SOURCE_ENTRIES[path] is not None]
    deleted_paths = [path for path in OVERLAY_PATHS if OVERLAY_SOURCE_ENTRIES[path] is None]
    if selected_paths:
        run("git", "checkout", BUILD_SOURCE_SHA, "--", *selected_paths, cwd=worktree)
    if deleted_paths:
        run("git", "rm", "--", *deleted_paths, cwd=worktree)
    for path, expected in OVERLAY_SOURCE_ENTRIES.items():
        require(index_entry(worktree, path) == expected, f"selected build source index tuple mismatch: {path}")
    staged_source = run("git", "diff", "--cached", "--name-only", SDK_CANDIDATE_SHA, cwd=worktree).splitlines()
    require(staged_source == OVERLAY_CHANGED_PATHS, "selected source escaped the declared overlay")
    require_candidate_paths(worktree, OVERLAY_CHANGED_PATHS, "build source selection")
    return worktree


def write_execution_inputs(preflight_dir: pathlib.Path, execution_inputs: dict[str, Any]) -> pathlib.Path:
    require(preflight_dir.parent.is_dir(), "preflight parent must already exist")
    require(not preflight_dir.exists(), "preflight directory already exists")
    preflight_dir.mkdir()
    receipt_path = preflight_dir / "execution-inputs.json"
    receipt_path.write_text(json.dumps(execution_inputs, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    require(load(receipt_path) == execution_inputs, "execution-input receipt readback mismatch")
    print(json.dumps(execution_inputs, sort_keys=True))
    require(execution_inputs["status"] == "ready", f"execution-input preflight invalid: {execution_inputs['errors']}")
    return receipt_path


def load_execution_inputs(preflight_dir: pathlib.Path, runtime: dict[str, str]) -> tuple[pathlib.Path, dict[str, Any]]:
    require(preflight_dir.is_dir(), "preflight input must be a directory")
    require({path.name for path in preflight_dir.iterdir()} == {"execution-inputs.json"}, "preflight file set mismatch")
    receipt_path = (preflight_dir / "execution-inputs.json").resolve(strict=True)
    require(receipt_path.parent == preflight_dir, "preflight receipt escaped input directory")
    execution_inputs = load(receipt_path)
    require(execution_inputs.get("schema") == "sdk-build-execution-inputs", "preflight schema mismatch")
    require(execution_inputs.get("version") == 1, "preflight version mismatch")
    require(
        execution_inputs.get("status") == "ready" and execution_inputs.get("errors") == [],
        "preflight is not ready",
    )
    for key, value in runtime.items():
        require(execution_inputs.get(key) == value, f"preflight runtime identity mismatch: {key}")
    require(execution_inputs.get("input_sdk_artifact_id") == SDK_INPUT_ARTIFACT_ID, "preflight artifact mismatch")
    require(execution_inputs.get("input_sdk_candidate") == SDK_CANDIDATE_SHA, "preflight candidate mismatch")
    require(execution_inputs.get("input_sdk_tree") == SDK_CANDIDATE_TREE, "preflight candidate tree mismatch")
    require(execution_inputs.get("build_source_sha") == BUILD_SOURCE_SHA, "preflight build source mismatch")
    require(execution_inputs.get("build_source_tree") == BUILD_SOURCE_TREE, "preflight build source tree mismatch")
    return receipt_path, execution_inputs


def require_committed_candidate_paths(repo: pathlib.Path, revision: str, label: str) -> list[str]:
    changed = run(
        "git",
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        revision,
        cwd=repo,
    ).splitlines()
    require_changed_path_boundary(changed, ALLOWED_MUTABLE_PATHS, label)
    status_lines = run(
        "git",
        "diff-tree",
        "--no-commit-id",
        "--name-status",
        "-r",
        revision,
        cwd=repo,
    ).splitlines()
    require_declared_path_statuses(status_lines, changed, label)
    return changed


def generate_and_test(
    worktree: pathlib.Path,
    temp: pathlib.Path,
    execution_inputs: dict[str, Any],
    diagnostics: pathlib.Path,
) -> tuple[dict[str, str], dict[str, Any]]:
    validate_composed_manifests(worktree, diagnostics)
    tool_observations, generator_identity = collect_tool_observations(worktree, execution_inputs)
    pre_generation_readback = {
        "schema": "sdk-build-pre-generation-readback",
        "version": 1,
        "execution_inputs": execution_inputs,
        "tool_observations": tool_observations,
    }
    print(json.dumps(pre_generation_readback, sort_keys=True))
    require(execution_inputs["status"] == "ready", "execution inputs are not ready")
    resolve_composed_cargo_lock(worktree, diagnostics)
    run_tool(
        "core-skills service library tests",
        "cargo",
        "test",
        "--manifest-path",
        "codex-rs/Cargo.toml",
        "-p",
        "codex-core-skills",
        "--lib",
        "service::tests::",
        cwd=worktree,
    )
    require_candidate_paths(
        worktree,
        sorted([*OVERLAY_CHANGED_PATHS, *SDK_GENERATED_PATHS]),
        "core-skills service library tests",
    )
    require(tool_observations["status"] == "ready", f"tool smoke invalid: {tool_observations['errors']}")
    generator_identity["manifest_structure_receipt_sha256"] = digest(
        diagnostics / "structural-receipt.json"
    )
    generator_identity["cargo_lock_attribution_sha256"] = digest(
        diagnostics / "lock-attribution.json"
    )
    python_project = worktree / "sdk/python"
    generation_env = {
        **os.environ,
        "UV_LINK_MODE": "copy",
        "UV_PROJECT_ENVIRONMENT": str(temp / "sdk-python-venv"),
        "UV_PYTHON": sys.executable,
        "UV_PYTHON_DOWNLOADS": "never",
    }
    generation_env.pop("CODEX_EXEC_PATH", None)

    run_tool(
        "SDK Python dependency sync",
        "uv",
        "sync",
        "--project",
        str(python_project),
        "--group",
        "dev",
        "--frozen",
        cwd=worktree,
        env=generation_env,
    )
    installed_runtime = run_tool(
        "SDK runtime pin verification",
        "uv",
        "run",
        "--project",
        str(python_project),
        "--frozen",
        "--no-sync",
        "python",
        "-c",
        "import importlib.metadata; print(importlib.metadata.version('openai-codex-cli-bin'))",
        cwd=worktree,
        env=generation_env,
    ).strip()
    require(
        installed_runtime == SDK_RUNTIME_VERSION,
        f"installed SDK runtime version mismatch: expected={SDK_RUNTIME_VERSION} observed={installed_runtime[:96]}",
    )
    generator_identity["installed_sdk_runtime"] = installed_runtime

    run_tool(
        "SDK artifact generation",
        "uv",
        "run",
        "--project",
        str(python_project),
        "--frozen",
        "--no-sync",
        "python",
        "scripts/update_sdk_artifacts.py",
        "generate-types",
        cwd=python_project,
        env=generation_env,
    )
    require_candidate_paths(
        worktree,
        sorted([*OVERLAY_CHANGED_PATHS, *SDK_GENERATED_PATHS]),
        "SDK generation",
    )
    run("git", "add", "--", *SDK_GENERATED_PATHS, cwd=worktree)

    run_tool(
        "V8 checksum manifest verification",
        "python3",
        ".github/scripts/rusty_v8_bazel.py",
        "check-module-bazel",
        "--version",
        "150.4.0",
        cwd=worktree,
    )
    require_candidate_paths(
        worktree,
        sorted([*OVERLAY_CHANGED_PATHS, *SDK_GENERATED_PATHS]),
        "V8 checksum verification",
    )

    run_tool(
        "MODULE.bazel.lock generation",
        "bazel",
        "mod",
        "deps",
        "--lockfile_mode=update",
        cwd=worktree,
    )
    require_candidate_paths(
        worktree,
        sorted([*OVERLAY_CHANGED_PATHS, *SDK_GENERATED_PATHS, "MODULE.bazel.lock"]),
        "Bazel lock generation",
    )
    run("git", "add", "--", "MODULE.bazel.lock", cwd=worktree)

    run_tool(
        "pnpm lock generation",
        "pnpm",
        "install",
        "--lockfile-only",
        "--no-frozen-lockfile",
        "--ignore-scripts",
        cwd=worktree,
    )
    require_candidate_paths(worktree, ALLOWED_MUTABLE_PATHS, "joint pnpm lock generation")
    run("git", "add", "--", "pnpm-lock.yaml", cwd=worktree)

    run_tool(
        "SDK Python contract tests",
        "uv",
        "run",
        "--project",
        str(python_project),
        "--frozen",
        "--no-sync",
        "pytest",
        "tests/test_contract_generation.py",
        "tests/test_client_rpc_methods.py",
        cwd=python_project,
        env=generation_env,
    )
    run_tool(
        "frozen pnpm install",
        "pnpm",
        "install",
        "--frozen-lockfile",
        "--ignore-scripts",
        cwd=worktree,
    )
    for command in ("build", "lint", "test"):
        run_tool(
            f"TypeScript SDK {command}",
            "pnpm",
            "--filter",
            "@openai/codex-sdk",
            "run",
            command,
            cwd=worktree,
        )
    run_tool(
        "Bazel lock verification",
        "bazel",
        "mod",
        "deps",
        "--lockfile_mode=error",
        cwd=worktree,
    )
    require_candidate_paths(worktree, ALLOWED_MUTABLE_PATHS, "post-test candidate")
    require(not run("git", "diff", "--name-only", cwd=worktree), "candidate has unstaged tracked changes")
    run("git", "diff", "--cached", "--check", SDK_CANDIDATE_SHA, cwd=worktree)
    generator_identity["commands"] = [
        "cargo metadata --manifest-path codex-rs/Cargo.toml --format-version 1",
        "cargo metadata --manifest-path codex-rs/Cargo.toml --locked --format-version 1",
        "cargo test --manifest-path codex-rs/Cargo.toml -p codex-core-skills --lib service::tests::",
        "uv sync --project sdk/python --group dev --frozen",
        "uv run --project sdk/python --frozen --no-sync python scripts/update_sdk_artifacts.py generate-types",
        "python3 .github/scripts/rusty_v8_bazel.py check-module-bazel --version 150.4.0",
        "bazel mod deps --lockfile_mode=update",
        "pnpm install --lockfile-only --no-frozen-lockfile --ignore-scripts",
        "uv run --project sdk/python --frozen --no-sync pytest tests/test_contract_generation.py tests/test_client_rpc_methods.py",
        "pnpm install --frozen-lockfile --ignore-scripts",
        "pnpm --filter @openai/codex-sdk run build",
        "pnpm --filter @openai/codex-sdk run lint",
        "pnpm --filter @openai/codex-sdk run test",
        "bazel mod deps --lockfile_mode=error",
    ]
    return generator_identity, tool_observations


def verify_emitted_bundle(
    bundle: pathlib.Path,
    expected_heads: dict[str, str],
    candidate_sha: str,
    candidate_tree: str,
    temp: pathlib.Path,
) -> None:
    bare = temp / "verify-candidate.git"
    run("git", "init", "--bare", str(bare))
    run("git", "-C", str(bare), "bundle", "verify", str(bundle))
    actual: dict[str, str] = {}
    for line in run("git", "-C", str(bare), "bundle", "list-heads", str(bundle)).splitlines():
        oid, ref = line.split(maxsplit=1)
        require(ref not in actual, f"duplicate emitted bundle ref: {ref}")
        actual[ref] = oid
    require(actual == expected_heads, "candidate bundle head map mismatch")
    candidate_ref = next(ref for ref, oid in actual.items() if oid == candidate_sha)
    fetch_bundle_ref(bare, bundle, candidate_ref, "refs/import/candidate")
    require(run("git", "-C", str(bare), "rev-parse", "refs/import/candidate^{tree}").strip() == candidate_tree, "candidate bundle tree mismatch")
    require(run("git", "-C", str(bare), "show", "-s", "--format=%P", "refs/import/candidate").split() == [SDK_CANDIDATE_SHA], "candidate bundle parent mismatch")
    require_committed_candidate_paths(bare, "refs/import/candidate", "fresh-bare candidate")
    for path in GENERATED_PATHS:
        parent_entry = tree_entry(bare, SDK_CANDIDATE_SHA, path)
        selected_entry = tree_entry(bare, "refs/import/candidate", path)
        require(parent_entry is not None and selected_entry is not None, f"fresh-bare generated path missing: {path}")
        require(selected_entry[:2] == parent_entry[:2], f"fresh-bare generated path type changed: {path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=pathlib.Path)
    parser.add_argument("--artifact-dir", type=pathlib.Path)
    parser.add_argument("--preflight-dir", type=pathlib.Path)
    parser.add_argument("--output-dir", type=pathlib.Path)
    parser.add_argument("--expected-workflow-sha", required=True)
    parser.add_argument("--expected-workflow-tree", required=True)
    parser.add_argument("--validate-runtime-only", action="store_true")
    parser.add_argument("--validate-manifest-structure-fixtures-only", action="store_true")
    parser.add_argument("--validate-uv-identity")
    parser.add_argument("--prepare-inputs-only", action="store_true")
    parser.add_argument("--metadata-manifest-only", action="store_true")
    parser.add_argument("--probe-sdk-rmcp-preimage-only", action="store_true")
    parser.add_argument("--provider-receipt", type=pathlib.Path)
    args = parser.parse_args()

    runtime = verify_runtime(args.expected_workflow_sha, args.expected_workflow_tree)
    if args.validate_runtime_only:
        require(
            args.repo_root is None
            and args.artifact_dir is None
            and args.preflight_dir is None
            and args.output_dir is None
            and args.validate_uv_identity is None
            and not args.validate_manifest_structure_fixtures_only
            and not args.prepare_inputs_only
            and not args.metadata_manifest_only,
            "runtime-only validation does not accept consumer paths",
        )
        print(json.dumps(runtime, sort_keys=True))
        return
    if args.validate_uv_identity is not None:
        require(
            args.repo_root is None
            and args.artifact_dir is None
            and args.preflight_dir is None
            and args.output_dir is None
            and not args.validate_manifest_structure_fixtures_only
            and not args.prepare_inputs_only
            and not args.metadata_manifest_only,
            "uv identity validation does not accept consumer paths",
        )
        receipt = uv_identity_receipt(args.validate_uv_identity)
        print(json.dumps(receipt, sort_keys=True))
        require(receipt["status"] == "ready", f"uv identity invalid: {receipt['errors']}")
        return
    if args.validate_manifest_structure_fixtures_only:
        require(
            args.repo_root is None
            and args.artifact_dir is None
            and args.preflight_dir is None
            and args.output_dir is None
            and not args.prepare_inputs_only
            and not args.metadata_manifest_only,
            "manifest structure fixture validation does not accept consumer paths",
        )
        print(json.dumps(manifest_structure_fixture_receipt(), sort_keys=True))
        return
    require(
        args.repo_root is not None and args.artifact_dir is not None,
        "repository and artifact paths are required",
    )
    repo = absolute_argument(args.repo_root, "repo-root", must_exist=True)
    artifact = absolute_argument(args.artifact_dir, "artifact-dir", must_exist=True)
    require(repo.is_dir() and artifact.is_dir(), "repository and artifact inputs must be directories")
    if args.probe_sdk_rmcp_preimage_only:
        require(args.output_dir is not None, "diagnostic output path is required")
        require(args.preflight_dir is None, "diagnostic mode does not accept a preflight path")
        require(args.provider_receipt is not None, "diagnostic provider receipt is required")
        provider_receipt = absolute_argument(args.provider_receipt, "provider-receipt", must_exist=True)
        require(provider_receipt.is_file() and not provider_receipt.is_symlink(), "provider receipt is unavailable")
        source_before = source_checkout_inventory(repo)
        require(source_before["head"] == DIAGNOSTIC_SOURCE_SHA, "diagnostic source SHA mismatch")
        require(source_before["tree"] == DIAGNOSTIC_SOURCE_TREE, "diagnostic source tree mismatch")
        require(source_before["parent"] == [DIAGNOSTIC_SOURCE_PARENT], "diagnostic source parent mismatch")
        require(not source_before["status"], "diagnostic source checkout is dirty")
        files = verify_sdk_artifact_files(artifact)
        with tempfile.TemporaryDirectory(prefix="w13825-sdk-probe-", dir=str(repo.parent)) as temp_name:
            temp = pathlib.Path(temp_name).resolve(strict=True)
            isolated_repo = import_sdk_bundle_for_probe(files["bundle"], temp)
            verify_imported_sdk_objects(isolated_repo)
            output = absolute_argument(args.output_dir, "output-dir", must_exist=False)
            emission = emit_sdk_rmcp_preimage_receipt(
                isolated_repo,
                output,
                provider_receipt,
                args.expected_workflow_sha,
                args.expected_workflow_tree,
            )
        source_after = source_checkout_inventory(repo)
        require(source_after == source_before, "diagnostic source checkout changed during isolated probe")
        print(json.dumps(emission, sort_keys=True, separators=(",", ":")))
        return
    verify_build_source_checkout(repo)

    if args.metadata_manifest_only:
        require(not args.prepare_inputs_only, "metadata manifest mode conflicts with input preparation")
        require(
            args.preflight_dir is None and args.output_dir is not None,
            "metadata manifest mode requires only output directory",
        )
        output = absolute_argument(args.output_dir, "output-dir", must_exist=False)
        require(output.parent.is_dir(), "metadata output parent is unavailable")
        files = verify_sdk_artifact_files(artifact)
        with tempfile.TemporaryDirectory(prefix="w13825-tree-manifest-", dir=str(output.parent)) as temp_name:
            temp = pathlib.Path(temp_name).resolve(strict=True)
            import_sdk_bundle(repo, files["bundle"], temp)
            sdk_receipt = load(files["receipt"])
            verify_sdk_input_entries(repo, sdk_receipt)
            emission = emit_tree_metadata_manifest(repo, files, output, runtime)
        print(json.dumps(emission, sort_keys=True, separators=(",", ":")))
        return

    if args.prepare_inputs_only:
        require(args.preflight_dir is not None and args.output_dir is None, "preflight path is required")
        preflight_dir = absolute_argument(args.preflight_dir, "preflight-dir", must_exist=False)
        require(preflight_dir.parent.is_dir(), "preflight parent must already exist")
        files = verify_sdk_artifact_files(artifact)
        with tempfile.TemporaryDirectory(prefix="w13825-sdk-preflight-", dir=str(preflight_dir.parent)) as temp_name:
            temp = pathlib.Path(temp_name).resolve(strict=True)
            import_sdk_bundle(repo, files["bundle"], temp)
            sdk_receipt = load(files["receipt"])
            verified_runtime_inputs = verify_sdk_input_entries(repo, sdk_receipt)
            worktree = prepare_candidate_worktree(repo, temp)
            execution_inputs = collect_execution_inputs(worktree, runtime, verified_runtime_inputs)
        write_execution_inputs(preflight_dir, execution_inputs)
        return

    require(args.preflight_dir is not None and args.output_dir is not None, "preflight and output paths are required")
    preflight_dir = absolute_argument(args.preflight_dir, "preflight-dir", must_exist=True)
    output = absolute_argument(args.output_dir, "output-dir", must_exist=False)
    require(output.parent.is_dir(), "output parent must already exist")
    require(not output.exists(), "output directory already exists")
    preflight_path, execution_inputs = load_execution_inputs(preflight_dir, runtime)
    output.mkdir()
    diagnostics = output / "diagnostics"
    diagnostics.mkdir()
    verify_imported_sdk_objects(repo)
    emit_sdk_bundle_path_receipt(repo, diagnostics)
    receipt_path = artifact / "receipt.json"
    require(receipt_path.is_file() and not receipt_path.is_symlink(), "SDK input receipt is unavailable")
    require(digest(receipt_path) == SDK_INPUT_RECEIPT_SHA256, "SDK input receipt digest mismatch after preflight")
    sdk_receipt = load(receipt_path)
    with tempfile.TemporaryDirectory(prefix="w13825-sdk-build-", dir=str(output.parent)) as temp_name:
        temp = pathlib.Path(temp_name).resolve(strict=True)
        verified_runtime_inputs = verify_sdk_input_entries(repo, sdk_receipt)
        worktree = prepare_candidate_worktree(repo, temp)
        observed_inputs = collect_execution_inputs(worktree, runtime, verified_runtime_inputs)
        require(observed_inputs == execution_inputs, "execution inputs changed after preflight")
        generator_identity, tool_observations = generate_and_test(
            worktree,
            temp,
            execution_inputs,
            diagnostics,
        )
        candidate_tree = run("git", "write-tree", cwd=worktree).strip()
        commit_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "github-actions[bot]",
            "GIT_AUTHOR_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
            "GIT_COMMITTER_NAME": "github-actions[bot]",
            "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
        }
        candidate_sha = run(
            "git",
            "commit-tree",
            candidate_tree,
            "-p",
            SDK_CANDIDATE_SHA,
            "-m",
            "Compose accepted SDK and build source with generated locks",
            cwd=worktree,
            env=commit_env,
        ).strip()
        require(run("git", "show", "-s", "--format=%P", candidate_sha, cwd=worktree).split() == [SDK_CANDIDATE_SHA], "candidate parent mismatch")
        candidate_paths = require_committed_candidate_paths(worktree, candidate_sha, "candidate diff")
        candidate_path_set = set(candidate_paths)

        path_dispositions: list[dict[str, Any]] = []
        generated_dispositions: list[dict[str, Any]] = []
        disposition_paths = sorted(set(ALLOWED_MUTABLE_PATHS) | set(OVERLAY_PATHS))
        overlay_dispositions: list[dict[str, Any]] = []
        for path in disposition_paths:
            parent_entry = tree_entry(worktree, SDK_CANDIDATE_SHA, path)
            selected_entry = tree_entry(worktree, candidate_sha, path)
            role = "generated" if path in GENERATED_PATHS else "build-source"
            changed = path in candidate_path_set
            if role == "build-source":
                source_entry = tree_entry(worktree, BUILD_SOURCE_SHA, path)
                expected_preimage = OVERLAY_SOURCE_PREIMAGE_ENTRIES[path]
                expected_postimage = OVERLAY_SOURCE_ENTRIES[path]
                require(parent_entry == expected_preimage, f"final overlay preimage mismatch: {path}")
                require(source_entry == expected_postimage, f"final overlay source tuple mismatch: {path}")
                require(selected_entry == expected_postimage, f"final overlay selected tuple mismatch: {path}")
                require(
                    changed == (expected_preimage != expected_postimage),
                    f"final overlay change disposition mismatch: {path}",
                )
                operation = overlay_operation(expected_preimage, expected_postimage)
                disposition = {
                    "A": "selected-build-source-addition",
                    "M": "selected-build-source-modification",
                    "D": "selected-build-source-deletion",
                    "E": "verified-build-source-exact",
                }[operation]
            else:
                source_entry = (
                    tree_entry(worktree, BUILD_SOURCE_SHA, path)
                    if path in OVERLAY_SOURCE_ENTRIES
                    else None
                )
                require(parent_entry is not None and selected_entry is not None, f"generated path missing: {path}")
                require(selected_entry[:2] == parent_entry[:2], f"generated path type changed: {path}")
                require(changed == (selected_entry != parent_entry), f"generated path change manifest mismatch: {path}")
                disposition = (
                    "resolved-from-build-source-seed"
                    if source_entry is not None
                    else "regenerated-change" if changed else "regenerated-noop"
                )
            path_disposition = {
                "path": path,
                "role": role,
                "disposition": disposition,
                "changed": changed,
                "parent": tuple_json(parent_entry),
                "build_source": tuple_json(source_entry),
                "selected": tuple_json(selected_entry),
            }
            path_dispositions.append(path_disposition)
            if role == "generated":
                generated_dispositions.append(path_disposition)
            else:
                overlay_dispositions.append(path_disposition)
        require(len(generated_dispositions) == len(GENERATED_PATHS), "generated disposition count mismatch")
        expected_overlay_dispositions = len(set(OVERLAY_PATHS) - set(GENERATED_PATHS))
        require(
            len(overlay_dispositions) == expected_overlay_dispositions,
            "overlay disposition count mismatch",
        )

        retained_patches = []
        for path, expected in PATCH_DEPENDENCIES.items():
            actual = tree_entry(worktree, candidate_sha, path)
            require(actual == expected, f"final candidate patch dependency mismatch: {path}")
            retained_patches.append({"path": path, "entry": tuple_json(actual)})
        retained_sdk_source = []
        for path in SDK_SOURCE_PATHS:
            expected = tree_entry(worktree, SDK_CANDIDATE_SHA, path)
            actual = tree_entry(worktree, candidate_sha, path)
            require(actual == expected, f"final candidate changed accepted SDK source: {path}")
            retained_sdk_source.append({"path": path, "entry": tuple_json(actual)})

        prefix = f"refs/w13825-sdk-build-{runtime['consumer_run_id']}-{runtime['consumer_run_attempt']}"
        emitted_heads = {
            f"{prefix}/base": BASE_SHA,
            f"{prefix}/build-source": BUILD_SOURCE_SHA,
            f"{prefix}/candidate": candidate_sha,
            f"{prefix}/materialized": MATERIALIZED_SHA,
            f"{prefix}/sdk-candidate": SDK_CANDIDATE_SHA,
            f"{prefix}/sdk-source": SDK_SOURCE_SHA,
            f"{prefix}/upstream": GLOBAL_UPSTREAM_SHA,
        }
        for ref, oid in emitted_heads.items():
            run("git", "update-ref", ref, oid, cwd=worktree)
        candidate_bundle = (output / "candidate.bundle").resolve()
        run("git", "bundle", "create", str(candidate_bundle), *sorted(emitted_heads), cwd=worktree)
        verify_emitted_bundle(candidate_bundle, emitted_heads, candidate_sha, candidate_tree, temp)

        receipt = {
            "schema": "sdk-build-hosted-consumer-disposition",
            "version": 2,
            "repository": REPOSITORY,
            "input_sdk_artifact_id": SDK_INPUT_ARTIFACT_ID,
            "input_sdk_candidate": SDK_CANDIDATE_SHA,
            "input_sdk_tree": SDK_CANDIDATE_TREE,
            "input_sdk_parent": SDK_CANDIDATE_PARENT,
            "build_source_branch": BUILD_SOURCE_BRANCH,
            "build_source_sha": BUILD_SOURCE_SHA,
            "build_source_tree": BUILD_SOURCE_TREE,
            "build_source_parent": BUILD_SOURCE_PARENT,
            "mutable_path_policy": "exact-declared-overlay-plus-allowed-generated-subset",
            "allowed_mutable_path_count": len(ALLOWED_MUTABLE_PATHS),
            "allowed_mutable_path_set_sha256": path_digest(ALLOWED_MUTABLE_PATHS),
            "actual_changed_path_count": len(candidate_paths),
            "actual_changed_path_set_sha256": path_digest(candidate_paths),
            "actual_changed_paths": candidate_paths,
            "build_source_path_count": len(BUILD_PATHS),
            "build_source_path_set_sha256": BUILD_PATHS_SHA256,
            "declared_overlay_path_count": len(OVERLAY_PATHS),
            "declared_overlay_path_set_sha256": OVERLAY_PATHS_SHA256,
            "changed_overlay_path_count": len(OVERLAY_CHANGED_PATHS),
            "changed_overlay_path_set_sha256": OVERLAY_CHANGED_PATHS_SHA256,
            "restore_path_count": len(RESTORE_PATHS),
            "restore_path_set_sha256": RESTORE_PATHS_SHA256,
            "restore_entries_sha256": RESTORE_ENTRIES_SHA256,
            "core_skills_path_count": len(CORE_SKILLS_PATHS),
            "core_skills_path_set_sha256": CORE_SKILLS_PATHS_SHA256,
            "core_skills_entries_sha256": CORE_SKILLS_ENTRIES_SHA256,
            "core_skills_additions_sha256": CORE_SKILLS_ADDITIONS_SHA256,
            "core_skills_exact_sha256": CORE_SKILLS_EXACT_SHA256,
            "overlay_contract": verify_overlay_contract(worktree),
            "overlay_dispositions": overlay_dispositions,
            "generated_path_count": len(GENERATED_PATHS),
            "generated_paths": GENERATED_PATHS,
            "generated_path_policy": "mandatory-resolution-or-generation-allowed-change-subset",
            "generated_path_dispositions": generated_dispositions,
            "candidate_sha": candidate_sha,
            "candidate_tree": candidate_tree,
            "candidate_parent": SDK_CANDIDATE_SHA,
            "paths": path_dispositions,
            "verified_retained_patch_dependencies": retained_patches,
            "verified_composite_runtime_inputs": verified_runtime_inputs,
            "preserved_sdk_source_paths": retained_sdk_source,
            "execution_input_receipt_sha256": digest(preflight_path),
            "execution_inputs": execution_inputs,
            "tool_observations": tool_observations,
            "generator_identity": generator_identity,
            "diagnostic_receipts": {
                "structural-receipt.json": digest(diagnostics / "structural-receipt.json"),
                "lock-attribution.json": digest(diagnostics / "lock-attribution.json"),
            },
        }
        receipt_path = output / "receipt.json"
        receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        provenance = {
            "schema": "sdk-build-hosted-consumer-provenance",
            "version": 2,
            "signed": False,
            **runtime,
            "input_sdk_artifact_id": SDK_INPUT_ARTIFACT_ID,
            "input_sdk_artifact_name": SDK_INPUT_ARTIFACT_NAME,
            "input_sdk_artifact_size": SDK_INPUT_ARTIFACT_SIZE,
            "input_sdk_archive_sha256": SDK_INPUT_ARCHIVE_SHA256,
            "input_sdk_run_id": SDK_INPUT_RUN_ID,
            "input_sdk_run_attempt": SDK_INPUT_RUN_ATTEMPT,
            "input_sdk_bundle_sha256": SDK_INPUT_BUNDLE_SHA256,
            "input_sdk_receipt_sha256": SDK_INPUT_RECEIPT_SHA256,
            "input_sdk_candidate": SDK_CANDIDATE_SHA,
            "input_sdk_tree": SDK_CANDIDATE_TREE,
            "input_sdk_parent": SDK_CANDIDATE_PARENT,
            "build_source_branch": BUILD_SOURCE_BRANCH,
            "build_source_sha": BUILD_SOURCE_SHA,
            "build_source_tree": BUILD_SOURCE_TREE,
            "build_source_parent": BUILD_SOURCE_PARENT,
            "mutable_path_policy": "exact-declared-overlay-plus-allowed-generated-subset",
            "allowed_mutable_path_count": len(ALLOWED_MUTABLE_PATHS),
            "allowed_mutable_path_set_sha256": path_digest(ALLOWED_MUTABLE_PATHS),
            "declared_overlay_path_count": len(OVERLAY_PATHS),
            "declared_overlay_path_set_sha256": OVERLAY_PATHS_SHA256,
            "changed_overlay_path_count": len(OVERLAY_CHANGED_PATHS),
            "changed_overlay_path_set_sha256": OVERLAY_CHANGED_PATHS_SHA256,
            "restore_path_count": len(RESTORE_PATHS),
            "restore_path_set_sha256": RESTORE_PATHS_SHA256,
            "restore_entries_sha256": RESTORE_ENTRIES_SHA256,
            "core_skills_path_count": len(CORE_SKILLS_PATHS),
            "core_skills_path_set_sha256": CORE_SKILLS_PATHS_SHA256,
            "core_skills_entries_sha256": CORE_SKILLS_ENTRIES_SHA256,
            "core_skills_additions_sha256": CORE_SKILLS_ADDITIONS_SHA256,
            "core_skills_exact_sha256": CORE_SKILLS_EXACT_SHA256,
            "actual_changed_path_count": len(candidate_paths),
            "actual_changed_path_set_sha256": path_digest(candidate_paths),
            "actual_changed_paths": candidate_paths,
            "candidate_sha": candidate_sha,
            "candidate_tree": candidate_tree,
            "candidate_parent": SDK_CANDIDATE_SHA,
            "candidate_bundle_heads": emitted_heads,
            "candidate_bundle_sha256": digest(candidate_bundle),
            "disposition_receipt_sha256": digest(receipt_path),
            "verified_composite_runtime_inputs": verified_runtime_inputs,
            "execution_input_receipt_sha256": digest(preflight_path),
            "execution_inputs": execution_inputs,
            "tool_observations": tool_observations,
            "generator_identity": generator_identity,
            "diagnostic_receipts": {
                "structural-receipt.json": digest(diagnostics / "structural-receipt.json"),
                "lock-attribution.json": digest(diagnostics / "lock-attribution.json"),
            },
        }
        provenance_path = output / "provenance.json"
        provenance_path.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        require(load(receipt_path) == receipt, "emitted disposition receipt readback mismatch")
        require(load(provenance_path) == provenance, "emitted provenance readback mismatch")

    print(json.dumps(provenance, sort_keys=True))


if __name__ == "__main__":
    main()
