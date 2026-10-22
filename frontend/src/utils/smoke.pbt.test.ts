import { describe, it, expect } from 'vitest';
import fc from 'fast-check';

/**
 * Property-based smoke test. Proves the fast-check harness runs with the
 * project convention of at least 100 iterations per property. Replaced by
 * real property tests in later tasks.
 *
 * Property: smoke-identity
 */
describe('fast-check harness', () => {
  it('runs a trivial property across many inputs (identity under double reverse)', () => {
    fc.assert(
      fc.property(fc.array(fc.integer()), (xs) => {
        const twiceReversed = [...xs].reverse().reverse();
        expect(twiceReversed).toEqual(xs);
      }),
      { numRuns: 100 },
    );
  });
});
