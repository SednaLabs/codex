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
* a target path rename and target content substitution, with mode/type and
  every untouched entry preserved exactly;
* negative cases for path collisions, selected shared blobs, non-target byte,
  mode, and type changes, zero IDs, missing map domains, ordered-parent
  changes, and changed refs;
* lightweight and annotated tags, including peeled commit mapping and an
  explicit signature-consequence record;
* a remote-ref snapshot taken before and after candidate execution, proving
  that source refs were not changed.

The runner invokes `../rewrite_candidate.py`, the same production
`git-filter-repo` callback and verification driver used by the workflow. It
emits identity-only maps/digests and discards temporary repositories and
contents. It is a hosted validation input, not a local test.
