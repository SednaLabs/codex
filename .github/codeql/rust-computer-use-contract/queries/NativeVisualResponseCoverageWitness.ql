/**
 * @name Native visual response production coverage witness
 * @id rust/native-visual-response-coverage-witness
 * @description Counts successful visual response exits reached by each production Android or browser handler so an empty or fixture-only analysis cannot masquerade as coverage.
 * @kind metric
 * @tags summary
 *       computer-use
 *
 * This is a summary metric rather than an alert.  A production analysis must
 * return one row for every selected handler and a non-zero successful-exit
 * count.  Missing rows or zero counts are evidence gaps, not a clean detector.
 */

import rust
import NativeVisualResponseFlow

from Function function
where
  productionVisualHandler(function)
select function,
  count(Expr exitExpr |
    exists(Expr responseExpr |
      successfulResultExit(function, exitExpr, responseExpr)
    )
  )
