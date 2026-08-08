import SwiftUI
import QuickLook

/// Detail screen for a saved scan: stats, 3D/AR viewer, sharing, renaming.
struct ScanDetailView: View {
    @EnvironmentObject private var store: ScanStore
    let scan: SavedScan

    @State private var isPreviewing = false
    @State private var isRenaming = false
    @State private var newName = ""

    var body: some View {
        List {
            Section("Captured Elements") {
                StatRow(label: "Walls", value: scan.wallCount, icon: "square.split.bottomrightquarter")
                StatRow(label: "Doors", value: scan.doorCount, icon: "door.left.hand.closed")
                StatRow(label: "Windows", value: scan.windowCount, icon: "window.casement")
                StatRow(label: "Objects", value: scan.objectCount, icon: "chair.lounge")
            }

            Section {
                Button {
                    isPreviewing = true
                } label: {
                    Label("View in 3D / AR", systemImage: "arkit")
                }

                ShareLink(item: store.usdzURL(for: scan)) {
                    Label("Share USDZ Model", systemImage: "square.and.arrow.up")
                }
            } footer: {
                Text("The USDZ file opens in Files, Notes, Safari, and any 3D tool that supports Universal Scene Description (Blender, Reality Composer, etc.).")
            }
        }
        .navigationTitle(scan.name)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button("Rename") {
                    newName = scan.name
                    isRenaming = true
                }
            }
        }
        .alert("Rename Scan", isPresented: $isRenaming) {
            TextField("Name", text: $newName)
            Button("Save") { store.rename(scan, to: newName) }
            Button("Cancel", role: .cancel) {}
        }
        .sheet(isPresented: $isPreviewing) {
            USDZPreview(url: store.usdzURL(for: scan))
                .ignoresSafeArea()
        }
    }
}

private struct StatRow: View {
    let label: String
    let value: Int
    let icon: String

    var body: some View {
        HStack {
            Label(label, systemImage: icon)
            Spacer()
            Text("\(value)")
                .foregroundStyle(.secondary)
                .monospacedDigit()
        }
    }
}

/// QuickLook preview for USDZ models. QuickLook provides both an orbitable
/// 3D object view and a one-tap "AR" mode that places the model in the room.
struct USDZPreview: UIViewControllerRepresentable {
    let url: URL

    func makeUIViewController(context: Context) -> QLPreviewController {
        let controller = QLPreviewController()
        controller.dataSource = context.coordinator
        return controller
    }

    func updateUIViewController(_ uiViewController: QLPreviewController, context: Context) {}

    func makeCoordinator() -> Coordinator {
        Coordinator(url: url)
    }

    final class Coordinator: NSObject, QLPreviewControllerDataSource {
        private let url: URL

        init(url: URL) {
            self.url = url
        }

        func numberOfPreviewItems(in controller: QLPreviewController) -> Int { 1 }

        func previewController(_ controller: QLPreviewController,
                               previewItemAt index: Int) -> QLPreviewItem {
            url as NSURL
        }
    }
}
