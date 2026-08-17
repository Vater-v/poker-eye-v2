# poker-eye-v2 repair pack

Targets v2 around commit `3f4f372`.

- `0002-readable-trainer-status.patch`: unified diff for a clean checkout.
- `0003-chip-scale-100.patch`: repairs an accidental local `default=10`.
  Clean upstream already has `default=100`, so this patch is already satisfied there.
- `apply_v2_repair.py`: preferred for a checkout with local edits. It performs
  semantic edits and makes a timestamped backup before writing.

Recommended:

```bat
python apply_v2_repair.py --repo C:\projects\pokereye\poker-eye-v2 --check
python apply_v2_repair.py --repo C:\projects\pokereye\poker-eye-v2
git diff -- main.py core\bootstrap.py core\trainer.py
```

The Android readable-status patch from the previous pack is independent and can stay applied.
