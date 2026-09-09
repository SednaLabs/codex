# History rewrite candidate fixtures

The hosted candidate should generate these fixtures in a temporary repository
before touching the bound source history. The fixture matrix is deliberately
small but exercises the invariants that the candidate proves:

* a linear chain and a two-parent merge, including ordered parent readback;
* one blob repeated at a target and an unrelated path (the scoped blob rule
  must reject this unless `global: true` is explicitly reviewed);
* binary and non-UTF-8 contents, which must remain byte-identical when not
  selected by a rule;
* a target path rename and target content substitution, with mode/type and
  every untouched entry preserved exactly;
* negative rules for missing `target_paths`, path collisions, shared blobs,
  zero/deleted map IDs, missing map-domain entries, and changed ref names;
* lightweight and annotated tags, including peeled commit mapping and an
  explicit signature-consequence record;
* a remote-ref snapshot taken before and after candidate execution, proving
  that source refs were not changed.

The fixture runner must emit identity-only maps/digests and discard temporary
repositories and contents. It is a hosted validation input, not a local test.
