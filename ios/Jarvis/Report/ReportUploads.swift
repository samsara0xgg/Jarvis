import Foundation
import os

/// Where a batch the network refused goes (Reporter.flush): handed over, delivered when the network is back.
@MainActor
protocol ReportUploading: AnyObject {
    func submit(_ frames: [ReportFrame], request: URLRequest)
}

/// A batch that could not be sent because the phone was offline is given to a background URLSession as an upload task from a file. iOS holds it
/// until the network returns and sends it even while the app is suspended or killed, then relaunches the app to hear the answer (the app delegate's
/// `handleEventsForBackgroundURLSession`). The frames stay in the queue until the brain's acks come, so a kill at any point loses nothing; the brain
/// stores a frame once by its `event_uid`, so a batch that is also sent by the normal path does no harm.
///
/// One task at a time: the frames that arrive meanwhile wait in the queue and go out in the flush that follows the answer.
@MainActor
final class ReportUploads: ReportUploading {
    static let shared = ReportUploads()
    static let sessionID = "com.yilunshi.jarvis.report-upload"
    /// how long iOS keeps a task waiting for the network; the brain takes events up to a week old at their own time
    nonisolated static let patience: TimeInterval = 3 * 24 * 3600

    nonisolated static let directory: URL = URL.applicationSupportDirectory.appending(path: "Report/uploads", directoryHint: .isDirectory)

    private var _session: URLSession?
    private let delegate = Delegate()
    private var backgroundCompletion: (() -> Void)?
    private var submitting = false
    private let log = Logger(subsystem: "com.allen.jarvis", category: "report")
    /// where the queue is settled, and where the next flush comes from (tests replace them)
    var reporter: () -> Reporter = { .shared }

    private var session: URLSession {
        if let s = _session { return s }
        let c = URLSessionConfiguration.background(withIdentifier: Self.sessionID)
        c.isDiscretionary = false                           // now, not when iOS finds it convenient
        c.sessionSendsLaunchEvents = true
        c.waitsForConnectivity = true
        c.timeoutIntervalForResource = Self.patience
        c.httpMaximumConnectionsPerHost = 1
        let s = URLSession(configuration: c, delegate: delegate, delegateQueue: nil)
        _session = s
        return s
    }

    /// Writes the batch's body to a file and starts a task for it, unless one is already waiting.
    func submit(_ frames: [ReportFrame], request: URLRequest) {
        guard !frames.isEmpty, !submitting else { return }
        submitting = true
        Task {
            defer { submitting = false }
            guard await session.allTasks.isEmpty else { return }
            let id = UUID().uuidString
            guard let file = Self.write(ReportBatch.body(frames), id: id) else { return log.info("background upload: no file") }
            let task = session.uploadTask(with: request, fromFile: file)
            task.taskDescription = id
            task.resume()
            log.info("background upload: \(frames.count) frames handed over")
        }
    }

    nonisolated static func write(_ body: Data, id: String, in dir: URL = ReportUploads.directory) -> URL? {
        let fm = FileManager.default
        try? fm.createDirectory(at: dir, withIntermediateDirectories: true, attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication])
        var root = dir
        var v = URLResourceValues(); v.isExcludedFromBackup = true
        try? root.setResourceValues(v)
        let file = dir.appending(path: "\(id).json")
        // readable after the first unlock: the system sends it while the phone is locked
        guard (try? body.write(to: file, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])) != nil else { return nil }
        return file
    }

    /// The answer for task `id` (status nil: the task failed). The frames it carried are settled as `flush` settles an answer; its file goes.
    func finished(id: String, status: Int?, body: Data?, in dir: URL = ReportUploads.directory) {
        let file = dir.appending(path: "\(id).json")
        defer { try? FileManager.default.removeItem(at: file) }
        guard let sent = try? Data(contentsOf: file) else { return }
        let r = reporter()
        r.settleBackground(uids: ReportBatch.uids(inBody: sent), status: status, body: body ?? Data())
        Task { await r.flush() }                            // what waited meanwhile
    }

    /// iOS relaunched the app for this session's events: the session is made again under its identifier so its delegate hears them.
    func handleBackgroundEvents(completion: @escaping () -> Void) {
        backgroundCompletion = completion
        _ = session
    }

    fileprivate func eventsDone() {
        backgroundCompletion?()
        backgroundCompletion = nil
    }

    /// Files whose task is long gone (a reinstall, a cancelled session) are not kept.
    nonisolated static func sweep(in dir: URL = ReportUploads.directory, olderThan age: TimeInterval = patience + 24 * 3600, now: Date = Date()) {
        let fm = FileManager.default
        for f in (try? fm.contentsOfDirectory(at: dir, includingPropertiesForKeys: [.contentModificationDateKey])) ?? [] {
            let modified = (try? f.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate
            if let modified, now.timeIntervalSince(modified) > age { try? fm.removeItem(at: f) }
        }
    }

    private final class Delegate: NSObject, URLSessionDataDelegate, @unchecked Sendable {
        private let lock = NSLock()
        private var bodies: [Int: Data] = [:]

        func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
            lock.lock(); bodies[dataTask.taskIdentifier, default: Data()].append(data); lock.unlock()
        }

        func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
            lock.lock(); let body = bodies.removeValue(forKey: task.taskIdentifier); lock.unlock()
            let status = error == nil ? (task.response as? HTTPURLResponse)?.statusCode : nil
            guard let id = task.taskDescription else { return }
            Task { @MainActor in ReportUploads.shared.finished(id: id, status: status, body: body) }
        }

        func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
            Task { @MainActor in ReportUploads.shared.eventsDone() }
        }
    }
}
