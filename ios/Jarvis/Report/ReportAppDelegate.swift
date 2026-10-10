import UIKit

/// Starts the reporter at every launch, including the background launches iOS makes for a location visit,
/// a Health delivery or a refresh: those need their delegates and handlers in place before launch returns.
final class ReportAppDelegate: NSObject, UIApplicationDelegate {
    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        #if DEBUG
        // Test wiring: `-reportStubURL http://127.0.0.1:PORT -reportStubToken x` sends to a local stub server.
        let d = UserDefaults.standard
        if let base = d.string(forKey: "reportStubURL").flatMap(URL.init(string:)), let token = d.string(forKey: "reportStubToken") {
            Reporter.shared.transport = HTTPReportTransport { (base, token) }
        }
        #endif
        // the paired brain's address and token, read when a batch goes out (nil until paired: frames wait in the queue)
        if Reporter.shared.transport == nil { Reporter.shared.transport = HTTPReportTransport { BrainLink.shared.voiceEndpoint } }
        Reporter.shared.launchedForLocation = launchOptions?[.location] != nil
        Reporter.shared.launch()
        return true
    }
}
