# SpaceMapper — LiDAR 3D room scanning for iPad & iPhone

A SwiftUI app that scans and maps physical spaces in 3D, built on **Apple RoomPlan** — the same framework that powers commercial room-scanning apps. Point the device at a room, walk around it, and get a structured 3D model (walls, doors, windows, furniture) exportable as USDZ.

## How apps like this work

There are two layers of Apple technology involved:

1. **ARKit + LiDAR** — the low-level layer. The LiDAR sensor fires laser pulses and measures return time to build a depth map, which ARKit fuses with camera imagery and motion data into a live 3D mesh of the environment (`ARMeshAnchor`).
2. **RoomPlan** — the high-level layer (iOS 16+). It runs on top of ARKit and adds machine-learning models that recognize *semantic structure*: this surface is a wall, that opening is a door, that box is a sofa. It also provides a ready-made scanning UI (`RoomCaptureView`) with live camera feedback and an animated 3D miniature of the room as it builds.

This app uses RoomPlan, which is by far the fastest path to a scanning app. If you later need raw-mesh fidelity (curved walls, clutter, outdoor spaces), drop down to ARKit scene reconstruction — see "Where to take it next" below.

## Requirements

- **Hardware:** a device with a LiDAR sensor — iPad Pro (2020 or later), iPhone 12 Pro / 13 Pro / 14 Pro / 15 Pro or later Pro models. The app runs on non-LiDAR devices but scanning is disabled with an explanatory banner.
- **OS:** iOS / iPadOS 17.0+
- **Tooling:** Xcode 16+ on a Mac (the project uses Xcode 16's folder-synchronized project format), plus an Apple ID for on-device code signing (a free account works).

## Running it

1. Open `SpaceMapper.xcodeproj` in Xcode.
2. Select the SpaceMapper target → Signing & Capabilities → pick your team, and change the bundle identifier from `com.example.SpaceMapper` to something unique.
3. Plug in your iPad/iPhone, select it as the run destination, and press Run.
4. On device: tap **＋**, slowly pan around the room, tap **Done**, name the scan, save.

Scanning cannot be tested in the Simulator — RoomPlan needs real LiDAR hardware. The rest of the app (scan list, viewer with a copied-in USDZ) works anywhere.

## What the app does

| Screen | What happens |
|---|---|
| Scan list | Saved scans with wall/door/window/object counts; swipe to delete |
| Scan session | Live RoomPlan capture with coaching overlays; Done → on-device processing into a `CapturedRoom` |
| Detail view | Element stats, **View in 3D / AR** (QuickLook — orbit the model or place it back in the room at 1:1 scale), share the USDZ |

Each scan is persisted under `Documents/Scans/<uuid>/` as three files: `scan.usdz` (the 3D model), `room.json` (the full `CapturedRoom`, re-loadable for future features), and `metadata.json`.

## Code map

```
SpaceMapper/
├── SpaceMapperApp.swift          App entry point
├── ContentView.swift             Scan list + LiDAR support gate
├── Scanning/
│   └── ScanSessionView.swift     RoomCaptureView SwiftUI bridge + capture flow
├── Storage/
│   └── ScanStore.swift           Persistence: USDZ export, JSON metadata
└── Viewer/
    └── ScanDetailView.swift      Stats, QuickLook 3D/AR preview, ShareLink
```

## Where to take it next

- **Multi-room capture** — `CapturedStructure` / `StructureBuilder` (iOS 17) merges successive room scans into one whole-floor model.
- **2D floor plans** — `CapturedRoom` exposes every wall's position and dimensions; project them onto a plane to render a measured floor plan (PDF/SVG).
- **Raw mesh scanning** — for spaces RoomPlan's box-model can't represent, run an `ARSession` with `sceneReconstruction = .meshWithClassification` and export the `ARMeshAnchor` geometry yourself.
- **Furniture-aware features** — `room.objects` carries category (sofa, table, bed…), dimensions, and transforms: room-size estimates, furniture inventories, "will it fit" checks.
- **Cloud sync / viewer** — USDZ opens natively in Safari and on the web via `<model-viewer>` after conversion to glTF.
