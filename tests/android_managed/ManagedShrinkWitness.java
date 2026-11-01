package io.github.jichuo1.meguviz.memory.android;

/** Test-only, deliberately unreferenced R8 input to check actual shrinking. */
final class ManagedShrinkWitness {
    static int unreachable() { return 20261101; }
}
