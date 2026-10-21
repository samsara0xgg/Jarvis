import UIKit

/// Starts the reporter and the push registration at every launch, including the background launches iOS makes for a location visit,
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
        Reporter.shared.uploads = ReportUploads.shared
        ReportUploads.sweep()
        Reporter.shared.launchedForLocation = launchOptions?[.location] != nil
        Reporter.shared.launch()
        // before launch returns: a button pressed on a notification can launch the app in the background
        PushNotifications.shared.install()
        // paired: permission (the first time) and the APNs token, which iOS issues again at each launch
        Task { await PushRegistry.shared.start() }
        RideLiveActivity.shared.observe()                   // the ride Live Activity's tokens, including when a push-to-start wakes the app
        LifelogRecorder.shared.resume()                     // closes what a kill left open; records on if the switch is on
        return true
    }

    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        PushRegistry.shared.gotDeviceToken(deviceToken)
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        // only the error's code: nothing here may carry a token
        NSLog("push: registering with APNs failed (%ld)", (error as NSError).code)
    }

    /// A silent push that asks where he is now (`{"locate": "<request_id>"}`, Map/LocatePush): one fix goes to the brain with the request's id before the wake ends.
    func application(_ application: UIApplication, didReceiveRemoteNotification userInfo: [AnyHashable: Any],
                     fetchCompletionHandler completionHandler: @escaping (UIBackgroundFetchResult) -> Void) {
        guard DeviceRole.senses, let id = LocatePush.requestId(userInfo) else { return completionHandler(.noData) }          // an iPad does not say where it lies (ADR 0336)
        Task { @MainActor in
            let hold = BackgroundHold.begin("jarvis.locate")        // the request after the fix needs the app running a little longer
            let sent = await LocatePush.handle(id, source: OneShotLocation())
            hold.end()
            completionHandler(sent ? .newData : .failed)
        }
    }

    /// iOS relaunched the app for a background upload session: the lifelog's (a piece finished while the app was not running) or the reporter's
    /// (a batch refused for want of a network went out when it returned).
    func application(_ application: UIApplication, handleEventsForBackgroundURLSession identifier: String, completionHandler: @escaping () -> Void) {
        switch identifier {
        case LifelogUploader.sessionID: LifelogUploader.shared.handleBackgroundEvents(completion: completionHandler)
        case ReportUploads.sessionID: ReportUploads.shared.handleBackgroundEvents(completion: completionHandler)
        default: completionHandler()
        }
    }
}
