/**
 * Poll until the condition holds. It has no deadline of its own: a slow host only makes the test
 * slower, and a condition that never holds is stopped by vitest's `testTimeout`.
 */
export async function until(predicate: () => boolean | Promise<boolean>, intervalMs = 5): Promise<void> {
  while (!(await predicate())) await new Promise((resolve) => setTimeout(resolve, intervalMs));
}
