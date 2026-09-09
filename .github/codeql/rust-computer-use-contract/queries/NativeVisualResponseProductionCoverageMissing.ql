/**
 * @name Native visual response production handler coverage missing
 * @id rust/native-visual-response-production-handler-coverage-missing
 * @description Production Android and browser visual handlers must remain discoverable with at least one recognized successful response exit so native-image guard analysis cannot become vacuous.
 * @kind problem
 * @problem.severity error
 * @precision high
 * @tags correctness
 *       maintainability
 *       computer-use
 */

import rust
import NativeVisualResponseFlow

predicate expectedProductionHandler(File file, string handlerName) {
  (
    file.getRelativePath() = "codex-rs/android-computer-use/src/lib.rs" and
    (
      handlerName = "observe" or
      handlerName = "step" or
      handlerName = "install_build_from_run"
    )
  )
  or
  (
    file.getRelativePath() = "codex-rs/browser-computer-use/src/lib.rs" and
    handlerName = "handle_with_provider"
  )
}

from File file, string handlerName
where
  expectedProductionHandler(file, handlerName) and
  not exists(Function function, Expr exitExpr, Expr responseExpr |
    function.getFile() = file and
    function.getName().getText() = handlerName and
    successfulResultExit(function, exitExpr, responseExpr)
  )
select file,
  "Expected production visual handler '" + handlerName +
    "' is missing or has no recognized successful response exit, so native-image guard coverage would become vacuous."
