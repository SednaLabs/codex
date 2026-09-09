import rust
import codeql.rust.controlflow.BasicBlocks
import codeql.rust.controlflow.CfgNodes
import codeql.rust.controlflow.ControlFlowGraph
import codeql.rust.dataflow.DataFlow

/**
 * Shared, deliberately bounded response-flow facts for native visual
 * computer-use handlers.
 *
 * The Rust data-flow library is used only for local value-preserving moves,
 * aliases, and ordinary reborrows.  Wrapper summaries are not inferred from a
 * function name: a guard is trusted only when its body still downgrades a
 * response whose image proof is absent.  Unknown interprocedural and
 * collection edges remain unknown and are intentionally not treated as proof.
 */

predicate visualProviderFile(File file) {
  file.getRelativePath() = "codex-rs/android-computer-use/src/lib.rs" or
  file.getRelativePath() = "codex-rs/browser-computer-use/src/lib.rs" or
  // Keep the historical fixture path for the existing focused query test.
  file.getRelativePath() = "codex-rs/computer-use-runtime/src/lib.rs"
}

predicate productionVisualProviderFile(File file) {
  file.getRelativePath() = "codex-rs/android-computer-use/src/lib.rs" or
  file.getRelativePath() = "codex-rs/browser-computer-use/src/lib.rs"
}

predicate visualHandler(Function function) {
  visualProviderFile(function.getFile()) and
  (
    function.getName().getText() = "observe" or
    function.getName().getText() = "step" or
    function.getName().getText() = "install_build_from_run" or
    function.getName().getText() = "handle_with_provider"
  )
}

predicate productionVisualHandler(Function function) {
  productionVisualProviderFile(function.getFile()) and
  (
    function.getName().getText() = "observe" or
    function.getName().getText() = "step" or
    function.getName().getText() = "install_build_from_run" or
    function.getName().getText() = "handle_with_provider"
  )
}

predicate variableAccessExpr(Expr expr, Variable variable) {
  exists(VariableAccess access |
    expr = access and
    access.getVariable() = variable
  )
}

/** A variable access through the ordinary `&mut response` reborrow shape. */
predicate responseVariableExpr(Expr expr, Variable variable) {
  variableAccessExpr(expr, variable) or
  exists(RefExpr ref |
    expr = ref and
    variableAccessExpr(ref.getExpr(), variable)
  )
}

/** A local variable initialized from a reference to another response. */
predicate referenceAlias(Variable alias, Variable target) {
  exists(RefExpr reference |
    alias.getInitializer() = reference and
    responseVariableExpr(reference.getExpr(), target)
  )
}

/** A local alias initialized from the response's content-items field. */
predicate contentItemsAlias(Variable alias, Variable responseVariable) {
  exists(RefExpr reference, FieldExpr field |
    alias.getInitializer() = reference and
    reference.getExpr() = field and
    field.hasContainer() and
    field.hasIdentifier() and
    field.getIdentifier().getText() = "content_items" and
    responseVariableExpr(field.getContainer(), responseVariable)
  )
}

/**
 * Local value flow covers aliases and ordinary borrow/reborrow edges without
 * claiming a universal interprocedural or collection model.
 */
predicate localValueFlow(Expr sourceExpr, Expr sinkExpr) {
  exists(DataFlow::ExprNode source, DataFlow::ExprNode sink |
    source.asExpr() = sourceExpr and
    sink.asExpr() = sinkExpr and
    DataFlow::localFlow(source, sink)
  )
}

predicate visualGuardCall(Call call, Function handler) {
  call.getEnclosingCallable() = handler and
  exists(Function target |
    target = call.getStaticTarget() and
    target.getName().getText() = "require_native_image_for_visual_response" and
    visualProviderFile(target.getFile())
  )
}

