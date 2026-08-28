/** Minimal assertion harness shared by the fixture pages. */
class Harness {
  constructor() {
    this.passed = 0;
    this.failures = [];
  }

  ok(condition, label) {
    if (condition) this.passed += 1;
    else this.failures.push(label);
  }

  /** Assert `haystack` does NOT contain `needle`. */
  no(haystack, needle, label) {
    this.ok(!String(haystack).includes(needle), label);
  }

  eq(actual, expected, label) {
    this.ok(
      actual === expected,
      `${label} (expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)})`,
    );
  }

  /** Assert `fn` throws, and that the message matches `pattern`. */
  throws(fn, pattern, label) {
    try {
      fn();
      this.failures.push(`${label} (did not throw)`);
    } catch (err) {
      const message = err && err.message ? err.message : String(err);
      this.ok(
        new RegExp(pattern).test(message),
        `${label} (message was: ${message})`,
      );
    }
  }

  report() {
    return {
      passed: this.passed,
      failed: this.failures.length,
      failures: this.failures,
    };
  }
}
