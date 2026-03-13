## Summary
Minimal-diff ANT EDX integration for BrainFlow, keeping changes as close as possible to master while preserving optional EDX support.

This PR keeps only required board integration, build gating, binding constants exposure, and minimal build docs.

## What is included
- Optional build toggle: BUILD_ANT_EDX (default OFF)
- Core EDX board integration (board id 67) in board controller
- Protobuf contract and generated wiring under guarded build path
- Optional dependency gating for Protobuf + gRPC only when BUILD_ANT_EDX=ON
- Board metadata + board factory routing
- Constants/enums exposure across bindings:
  - BoardIds::ANT_NEURO_EDX_BOARD = 67
  - IpProtocolTypes::EDX = 3
- Minimal build documentation updates:
  - docs/BuildBrainFlow.rst
  - docs/BrainFlowDev.rst

## What is intentionally excluded
- Probe/helper scripts
- Demo/example churn
- CI/workflow modifications
- Broad docs expansion unrelated to enabling this board

## Behavior guarantees
- Non-EDX users are unaffected when BUILD_ANT_EDX=OFF.
- gRPC/protobuf discovery is gated behind BUILD_ANT_EDX=ON.

## Validation performed
- Default configure path without EDX deps: success
- BUILD_ANT_EDX=ON without protobuf/gRPC installed: expected fail-fast at dependency discovery
- Verified board/protocol exposure in bindings for board 67 and protocol EDX=3

## Notes
System packages strategy only; optional Conan support is included for EDX dependency resolution, with no vendored runtime binaries.
