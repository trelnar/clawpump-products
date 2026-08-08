import SwiftUI
import RoomPlan

struct ContentView: View {
    @EnvironmentObject private var store: ScanStore
    @State private var isScanning = false

    var body: some View {
        NavigationStack {
            Group {
                if store.scans.isEmpty {
                    ContentUnavailableView {
                        Label("No Scans Yet", systemImage: "cube.transparent")
                    } description: {
                        Text("Walk around a room with your device to capture a 3D model of the space.")
                    } actions: {
                        if RoomCaptureSession.isSupported {
                            Button("Start Scanning") { isScanning = true }
                                .buttonStyle(.borderedProminent)
                        }
                    }
                } else {
                    List {
                        ForEach(store.scans) { scan in
                            NavigationLink(value: scan.id) {
                                ScanRow(scan: scan)
                            }
                        }
                        .onDelete { indexSet in
                            store.delete(at: indexSet)
                        }
                    }
                }
            }
            .navigationTitle("SpaceMapper")
            .navigationDestination(for: UUID.self) { id in
                if let scan = store.scans.first(where: { $0.id == id }) {
                    ScanDetailView(scan: scan)
                }
            }
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        isScanning = true
                    } label: {
                        Label("New Scan", systemImage: "plus.viewfinder")
                    }
                    .disabled(!RoomCaptureSession.isSupported)
                }
            }
            .fullScreenCover(isPresented: $isScanning) {
                ScanSessionView()
            }
            .safeAreaInset(edge: .bottom) {
                if !RoomCaptureSession.isSupported {
                    UnsupportedDeviceBanner()
                }
            }
        }
    }
}

private struct ScanRow: View {
    let scan: SavedScan

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(scan.name)
                .font(.headline)
            Text(scan.createdAt, format: .dateTime.day().month().year().hour().minute())
                .font(.caption)
                .foregroundStyle(.secondary)
            Text("\(scan.wallCount) walls · \(scan.doorCount) doors · \(scan.windowCount) windows · \(scan.objectCount) objects")
                .font(.caption2)
                .foregroundStyle(.tertiary)
        }
        .padding(.vertical, 2)
    }
}

private struct UnsupportedDeviceBanner: View {
    var body: some View {
        Label("Scanning requires a device with a LiDAR sensor (iPhone Pro or iPad Pro).",
              systemImage: "exclamationmark.triangle")
            .font(.footnote)
            .padding(12)
            .frame(maxWidth: .infinity)
            .background(.yellow.opacity(0.15), in: RoundedRectangle(cornerRadius: 12))
            .padding()
    }
}

#Preview {
    ContentView()
        .environmentObject(ScanStore())
}
