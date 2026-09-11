# History rewrite candidate fixtures

The hosted candidate should generate these fixtures in a temporary repository
before touching the bound source history. The fixture matrix is deliberately
small but exercises the invariants that the candidate proves:

* a linear chain and a two-parent merge, including ordered parent readback;
* a selected blob repeated at a target and an unrelated exact path (the scoped
  blob rule must reject it), alongside an unrelated duplicated blob that must
  remain allowed;
* binary and non-UTF-8 contents, which must remain byte-identical when not
  selected by a rule;
* byte-exact classifier differential output against the frozen classifier at
  `bc95757b43a5c293de98cfef210baed11da205e3`, including an embedded-NUL commit
  body, tab/newline/non-UTF-8 path bytes, subtree ordering, and repeated root
  trees;
* a target path rename and target content substitution, with mode/type and
  every untouched entry preserved exactly;
* a commit whose sole tree change collapses under an approved replacement,
  retained one-to-one with its ordered parent instead of being pruned;
* workflow-shaped relative repository, work, output, policy, and preimage
  arguments, including callback-file resolution across `git -C`;
* negative cases for path collisions, selected shared blobs, non-target byte,
  mode, and type changes, zero IDs, missing map domains, ordered-parent
  changes, and changed refs;
* lightweight and annotated tags, including peeled commit mapping and an
  explicit signature-consequence record, original-to-isolated ref joining,
  and raw tag-object signature-presence fixtures for unsigned, syntactically
  signed-but-unvalidated, malformed, and unknown-armored tag objects;
* a remote-ref snapshot taken before and after candidate execution, proving
  that source refs were not changed.

The runner invokes `../rewrite_candidate.py`, the same production
`git-filter-repo` callback and verification driver used by the workflow. It
also requires the workflow to materialize the frozen reference classifier in
an isolated test-only repository and verify its commit, tree, blob, and file
digests before the differential case runs. The fixture emits identity-only
maps/digests and discards temporary repositories and contents. It is a hosted
validation input, not a local test.

The runner also executes the production `publication.py` CLI against a
disposable local bare Git remote. It deterministically regenerates the same
candidate from the frozen synthetic objects, compares the complete approved
proof set, performs the real `git push --atomic` command with one explicit
`--force-with-lease=<ref>:<old>` per ref, then verifies the complete advertised
heads/tags namespace, object types, and a clean isolated fetch. A rejected
pre-receive hook proves that an atomic failure leaves every ref unchanged.

The companion API-shaped fixtures cover immutable manifest and SHA-256 proof
binding, empty/extra/missing/zero-ref rejection, stale backup ordering, exact
environment and approval identities, mandatory writer identities, one-read
active-writer rejection, exact protected-ref and publisher-App exceptions,
authorised control suppression and restoration, durable phase binding,
ambiguous transport readback, and readback failure. They use no GitHub
credential and never contact GitHub. Passing these fixtures proves that the
same executable CLI used by the workflow works against a controlled Git
transport; it does not prove that a real repository publication or live
control mutation occurred.

If the publication runner is hard-killed after writer suppression, an operator
can independently restore the captured writer states from the uploaded
`history-rewrite-publication-intent-<run-id>` artifact. First read the artifact
metadata from the GitHub API and download its zip without extracting it. Then,
in a trusted environment with a freshly minted release-publisher App token in
`GH_TOKEN`, run:

```text
python3 scripts/history-rewrite/publication.py restore-intent-artifact \
  --artifact-zip INTENT.zip --artifact-api-json INTENT-api.json \
  --run-id RUN_ID --artifact-id ARTIFACT_ID \
  --artifact-api-digest DIGEST_WITHOUT_SHA256_PREFIX \
  --frozen-sha HARNESS_SHA --frozen-tree HARNESS_TREE \
  --manifest-sha256 APPROVED_MANIFEST_SHA256 \
  --receipt independent-restoration-receipt.json
```

The command verifies the API artifact ID, run binding, API SHA-256, exact
four-file archive domain, external harness and manifest identities, durable
preflight, restoration intent, control plan, and publisher App identity before
restoring each captured state and reading it back. This is the hard-kill
recovery path; the workflow's ordinary `always()` step is not claimed to
survive runner termination.

Immediately before the atomic ref update, the publisher performs one fresh,
finite state snapshot of both mandatory writer workflows, their active runs, the continuing
mirror pause, all three protected branch rules, force-push App allowances, and
all applicable repository rulesets. An active writer fails the attempt and
must be drained externally with the approved blocking watcher before a wholly
fresh dispatch. Per-ref atomic leases protect the approved old ref map, but do
not prevent an external administrator from changing controls after that final
read; the publication receipt records this remaining operational-window risk.
