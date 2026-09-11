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
finite state snapshot of both mandatory writer workflows, their active runs,
the continuing mirror pause, all three protected branch rules, force-push,
push and PR App allowances, check sources/strictness, other execution gates,
and all applicable repository rulesets. The normal force-push-only state is
not publication-ready: rewritten commit IDs do not inherit old CI checks.
The separately approved maintenance plan restricts pushes and force pushes to
the publisher App, adds its PR exception on the two PR-protected branches,
and temporarily suspends their ordinary required checks. The exact queue-only
ruleset `20008703` stays active with unchanged rules/conditions/parameters and
exactly one temporary publisher-App `always` exception. This is an explicit
maintenance exception, not evidence of passing ordinary CI. A
read-only API response that omits `bypass_actors` is recorded as `not_returned`,
not as evidence that the list is empty; a separately authorized administrator
retains ownership of full protection and restoration readback. The manifest
binds both the full administrator before/after plan and the expected read-token
projection. Missing actor visibility is never reported as live verification.
Extra actors, queue-rule drift, or an unknown applicable active ruleset fails
closed. An active writer must be drained externally with the blocking watcher before a wholly
fresh dispatch. Per-ref atomic leases protect the approved old ref map, but do
not prevent an external administrator from changing controls after that final
read; the publication receipt records this remaining operational-window risk.

## Read-only preparation and finite encrypted custody

Dispatch `mode=snapshot` on the exact admitted existing branch with
`workflow_harness_sha` and `workflow_harness_tree`. Its job has only read
permissions, no protected environment, no App secrets, and no publication or
control-mutation path. This does not grant its token permission to enumerate
classic protection rules: the default `GITHUB_TOKEN` may be denied that entire
surface. Such a denial fails closed after one query; neither a narrower query,
an empty inventory, nor an automatic credential fallback is accepted. A
separately authorized protection-read observer must be provisioned and bound
before publication can proceed. That observer should remain separate from the
publisher and from the administrator who changes controls. This workflow does
not provision it or silently increase the publisher's permissions.

With an authorized observation principal, capture the normal,
publication-blocked state. Capture a separate administrator snapshot with the
same `snapshot` CLI under that separately authorized principal. Prepare
expected state without changing GitHub:

```text
python3 scripts/history-rewrite/publication.py plan-maintenance \
  --administrator-before ADMINISTRATOR-SNAPSHOT.json \
  --read-token-before WORKFLOW-SNAPSHOT.json --output MAINTENANCE-PLAN.json
```

The plan preserves the exact rollback preimage, including check source IDs and
strictness. Bind it as `controls.maintenance_plan` plus its SHA-256, and bind
`read_token_after` as `controls.protection_snapshot` plus its SHA-256. The
operator approves the complete manifest and maintenance exception before the
administrative window opens. After the administrator applies only that delta,
fresh administrator readback and the workflow's exact expected-state check
precede publication. Restore exact original protections and remove the queue
exception on success or failure. Administrative rollback is separate from
the App's writer-restoration CLI, including after a killed runner. Never leave
the maintenance window open while developing or repeating review.

`mode=custody` is a separate hosted, read-permission job. Its canonical
`history-rewrite-custody-v1` manifest binds `repository`,
`requested_retention_days: 90`, and exactly four `artifacts`: the backup
ciphertext/receipt and candidate ciphertext/proof. Each entry supplies `id`,
`name`, `run_id`, `head_sha`, `size_in_bytes`, and `sha256`. Provide its
gzip+base64 bytes and approved digest through `custody_manifest_gzip_b64` and
`custody_manifest_sha256`, plus the exact executing harness SHA/tree.
The job checks successful original run/API identity, size, digest and expiry,
copies original ZIP bytes without opening or decrypting them, and uploads
those bytes with the original provenance. It verifies the destination API
digest against the upload result and derives an archival deadline seven days
before the actual expiry. Truncated retention fails the bridge check; requested
retention alone is never proof. This is finite hosted escrow, not indefinite
archival storage. A separately selected durable destination remains required
before its recorded archival deadline. Original artifact IDs/digests are not
replaced by the new wrapper's ID/digest, and source archives are not deleted.
