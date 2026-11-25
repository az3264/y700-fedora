# Patches

Patches applied on top of BASE.env. Each entry's `source` is an upstream URL pinned
to a commit, or `armada` if it's original; a URL source with no `notes` is verbatim.
`notes` mean the file was modified.

- `patches/0001-thunkgen-llvm23-clearstatcache.patch`
  source: armada
  notes: guards `FileManager::clearStatCache()` in `ThunkLibs/Generator/gen.cpp`
  with `#if LLVM_VERSION_MAJOR < 23`; LLVM 23 deleted the clang stat cache, and
  FEX master still calls it unguarded. Drop this once the pinned commit is newer
  than the upstream LLVM 23 fix.
