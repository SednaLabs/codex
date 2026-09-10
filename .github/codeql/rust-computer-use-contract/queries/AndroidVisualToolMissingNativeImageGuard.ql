/**
 * @name Android visual computer-use handler missing native-image guard
 * @description Android visual computer-use handlers must return the same response version that passed a body-backed native-image guard before a successful response can exit.
 * @kind problem
 * @problem.severity error
 * @precision high
 * @id rust/android-visual-tool-missing-native-image-guard
 * @tags correctness
 *       maintainability
 *       computer-use
 */

import rust
import NativeVisualResponseFlow

from Function function, Expr exitExpr, Expr responseExpr
where
  visualHandler(function) and
  successfulResultExit(function, exitExpr, responseExpr) and
  not definitelyFailedResponse(responseExpr) and
  not responseVersionGuarded(function, exitExpr, responseExpr)
select exitExpr,
  "This successful visual computer-use response is not the same response version that passed a body-backed native-image guard. Preserve aliases and borrows, then guard the returned response without later replacing, resetting, or removing its visual evidence."
