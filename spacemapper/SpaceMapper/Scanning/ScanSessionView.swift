import SwiftUI
import RoomPlan

/// Full-screen scanning flow: live RoomPlan capture with guidance overlays,
/// followed by a save sheet once processing completes.
struct ScanSessionView: View {
    @EnvironmentObject private var store: ScanStore
    @Environment(\.dismiss) private var dismiss
    @StateObject private var model = ScanSessionModel()
    @State private var scanName = ""
    @State private var saveError: String?

    var body: some View {
        ZStack {
            RoomCaptureHost(model: model)
                .ignoresSafeArea()

            VStack {
                topBar
                Spacer()
                bottomBar
            }
        }
        .alert("Couldn't Save Scan", isPresented: .constant(saveError != nil)) {
            Button("OK") { saveError = nil }
        } message: {
            Text(saveError ?? "")
        }
    }

    private var topBar: some View {
        HStack {
            Button("Cancel") {
                model.cancel()
                dismiss()
            }
            .buttonStyle(.bordered)

            Spacer()

            if case .scanning = model.phase {
                Text("Slowly pan your device around the room")
                    .font(.footnote)
                    .padding(8)
                    .background(.ultraThinMaterial, in: Capsule())
            }
        }
        .padding()
    }

    @ViewBuilder
    private var bottomBar: some View {
        switch model.phase {
        case .scanning:
            Button {
                model.finish()
            } label: {
                Label("Done", systemImage: "checkmark")
                    .frame(maxWidth: 200)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .padding(.bottom, 30)

        case .processing:
            ProgressView("Building 3D model…")
                .padding()
                .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 16))
                .padding(.bottom, 30)

        case .finished:
            VStack(spacing: 12) {
                TextField("Scan name", text: $scanName)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 280)
                Button {
                    saveAndDismiss()
                } label: {
                    Label("Save Scan", systemImage: "square.and.arrow.down")
                        .frame(maxWidth: 200)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
            }
            .padding()
            .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 16))
            .padding(.bottom, 30)

        case .failed(let message):
            VStack(spacing: 12) {
                Label(message, systemImage: "exclamationmark.triangle")
                Button("Close") { dismiss() }
                    .buttonStyle(.bordered)
            }
            .padding()
            .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 16))
            .padding(.bottom, 30)
        }
    }

    private func saveAndDismiss() {
        guard case .finished(let room) = model.phase else { return }
        do {
            try store.save(room: room, name: scanName)
            dismiss()
        } catch {
            saveError = error.localizedDescription
        }
    }
}

// MARK: - View model

@MainActor
final class ScanSessionModel: ObservableObject {
    enum Phase {
        case scanning
        case processing
        case finished(CapturedRoom)
        case failed(String)
    }

    @Published var phase: Phase = .scanning
    weak var captureView: RoomCaptureView?

    func finish() {
        phase = .processing
        captureView?.captureSession.stop()
    }

    func cancel() {
        captureView?.captureSession.stop(pauseARSession: true)
    }
}

// MARK: - RoomCaptureView bridge

/// Wraps RoomPlan's UIKit RoomCaptureView (live camera feed, scanning
/// feedback, and the animated 3D miniature) for use in SwiftUI.
struct RoomCaptureHost: UIViewRepresentable {
    @ObservedObject var model: ScanSessionModel

    func makeUIView(context: Context) -> RoomCaptureView {
        let view = RoomCaptureView(frame: .zero)
        view.delegate = context.coordinator
        model.captureView = view

        var configuration = RoomCaptureSession.Configuration()
        configuration.isCoachingEnabled = true
        view.captureSession.run(configuration: configuration)
        return view
    }

    func updateUIView(_ uiView: RoomCaptureView, context: Context) {}

    func makeCoordinator() -> RoomCaptureCoordinator {
        RoomCaptureCoordinator(model: model)
    }
}

/// Delegate for RoomCaptureView.
///
/// This lives at file scope rather than nested inside `RoomCaptureHost`:
/// `RoomCaptureViewDelegate` inherits `NSCoding`, and Swift cannot give a
/// nested type a stable Objective-C runtime name, so an explicit `@objc`
/// name is required.
@objc(SMRoomCaptureCoordinator)
final class RoomCaptureCoordinator: NSObject, RoomCaptureViewDelegate {
    private let model: ScanSessionModel

    init(model: ScanSessionModel) {
        self.model = model
        super.init()
    }

    // NSCoding conformance is only inherited from the delegate protocol —
    // the coordinator is never archived, so these are minimal stubs.
    func encode(with coder: NSCoder) {}
    init?(coder: NSCoder) { return nil }

    func captureView(shouldPresent roomDataForProcessing: CapturedRoomData,
                     error: Error?) -> Bool {
        true
    }

    func captureView(didPresent processedResult: CapturedRoom, error: Error?) {
        Task { @MainActor in
            if let error {
                model.phase = .failed(error.localizedDescription)
            } else {
                model.phase = .finished(processedResult)
            }
        }
    }
}
