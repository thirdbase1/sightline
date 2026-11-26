"""Completeness check that uses the SAME skip rules as the indexer.

A naive `os.walk` diff counts build output as "missing" and makes correct
behaviour look like a bug - mui's 21,505 generated icon files are excluded on
purpose, and the first version of this check reported them as 21,510 missing.
"""
import sys, os
sys.path.insert(0,'src')
from pathlib import Path
from sightline import index as I

def expected(root):
    """Every file the indexer SHOULD see: same walk, same skips."""
    out = set()
    rootp = Path(root)
    stack = [rootp]
    while stack:
        cur = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError:
            continue
        for e in entries:
            if e.name in {".git", ".hg", ".svn", ".DS_Store", ".sightline"}:
                continue
            p = Path(e.path)
            if e.is_dir(follow_symlinks=False):
                if I._should_skip_dir(e.name, cur):
                    continue
                stack.append(p)
            elif e.is_file(follow_symlinks=False):
                if p.stat().st_size > I.MAX_FILE_BYTES:
                    continue
                if not I._worth_listing(p) and I._language(p) is None:
                    continue
                if I._looks_generated(p):
                    continue
                out.add(str(p.relative_to(rootp)))
    return out

for root in sys.argv[1:]:
    idx = I.build(root, with_cochange=False)
    exp = expected(root)
    missing = exp - set(idx.files)
    extra = set(idx.files) - exp
    tag = "OK" if not missing else "MISSING"
    print(f"  {Path(root).name:12s} expected {len(exp):6d}  indexed {len(idx.files):6d}  "
          f"missing {len(missing):4d}  extra {len(extra):4d}  {tag}")
    if missing:
        print(f"    e.g. {sorted(missing)[:3]}")
