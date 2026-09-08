import rust

/**
 * Shared helpers for native computer-use contract queries.
 */

predicate cfgTestMeta(Meta meta) {
  meta.getMetaPath().getText() = "cfg" and
  meta.toString().regexpMatch("(?s).*\\btest\\b.*")
}

predicate inlineRustTestModule(Module modItem) {
  cfgTestMeta(modItem.getAnAttr().getMeta()) or
  modItem.getName().getText() = "tests"
}

predicate insideInlineRustTest(AstNode node) {
  exists(Module modItem |
    inlineRustTestModule(modItem) and
    modItem.getFile() = node.getFile() and
    modItem.getLocation().getStartLine() <= node.getLocation().getStartLine() and
    node.getLocation().getEndLine() <= modItem.getLocation().getEndLine()
  )
}

predicate nodeInsideFunction(AstNode node, Function function) {
  node.getFile() = function.getFile() and
  function.getLocation().getStartLine() <= node.getLocation().getStartLine() and
  node.getLocation().getEndLine() <= function.getLocation().getEndLine()
}

predicate androidComputerUseRuntimeFile(File file) {
  // Keep the historical fixture path while binding production analysis to the
  // current Android provider crate.  The fixture path is intentionally
  // retained so the existing focused contract tests remain valid; production
  // witnesses must use the real path below.
  file.getRelativePath() = "codex-rs/computer-use-runtime/src/lib.rs" or
  file.getRelativePath() = "codex-rs/android-computer-use/src/lib.rs"
}

predicate computerUseHandlerFile(File file) {
  file.getRelativePath() = "codex-rs/core/src/tools/handlers/computer_use.rs"
}

predicate appServerProtocolV2File(File file) {
  file.getRelativePath() = "codex-rs/app-server-protocol/src/protocol/v2.rs"
}

predicate threadHistoryFile(File file) {
  file.getRelativePath() = "codex-rs/app-server-protocol/src/protocol/thread_history.rs"
}

predicate computerUsePipelineFile(File file) {
  file.getRelativePath().regexpMatch(
    "codex-rs/(protocol|core|app-server|app-server-protocol|tui|computer-use-runtime)/src/.*\\.rs"
  ) and
  not file.getRelativePath().regexpMatch("(?s).*(/tests?/.*|.*_tests\\.rs|test\\.rs|tests\\.rs)$")
}

predicate functionContainsStringLiteral(Function function, string literalText) {
  exists(StringLiteralExpr literal |
    nodeInsideFunction(literal, function) and
    literal.toString() = literalText
  )
}

predicate functionContainsPathSegment(Function function, string segment) {
  exists(PathAstNode path |
    nodeInsideFunction(path, function) and
    path.getPath().getSegment().getIdentifier().getText() = segment
  )
}

predicate fileContainsPathSegment(File file, string segment) {
  exists(PathAstNode path |
    path.getFile() = file and
    path.getPath().getSegment().getIdentifier().getText() = segment
  ) or
  // Enum and variant declarations are not PathAstNodes, but these contract
  // queries use the helper to ask whether a guarded file carries a schema name.
  exists(Enum enumDecl |
    enumDecl.getFile() = file and
    enumDecl.getName().getText() = segment
  ) or
  exists(Variant variant |
    variant.getFile() = file and
    variant.getName().getText() = segment
  )
}

predicate pathNodeInsidePattern(Pat pat, PathAstNode pathNode) {
  pathNode.getFile() = pat.getFile() and
  pat.getLocation().getStartLine() <= pathNode.getLocation().getStartLine() and
  pathNode.getLocation().getEndLine() <= pat.getLocation().getEndLine()
}

predicate matchArmPatternMentionsVariant(MatchArm arm, string variantName) {
  exists(PathAstNode pat |
    pathNodeInsidePattern(arm.getPat(), pat) and
    pat.getPath().getSegment().getIdentifier().getText() = variantName
  )
}

predicate matchHandlesVariant(MatchExpr matchExpr, string variantName) {
  exists(MatchArm arm |
    arm = matchExpr.getAnArm() and
    matchArmPatternMentionsVariant(arm, variantName)
  )
}

predicate inputImageConstructor(StructExpr expr) {
  expr.getPath().getSegment().getIdentifier().getText() = "InputImage"
}

predicate visualAndroidRuntimeHandler(Function function) {
  androidComputerUseRuntimeFile(function.getFile()) and
  (
    function.getName().getText() = "observe" or
    function.getName().getText() = "step" or
    function.getName().getText() = "install_build_from_run"
  )
}