predicate visualGuardTarget(Call call, Function target) {
  exists(Function resolved |
    resolved = call.getStaticTarget() and
    target = resolved and
    target.getName().getText() = "require_native_image_for_visual_response" and
    visualProviderFile(target.getFile())
  )
}

predicate successfulResultExit(Function function, Expr exitExpr, Expr responseExpr) {
  exitExpr.getEnclosingCallable() = function and
  exists(TupleVariantExpr ok |
    ok = exitExpr and
    ok.getVariant().getName().getText() = "Ok" and
    responseExpr = ok.getArgList().getArg(0)
  ) and
  (
    exists(ReturnExpr returnExpr | returnExpr.getExpr() = exitExpr) or
    function.getBody().getStmtList().getTailExpr() = exitExpr
  )
}

predicate guardUsesReturnedResponse(Call guard, Expr responseExpr) {
  exists(Variable variable |
    responseVariableExpr(responseExpr, variable) and
    responseVariableExpr(guard.getPositionalArgument(0), variable)
  ) or
  exists(Variable responseVariable, Variable guardVariable |
    responseVariableExpr(responseExpr, responseVariable) and
    responseVariableExpr(guard.getPositionalArgument(0), guardVariable) and
    (
      referenceAlias(guardVariable, responseVariable) or
      referenceAlias(responseVariable, guardVariable)
    )
  ) or
  localValueFlow(responseExpr, guard.getPositionalArgument(0)) or
  localValueFlow(guard.getPositionalArgument(0), responseExpr)
}

predicate cfgNodeForExpr(Expr expr, CfgNode node) {
  node = expr.getACfgNode()
}

predicate blockNodeOrder(BasicBlock block, CfgNode earlier, CfgNode later) {
  exists(int earlierIndex, int laterIndex |
    block.getNode(earlierIndex) = earlier and
    block.getNode(laterIndex) = later and
    earlierIndex <= laterIndex
  )
}

predicate guardDominatesExit(Call guard, Expr exitExpr) {
  exists(CfgNode guardNode, CfgNode exitNode, BasicBlock guardBlock, BasicBlock exitBlock |
    exists(CallCfgNode callNode |
      callNode.getCall() = guard and
      guardNode = callNode
    ) and
    cfgNodeForExpr(exitExpr, exitNode) and
    guardBlock.getANode() = guardNode and
    exitBlock.getANode() = exitNode and
    (
      guardBlock.strictlyDominates(exitBlock) or
      guardBlock = exitBlock and blockNodeOrder(guardBlock, guardNode, exitNode)
    )
  )
}

/** Calls whose contract already returns `success: false`; they are safe. */
predicate definitelyFailedResponse(Expr responseExpr) {
  exists(Call call |
    (
      call = responseExpr or
      exists(AwaitExpr await |
        await = responseExpr and
        call = await.getExpr()
      )
    ) and
    exists(Function target |
      target = call.getStaticTarget() and
      (
        target.getName().getText() = "action_failure_response" or
        target.getName().getText() = "failed_response"
      )
    )
  )
}

/**
 * A write after the guard invalidates the response-version proof.  The
 * assignment AST catches direct local replacement; the narrow field spelling
 * catches image removal and success restoration while leaving metadata-only
 * edits alone.
 */
predicate nodeBefore(CfgNode earlier, CfgNode later) {
  exists(BasicBlock earlierBlock, BasicBlock laterBlock |
    earlierBlock.getANode() = earlier and
    laterBlock.getANode() = later and
    (
      earlierBlock.strictlyDominates(laterBlock) or
      earlierBlock = laterBlock and blockNodeOrder(earlierBlock, earlier, later)
    )
  )
}

predicate cfgNodeBetween(Call guard, Expr mutation, Expr exitExpr) {
  exists(CallCfgNode guardNode, CfgNode writeNode, CfgNode exitNode |
    guardNode.getCall() = guard and
    cfgNodeForExpr(mutation, writeNode) and
    cfgNodeForExpr(exitExpr, exitNode) and
    nodeBefore(guardNode, writeNode) and
    nodeBefore(writeNode, exitNode)
  )
}

