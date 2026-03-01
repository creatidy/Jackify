# Inline Metadata Repair (Native Installer)

## Problem

Some `.wabbajack` archives contain stale `InlineFile` metadata in `modlist`:

- `Directives[].Hash` and/or `Directives[].Size` do not match bytes stored in `SourceDataID` entries.

This causes `jackify-engine` included-file verification to fail with:

- `expected <hash> got <hash>`

## Fix

Before running `jackify-engine` for local file installs (`-w <file>.wabbajack`), Jackify now:

1. Validates all `InlineFile` directives against actual `SourceDataID` bytes.
2. Rewrites mismatched `Hash` and `Size` fields in `modlist`.
3. Writes a sibling archive: `*.jackify-repaired.wabbajack`.
4. Launches engine with the repaired archive.

## Where

- `jackify/backend/utils/wabbajack_inline_repair.py`
- Hooked in:
  - `jackify/backend/core/modlist_operations_configuration_cli.py`
  - `jackify/frontends/gui/screens/install_modlist_installer_thread.py`

## Toggle

Enabled by default.

- Disable with: `JACKIFY_REPAIR_INLINE_METADATA=0`

## Runtime log

When repair is applied, install output contains:

- `[INLINE_REPAIR] repaired src=... dst=... inline_changed=... profile_inline_changed=...`
