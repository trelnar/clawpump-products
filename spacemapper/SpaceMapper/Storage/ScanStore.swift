import Foundation
import RoomPlan

/// A saved room scan on disk. Each scan lives in its own folder under
/// Documents/Scans/<id>/ containing:
///   - scan.usdz       the exported 3D model
///   - room.json       the full CapturedRoom (re-loadable for future features)
///   - metadata.json   this struct
struct SavedScan: Identifiable, Codable, Hashable {
    let id: UUID
    var name: String
    let createdAt: Date
    let wallCount: Int
    let doorCount: Int
    let windowCount: Int
    let objectCount: Int
}

@MainActor
final class ScanStore: ObservableObject {
    @Published private(set) var scans: [SavedScan] = []

    private let fileManager = FileManager.default

    private var scansDirectory: URL {
        let documents = fileManager.urls(for: .documentDirectory, in: .userDomainMask)[0]
        return documents.appendingPathComponent("Scans", isDirectory: true)
    }

    init() {
        loadFromDisk()
    }

    func usdzURL(for scan: SavedScan) -> URL {
        folder(for: scan.id).appendingPathComponent("scan.usdz")
    }

    func roomJSONURL(for scan: SavedScan) -> URL {
        folder(for: scan.id).appendingPathComponent("room.json")
    }

    /// Exports the captured room to USDZ and persists it alongside its metadata.
    func save(room: CapturedRoom, name: String) throws {
        let id = UUID()
        let folder = folder(for: id)
        try fileManager.createDirectory(at: folder, withIntermediateDirectories: true)

        try room.export(to: folder.appendingPathComponent("scan.usdz"),
                        exportOptions: .parametric)

        let encoder = JSONEncoder()
        try encoder.encode(room).write(to: folder.appendingPathComponent("room.json"))

        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        let scan = SavedScan(
            id: id,
            name: trimmed.isEmpty ? "Room \(scans.count + 1)" : trimmed,
            createdAt: .now,
            wallCount: room.walls.count,
            doorCount: room.doors.count,
            windowCount: room.windows.count,
            objectCount: room.objects.count
        )
        try encoder.encode(scan).write(to: folder.appendingPathComponent("metadata.json"))

        scans.insert(scan, at: 0)
    }

    func delete(at offsets: IndexSet) {
        for index in offsets {
            try? fileManager.removeItem(at: folder(for: scans[index].id))
        }
        scans.remove(atOffsets: offsets)
    }

    func rename(_ scan: SavedScan, to newName: String) {
        guard let index = scans.firstIndex(where: { $0.id == scan.id }) else { return }
        var updated = scans[index]
        updated.name = newName
        scans[index] = updated
        let url = folder(for: scan.id).appendingPathComponent("metadata.json")
        try? JSONEncoder().encode(updated).write(to: url)
    }

    // MARK: - Private

    private func folder(for id: UUID) -> URL {
        scansDirectory.appendingPathComponent(id.uuidString, isDirectory: true)
    }

    private func loadFromDisk() {
        guard let folders = try? fileManager.contentsOfDirectory(
            at: scansDirectory,
            includingPropertiesForKeys: nil
        ) else { return }

        let decoder = JSONDecoder()
        scans = folders
            .compactMap { folder -> SavedScan? in
                let metadataURL = folder.appendingPathComponent("metadata.json")
                guard let data = try? Data(contentsOf: metadataURL) else { return nil }
                return try? decoder.decode(SavedScan.self, from: data)
            }
            .sorted { $0.createdAt > $1.createdAt }
    }
}
