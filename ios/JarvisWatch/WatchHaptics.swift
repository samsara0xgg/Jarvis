import WatchKit

/// Her vocabulary in taps, for when he is not looking. WKHapticType has no way to set a strength, so a rising run is three different types
/// (each of the system's own, in the order they feel from light to firm). In the simulator they are not played: it sounds them through the Mac. Judge on the wrist.
@MainActor enum WatchHaptics {
    enum Cue {
        case leave          // 该出门了: two light taps
        case alight         // 下一站下车: three taps, each firmer
        case waits          // 有事等你: one long
        case listening      // 她在听: one very short

        var steps: [(type: WKHapticType, after: Double)] {
            switch self {
            case .leave: [(.click, 0), (.click, 0.18)]
            case .alight: [(.click, 0), (.directionUp, 0.25), (.notification, 0.35)]
            case .waits: [(.notification, 0)]
            case .listening: [(.click, 0)]
            }
        }
    }

    static func play(_ cue: Cue) {
        #if targetEnvironment(simulator)
        return
        #else
        Task { @MainActor in
            for s in cue.steps {
                if s.after > 0 { try? await Task.sleep(for: .seconds(s.after)) }
                WKInterfaceDevice.current().play(s.type)
            }
        }
        #endif
    }
}