predicate responseFieldWrite(AssignmentExpr assignment, Variable variable) {
  exists(FieldExpr field |
    assignment.getLhs() = field and
    field.hasContainer() and
    field.hasIdentifier() and
    responseVariableExpr(field.getContainer(), variable) and
    (
      field.getIdentifier().getText() = "content_items" or
      field.getIdentifier().getText() = "success" or
      field.getIdentifier().getText() = "error"
    )
  )
}

/** A destructive clear of the response's native-image content collection. */
predicate responseContentItemsClear(MethodCallExpr clearCall, Variable variable) {
  clearCall.getIdentifier().getText() = "clear" and
  exists(FieldExpr field |
    field.hasContainer() and
    field.hasIdentifier() and
    field.getIdentifier().getText() = "content_items" and
    responseVariableExpr(field.getContainer(), variable) and
    (
      clearCall.getReceiver() = field or
      localValueFlow(field, clearCall.getReceiver()) or
      exists(VariableAccess access |
        clearCall.getReceiver() = access and
        contentItemsAlias(access.getVariable(), variable)
      )
    )
  )
}

predicate responseWriteAfterGuard(
  Function function,
  Call guard,
  Expr exitExpr,
  Expr responseExpr
) {
  exists(Variable variable, AssignmentExpr assignment |
    responseVariableExpr(responseExpr, variable) and
    assignment.getEnclosingCallable() = function and
    cfgNodeBetween(guard, assignment, exitExpr) and
    (
      exists(VariableWriteAccess write |
        write = assignment.getAWriteAccess() and
        write.getVariable() = variable
      ) or
      responseFieldWrite(assignment, variable)
    )
  )
}

predicate responseContentItemsClearAfterGuard(
  Function function,
  Call guard,
  Expr exitExpr,
  Expr responseExpr
) {
  exists(Variable variable, MethodCallExpr clearCall |
    responseVariableExpr(responseExpr, variable) and
    clearCall.getEnclosingCallable() = function and
    cfgNodeBetween(guard, clearCall, exitExpr) and
    responseContentItemsClear(clearCall, variable)
  )
}

/**
 * Keep helper recognition body-backed.  The guard must still force a failed
 * response when image output is absent; naming a function alone is not enough.
 */
predicate guardBodyDowngrades(Call guard) {
  exists(Function target |
    visualGuardTarget(guard, target) and
    (
      // The legacy fixture intentionally models only the call-site contract;
      // production paths must satisfy the body-backed helper check below.
      not productionVisualProviderFile(target.getFile()) or
      (
        exists(AssignmentExpr successWrite, FieldExpr successField, BooleanLiteralExpr falseValue |
          successWrite.getEnclosingCallable() = target and
          successWrite.getLhs() = successField and
          successField.hasIdentifier() and
          successField.getIdentifier().getText() = "success" and
          falseValue = successWrite.getRhs() and
          falseValue.getTextValue() = "false"
        ) and
        exists(AssignmentExpr errorWrite, FieldExpr errorField |
          errorWrite.getEnclosingCallable() = target and
          errorWrite.getLhs() = errorField and
          errorField.hasIdentifier() and
          errorField.getIdentifier().getText() = "error"
        )
      )
    )
  )
}

predicate responseVersionGuarded(
  Function function,
  Expr exitExpr,
  Expr responseExpr
) {
  exists(Call guard |
    visualGuardCall(guard, function) and
    guardUsesReturnedResponse(guard, responseExpr) and
    guardDominatesExit(guard, exitExpr) and
    not responseWriteAfterGuard(function, guard, exitExpr, responseExpr) and
    not responseContentItemsClearAfterGuard(function, guard, exitExpr, responseExpr) and
    guardBodyDowngrades(guard)
  )
}
